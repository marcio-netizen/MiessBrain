"""
processador.py — Core business logic Miess CFO
Extraído de gerar_cfo.py; stateless, sem I/O — recebe DataFrames, devolve DataFrames.
"""
import re
import numpy as np
import pandas as pd

# ── Constantes ──────────────────────────────────────────────────────────────────
ZERO_COST_ITEMS = {'BRINDESITE', 'PRIMEA1', 'PKM426-4', 'MIMORA', 'BSG-TESTER'}

UTM_MAP = {
    r'(?i)^(insta(gram)?|ig|crm-?int?agram|crminstagram)$': 'Instagram',
    r'(?i)^(whatsapp|crm-?whatsapp)$': 'WhatsApp',
    r'(?i)^(youtube|crm-?youtube|yc)$': 'YouTube',
    r'(?i)^(btg|btg360)$': 'BTG360',
    r'(?i)^(crm-?vendas?)$': 'CRM-Vendas',
}

CONFIRMED_CT = {
    ('3562081013F', '5'): 5.94,
    ('3562081013F', '8 a 10'): 7.34,
    ('3562081013F', 'forever'): 7.34,
    ('7633RC052611', 'forever'): 48.71,
    ('994299409941', 'forever'): 77.35,
    ('IN0139-3', '6-7 Prime'): 93.49,
    ('IN0139-3', 'forever'): 76.06,
    ('F9941-3', '6-7 Prime'): 107.75,
    ('F9941-3', 'forever'): 102.00,
    ('83169521096', '5'): 41.44,
    ('83169521096', '8 a 10'): 59.15,
    ('83169521096', '12 e 13'): 59.19,
    ('83169521096', 'forever'): 59.19,
}

# ── Helpers de conversão ────────────────────────────────────────────────────────
def br2float(v):
    if pd.isna(v) or v == '' or v == '-':
        return 0.0
    s = str(v).strip().replace('\x00', '').replace('R$', '').strip()
    if not s or s in ('nan', 'None'):
        return 0.0
    has_comma = ',' in s
    has_dot   = '.' in s
    if has_comma and has_dot:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif has_comma:
        s = s.replace(',', '.')
    try:
        return float(s)
    except Exception:
        return 0.0


def cod_clean(s):
    return str(s).strip().upper().replace('-', '').replace(' ', '') if s else ''


def classify_category(cat_str):
    if not cat_str or cat_str == 'nan':
        return 'Outros'
    s = '/' + str(cat_str).strip('/') + '/'
    if '/8/458/' in s or '/8/485/' in s:
        return 'Kit/Duplinha'
    if '/8/457/' in s or '/8/486/' in s:
        return 'Atacado'
    if '/4/248/' in s:
        return 'Outlet'
    return 'Regular'


def norm_utm(v):
    s = str(v).strip() if v else ''
    if not s or s in ('nan', 'undefined', ''):
        return 'Orgânico/Direto'
    for pat, label in UTM_MAP.items():
        if re.match(pat, s):
            return label
    return s


def norm_coupon(v):
    if not v or str(v).strip() in ('nan', ''):
        return ''
    s = str(v).strip()
    u = s.upper()
    if u in ('FRETE0', 'FRETE 0'):
        return 'FRETE0'
    if u == 'MIMO':
        return 'MIMO'
    if u == 'LIVE':
        return 'LIVE'
    if u == 'GOZE':
        return 'GOZE'
    if re.match(r'(?i)^bonifiq\d+', s):
        return 'BonifiQ ' + re.search(r'\d+', s).group() + '%'
    if re.match(r'(?i)^produto\d{4}-', s):
        return 'X (link produto)'
    return u


# ── VG (vigência) ───────────────────────────────────────────────────────────────
def vg_includes_day(vg_str, day):
    if not vg_str or str(vg_str).strip() in ('', 'nan', 'forever', 'TRUE', 'PC', '-'):
        return True
    s = str(vg_str).strip()
    m = re.match(r'(\d+)\s*[àa]\s*(\d+)', s)
    if m:
        return int(m.group(1)) <= day <= int(m.group(2))
    m = re.match(r'(\d+)\s+e\s+(\d+)', s)
    if m:
        return day in (int(m.group(1)), int(m.group(2)))
    if '•' in s:
        for part in s.split('•'):
            part = part.strip()
            try:
                if day == int(part):
                    return True
            except Exception:
                pass
        return False
    try:
        return day == int(s)
    except Exception:
        return True


