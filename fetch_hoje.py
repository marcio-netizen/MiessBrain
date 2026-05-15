"""Busca pedidos de hoje e faz merge no Supabase."""
import io, os, sys, time, pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from vtex_api import _get_orders_page, _get_order_detail, _flatten_order
from supabase import create_client

URL = 'https://rdtbyvstubprnmfjfpte.supabase.co'
KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJkdGJ5dnN0dWJwcm5tZmpmcHRlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg4NzE0NzksImV4cCI6MjA5NDQ0NzQ3OX0.GTMFl5P-MMBjktqzt4yzoMHt7BcsbLZhLwFZ7A68q50'
STORE = 'miess'
APP_KEY   = 'vtexappkey-miess-SAKYMG'
APP_TOKEN = 'SJQCWOLQCZJFQCBQAQGYNWTEIFUNLRTSNGHFWVCDWRPOWJVHLMCZNYLVWOUUDQIDPQQVFDGICLVQWZLMJEWSXTVWKJLDPUHDQAMTJHFGCZMXSZYDKTPBFRDGJNHJMAUG'

TZ_BR  = ZoneInfo('America/Sao_Paulo')
now_br = datetime.now(TZ_BR)
d_from = now_br.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
d_to   = now_br.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
print(f'Buscando pedidos {d_from} -> {d_to}')

# Listar IDs
page, order_ids = 1, []
while True:
    data  = _get_orders_page(STORE, APP_KEY, APP_TOKEN, d_from, d_to, page, 100)
    batch = [o['orderId'] for o in data.get('list', [])]
    order_ids += batch
    total = data.get('paging', {}).get('total', 0)
    print(f'  Página {page}: {len(batch)} pedidos (total={total})')
    if len(order_ids) >= total or not batch: break
    page += 1
    time.sleep(1.5)

print(f'Total: {len(order_ids)} pedidos — detalhando...')
rows = []
for i, oid in enumerate(order_ids):
    try:
        rows.extend(_flatten_order(_get_order_detail(STORE, APP_KEY, APP_TOKEN, oid)))
    except Exception as e:
        print(f'  Erro {oid}: {e}')
    time.sleep(0.8)
    if (i+1) % 20 == 0:
        print(f'  {i+1}/{len(order_ids)} processados')

if not rows:
    print('Nenhum pedido encontrado hoje.')
    sys.exit()

df_hoje = pd.DataFrame(rows)
print(f'Hoje: {len(df_hoje)} linhas de SKU')

# Baixar base atual do Supabase
sb = create_client(URL, KEY)
print('Baixando base do Supabase...')
data_old = sb.storage.from_('vtex-data').download('realtime.parquet')
df_old   = pd.read_parquet(io.BytesIO(data_old))
print(f'Base atual: {len(df_old):,} linhas')

# Merge
df_all = pd.concat([df_old, df_hoje], ignore_index=True)
df_all = df_all.drop_duplicates(subset=['Order','Reference Code'], keep='last')
print(f'Após merge: {len(df_all):,} linhas | {df_all["Order"].nunique():,} pedidos')

# Normalizar tipos mistos: colunas object com valores int/float viram string
for col in df_all.select_dtypes(include='object').columns:
    df_all[col] = df_all[col].astype(str).replace({'nan': '', 'None': ''})

# Upload
buf = io.BytesIO()
df_all.to_parquet(buf, index=False)
buf.seek(0)
sb.storage.from_('vtex-data').upload('realtime.parquet', buf.getvalue(),
    file_options={'content-type':'application/octet-stream','upsert':'true'})
print('Upload OK — dashboard atualizado!')
