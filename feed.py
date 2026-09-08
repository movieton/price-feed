#!/usr/bin/env python3
"""Generate the marketing YML format, validate, then atomically publish."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote
import xml.etree.ElementTree as ET

from shopify import Client, FeedError

PARAMS = ('Артикул', 'Цвет', 'Пол', 'Возраст', 'Сезон', 'Вид активности', 'Тип товара', 'Модельный год', 'Назначение')
REQUIRED = ('name', 'url', 'price', 'categoryId', 'picture')


def emit(event, **fields):
    print(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}, ensure_ascii=False), flush=True)


def config_check(c, live=False):
    if c['price_mode'] not in ('base', 'market') or not re.fullmatch('[A-Z]{3}', c['currency']):
        raise FeedError('invalid_price_configuration')
    if c['missing_policy'] not in ('fail', 'skip'):
        raise FeedError('invalid_missing_policy')
    if c['availability'] != 'tracked_aggregate_positive':
        raise FeedError('unsupported_availability_rule')
    if c['language_mode'] != 'primary':
        raise FeedError('translation_mapping_required')
    for name in ('request_timeout_seconds', 'bulk_timeout_seconds', 'poll_seconds', 'download_timeout_seconds', 'max_download_bytes', 'max_retry_delay_seconds'):
        if not isinstance(c[name], (int, float)) or c[name] <= 0:
            raise FeedError('invalid_numeric_configuration')
    if type(c['min_offers']) is not int or c['min_offers'] < 0:
        raise FeedError('invalid_min_offers')
    if not isinstance(c['retries'], int) or not 0 <= c['retries'] <= 10:
        raise FeedError('invalid_retries')
    if not 0 <= c['max_skip_fraction'] <= 1 or not 0 <= c['max_drop_fraction'] <= 1:
        raise FeedError('invalid_fraction')
    if live and (not c.get('live_confirmed') or not c.get('language') or not c.get('market') or not c.get('catalog_query') or not c.get('availability_confirmed')):
        raise FeedError('live_configuration_not_confirmed')
    if live and c['price_mode'] == 'market' and not c.get('storefront_base_url'):
        raise FeedError('market_storefront_url_required')
    if c.get('storefront_base_url'):
        https(c['storefront_base_url'])
    for target in c['option_params'].values():
        if target not in PARAMS or target == 'Артикул':
            raise FeedError('invalid_parameter_mapping')
    ids = [str(x['id']) for x in c['categories']]
    if len(set(ids)) != len(ids) or any(not x for x in ids):
        raise FeedError('invalid_categories')
    parents = {str(x['id']): str(x.get('parentId', '')) for x in c['categories']}
    for cat in c['categories']:
        if not str(cat.get('name', '')).strip():
            raise FeedError('category_name_missing')
        seen = set()
        current = str(cat['id'])
        while current:
            if current in seen or current not in parents:
                raise FeedError('invalid_category_tree')
            seen.add(current)
            current = parents[current]
    if any(str(v) not in ids for v in c['category_map'].values()):
        raise FeedError('unknown_category_mapping')


def money(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise FeedError('invalid_price') from None
    if not number.is_finite() or number <= 0:
        raise FeedError('invalid_price')
    return format(number, 'f')


def https(value):
    parsed = urlsplit(value or '')
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise FeedError('invalid_https_url')
    return parsed


def add(parent, tag, value, **attrs):
    if value is not None and str(value).strip():
        ET.SubElement(parent, tag, attrs).text = str(value)


def make_offer(v, c, extra):
    match = re.fullmatch(r'gid://shopify/ProductVariant/(\d+)', v.get('id') or '')
    if not match:
        raise FeedError('invalid_variant_id')
    vid = match.group(1)
    p = v['product']
    if not (p.get('title') or '').strip():
        raise FeedError('missing_name')
    title = p['title']
    options = v.get('selectedOptions', [])
    suffix = ' / '.join(o['value'] for o in options if o['value'] and o['value'] != 'Default Title')
    if suffix:
        title += ' — ' + suffix
    url = https(p.get('onlineStoreUrl'))
    if c.get('storefront_base_url'):
        if not p.get('handle'):
            raise FeedError('missing_handle')
        url = https(c['storefront_base_url'].rstrip('/') + '/products/' + quote(p['handle'], safe=''))
    query = [(k, val) for k, val in parse_qsl(url.query, keep_blank_values=True) if k != 'variant']
    query.append(('variant', vid))
    url = urlunsplit(url._replace(query=urlencode(query)))
    if c['price_mode'] == 'market':
        price = (v.get('contextualPricing') or {}).get('price') or {}
        if price.get('currencyCode') != c['currency']:
            raise FeedError('market_currency_mismatch')
        amount = money(price.get('amount'))
    else:
        amount = money(v.get('price'))
    inventory = v.get('inventoryQuantity')
    if not (v.get('inventoryItem') or {}).get('tracked') or type(inventory) is not int:
        raise FeedError('unknown_stock')
    offer = ET.Element('offer', id=vid, available=str(inventory > 0).lower())
    details = extra.get(vid, {})
    if details.get('uuid'):
        offer.set('uuid', details['uuid'])
    add(offer, 'name', title)
    add(offer, 'url', url)
    add(offer, 'price', amount)
    add(offer, 'categoryId', c['category_map'].get(p.get('productType')))
    picture = (v.get('image') or p.get('featuredImage') or {}).get('url')
    if picture:
        https(picture)
    add(offer, 'picture', picture)
    add(offer, 'vendor', p.get('vendor'))
    add(offer, 'barcode', v.get('barcode'))
    params = {}
    for option in options:
        target = c['option_params'].get(option['name'])
        if target:
            if target in params and params[target] != option['value']:
                raise FeedError('conflicting_parameters')
            params[target] = option['value']
    for key, value in details.get('params', {}).items():
        if key not in PARAMS or key == 'Артикул':
            raise FeedError('invalid_extra_parameter')
        if key in params and params[key] != value:
            raise FeedError('conflicting_parameters')
        params[key] = value
    params['Артикул'] = v.get('sku')
    for name in PARAMS:
        add(offer, 'param', params.get(name), name=name)
    for field in REQUIRED:
        if not offer.findtext(field, '').strip():
            raise FeedError('missing_' + field)
    return offer


def build(source, c, extra, stats):
    root = ET.Element('yml_catalog', date=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'))
    shop = ET.SubElement(root, 'shop')
    for key in ('name', 'company', 'url'):
        add(shop, key, c['shop'].get(key))
    cats = ET.SubElement(shop, 'categories')
    for cat in c['categories']:
        attrs = {'id': str(cat['id'])}
        if 'parentId' in cat:
            attrs['parentId'] = str(cat['parentId'])
        add(cats, 'category', cat['name'], **attrs)
    offers = ET.SubElement(shop, 'offers')
    products = set()
    ids = set()
    with open(source, encoding='utf-8') as data:
        for line in data:
            if not line.strip():
                continue
            v = json.loads(line)
            if '__parentId' in v:
                raise FeedError('unexpected_nested_bulk_record')
            stats['variants'] += 1
            p = v['product']
            products.add(p['id'])
            stats['products'] = len(products)
            if v.get('id') and v['id'] in ids:
                raise FeedError('duplicate_variant_id')
            ids.add(v.get('id'))
            if p['status'] not in c['product_statuses']:
                stats['filtered'] += 1
                continue
            try:
                offers.append(make_offer(v, c, extra))
                stats['offers'] += 1
            except FeedError as error:
                stats['skipped'] += 1
                stats['reasons'][str(error)] += 1
                if len(stats['issues']) < 100:
                    raw_id = v.get('id') or ''
                    safe_id = raw_id if re.fullmatch(r'gid://shopify/ProductVariant/\d+', raw_id) else 'invalid'
                    stats['issues'].append({'variant': safe_id, 'reason': str(error)})
    if stats['skipped'] and c['missing_policy'] == 'fail':
        raise FeedError('required_data_missing_or_invalid')
    eligible = stats['variants'] - stats['filtered']
    if stats['skipped'] / max(eligible, 1) > c['max_skip_fraction']:
        raise FeedError('skip_limit')
    return root


def validate(path, c, previous=None):
    root = ET.parse(path).getroot()
    if root.tag != 'yml_catalog' or root.find('shop') is None:
        raise FeedError('invalid_xml_structure')
    cats = {e.attrib['id'] for e in root.findall('./shop/categories/category') if (e.text or '').strip()}
    ids, uuids = set(), set()
    offers = root.findall('./shop/offers/offer')
    if len(offers) < c['min_offers']:
        raise FeedError('too_few_offers')
    for offer in offers:
        oid = offer.get('id')
        if not oid or oid in ids:
            raise FeedError('duplicate_or_missing_id')
        ids.add(oid)
        uid = offer.get('uuid')
        if uid and uid in uuids:
            raise FeedError('duplicate_uuid')
        if uid:
            uuids.add(uid)
        for field in REQUIRED:
            if not offer.findtext(field, '').strip():
                raise FeedError('missing_' + field)
        money(offer.findtext('price'))
        if offer.findtext('categoryId') not in cats:
            raise FeedError('unknown_category')
        https(offer.findtext('url'))
        https(offer.findtext('picture'))
        if offer.get('available') not in ('true', 'false'):
            raise FeedError('invalid_availability')
        if any(p.get('name') not in PARAMS for p in offer.findall('param')):
            raise FeedError('invalid_param')
    if previous and Path(previous).exists():
        old = len(ET.parse(previous).findall('./shop/offers/offer'))
        if old and len(offers) < old * (1 - c['max_drop_fraction']):
            raise FeedError('offer_count_drop')


@contextmanager
def lock(path):
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FeedError('already_running') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def publish(root, target, c):
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            ET.indent(root, space='  ')
            ET.ElementTree(root).write(out, encoding='utf-8', xml_declaration=True)
            out.flush()
            os.fsync(out.fileno())
        validate(temporary, c, target)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(c, source=None, extra=None):
    started = time.monotonic()
    stats = dict(products=0, variants=0, offers=0, skipped=0, filtered=0, reasons=Counter(), issues=[])
    try:
        config_check(c, live=source is None)
        target = Path(c['output']).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        state_dir = Path(c['state_dir']).resolve()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock(state_dir / 'run.lock'):
            with tempfile.TemporaryDirectory(dir=state_dir) as temporary:
                if source is None:
                    source = Path(temporary) / 'bulk.jsonl'
                    Client(c).export(source, state_dir / 'operation.json')
                root = build(source, c, extra or {}, stats)
                publish(root, target, c)
        emit('success', duration_seconds=round(time.monotonic() - started, 3), **stats)
        return 0
    except Exception as error:
        # Do not serialize response bodies, exception repr, tokens or signed URLs.
        code = str(error) if isinstance(error, FeedError) else type(error).__name__
        emit('failure', error=code, duration_seconds=round(time.monotonic() - started, 3), **stats)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--fixture', help='Local bulk-format JSONL; does not contact Shopify')
    args = parser.parse_args()
    try:
        c = json.loads(Path(args.config).read_text(encoding='utf-8'))
        extra = json.loads(Path(c['extra_data']).read_text(encoding='utf-8')) if c.get('extra_data') else {}
    except Exception:
        emit('failure', error='configuration_read_failed')
        return 1
    return run(c, args.fixture, extra)


if __name__ == '__main__':
    raise SystemExit(main())
