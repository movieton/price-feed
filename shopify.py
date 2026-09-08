"""Read-only catalogue extraction using Shopify bulk queries."""
import json
import http.client
import os
import random
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path


class FeedError(Exception):
    pass


START = '''mutation Start($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status } userErrors { field message }
  }
}'''
POLL = '''query Poll($id: ID!) {
  node(id: $id) { ... on BulkOperation { id status url errorCode objectCount } }
}'''
SHOP = 'query Shop { shop { currencyCode } }'


def bulk_query(c):
    price = 'price'
    if c['price_mode'] == 'market':
        country = c['country']
        if not re.fullmatch('[A-Z]{2}', country):
            raise FeedError('invalid_country')
        price = 'contextualPricing(context: {country: %s}) { price { amount currencyCode } }' % country
    return '''{ productVariants(query: %s) { edges { node {
      id title sku barcode %s inventoryQuantity inventoryItem { tracked }
      image { url } selectedOptions { name value }
      product { id title handle status productType vendor onlineStoreUrl featuredImage { url } }
    } } } }''' % (json.dumps(c['catalog_query']), price)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FeedError('redirect_rejected')


class Client:
    def __init__(self, c):
        self.c = c
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', c['shop_domain']):
            raise FeedError('invalid_shop_domain')
        if not re.fullmatch(r'20\d{2}-(01|04|07|10)', c['api_version']):
            raise FeedError('invalid_api_version')
        self.opener = urllib.request.build_opener(NoRedirect())
        self.token = os.environ.get('SHOPIFY_ADMIN_TOKEN')
        if not self.token:
            self.token = self.fetch_token()

    def fetch_token(self):
        client_id = os.environ.get('SHOPIFY_CLIENT_ID')
        client_secret = os.environ.get('SHOPIFY_CLIENT_SECRET')
        if not client_id or not client_secret:
            raise FeedError('missing_credentials')
        body = urlencode({'grant_type': 'client_credentials', 'client_id': client_id,
                          'client_secret': client_secret}).encode()
        for attempt in range(self.c['retries'] + 1):
            req = urllib.request.Request('https://' + self.c['shop_domain'] + '/admin/oauth/access_token', body,
                                         {'Content-Type': 'application/x-www-form-urlencoded'})
            try:
                with self.opener.open(req, timeout=self.c['request_timeout_seconds']) as response:
                    result = json.load(response)
                if not result.get('access_token') or 'read_products' not in result.get('scope', '').split(','):
                    raise FeedError('token_or_read_products_scope_missing')
                if result.get('expires_in', 0) <= self.c['bulk_timeout_seconds'] + self.c['download_timeout_seconds'] + 300:
                    raise FeedError('token_lifetime_too_short')
                return result['access_token']
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == self.c['retries']:
                    raise FeedError('oauth_http_%d' % e.code) from None
                self.pause(attempt, e.headers.get('Retry-After'))
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException):
                if attempt == self.c['retries']:
                    raise FeedError('oauth_network_failure') from None
                self.pause(attempt)

    def pause(self, attempt, retry_after=None):
        try:
            delay = float(retry_after) if retry_after else 2 ** attempt + random.random()
        except ValueError:
            delay = 2 ** attempt
        if delay > self.c['max_retry_delay_seconds']:
            raise FeedError('retry_delay_exceeds_budget')
        time.sleep(max(0, delay))

    def graphql(self, query, variables=None, retry=True):
        url = 'https://%s/admin/api/%s/graphql.json' % (self.c['shop_domain'], self.c['api_version'])
        attempts = self.c['retries'] + 1 if retry else 1
        for attempt in range(attempts):
            req = urllib.request.Request(url, json.dumps({'query': query, 'variables': variables or {}}).encode(),
                                         {'Content-Type': 'application/json', 'X-Shopify-Access-Token': self.token})
            try:
                with self.opener.open(req, timeout=self.c['request_timeout_seconds']) as response:
                    result = json.load(response)
                errors = result.get('errors', [])
                if errors:
                    if all(e.get('extensions', {}).get('code') == 'THROTTLED' for e in errors) and attempt + 1 < attempts:
                        throttle = result.get('extensions', {}).get('cost', {})
                        state = throttle.get('throttleStatus', {})
                        delay = max(1, (throttle.get('requestedQueryCost', 1) - state.get('currentlyAvailable', 0)) / max(state.get('restoreRate', 1), 1))
                        self.pause(attempt, delay)
                        continue
                    raise FeedError('graphql_error')
                if not result.get('data'):
                    raise FeedError('graphql_missing_data')
                return result['data']
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                    raise FeedError('http_%d' % e.code) from None
                self.pause(attempt, e.headers.get('Retry-After'))
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException):
                if attempt + 1 == attempts:
                    raise FeedError('network_timeout_or_failure') from None
                self.pause(attempt)
        raise FeedError('retry_exhausted')

    def download(self, url, target):
        # Signed bulk URL never receives the Admin token; never log it.
        if not url.startswith('https://'):
            raise FeedError('bulk_url_not_https')
        for attempt in range(self.c['retries'] + 1):
            try:
                started = time.monotonic()
                with self.opener.open(url, timeout=self.c['request_timeout_seconds']) as response, open(target, 'wb') as output:
                    size = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.c['max_download_bytes'] or time.monotonic() - started > self.c['download_timeout_seconds']:
                            raise FeedError('download_limit')
                        output.write(chunk)
                    expected = response.headers.get('Content-Length')
                    if expected is not None and size != int(expected):
                        raise FeedError('download_incomplete')
                return
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == self.c['retries']:
                    raise FeedError('download_http_%d' % e.code) from None
                self.pause(attempt, e.headers.get('Retry-After'))
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException):
                if attempt == self.c['retries']:
                    raise FeedError('download_network_failure') from None
                self.pause(attempt)

    def export(self, target, state_path):
        if self.c['price_mode'] == 'base':
            if self.graphql(SHOP)['shop']['currencyCode'] != self.c['currency']:
                raise FeedError('base_currency_mismatch')
        state = Path(state_path)
        query = bulk_query(self.c)
        import hashlib
        fingerprint = hashlib.sha256((self.c['shop_domain'] + self.c['api_version'] + query).encode()).hexdigest()
        if state.exists():
            saved = json.loads(state.read_text())
            if saved['fingerprint'] != fingerprint:
                raise FeedError('pending_operation_config_changed')
            operation_id = saved['id']
        else:
            # Submission is not retried after an ambiguous network failure: avoid duplicate jobs.
            job = self.graphql(START, {'query': query}, retry=False)['bulkOperationRunQuery']
            if job['userErrors'] or not job['bulkOperation']:
                raise FeedError('bulk_submission_rejected')
            operation_id = job['bulkOperation']['id']
            state.write_text(json.dumps({'id': operation_id, 'fingerprint': fingerprint}))
            state.chmod(0o600)
        deadline = time.monotonic() + self.c['bulk_timeout_seconds']
        while time.monotonic() < deadline:
            job = self.graphql(POLL, {'id': operation_id})['node']
            if not job:
                raise FeedError('bulk_operation_missing')
            if job['status'] == 'COMPLETED':
                if job['url']:
                    self.download(job['url'], target)
                else:
                    Path(target).write_text('')
                with Path(target).open(encoding='utf-8') as downloaded:
                    count = sum(1 for line in downloaded if line.strip())
                if count != int(job['objectCount']):
                    raise FeedError('bulk_count_mismatch')
                state.unlink()
                return
            if job['status'] in ('FAILED', 'CANCELED', 'EXPIRED'):
                state.unlink()
                raise FeedError('bulk_' + job['status'].lower())
            time.sleep(self.c['poll_seconds'])
        raise FeedError('bulk_timeout_resume_next_run')
