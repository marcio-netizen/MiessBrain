"""
app.py — Dashboard CFO Miess
Streamlit · Python 3.10+

Estratégia de velocidade:
  1. CSV mensal exportado do VTEX → carregamento instantâneo
  2. Botão "Atualizar hoje" → API busca só pedidos de hoje (~50-100 pedidos, < 1 min)
  3. Cache parquet local → segunda abertura instantânea
"""
import io, os, hashlib, hmac
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timezone, timedelta

import sys
sys.path.insert(0, os.path.dirname(__file__))
from processador import (
    build_cost_tables, process_vtex, calc_pl,
    calc_pedidos, calc_produtos, calc_marcas, calc_kits, calc_guia_compras,
)

CACHE_DIR   = os.path.join(os.path.dirname(__file__), 'cache')
PARQUET_RT  = os.path.join(CACHE_DIR, 'vtex_realtime.parquet')
os.makedirs(CACHE_DIR, exist_ok=True)

st.set_page_config(
    page_title='CFO Miess', page_icon='💄',
    layout='wide', initial_sidebar_state='expanded',
)

# ── Autenticação por senha ────────────────────────────────────────────────────────
def check_password():
    if st.session_state.get('auth_ok'):
        return True
    st.markdown(
        "<h2 style='color:#C13535; text-align:center; margin-top:80px'>💄 CFO Miess</h2>",
        unsafe_allow_html=True,
    )
    col = st.columns([1, 2, 1])[1]
    pwd = col.text_input('Senha', type='password', label_visibility='collapsed',
                         placeholder='Senha de acesso')
    if col.button('Entrar', use_container_width=True, type='primary'):
        try:
            senha_correta = str(st.secrets['dashboard_password']).strip()
        except Exception:
            senha_correta = 'miess2026'
        if pwd.strip() == senha_correta:
            st.session_state['auth_ok'] = True
            st.rerun()
        else:
            col.error(f'Senha incorreta')
    return False

if not check_password():
    st.stop()

CORAL = '#C13535'

def brl(v):
    return f"R$ {v:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

def pct(v):
    return f"{v*100:.1f}%"


# ── Cache: tabelas de custo ──────────────────────────────────────────────────────
@st.cache_resource(show_spinner='Carregando tabelas de custo…')
def load_cost_tables(de_bytes, ck_bytes, kp_bytes):
    de_raw = pd.read_csv(io.BytesIO(de_bytes), sep=';', encoding='utf-8-sig',
                         dtype=str, keep_default_na=False)
    ck_raw = pd.read_csv(io.BytesIO(ck_bytes), sep=None, engine='python',
                         dtype=str, encoding_errors='replace', keep_default_na=False)
    kp_raw = pd.read_excel(io.BytesIO(kp_bytes), header=None, dtype=str)
    return build_cost_tables(de_raw, ck_raw, kp_raw)


# ── Supabase ─────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_supabase():
    try:
        url = st.secrets['supabase']['url']
        key = st.secrets['supabase']['key']
        from db import get_client
        return get_client(url, key)
    except Exception:
        return None

@st.cache_data(ttl=3600, show_spinner='Carregando tabelas de custo do servidor…')
def load_cost_from_supabase():
    sb = get_supabase()
    if sb is None:
        return None
    try:
        from db import download_cost_file
        de_b = download_cost_file(sb, 'de')
        ck_b = download_cost_file(sb, 'ck')
        kp_b = download_cost_file(sb, 'kp')
        if de_b is None or ck_b is None or kp_b is None:
            return None
        return load_cost_tables(de_b, ck_b, kp_b)
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner='Baixando dados do servidor…')
def load_from_supabase():
    sb = get_supabase()
    if sb is None:
        return None
    try:
        from db import download_parquet
        df = download_parquet(sb)
        for col in ['Quantity_SKU','SKU Value','SKU Selling Price','SKU Total Price',
                    'Payment Value','Shipping Value','Total Value','Discounts Totals']:
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df
    except Exception:
        return None


# ── Parquet cache helpers ────────────────────────────────────────────────────────
def _cache_path(label):
    return os.path.join(CACHE_DIR, f'vtex_{label}.parquet')

def _save_cache(df, label):
    try:
        df.to_parquet(_cache_path(label), index=False)
    except Exception:
        pass

