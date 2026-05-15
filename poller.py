"""
poller.py — Sincronizador incremental VTEX → parquet local
Rode em terminal separado: python poller.py
Busca só os pedidos novos/alterados desde a última execução (incremental).
O dashboard lê o parquet local — sempre atualizado, sem espera.
"""
import os, sys, time, json, logging
from datetime import datetime, timezone, timedelta
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from vtex_api import fetch_orders, _get_orders_page, _get_order_detail, _flatten_order, _headers

# ── Configuração ─────────────────────────────────────────────────────────────────
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

def load_secrets():
    secrets_path = os.path.join(os.path.dirname(__file__), '.streamlit', 'secrets.toml')
    if tomllib and os.path.exists(secrets_path):
        with open(secrets_path, 'rb') as f:
            s = tomllib.load(f)
        return s.get('vtex', {})
    # fallback: ler como texto simples
    cfg = {}
    if os.path.exists(secrets_path):
        for line in open(secrets_path):
            line = line.strip()
            if '=' in line and not line.startswith('#') and not line.startswith('['):
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

secrets    = load_secrets()
STORE      = secrets.get('store_name', 'miess')
APP_KEY    = secrets.get('app_key', '')
APP_TOKEN  = secrets.get('app_token', '')

CACHE_DIR   = os.path.join(os.path.dirname(__file__), 'cache')
PARQUET_ALL = os.path.join(CACHE_DIR, 'vtex_realtime.parquet')
STATE_FILE  = os.path.join(CACHE_DIR, 'poller_state.json')
INTERVAL    = 300   # segundos entre polling (5 min)
LOOKBACK    = 15    # minutos de lookback para não perder pedidos em trânsito

os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('poller')


# ── Estado persistente ───────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    # primeira execução: início do mês atual
    now = datetime.now(timezone.utc)
    return {'last_fetch': now.replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0).isoformat()}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


# ── Fetch incremental ─────────────────────────────────────────────────────────────
def fetch_incremental(since_iso):
    """Busca pedidos criados desde `since_iso` (com lookback de segurança)."""
    since_dt = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
    since_dt = since_dt - timedelta(minutes=LOOKBACK)
    now_dt   = datetime.now(timezone.utc)

    date_from = since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    date_to   = now_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    log.info(f'Buscando pedidos {date_from} → {date_to}')

    # Lista de IDs novos
    page      = 1
    per_page  = 100
    order_ids = []

    while True:
        data = _get_orders_page(STORE, APP_KEY, APP_TOKEN, date_from, date_to, page, per_page)
        batch = [o['orderId'] for o in data.get('list', [])]
        order_ids += batch
        total = data.get('paging', {}).get('total', 0)
        log.info(f'  Página {page}: {len(batch)} pedidos (total={total})')
        if len(order_ids) >= total or not batch:
            break
        page += 1
        time.sleep(1.5)

    if not order_ids:
        log.info('  Nenhum pedido novo.')
        return pd.DataFrame()

    log.info(f'  Detalhando {len(order_ids)} pedidos…')
    rows = []
    for i, oid in enumerate(order_ids):
        try:
            detail = _get_order_detail(STORE, APP_KEY, APP_TOKEN, oid)
            rows.extend(_flatten_order(detail))
        except Exception as e:
            log.warning(f'  Erro pedido {oid}: {e}')
        time.sleep(0.8)
        if (i + 1) % 20 == 0:
            log.info(f'  {i+1}/{len(order_ids)} processados')

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    log.info(f'  ✅ {len(df)} linhas de SKU obtidas')
    return df


# ── Supabase (opcional) ───────────────────────────────────────────────────────────
def get_supabase_client():
    secrets = load_secrets()
    url = secrets.get('supabase_url', '')
    key = secrets.get('supabase_key', '')
    if url and key:
        try:
            from db import get_client
            return get_client(url, key)
        except Exception as e:
            log.warning(f'Supabase não disponível: {e}')
    return None


# ── Merge com parquet existente ──────────────────────────────────────────────────
def merge_and_save(df_new):
    if os.path.exists(PARQUET_ALL):
        try:
            df_old = pd.read_parquet(PARQUET_ALL)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
        except Exception:
            df_all = df_new
    else:
        df_all = df_new

    if 'Order' in df_all.columns and 'Reference Code' in df_all.columns:
        df_all = df_all.drop_duplicates(subset=['Order', 'Reference Code'], keep='last')

    # Salvar local
    df_all.to_parquet(PARQUET_ALL, index=False)
    log.info(f'  Parquet local: {len(df_all):,} linhas')

    # Fazer upload para Supabase (se configurado)
    sb = get_supabase_client()
    if sb:
        try:
            from db import upload_parquet
            upload_parquet(sb, df_all)
            log.info(f'  ✅ Upload Supabase OK')
        except Exception as e:
            log.warning(f'  Upload Supabase falhou: {e}')

    return df_all


# ── Loop principal ────────────────────────────────────────────────────────────────
def run():
    if not APP_KEY or not APP_TOKEN:
        log.error('App Key / Token não encontrados em secrets.toml — abortando.')
        sys.exit(1)

    log.info(f'Poller iniciado — store={STORE} | intervalo={INTERVAL}s')

    while True:
        state = load_state()
        try:
            df_new = fetch_incremental(state['last_fetch'])
            if len(df_new):
                merge_and_save(df_new)
            state['last_fetch'] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        except KeyboardInterrupt:
            log.info('Poller interrompido pelo usuário.')
            break
        except Exception as e:
            log.error(f'Erro no ciclo de fetch: {e}')

        log.info(f'Aguardando {INTERVAL}s até o próximo ciclo…')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    run()
