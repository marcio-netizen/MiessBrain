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