def _load_cache(label, max_age_hours=2):
    path = _cache_path(label)
    if not os.path.exists(path):
        return None
    age = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
    if age > max_age_hours:
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


# ── Leitura CSV VTEX ─────────────────────────────────────────────────────────────
@st.cache_data(show_spinner='Lendo CSV…')
def read_vtex_csv(file_bytes):
    h = hashlib.md5(file_bytes).hexdigest()[:8]
    cached = _load_cache(f'csv_{h}', max_age_hours=24)
    if cached is not None:
        return cached
    df = pd.read_csv(io.BytesIO(file_bytes), sep=';', encoding='utf-8-sig',
                     dtype=str, keep_default_na=False)
    _save_cache(df, f'csv_{h}')
    return df


# ── Busca API (só pedidos de hoje) ───────────────────────────────────────────────
def fetch_hoje(store_name, app_key, app_token):
    from vtex_api import fetch_orders
    now     = datetime.now(timezone.utc)
    d_from  = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    d_to    = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    cached = _load_cache('hoje', max_age_hours=1)
    if cached is not None:
        st.sidebar.success(f'✅ Cache de hoje carregado ({len(cached)} linhas)')
        return cached

    bar = st.sidebar.progress(0, text='Buscando pedidos de hoje…')
    def cb(cur, tot, msg=''):
        bar.progress(min(cur / max(tot, 1), 1.0), text=msg or f'{cur}/{tot}')

    df = fetch_orders(store_name, app_key, app_token, d_from, d_to,
                      max_orders=300, progress_cb=cb)
    bar.empty()
    if df is not None and len(df):
        _save_cache(df, 'hoje')
    return df


# ── Sidebar ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title('💄 CFO Miess')

    # ── Status do poller ────────────────────────────────────────────────────────
    if os.path.exists(PARQUET_RT):
        mtime    = datetime.fromtimestamp(os.path.getmtime(PARQUET_RT))
        age_min  = (datetime.now() - mtime).seconds // 60
        df_rt    = pd.read_parquet(PARQUET_RT)
        n_rt     = df_rt['Order'].nunique() if 'Order' in df_rt.columns else len(df_rt)
        if age_min < 10:
            st.success(f'🟢 Tempo real ativo\n{n_rt:,} pedidos · atualizado há {age_min} min')
        else:
            st.warning(f'🟡 Poller parado há {age_min} min\n{n_rt:,} pedidos')
    else:
        st.info('⚪ Poller não iniciado ainda\nVeja instruções abaixo')

    st.caption('Para dados em tempo real, abra um segundo terminal e rode:\n`python poller.py`')

    st.divider()

    # ── Fallback: CSV manual ────────────────────────────────────────────────────
    with st.expander('📂 Carregar CSV manual (fallback)'):
        vtex_file = st.file_uploader(
            'Vendas VTEX .csv', type=['csv'], key='vtex_csv',
            help='Usar quando o poller não estiver rodando'
        )

    st.divider()

    # ── Arquivos de custo ───────────────────────────────────────────────────────
    cost_tables = load_cost_from_supabase()

    if cost_tables is not None:
        st.success('✅ Custos carregados automaticamente')
        expander_label = '🔁 Atualizar arquivos de custo'
    else:
        st.warning('⚠️ Faça upload dos arquivos de custo')
        expander_label = '💰 Carregar arquivos de custo'

    with st.expander(expander_label, expanded=(cost_tables is None)):
        de_file = st.file_uploader('custo total estoque.xls', type=['csv','xls','xlsx'], key='de')
        ck_file = st.file_uploader('custo kits.csv',          type=['csv'],             key='ck')
        kp_file = st.file_uploader('kits promomos.xlsx',      type=['xlsx'],            key='kp')

        if de_file and ck_file and kp_file:
            de_b = de_file.read()
            ck_b = ck_file.read()
            kp_b = kp_file.read()
            sb = get_supabase()
            if sb is not None:
                from db import upload_cost_file
                with st.spinner('Salvando custos no servidor…'):
                    upload_cost_file(sb, 'de', de_b)
                    upload_cost_file(sb, 'ck', ck_b)
                    upload_cost_file(sb, 'kp', kp_b)
            cost_tables = load_cost_tables(de_b, ck_b, kp_b)
            st.cache_data.clear()
            st.success('✅ Custos salvos! Não precisará enviar novamente.')

    if st.button('🔄 Recarregar dados'):
        st.cache_data.clear()
        st.rerun()


