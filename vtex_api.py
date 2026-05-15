"""
vtex_api.py — Conector VTEX OMS API
Estratégia de 2 fases:
  Fase 1: lista paginada de pedidos (summary) — 1 req por 100 pedidos
  Fase 2: detalhe individual só para pedidos recentes ou amostragem

Para grandes volumes (mês inteiro), recomenda-se upload de CSV.
Para "hoje" ou "últimos 7 dias", a API funciona sem problema de rate limit.
"""
import time
import requests
import pandas as pd
from datetime import datetime, timezone


def _headers(app_key, app_token):
    return {
        'X-VTEX-API-AppKey':   app_key,
        'X-VTEX-API-AppToken': app_token,
        'Accept':              'application/json',
        'Content-Type':        'application/json',
    }


def _request_with_retry(url, hdrs, params=None, max_retries=6):
    """GET com retry exponencial em 429/5xx."""
    delay = 2.0
    for attempt in range(max_retries):
        resp = requests.get(url, headers=hdrs, params=params, timeout=30)
        if resp.status_code == 429:
            wait = delay * (2 ** attempt)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def _get_orders_page(store_name, app_key, app_token, date_from, date_to, page=1, per_page=100):
    url = f"https://{store_name}.vtexcommercestable.com.br/api/oms/pvt/orders"
    params = {
        'f_creationDate': f"creationDate:[{date_from} TO {date_to}]",
        'orderBy':        'creationDate,desc',
        'page':           page,
        'per_page':       per_page,
    }
    return _request_with_retry(url, _headers(app_key, app_token), params)


def _get_order_detail(store_name, app_key, app_token, order_id):
    url = f"https://{store_name}.vtexcommercestable.com.br/api/oms/pvt/orders/{order_id}"
    return _request_with_retry(url, _headers(app_key, app_token))


def _flatten_order(o):
    """Achata um pedido detalhado em linhas SKU (uma por item)."""
    rows = []
    raw_dt = o.get('creationDate', '')
    try:
        from datetime import timezone as _tz
        import datetime as _dt
        created_at = _dt.datetime.fromisoformat(raw_dt.replace('Z', '+00:00')).astimezone(_tz.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    except Exception:
        created_at = raw_dt
    status     = o.get('status', '')
    order_id   = o.get('orderId', '')
    total_value = o.get('value', 0) / 100

    payment_value = 0.0
    pgto_name     = ''
    installments  = 1
    if o.get('paymentData') and o['paymentData'].get('transactions'):
        pmts = o['paymentData']['transactions'][0].get('payments', [])
        if pmts:
            pgto_name     = pmts[0].get('paymentSystemName', '')
            installments  = pmts[0].get('installments', 1)
            payment_value = sum(p.get('value', 0) for p in pmts) / 100

    totals_list     = o.get('totals', [])
    shipping_value  = next((t['value'] for t in totals_list if t.get('id') == 'Shipping'), 0) / 100
    discounts_total = next((t['value'] for t in totals_list if t.get('id') == 'Discounts'), 0) / 100

    discount_names = '; '.join(
        promo.get('name', '')
        for promo in o.get('ratesAndBenefitsData', {}).get('rateAndBenefitsIdentifiers', [])
    )
    coupon = (o.get('marketingData') or {}).get('coupon', '')

    mkt          = o.get('marketingData') or {}
    utm_source   = mkt.get('utmSource', '')
    utm_medium   = mkt.get('utmMedium', '')
    utm_campaign = mkt.get('utmCampaign', '')

    addr   = (o.get('shippingData') or {}).get('address') or {}
    uf     = addr.get('state', '')
    cidade = addr.get('city', '')

    for item in o.get('items', []):
        ref_code    = item.get('refId', '') or item.get('sellerSku', '')
        sku_name    = item.get('name', '')
        qty         = item.get('quantity', 1)
        sku_value   = item.get('price', 0) / 100
        sku_selling = item.get('sellingPrice', 0) / 100
        sku_total   = item.get('sellingPrice', 0) * qty / 100
        cat_ids     = '/'.join(str(c) for c in item.get('categoryIds', []))

        rows.append({
            'Order':               order_id,
            'Creation Date':       created_at,
            'Status':              status,
            'Reference Code':      ref_code,
            'SKU Name':            sku_name,
            'Category Ids Sku':    cat_ids,
            'Quantity_SKU':        qty,
            'SKU Value':           sku_value,
            'SKU Selling Price':   sku_selling,
            'SKU Total Price':     sku_total,
            'Payment Value':       payment_value,
            'Shipping Value':      shipping_value,
            'Total Value':         total_value,
            'Discounts Totals':    discounts_total,
            'Discounts Names':     discount_names,
            'Coupon':              coupon,
            'Payment System Name': pgto_name,
            'Installments':        installments,
            'UtmSource':           utm_source,
            'UtmMedium':           utm_medium,
            'UtmCampaign':         utm_campaign,
            'UF':                  uf,
            'City':                cidade,
        })
    return rows


def fetch_orders(store_name, app_key, app_token,
                 date_from=None, date_to=None,
                 max_orders=500, progress_cb=None):
    """
    Busca pedidos via VTEX OMS API e retorna DataFrame.

    Para volumes grandes (mês inteiro), use max_orders pequeno ou prefira upload CSV.
    Padrão max_orders=500 cobre ~5 dias de operação sem estourar rate limit.

    Parameters
    ----------
    max_orders : int — teto de pedidos a buscar em detalhe (evita timeout)
    """
    now = datetime.now(timezone.utc)
    if date_from is None:
        date_from = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    if date_to is None:
        date_to = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    per_page = 100

    # Fase 1: coletar IDs via listagem paginada
    first       = _get_orders_page(store_name, app_key, app_token, date_from, date_to, 1, per_page)
    total_orders = first.get('paging', {}).get('total', 0)
    n_pages      = -(-min(total_orders, max_orders) // per_page)  # ceil

    order_ids = [o['orderId'] for o in first.get('list', [])]
    if progress_cb:
        progress_cb(1, n_pages, f'Lista página 1/{n_pages} — {total_orders} pedidos no período')

    for pg in range(2, n_pages + 1):
        time.sleep(1.5)
        data = _get_orders_page(store_name, app_key, app_token, date_from, date_to, pg, per_page)
        order_ids += [o['orderId'] for o in data.get('list', [])]
        if progress_cb:
            progress_cb(pg, n_pages, f'Lista página {pg}/{n_pages}')

    order_ids = order_ids[:max_orders]

    # Fase 2: detalhar cada pedido
    all_rows  = []
    n_total   = len(order_ids)
    for i, oid in enumerate(order_ids):
        try:
            detail = _get_order_detail(store_name, app_key, app_token, oid)
            all_rows.extend(_flatten_order(detail))
        except Exception:
            pass
        time.sleep(0.8)
        if progress_cb and i % 10 == 0:
            progress_cb(i + 1, n_total, f'Detalhe pedido {i+1}/{n_total}')

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    for col in ['Quantity_SKU', 'SKU Value', 'SKU Selling Price', 'SKU Total Price',
                'Payment Value', 'Shipping Value', 'Total Value', 'Discounts Totals']:
        if col in df.columns:
            df[col] = df[col].astype(str)
    return df
