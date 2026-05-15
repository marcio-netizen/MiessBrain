"""
db.py — Supabase Storage: upload/download do parquet de pedidos
O poller faz upload; o dashboard faz download (cache 5 min).
"""
import io
import pandas as pd


def get_client(url: str, key: str):
    from supabase import create_client
    return create_client(url, key)


BUCKET  = 'vtex-data'
OBJ_KEY = 'realtime.parquet'


def upload_parquet(client, df: pd.DataFrame):
    """Salva DataFrame como parquet no Supabase Storage."""
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    try:
        client.storage.from_(BUCKET).remove([OBJ_KEY])
    except Exception:
        pass
    client.storage.from_(BUCKET).upload(
        OBJ_KEY, buf.getvalue(),
        file_options={'content-type': 'application/octet-stream', 'upsert': 'true'},
    )


def download_parquet(client) -> pd.DataFrame:
    """Baixa parquet do Supabase Storage e retorna DataFrame."""
    data = client.storage.from_(BUCKET).download(OBJ_KEY)
    return pd.read_parquet(io.BytesIO(data))


# ── Tabelas de custo (JSON compacto, evita limite de 50MB do Supabase) ───────────
_COST_KEY = 'cost/data.json'

def upload_cost_data(client, cost_tables: dict):
    """Serializa só os dicts de custo (sem DataFrames/closures) e salva no Supabase."""
    import json
    payload = {
        'estoque_cod':        cost_tables['estoque_cod'],
        'estoque_disp':       cost_tables['estoque_disp'],
        'estoque_marca':      cost_tables['estoque_marca'],
        'estoque_marca_disp': cost_tables['estoque_marca_disp'],
        'custo_kits_cod':     cost_tables['custo_kits_cod'],
        'promomos_ct':        cost_tables['promomos_ct'],
        'promo_brinde':       list(cost_tables['promo_brinde']),
    }
    buf = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    try:
        client.storage.from_(BUCKET).remove([_COST_KEY])
    except Exception:
        pass
    client.storage.from_(BUCKET).upload(
        _COST_KEY, buf,
        file_options={'content-type': 'application/json', 'upsert': 'true'},
    )

def download_cost_data(client):
    """Baixa o JSON de custos do Supabase. Retorna dict ou None."""
    import json
    try:
        raw = client.storage.from_(BUCKET).download(_COST_KEY)
        return json.loads(raw)
    except Exception:
        return None