# ── Montar DataFrame final ────────────────────────────────────────────────────────
vtex_df_raw = None

# 1ª prioridade: Supabase (tempo real online)
vtex_df_raw = load_from_supabase()

# 2ª prioridade: parquet local do poller
if vtex_df_raw is None and os.path.exists(PARQUET_RT):
    try:
        vtex_df_raw = pd.read_parquet(PARQUET_RT)
        for col in ['Quantity_SKU','SKU Value','SKU Selling Price','SKU Total Price',
                    'Payment Value','Shipping Value','Total Value','Discounts Totals']:
            if col in vtex_df_raw.columns:
                vtex_df_raw[col] = vtex_df_raw[col].astype(str)
    except Exception:
        vtex_df_raw = None

# 3ª prioridade: CSV manual
if vtex_df_raw is None and vtex_file is not None:
    vtex_df_raw = read_vtex_csv(vtex_file.read())


# ── Main ─────────────────────────────────────────────────────────────────────────
# Timestamp de atualização dos dados
if os.path.exists(PARQUET_RT):
    mtime_str = datetime.fromtimestamp(os.path.getmtime(PARQUET_RT)).strftime('%d/%m/%Y %H:%M')
    fonte_str = f'🟢 Tempo real · dados de {mtime_str}'
else:
    mtime_str = datetime.now().strftime('%d/%m/%Y %H:%M')
    fonte_str = '📂 CSV manual'

st.markdown(
    f"<h1 style='color:{CORAL}; margin-bottom:2px'>Dashboard CFO Miess</h1>"
    f"<p style='color:#888; margin-top:0; font-size:13px'>{fonte_str}</p>",
    unsafe_allow_html=True,
)

if vtex_df_raw is None:
    st.info(
        '**Como ativar dados em tempo real:**\n\n'
        '1. Abra um segundo terminal na pasta `dashboard_cfo/`\n'
        '2. Execute: `python poller.py`\n'
        '3. O poller sincroniza os pedidos a cada 5 minutos automaticamente\n\n'
        '**Ou use o fallback:** expanda "📂 Carregar CSV manual" na sidebar e faça upload do arquivo.'
    )
    st.stop()

if cost_tables is None:
    st.info('Faça upload dos 3 arquivos de custo na barra lateral para ver os resultados completos.')
    st.stop()


# ── Processar ─────────────────────────────────────────────────────────────────────
with st.spinner('Calculando P&L…'):
    dv       = process_vtex(vtex_df_raw, cost_tables)
    pl       = calc_pl(dv)
    dv_pl    = pl['dv_pl']
    pedidos  = calc_pedidos(dv, dv_pl)
    produtos = calc_produtos(dv_pl, cost_tables['estoque_disp'])
    marcas   = calc_marcas(dv_pl, cost_tables['estoque_marca_disp'])
    kits     = calc_kits(dv_pl, cost_tables['estoque_disp'])
    guia     = calc_guia_compras(dv_pl, cost_tables['estoque_disp'], pl['n_dias'])


# ── Filtro de período ─────────────────────────────────────────────────────────────
dias_disponiveis = sorted(dv_pl['dia'].unique().tolist()) if 'dia' in dv_pl.columns else []
if len(dias_disponiveis) >= 2:
    periodo = st.select_slider(
        'Filtrar período (dias do mês)',
        options=dias_disponiveis,
        value=(dias_disponiveis[0], dias_disponiveis[-1]),
    )
    if periodo[0] != dias_disponiveis[0] or periodo[1] != dias_disponiveis[-1]:
        dv_pl    = dv_pl[dv_pl['dia'].between(periodo[0], periodo[1])].copy()
        pl       = calc_pl(dv[dv['dia'].between(periodo[0], periodo[1])])
        dv_pl    = pl['dv_pl']
        produtos = calc_produtos(dv_pl, cost_tables['estoque_disp'])
        marcas   = calc_marcas(dv_pl, cost_tables['estoque_marca_disp'])
        kits     = calc_kits(dv_pl, cost_tables['estoque_disp'])
        guia     = calc_guia_compras(dv_pl, cost_tables['estoque_disp'], pl['n_dias'])
elif dias_disponiveis:
    st.caption(f"📅 Dia {dias_disponiveis[0]} do mês")