# ── Parser promomos ─────────────────────────────────────────────────────────────
COL_COD = 2; COL_VG = 9; COL_CT = 22

def parse_promomos(kp_raw):
    """Retorna (promomos_ct, promo_brinde)."""
    promomos_ct  = {}
    promo_brinde = set()
    _in_data = False
    for idx in range(len(kp_raw)):
        row = kp_raw.iloc[idx]
        v2 = str(row[COL_COD]).strip() if len(row) > COL_COD and pd.notna(row[COL_COD]) else ''
        if v2 == 'Código':
            _in_data = True
            continue
        if not _in_data or not v2 or v2 == 'nan':
            continue
        cod = v2.upper()
        if 'BRINDESITE' in cod:
            promo_brinde.add(cod)
        vg_str = str(row[COL_VG]).strip() if len(row) > COL_VG and pd.notna(row[COL_VG]) else 'forever'
        if vg_str in ('nan', ''):
            vg_str = 'forever'
        ct_raw = str(row[COL_CT]).strip() if len(row) > COL_CT and pd.notna(row[COL_CT]) else ''
        custo  = br2float(ct_raw) if ct_raw and ct_raw not in ('nan', '') else 0.0
        if custo > 0:
            promomos_ct.setdefault(cod, []).append((vg_str, custo))
    return promomos_ct, promo_brinde


# ── Lookup de custo ─────────────────────────────────────────────────────────────
def make_get_custo(promomos_ct, custo_kits_cod, estoque_cod):
    def get_custo(ref_code, cat, day, is_prime_cg):
        ref_up = str(ref_code).strip().upper() if ref_code else ''

        for zero in ZERO_COST_ITEMS:
            if zero in ref_up:
                return 0.0, 'BRINDE'

        for (k_ref, vg_str), ct in CONFIRMED_CT.items():
            if ref_up == k_ref.upper() and vg_includes_day(vg_str, day):
                if is_prime_cg and day in (6, 7):
                    ct_p = CONFIRMED_CT.get((k_ref, '6-7 Prime'), ct)
                    return ct_p, 'CT_PRIME'
                return ct, 'CT_CONFIRMADO'

        if cat in ('Kit/Duplinha', 'Atacado') and ref_up in promomos_ct:
            for vg_str, ct in promomos_ct[ref_up]:
                if vg_includes_day(vg_str, day) and ct > 0:
                    return ct, 'PROMOMOS'

        if cat in ('Kit/Duplinha', 'Atacado') and ref_code in custo_kits_cod:
            return custo_kits_cod[ref_code], 'CUSTO_KITS'

        if ref_code in estoque_cod:
            return estoque_cod[ref_code], 'ESTOQUE'

        return 0.0, 'SEM_CUSTO'
    return get_custo


# ── Reconstruir tabelas de custo a partir do JSON salvo no Supabase ─────────────
def rebuild_cost_tables(data: dict):
    """Reconstrói cost_tables a partir do dict deserializado do Supabase."""
    estoque_cod        = data['estoque_cod']
    estoque_disp       = data['estoque_disp']
    estoque_marca      = data['estoque_marca']
    estoque_marca_disp = data['estoque_marca_disp']
    custo_kits_cod     = data['custo_kits_cod']
    promomos_ct        = {k: [tuple(x) for x in v] for k, v in data['promomos_ct'].items()}
    promo_brinde       = set(data['promo_brinde'])
    get_custo          = make_get_custo(promomos_ct, custo_kits_cod, estoque_cod)
    return {
        'estoque_cod':        estoque_cod,
        'estoque_disp':       estoque_disp,
        'estoque_marca':      estoque_marca,
        'estoque_marca_disp': estoque_marca_disp,
        'custo_kits_cod':     custo_kits_cod,
        'promomos_ct':        promomos_ct,
        'promo_brinde':       promo_brinde,
        'get_custo':          get_custo,
        'de_fat':             pd.DataFrame(),
    }


