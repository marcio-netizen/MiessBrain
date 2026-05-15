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


# ── Arquivos de custo ────────────────────────────────────────────────────────────
_COST_KEYS = {'de': 'cost/de.bin', 'ck': 'cost/ck.bin', 'kp': 'cost/kp.bin'}

def upload_cost_file(client, name: str, data: bytes):
    """Salva um arquivo de custo no Supabase Storage (de, ck ou kp)."""
    key = _COST_KEYS[name]
    try:
        client.storage.from_(BUCKET).remove([key])
    except Exception:
        pass
    client.storage.from_(BUCKET).upload(
        key, data,
        file_options={'content-type': 'application/octet-stream', 'upsert': 'true'},
    )

def download_cost_file(client, name: str):
    """Baixa bytes de um arquivo de custo (de, ck ou kp). Retorna None se não existir."""
    try:
        return client.storage.from_(BUCKET).download(_COST_KEYS[name])
    except Exception:
        return None