# ── KPI Cards ─────────────────────────────────────────────────────────────────────
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric('Receita Bruta',   brl(pl['rb_total']))
k2.metric('Rec. Líquida',    brl(pl['rl_total']))
k3.metric('Descontos',       brl(pl['descontos']),
          delta=f"-{pl['descontos']/pl['rb_total']*100:.1f}% RB" if pl['rb_total'] else None,
          delta_color='inverse')
k4.metric('Custo',           brl(pl['custo_total']))
k5.metric('Lucro Bruto',     brl(pl['lucro_bruto']),
          delta=pct(pl['margem']))
k6.metric('Pedidos',         f"{pl['n_pedidos']:,}",
          delta=f"{pl['n_dias']} dias")

st.divider()


# ── Abas ─────────────────────────────────────────────────────────────────────────
tab_pl, tab_prod, tab_marcas, tab_kits, tab_mkt, tab_guia, tab_pedidos = st.tabs([
    '📊 P&L', '🏷 Produtos', '🏢 Marcas', '🎁 Kits', '📣 Marketing', '🛒 Guia Compras', '📦 Pedidos',
])


# ── P&L ──────────────────────────────────────────────────────────────────────────
with tab_pl:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader('Demonstrativo')
        rows_pl = [
            ('Receita Bruta',    brl(pl['rb_total']),    ''),
            ('Descontos',        f"({brl(pl['descontos'])})",
             f"{pl['descontos']/pl['rb_total']*100:.1f}%" if pl['rb_total'] else ''),
            ('Receita Líquida',  brl(pl['rl_total']),    ''),
            ('Custo Produtos',   brl(pl['custo_total']),
             f"{pl['custo_total']/pl['rl_total']*100:.1f}%" if pl['rl_total'] else ''),
            ('  → C&G Campanha', brl(pl['custo_cg']),    ''),
            ('Lucro Bruto',      brl(pl['lucro_bruto']), pct(pl['margem'])),
        ]
        st.dataframe(pd.DataFrame(rows_pl, columns=['Indicador', 'Valor', '%']),
                     hide_index=True, use_container_width=True)

    with c2:
        fig = go.Figure(go.Waterfall(
            orientation='v',
            measure=['absolute', 'relative', 'total', 'relative', 'total'],
            x=['RB', '-Descontos', 'RL', '-Custo', 'Lucro'],
            y=[pl['rb_total'], -pl['descontos'], pl['rl_total'],
               -pl['custo_total'], pl['lucro_bruto']],
            connector={'line': {'color': '#ccc'}},
            increasing={'marker': {'color': CORAL}},
            decreasing={'marker': {'color': '#aaa'}},
            totals={'marker': {'color': '#2ecc71'}},
        ))
        fig.update_layout(height=320, margin=dict(t=10, b=10), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    if 'dia' in dv_pl.columns:
        st.subheader('Vendas por dia')
        dia_agg = dv_pl.groupby('dia').agg(rl=('rl_total','sum'), custo=('custo_total','sum')).reset_index()
        dia_agg['lucro'] = dia_agg['rl'] - dia_agg['custo']
        fig2 = px.bar(dia_agg, x='dia', y=['rl','lucro'],
                      color_discrete_map={'rl': CORAL, 'lucro': '#2ecc71'}, barmode='group',
                      labels={'value': 'R$', 'dia': 'Dia', 'variable': ''})
        fig2.update_layout(height=280, margin=dict(t=5, b=5))
        st.plotly_chart(fig2, use_container_width=True)


# ── Produtos ──────────────────────────────────────────────────────────────────────
with tab_prod:
    col_ord, col_cat, col_acao = st.columns(3)
    ord_by   = col_ord.selectbox('Ordenar', ['rl','lucro','margem','qtd'])
    cats     = produtos['categoria'].unique().tolist()
    cat_sel  = col_cat.multiselect('Categoria', cats, default=cats)
    acoes    = ['✅ Replicar','📊 Manter','⚠️ Revisar']
    acao_sel = col_acao.multiselect('Ação', acoes, default=acoes)

    df_show = produtos[
        produtos['categoria'].isin(cat_sel) & produtos['acao'].isin(acao_sel)
    ].sort_values(ord_by, ascending=False).head(60)

    st.dataframe(
        df_show[['ref_code','SKU Name','categoria','marca','qtd','rl','lucro','margem','acao','estoque']]
        .rename(columns={'ref_code':'Código','SKU Name':'Produto','categoria':'Cat.',
                         'marca':'Marca','qtd':'Qtd','rl':'Rec.Líq.','lucro':'Lucro',
                         'margem':'Margem','acao':'Ação','estoque':'Est.Disp.'})
        .style.format({'Rec.Líq.':'R$ {:,.2f}','Lucro':'R$ {:,.2f}','Margem':'{:.1%}'}),
        hide_index=True, use_container_width=True,
    )

    st.subheader('Margem × Receita')
    fig3 = px.scatter(df_show, x='rl', y='margem', size='qtd', color='acao',
                      hover_name='SKU Name', hover_data=['ref_code','lucro'],
                      color_discrete_map={'✅ Replicar':'#2ecc71','📊 Manter':'#f39c12','⚠️ Revisar':CORAL},
                      labels={'rl':'Rec. Líquida (R$)','margem':'Margem'})
    fig3.update_layout(height=380, margin=dict(t=5))
    st.plotly_chart(fig3, use_container_width=True)


# ── Marcas ────────────────────────────────────────────────────────────────────────
with tab_marcas:
    fig4 = px.bar(marcas.head(15), x='marca', y='rl', color='margem',
                  color_continuous_scale=['#aaa', CORAL, '#2ecc71'],
                  labels={'rl':'Rec. Líquida (R$)','marca':'Marca','margem':'Margem'})
    fig4.update_layout(height=320, margin=dict(t=5))
    st.plotly_chart(fig4, use_container_width=True)

    st.dataframe(
        marcas[['marca','qtd','rl','lucro','margem','acao','estoque']]
        .rename(columns={'marca':'Marca','qtd':'Qtd','rl':'Rec.Líq.',
                         'lucro':'Lucro','margem':'Margem','acao':'Ação','estoque':'Est.Disp.'})
        .style.format({'Rec.Líq.':'R$ {:,.2f}','Lucro':'R$ {:,.2f}','Margem':'{:.1%}'}),
        hide_index=True, use_container_width=True,
    )


# ── Kits ─────────────────────────────────────────────────────────────────────────
with tab_kits:
    c1, c2 = st.columns([1, 1])
    with c1:
        st.dataframe(
            kits[['ref_code','SKU Name','qtd','rl','lucro','margem','acao','estoque']]
            .rename(columns={'ref_code':'Código','SKU Name':'Kit','qtd':'Qtd','rl':'Rec.Líq.',
                             'lucro':'Lucro','margem':'Margem','acao':'Ação','estoque':'Est.Disp.'})
            .style.format({'Rec.Líq.':'R$ {:,.2f}','Lucro':'R$ {:,.2f}','Margem':'{:.1%}'}),
            hide_index=True, use_container_width=True,
        )
    with c2:
        fig5 = px.bar(kits.head(20), x='rl', y='SKU Name', orientation='h',
                      color='margem', color_continuous_scale=['#aaa', CORAL, '#2ecc71'],
                      labels={'rl':'Rec. Líquida (R$)','SKU Name':''})
        fig5.update_layout(height=480, margin=dict(t=5, l=5))
        st.plotly_chart(fig5, use_container_width=True)


# ── Marketing ─────────────────────────────────────────────────────────────────────
with tab_mkt:
    utm_agg = dv_pl.groupby('utm_source_norm').agg(
        rl=('rl_total','sum'), pedidos=('Order','nunique'), custo=('custo_total','sum'),
    ).reset_index()
    utm_agg['lucro']  = utm_agg['rl'] - utm_agg['custo']
    utm_agg['margem'] = utm_agg['lucro'] / utm_agg['rl'].replace(0, np.nan)
    utm_agg['ticket'] = utm_agg['rl'] / utm_agg['pedidos'].replace(0, np.nan)
    utm_agg = utm_agg.sort_values('rl', ascending=False)

    c1, c2 = st.columns(2)
    with c1:
        fig6 = px.pie(utm_agg, names='utm_source_norm', values='rl',
                      color_discrete_sequence=px.colors.qualitative.Set2, title='% Receita por canal')
        fig6.update_layout(height=320)
        st.plotly_chart(fig6, use_container_width=True)
    with c2:
        st.dataframe(
            utm_agg.rename(columns={'utm_source_norm':'Canal','rl':'Rec.Líq.','pedidos':'Pedidos',
                                    'lucro':'Lucro','margem':'Margem','ticket':'Ticket Médio'})
            .style.format({'Rec.Líq.':'R$ {:,.2f}','Lucro':'R$ {:,.2f}',
                           'Margem':'{:.1%}','Ticket Médio':'R$ {:,.2f}'}),
            hide_index=True, use_container_width=True,
        )

    st.subheader('Cupons')
    coupon_agg = pedidos[pedidos['coupon'] != ''].groupby('coupon').agg(
        pedidos=('Order','count'), rl=('rl_pedido','sum')
    ).reset_index().sort_values('rl', ascending=False).head(15)
    fig7 = px.bar(coupon_agg, x='coupon', y='rl', text_auto='.2s',
                  labels={'coupon':'Cupom','rl':'Rec. Líquida (R$)'})
    fig7.update_layout(height=280, margin=dict(t=5))
    st.plotly_chart(fig7, use_container_width=True)


# ── Guia de Compras ───────────────────────────────────────────────────────────────
with tab_guia:
    st.subheader('🛒 Guia de Compras — projeção 30 dias')
    prios = ['🔴 URGENTE','🟠 ALTA','🟡 MÉDIA','🟢 OK']
    prio_sel = st.multiselect('Prioridade', prios, default=['🔴 URGENTE','🟠 ALTA'])
    g_show = guia[guia['prioridade'].isin(prio_sel)].sort_values(
        ['prioridade','sugestao'], ascending=[True, False])

    st.dataframe(
        g_show[['ref_code','nome','qtd','giro_diario','proj_30d','disp','sugestao','prioridade']]
        .rename(columns={'ref_code':'Código','nome':'Produto','qtd':'Qtd Vendida',
                         'giro_diario':'Giro/Dia','proj_30d':'Proj.30d',
                         'disp':'Disp.','sugestao':'Sugestão Compra','prioridade':'Prioridade'})
        .style.format({'Giro/Dia':'{:.1f}','Proj.30d':'{:.0f}',
                       'Disp.':'{:.0f}','Sugestão Compra':'{:.0f}'}),
        hide_index=True, use_container_width=True,
    )

    urgentes = int((guia['prioridade'] == '🔴 URGENTE').sum())
    altas    = int((guia['prioridade'] == '🟠 ALTA').sum())
    if urgentes:
        st.error(f'⚠️ {urgentes} SKUs com ruptura em < 7 dias!')
    if altas:
        st.warning(f'🟠 {altas} SKUs com estoque < 14 dias.')


# ── Pedidos ───────────────────────────────────────────────────────────────────────
with tab_pedidos:
    st.dataframe(
        pedidos[['Order','data','uf','status','canal_pgto','utm_source',
                 'coupon','n_itens','rl_pedido','lucro_bruto','margem']]
        .rename(columns={'Order':'Pedido','data':'Data','uf':'UF','status':'Status',
                         'canal_pgto':'Pagamento','utm_source':'Canal','coupon':'Cupom',
                         'n_itens':'Itens','rl_pedido':'Rec.Líq.','lucro_bruto':'Lucro','margem':'Margem'})
        .style.format({'Rec.Líq.':'R$ {:,.2f}','Lucro':'R$ {:,.2f}','Margem':'{:.1%}'}),
        hide_index=True, use_container_width=True,
    )
    c1, c2 = st.columns(2)
    with c1:
        pgto = pedidos.groupby('canal_pgto').agg(
            pedidos=('Order','count'), rl=('rl_pedido','sum')
        ).reset_index().sort_values('rl', ascending=False)
        fig8 = px.bar(pgto, x='canal_pgto', y='rl', labels={'canal_pgto':'Forma pgto','rl':'R$'})
        fig8.update_layout(height=280, margin=dict(t=5))
        st.plotly_chart(fig8, use_container_width=True)
    with c2:
        uf_agg = pedidos.groupby('uf').agg(
            pedidos=('Order','count'), rl=('rl_pedido','sum')
        ).reset_index().sort_values('rl', ascending=False).head(10)
        fig9 = px.bar(uf_agg, x='uf', y='rl', labels={'uf':'UF','rl':'R$'})
        fig9.update_layout(height=280, margin=dict(t=5))
        st.plotly_chart(fig9, use_container_width=True)