# ── Carregar tabelas de custo ───────────────────────────────────────────────────
def build_cost_tables(de_raw, ck_raw, kp_raw):
    de_raw = de_raw.copy()
    de_raw.columns = [c.strip() for c in de_raw.columns]
    de_raw['Almoxarifado'] = de_raw['Almoxarifado'].str.strip()
    de_fat = de_raw[de_raw['Almoxarifado'] == 'FATURAMENTO'].copy()
    de_fat['Código']    = de_fat['Código'].str.strip()
    de_fat['custo_unit'] = de_fat['Custo unit. líquido'].apply(br2float)
    de_fat['saldo']      = de_fat['Saldo'].apply(br2float)
    de_fat['reservado']  = de_fat['Reservado'].apply(br2float)
    de_fat['disponivel'] = de_fat['saldo'] - de_fat['reservado']
    de_fat['marca']      = de_fat['MARP_NOM'].str.strip() if 'MARP_NOM' in de_fat.columns else ''

    estoque_cod        = de_fat.set_index('Código')['custo_unit'].to_dict()
    estoque_disp       = de_fat.groupby('Código')['disponivel'].sum().to_dict()
    estoque_marca      = de_fat.groupby('Código')['marca'].first().to_dict()
    estoque_marca_disp = de_fat.groupby('marca')['disponivel'].sum().to_dict()

    ck_raw = ck_raw.copy()
    ck_raw.columns = [c.strip().replace('\x00', '').strip('﻿') for c in ck_raw.columns]
    ck_raw['custo_unit'] = ck_raw['Custo unit. líquido'].apply(br2float)
    ck_fat = ck_raw[ck_raw['Almoxarifado'].str.strip() == 'FATURAMENTO'].copy()
    ck_fat['Código']  = ck_fat['Código'].str.strip()
    custo_kits_cod    = ck_fat.set_index('Código')['custo_unit'].to_dict()

    promomos_ct, promo_brinde = parse_promomos(kp_raw)

    get_custo = make_get_custo(promomos_ct, custo_kits_cod, estoque_cod)

    return {
        'estoque_cod':        estoque_cod,
        'estoque_disp':       estoque_disp,
        'estoque_marca':      estoque_marca,
        'estoque_marca_disp': estoque_marca_disp,
        'custo_kits_cod':     custo_kits_cod,
        'promomos_ct':        promomos_ct,
        'promo_brinde':       promo_brinde,
        'get_custo':          get_custo,
        'de_fat':             de_fat,
    }


# ── Processar VTEX ──────────────────────────────────────────────────────────────
def process_vtex(dv_raw, cost_tables):
    get_custo     = cost_tables['get_custo']
    estoque_marca = cost_tables['estoque_marca']

    dv = dv_raw.copy()
    dv.columns = [c.strip() for c in dv.columns]
    dv['Status'] = dv['Status'].str.strip()
    dv = dv[~dv['Status'].str.contains('Cancelado|cancel', case=False, na=False)].copy()

    from zoneinfo import ZoneInfo
    TZ_BR = ZoneInfo('America/Sao_Paulo')
    dv['Creation Date'] = dv['Creation Date'].str.strip()
    dv['dt_criacao']    = pd.to_datetime(dv['Creation Date'], errors='coerce', utc=True,
                                          format='mixed')
    dt_br               = dv['dt_criacao'].dt.tz_convert(TZ_BR)
    dv['dia']           = dt_br.dt.day.fillna(0).astype(int)
    dv['data_criacao']  = dt_br.dt.strftime('%Y-%m-%d')

    num_cols = ['Payment Value', 'SKU Value', 'SKU Selling Price', 'SKU Total Price',
                'Shipping Value', 'Total Value', 'Discounts Totals', 'Quantity_SKU',
                'Shipping List Price']
    for col in num_cols:
        if col in dv.columns:
            dv[col] = dv[col].apply(br2float)

    dv['categoria']    = dv['Category Ids Sku'].apply(classify_category)
    dv['is_prime_cg']  = dv['Discounts Names'].str.contains(
        r'C&G Compre Kit e Cliente Prime', case=False, na=False)
    dv['utm_source_norm'] = dv['UtmSource'].apply(norm_utm)
    dv['utm_medium_norm'] = dv['UtmMedium'].apply(norm_utm)
    dv['coupon_norm']     = dv['Coupon'].apply(norm_coupon)
    dv['ref_code']        = dv['Reference Code'].str.strip()

    custos, fontes = [], []
    for _, row in dv.iterrows():
        ct, fonte = get_custo(row['ref_code'], row['categoria'], row['dia'], row['is_prime_cg'])
        custos.append(ct * row['Quantity_SKU'])
        fontes.append(fonte)
    dv['custo_total'] = custos
    dv['fonte_custo'] = fontes

    dv['rb_sku']      = dv['SKU Value'] * dv['Quantity_SKU']
    dv['rl_total']    = dv['SKU Total Price']
    dv['item_status'] = dv['ref_code'].str.upper().apply(
        lambda r: 'BRINDE' if any(z in r for z in ZERO_COST_ITEMS) else '')
    dv['canal_pgto']  = dv['Payment System Name'].str.strip()
    dv['marca']       = dv['ref_code'].map(estoque_marca).fillna('')

    return dv


