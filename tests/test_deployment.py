import os
import unittest
from unittest.mock import patch
from sqlalchemy.engine import make_url
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.testclient import TestClient
from llm_router.configuration import database_url
from llm_router.serve import server_options
from llm_router.probes import ProbeHostBoundary, RAILWAY_PROBE_HOST
from llm_router.abuse import AbuseProtection


class DeploymentTests(unittest.TestCase):
    def test_postgres_urls_keep_credentials_and_options(self):
        for scheme in ['postgres', 'postgresql', 'postgresql+psycopg']:
            original = make_url(scheme+'://user:p%40ss%2Fword@db.internal:5432/router?sslmode=require')
            converted = make_url(database_url(original.render_as_string(hide_password=False)))
            self.assertEqual(converted.drivername, 'postgresql+psycopg')
            self.assertEqual(converted.password, 'p@ss/word')
            self.assertEqual(converted.query, original.query)
            self.assertEqual(converted.host, 'db.internal')

    def test_sqlite_url_unchanged(self):
        self.assertEqual(database_url('sqlite:///data/workspace.sqlite3'), 'sqlite:///data/workspace.sqlite3')

    def test_platform_port_takes_precedence(self):
        with patch.dict(os.environ, {'ROUTER_DEPLOYMENT':'saas','PORT':'8123','ROUTER_PORT':'8765','ROUTER_WORKERS':'1'}, clear=True):
            options = server_options()
        self.assertEqual(options['port'], 8123)
        self.assertEqual(options['workers'], 1)
        self.assertEqual(options['host'], '0.0.0.0')
        self.assertFalse(options['proxy_headers'])

    def test_production_entrypoint_rejects_local(self):
        with patch.dict(os.environ, {'ROUTER_DEPLOYMENT':'local'}, clear=True):
            with self.assertRaises(RuntimeError):
                server_options()

    def test_invalid_platform_port_rejected(self):
        for value in ['abc','0','65536']:
            with patch.dict(os.environ, {'ROUTER_DEPLOYMENT':'saas','PORT':value}, clear=True):
                with self.assertRaises(RuntimeError):
                    server_options()

    def test_probe_hostname_only_allows_probes(self):
        async def ok(request):
            return PlainTextResponse('ok')
        app = Starlette(routes=[Route('/',ok),Route('/readyz',ok),Route('/healthz',ok),Route('/login',ok)])
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=['router.example',RAILWAY_PROBE_HOST])
        app.add_middleware(ProbeHostBoundary)
        with TestClient(app) as client:
            for path in ['/readyz','/healthz']:
                self.assertEqual(client.get(path, headers={'Host':RAILWAY_PROBE_HOST}).status_code, 200)
            for path in ['/','/login']:
                self.assertEqual(client.get(path, headers={'Host':RAILWAY_PROBE_HOST}).status_code, 400)
            self.assertEqual(client.get('/readyz',headers={'Host':'attacker.example'}).status_code,400)
            self.assertEqual(client.get('/',headers={'Host':'router.example'}).status_code,200)

    def test_railway_ip_header_still_requires_trusted_peer(self):
        scope = {'client': ('192.0.2.10',123), 'headers': [(b'x-real-ip',b'198.51.100.5')]}
        with patch.dict(os.environ, {'ROUTER_CLIENT_IP_HEADER':'x-real-ip','ROUTER_TRUSTED_PROXY_CIDRS':''}):
            untrusted = AbuseProtection(None,None,b'test')
        self.assertEqual(untrusted.client_identity(scope), untrusted.client_identity({**scope,'headers':[]}))
        with patch.dict(os.environ, {'ROUTER_CLIENT_IP_HEADER':'x-real-ip','ROUTER_TRUSTED_PROXY_CIDRS':'192.0.2.10/32'}):
            trusted = AbuseProtection(None,None,b'test')
        self.assertNotEqual(trusted.client_identity(scope), trusted.client_identity({**scope,'headers':[]}))

    def test_unknown_client_header_rejected(self):
        with patch.dict(os.environ, {'ROUTER_CLIENT_IP_HEADER':'untrusted-header'}):
            with self.assertRaises(RuntimeError):
                AbuseProtection(None,None,b'test')
