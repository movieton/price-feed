import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import xml.etree.ElementTree as ET

import feed
import shopify

ROOT = Path(__file__).resolve().parents[1]


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.c = json.loads((ROOT / 'config.example.json').read_text())
        self.c['output'] = str(self.directory / 'public/feed.xml')
        self.c['state_dir'] = str(self.directory / 'private')
        self.rows = [json.loads(x) for x in (ROOT / 'examples/catalog.jsonl').read_text().splitlines()]
        self.source = self.directory / 'input.jsonl'

    def run_feed(self):
        self.source.write_text(''.join(json.dumps(v) + '\n' for v in self.rows))
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            result = feed.run(self.c, self.source)
        self.log = json.loads(output.getvalue())
        return result

    def xml(self):
        return ET.parse(self.c['output'])

    def test_variants_escaping_and_stock(self):
        self.assertEqual(self.run_feed(), 0)
        offers = self.xml().findall('./shop/offers/offer')
        self.assertEqual([o.get('id') for o in offers], ['1001', '1002'])
        self.assertEqual([o.get('available') for o in offers], ['true', 'false'])
        self.assertIn('Runner & Road <GTX>', offers[0].findtext('name'))
        self.assertIn('variant=1002', offers[1].findtext('url'))
        self.assertEqual([o.findtext('currencyId') for o in offers], ['EUR', 'EUR'])
        self.assertEqual(offers[0].findtext('barcode'), '0460000000017')
        self.assertIsNone(offers[1].find('barcode'))
        self.assertEqual(offers[0].find('param[@name="Артикул"]').text, 'RUN-"BLACK"-42')
        self.assertIsNone(offers[0].find('param[@name="Размер"]'))
        self.assertIn('&amp;', Path(self.c['output']).read_text())
        self.assertEqual(self.log['products'], 1)
        self.assertEqual(self.log['variants'], 2)

    def test_last_good_survives_missing_required_fields(self):
        self.c['missing_policy'] = 'fail'
        self.assertEqual(self.run_feed(), 0)
        before = Path(self.c['output']).read_bytes()
        for field in ('title', 'onlineStoreUrl', 'featuredImage', 'productType'):
            original = copy.deepcopy(self.rows)
            self.rows[0]['product'][field] = None if field == 'featuredImage' else ''
            self.assertEqual(self.run_feed(), 1, field)
            self.assertEqual(Path(self.c['output']).read_bytes(), before)
            self.rows = original

    def test_invalid_prices_preserve_previous(self):
        self.c['missing_policy'] = 'fail'
        self.assertEqual(self.run_feed(), 0)
        before = Path(self.c['output']).read_bytes()
        for value in ('NaN', 'Infinity', '-1', '0', '', None, '1,23'):
            self.rows[0]['price'] = value
            self.assertEqual(self.run_feed(), 1)
            self.assertEqual(Path(self.c['output']).read_bytes(), before)

    def test_duplicates_are_fatal_even_with_skip(self):
        self.c.update(missing_policy='skip', max_skip_fraction=1)
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.assertEqual(self.run_feed(), 1)
        self.assertEqual(self.log['error'], 'duplicate_variant_id')

    def test_skip_is_default_and_updates_existing_feed_despite_large_drop(self):
        self.assertEqual(self.run_feed(), 0)
        self.rows[0]['product']['featuredImage'] = None
        self.rows[1]['price'] = '115.00'
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.log['skipped'], 1)
        self.assertEqual(self.log['reasons'], {'missing_picture': 1})
        offers = self.xml().findall('./shop/offers/offer')
        self.assertEqual([o.get('id') for o in offers], ['1002'])
        self.assertEqual(offers[0].findtext('price'), '115.00')

    def test_missing_fields_exclude_only_affected_variant(self):
        original = copy.deepcopy(self.rows)
        for field in ('title', 'onlineStoreUrl', 'featuredImage', 'productType'):
            for missing in (None, ''):
                with self.subTest(field=field, missing=missing):
                    self.rows = copy.deepcopy(original)
                    self.rows[0]['product'][field] = missing
                    self.assertEqual(self.run_feed(), 0)
                    self.assertEqual(self.log['skipped'], 1)
                    self.assertEqual(self.xml().find('./shop/offers/offer').get('id'), '1002')

    def test_all_variants_missing_id_publish_empty_feed(self):
        self.assertEqual(self.run_feed(), 0)
        for row in self.rows:
            row.pop('id')
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.log['skipped'], 2)
        self.assertEqual(self.xml().findall('./shop/offers/offer'), [])

    def test_unknown_inventory_fails_and_backorder_does_not_override_zero(self):
        self.rows[1]['availableForSale'] = True
        self.rows[1]['inventoryPolicy'] = 'CONTINUE'
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.xml().findall('./shop/offers/offer')[1].get('available'), 'false')
        self.rows[0]['inventoryItem']['tracked'] = False
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.log['reasons'], {'unknown_stock': 1})

    def test_market_currency_and_amount(self):
        self.c['price_mode'] = 'market'
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.xml().findtext('./shop/offers/offer/price'), '89.90')
        self.assertEqual(self.xml().findtext('./shop/offers/offer/currencyId'), 'EUR')
        self.rows[0]['contextualPricing']['price']['currencyCode'] = 'USD'
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.log['reasons'], {'market_currency_mismatch': 1})

    def test_market_url_prefix(self):
        self.c['storefront_base_url'] = 'https://example.fr/fr'
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.xml().findtext('./shop/offers/offer/url'), 'https://example.fr/fr/products/runner?variant=1001')

    def test_uuid_and_exact_parameters(self):
        v = self.rows[0]
        extra = {'1001': {'uuid': 'accounting-unique-1', 'params': {'Пол': 'Мужской'}}}
        offer = feed.make_offer(v, self.c, extra)
        self.assertEqual(offer.get('uuid'), 'accounting-unique-1')
        self.assertEqual(offer.find('param[@name="Пол"]').text, 'Мужской')
        extra['1001']['params']['пол'] = 'wrong case'
        with self.assertRaisesRegex(feed.FeedError, 'invalid_extra_parameter'):
            feed.make_offer(v, self.c, extra)

    def test_empty_and_large_drop_are_rejected(self):
        self.c.update(min_offers=1, max_drop_fraction=0.25)
        self.assertEqual(self.run_feed(), 0)
        self.rows = self.rows[:1]
        self.assertEqual(self.run_feed(), 1)
        self.assertEqual(self.log['error'], 'offer_count_drop')
        self.rows = []
        self.assertEqual(self.run_feed(), 1)

    def test_xml_control_character_and_replace_failure_keep_good_file(self):
        self.assertEqual(self.run_feed(), 0)
        before = Path(self.c['output']).read_bytes()
        self.rows[0]['product']['title'] += '\x01'
        self.assertEqual(self.run_feed(), 1)
        self.assertEqual(Path(self.c['output']).read_bytes(), before)
        self.rows[0]['product']['title'] = 'Normal'
        with patch('feed.os.replace', side_effect=OSError('disk error')):
            self.assertEqual(self.run_feed(), 1)
        self.assertEqual(Path(self.c['output']).read_bytes(), before)
        self.assertEqual(list(Path(self.c['output']).parent.glob('.feed.xml.*')), [])

    def test_live_is_blocked_before_network(self):
        with patch('feed.Client') as client, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(feed.run(self.c), 1)
        client.assert_not_called()

    def test_lock(self):
        path = self.directory / 'lock'
        with feed.lock(path):
            with self.assertRaisesRegex(feed.FeedError, 'already_running'):
                with feed.lock(path):
                    pass
        with feed.lock(path):
            pass

    def test_category_cycles(self):
        self.c['categories'][0]['parentId'] = '2'
        self.assertEqual(self.run_feed(), 1)

    def test_bulk_scale(self):
        template = self.rows[0]
        self.rows = []
        for n in range(2000):
            for size in range(3):
                row = copy.deepcopy(template)
                row['id'] = 'gid://shopify/ProductVariant/' + str(10000 + n * 3 + size)
                row['product']['id'] = 'gid://shopify/Product/' + str(n)
                self.rows.append(row)
        self.assertEqual(self.run_feed(), 0)
        self.assertEqual(self.log['products'], 2000)
        self.assertEqual(self.log['offers'], 6000)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / 'config.example.json').read_text())
        with patch.dict('os.environ', SHOPIFY_ADMIN_TOKEN='secret-test'):
            self.client = shopify.Client(self.c)

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_throttled_retry(self):
        bad = self.response({'errors': [{'extensions': {'code': 'THROTTLED'}}]})
        good = self.response({'data': {'shop': {'currencyCode': 'EUR'}}})
        with patch.object(self.client.opener, 'open', side_effect=[bad, good]), patch('shopify.time.sleep') as sleep:
            self.assertEqual(self.client.graphql(shopify.SHOP)['shop']['currencyCode'], 'EUR')
            sleep.assert_called_once()

    def test_credential_exchange(self):
        reply = self.response({'access_token': 'new-token', 'scope': 'read_products', 'expires_in': 86399})
        with patch.dict('os.environ', {'SHOPIFY_CLIENT_ID': 'test-id', 'SHOPIFY_CLIENT_SECRET': 'test-secret'}, clear=True), patch('urllib.request.OpenerDirector.open', return_value=reply) as request:
            client = shopify.Client(self.c)
        self.assertEqual(client.token, 'new-token')
        self.assertIn(b'grant_type=client_credentials', request.call_args[0][0].data)

    def test_download_has_no_admin_header(self):
        with tempfile.TemporaryDirectory() as d:
            response = io.BytesIO(b'{}\n')
            response.headers = {'Content-Length': '3'}
            with patch.object(self.client.opener, 'open', return_value=response) as request:
                self.client.download('https://example.com/signed', Path(d)/'bulk')
            self.assertEqual(request.call_args[0], ('https://example.com/signed',))

    def test_completed_bulk_and_count_mismatch(self):
        for count in ('1', '2'):
            with tempfile.TemporaryDirectory() as d:
                target = Path(d)/'bulk'
                state = Path(d)/'state'
                replies = [{'shop': {'currencyCode': 'EUR'}}, {'bulkOperationRunQuery': {'userErrors': [], 'bulkOperation': {'id': 'gid://shopify/BulkOperation/1'}}}, {'node': {'status': 'COMPLETED', 'url': 'https://example.com/result', 'objectCount': count}}]
                with patch.object(self.client, 'graphql', side_effect=replies), patch.object(self.client, 'download', side_effect=lambda url, path: path.write_text('{}\n')):
                    if count == '1':
                        self.client.export(target, state)
                        self.assertFalse(state.exists())
                    else:
                        with self.assertRaisesRegex(shopify.FeedError, 'bulk_count_mismatch'):
                            self.client.export(target, state)
                        self.assertTrue(state.exists())

    def test_bulk_resume_uses_saved_id_without_new_submission(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)/'state'
            fingerprint = hashlib.sha256((self.c['shop_domain'] + self.c['api_version'] + shopify.bulk_query(self.c)).encode()).hexdigest()
            state.write_text(json.dumps({'id': 'saved-id', 'fingerprint': fingerprint}))
            replies = [{'shop': {'currencyCode': 'EUR'}}, {'node': {'status': 'COMPLETED', 'url': None, 'objectCount': '0'}}]
            with patch.object(self.client, 'graphql', side_effect=replies) as gql:
                self.client.export(Path(d)/'bulk', state)
            self.assertEqual(gql.call_args_list[1].args, (shopify.POLL, {'id': 'saved-id'}))

    def test_http_retry_and_no_retry_auth(self):
        error = urllib.error.HTTPError('https://example.com', 429, '', {'Retry-After': '2'}, None)
        with patch.object(self.client.opener, 'open', side_effect=[error, self.response({'data': {'ok': True}})]), patch('shopify.time.sleep') as sleep:
            self.assertTrue(self.client.graphql(shopify.SHOP)['ok'])
            sleep.assert_called_once_with(2)
        error.code = 401
        with patch.object(self.client.opener, 'open', side_effect=error) as call:
            with self.assertRaisesRegex(shopify.FeedError, 'http_401'):
                self.client.graphql(shopify.SHOP)
            self.assertEqual(call.call_count, 1)

    def test_failed_bulk_never_downloads_partial_data(self):
        with tempfile.TemporaryDirectory() as d:
            replies = [{'shop': {'currencyCode': 'EUR'}}, {'bulkOperationRunQuery': {'userErrors': [], 'bulkOperation': {'id': 'gid://shopify/BulkOperation/1'}}}, {'node': {'status': 'FAILED', 'partialDataUrl': 'https://example.com/partial'}}]
            with patch.object(self.client, 'graphql', side_effect=replies), patch.object(self.client, 'download') as download:
                with self.assertRaisesRegex(shopify.FeedError, 'bulk_failed'):
                    self.client.export(Path(d)/'bulk', Path(d)/'state')
                download.assert_not_called()

    def test_api_exception_does_not_leak_secrets_or_replace_feed(self):
        with tempfile.TemporaryDirectory() as d:
            self.c.update(live_confirmed=True, availability_confirmed=True, language='en', market='confirmed', output=d+'/feed.xml', state_dir=d+'/private')
            Path(self.c['output']).write_text('previous')
            with patch.object(shopify.Client, 'export', side_effect=RuntimeError('secret-test signed-url')), patch.dict('os.environ', SHOPIFY_ADMIN_TOKEN='secret-test'), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(feed.run(self.c), 1)
                self.assertNotIn('secret-test', out.getvalue())
                self.assertNotIn('signed-url', out.getvalue())
            self.assertEqual(Path(self.c['output']).read_text(), 'previous')


if __name__ == '__main__':
    unittest.main()
