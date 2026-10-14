"""Shared request throttles; forwarded client identity is trusted only explicitly."""
import hashlib
import hmac
import ipaddress
import os
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


class AbuseProtection:
    def __init__(self, app, store, key, enabled=True):
        self.app, self.store, self.key, self.enabled = app, store, key, enabled
        self.proxies = [ipaddress.ip_network(value.strip()) for value in os.environ.get("ROUTER_TRUSTED_PROXY_CIDRS", "").split(",") if value.strip()]

    def client_identity(self, scope):
        peer = (scope.get("client") or ("unknown", 0))[0]
        try:
            address = ipaddress.ip_address(peer)
            if any(address in network for network in self.proxies):
                # Reverse proxy must overwrite this single-value header.
                values = [value for key, value in scope.get("headers", []) if key.lower() == b"x-router-client-ip"]
                if len(values) == 1:
                    address = ipaddress.ip_address(values[0].decode("ascii"))
            peer = str(address)
        except (ValueError, UnicodeError):
            pass
        return hmac.new(self.key, peer.encode(), hashlib.sha256).hexdigest()

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        group, limit = ("auth", 20) if path in {"/login", "/auth/callback"} else ("write", 60) if scope["method"] != "GET" else ("read", 180)
        # Static assets and infrastructure probes do not consume user quotas.
        if path in {"/healthz", "/readyz", "/styles.css", "/app.js", "/scene.js", "/report.js", "/workspace.js", "/favicon.svg"} or path.startswith(("/vendor/", "/fonts/")):
            return await self.app(scope, receive, send)
        try:
            allowed = await run_in_threadpool(self.store.allow_request, group+":"+self.client_identity(scope), limit)
        except Exception:
            response = JSONResponse({"error": {"code": "admission_unavailable"}} if path.startswith("/v1/") else {"error": "admission_unavailable"}, status_code=503, headers={"Retry-After": "5"})
            return await response(scope, receive, send)
        if not allowed:
            response = JSONResponse({"error": {"code": "rate_limited"}} if path.startswith("/v1/") else {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