# ── Agregações P&L ──────────────────────────────────────────────────────────────
def calc_pl(dv):
    """Retorna dict com métricas globais."""
    dv_pl = dv[dv['item_status'] != 'BRINDE'].copy()
    dv_cg = dv_pl[(dv_pl['rl_total'] == 0) & (dv_pl['custo_total'] > 0)]

    rb_total    = dv_pl['rb_sku'].sum()
    rl_total    = dv_pl['rl_total'].sum()
    custo_total = dv_pl['custo_total'].sum()
    custo_cg    = dv_cg['custo_total'].sum()
    lucro_bruto = rl_total - custo_total
    margem      = lucro_bruto / rl_total if rl_total else 0
    n_pedidos   = dv['Order'].nunique()
    n_dias      = int(dv_pl['dia'].max()) if len(dv_pl) else 1

    return {
        'rb_total':    rb_total,
        'rl_total':    rl_total,
        'descontos':   rb_total - rl_total,
        'custo_total': custo_total,
        'custo_cg':    custo_cg,
        'lucro_bruto': lucro_bruto,
        'margem':      margem,
        'n_pedidos':   n_pedidos,
        'n_dias':      n_dias,
        'dv_pl':       dv_pl,
        'dv_cg':       dv_cg,
    }


def calc_pedidos(dv, dv_pl):
    pedidos = dv.groupby('Order').agg(
        data=('data_criacao', 'first'),
        uf=('UF', 'first'),
        status=('Status', 'first'),
        payment_value=('Payment Value', 'first'),
        shipping_value=('Shipping Value', 'sum'),
        canal_pgto=('canal_pgto', 'first'),
        installments=('Installments', 'first'),
        utm_source=('utm_source_norm', 'first'),
        utm_medium=('utm_medium_norm', 'first'),
        coupon=('coupon_norm', 'first'),
        n_itens=('Quantity_SKU', 'sum'),
        rb_pedido=('rb_sku', 'sum'),
        rl_pedido=('rl_total', 'sum'),
        custo_pedido=('custo_total', 'sum'),
        categoria_principal=('categoria', lambda x: x.mode()[0] if len(x) else ''),
    ).reset_index()
    pedidos['lucro_bruto'] = pedidos['rl_pedido'] - pedidos['custo_pedido']
    pedidos['margem'] = (pedidos['lucro_bruto'] / pedidos['rl_pedido'].replace(0, np.nan)).fillna(0)
    return pedidos


