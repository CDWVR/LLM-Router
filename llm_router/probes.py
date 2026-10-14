"""Permit the platform probe hostname only on health routes."""
from starlette.responses import PlainTextResponse

RAILWAY_PROBE_HOST = "healthcheck.railway.app"


class ProbeHostBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            hosts = [value.decode("latin1").split(":", 1)[0].lower() for key, value in scope.get("headers", []) if key.lower() == b"host"]
            if RAILWAY_PROBE_HOST in hosts and scope.get("path") not in {"/healthz", "/readyz"}:
                return await PlainTextResponse("Invalid host", status_code=400)(scope, receive, send)
        await self.app(scope, receive, send)
