import concurrent.futures
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from llm_router.store import Store
from llm_router.abuse import AbuseProtection


class AbuseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store('sqlite:///'+str(Path(self.folder.name)/'limits.db'))

    def tearDown(self):
        self.store.engine.dispose()
        self.folder.cleanup()

    def test_atomic_limit_shared_by_store_instances(self):
        other = Store(str(self.store.engine.url))
        try:
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                result = list(pool.map(lambda i: [self.store, other][i%2].allow_request('auth:client', 5), range(20)))
            self.assertEqual(sum(result), 5)
        finally:
            other.engine.dispose()

    def test_window_reset_and_group_isolation(self):
        with patch('llm_router.store.time.time', return_value=120):
            self.assertTrue(self.store.allow_request('auth:client', 1))
            self.assertFalse(self.store.allow_request('auth:client', 1))
            self.assertTrue(self.store.allow_request('write:client', 1))
        with patch('llm_router.store.time.time', return_value=180):
            self.assertTrue(self.store.allow_request('auth:client', 1))

    def test_forwarded_identity_requires_trusted_proxy(self):
        scope = {'client': ('192.0.2.10', 123), 'headers': [(b'x-router-client-ip', b'198.51.100.2')]}
        with patch.dict(os.environ, {'ROUTER_TRUSTED_PROXY_CIDRS': ''}):
            middleware = AbuseProtection(None, self.store, b'test-key')
        self.assertEqual(middleware.client_identity(scope), middleware.client_identity({**scope, 'headers': []}))
        with patch.dict(os.environ, {'ROUTER_TRUSTED_PROXY_CIDRS': '192.0.2.10/32'}):
            trusted = AbuseProtection(None, self.store, b'test-key')
        self.assertNotEqual(trusted.client_identity(scope), trusted.client_identity({**scope, 'headers': []}))
        bad = {**scope, 'headers': [(b'x-router-client-ip', b'not-an-ip')]}
        self.assertEqual(trusted.client_identity(bad), trusted.client_identity({**scope, 'headers': []}))

    def test_http_throttle_and_database_failure(self):
        async def response(request):
            return JSONResponse({'ok': True})
        app = Starlette(routes=[Route('/login', response), Route('/healthz', response)])
        app.add_middleware(AbuseProtection, store=self.store, key=b'test-key')
        with TestClient(app) as client:
            for _ in range(20):
                self.assertEqual(client.get('/login').status_code, 200)
            denied = client.get('/login')
            self.assertEqual(denied.status_code, 429)
            self.assertEqual(denied.headers['retry-after'], '60')
            self.assertEqual(client.get('/healthz').status_code, 200)
            with patch.object(self.store, 'allow_request', side_effect=RuntimeError('database down')):
                self.assertEqual(client.get('/login').status_code, 503)