def calc_produtos(dv_pl, estoque_disp):
    prod = dv_pl.groupby(['ref_code', 'SKU Name', 'categoria', 'marca']).agg(
        qtd=('Quantity_SKU', 'sum'),
        rb=('rb_sku', 'sum'),
        rl=('rl_total', 'sum'),
        custo=('custo_total', 'sum'),
    ).reset_index()
    prod['lucro']   = prod['rl'] - prod['custo']
    prod['margem']  = (prod['lucro'] / prod['rl'].replace(0, np.nan)).fillna(0)
    prod['estoque'] = prod['ref_code'].map(estoque_disp).fillna(0).round(0).astype(int)
    avg_mg = prod[prod['rl'] > 0]['margem'].mean()
    def _acao(r):
        if r['lucro'] <= 0: return '⚠️ Revisar'
        if r['margem'] >= avg_mg: return '✅ Replicar'
        return '📊 Manter'
    prod['acao'] = prod.apply(_acao, axis=1)
    return prod


def calc_marcas(dv_pl, estoque_marca_disp):
    marcas = dv_pl.groupby('marca').agg(
        qtd=('Quantity_SKU', 'sum'),
        rl=('rl_total', 'sum'),
        custo=('custo_total', 'sum'),
    ).reset_index()
    marcas['lucro']   = marcas['rl'] - marcas['custo']
    marcas['margem']  = (marcas['lucro'] / marcas['rl'].replace(0, np.nan)).fillna(0)
    marcas['estoque'] = marcas['marca'].map(estoque_marca_disp).fillna(0).round(0).astype(int)
    avg_mg = marcas[marcas['rl'] > 0]['margem'].mean()
    def _acao(r):
        if r['lucro'] <= 0: return '⚠️ Revisar'
        if r['margem'] >= avg_mg: return '✅ Ampliar'
        return '📊 Manter'
    marcas['acao'] = marcas.apply(_acao, axis=1)
    return marcas[marcas['rl'] > 0].sort_values('rl', ascending=False)


def calc_kits(dv_pl, estoque_disp):
    kits = dv_pl[dv_pl['categoria'].isin(['Kit/Duplinha', 'Atacado'])].groupby(
        ['ref_code', 'SKU Name']).agg(
        qtd=('Quantity_SKU', 'sum'),
        rl=('rl_total', 'sum'),
        custo=('custo_total', 'sum'),
    ).reset_index()
    kits['lucro']   = kits['rl'] - kits['custo']
    kits['margem']  = (kits['lucro'] / kits['rl'].replace(0, np.nan)).fillna(0)
    kits['estoque'] = kits['ref_code'].map(estoque_disp).fillna(0).round(0).astype(int)
    avg_mg = kits[kits['rl'] > 0]['margem'].mean()
    def _acao(r):
        if r['lucro'] <= 0: return '⚠️ Revisar'
        if r['margem'] >= avg_mg: return '✅ Replicar'
        return '📊 Manter'
    kits['acao'] = kits.apply(_acao, axis=1)
    return kits[kits['rl'] > 0].sort_values('rl', ascending=False)


def calc_guia_compras(dv_pl, estoque_disp, n_dias):
    giro = dv_pl.groupby('ref_code').agg(
        nome=('SKU Name', 'first'),
        qtd=('Quantity_SKU', 'sum'),
        rl=('rl_total', 'sum'),
        custo=('custo_total', 'sum'),
    ).reset_index()
    giro['lucro']        = giro['rl'] - giro['custo']
    giro['giro_diario']  = giro['qtd'] / max(n_dias, 1)
    giro['proj_30d']     = giro['giro_diario'] * 30
    giro['disp']         = giro['ref_code'].map(estoque_disp).fillna(0)
    giro['sugestao']     = (giro['proj_30d'] - giro['disp']).clip(lower=0)
    giro['dias_estoque'] = (giro['disp'] / giro['giro_diario'].replace(0, np.nan)).fillna(999).round(0)

    def _prioridade(r):
        if r['lucro'] > 100 and r['dias_estoque'] < 7:  return '🔴 URGENTE'
        if r['dias_estoque'] < 7 or (r['lucro'] > 50 and r['dias_estoque'] < 14): return '🟠 ALTA'
        if r['dias_estoque'] < 14: return '🟡 MÉDIA'
        return '🟢 OK'
    giro['prioridade'] = giro.apply(_prioridade, axis=1)
    return giro[giro['qtd'] > 0].sort_values('prioridade')
