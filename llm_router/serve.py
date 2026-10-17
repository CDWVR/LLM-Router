"""Production-only entrypoint; reads platform PORT without shell expansion."""
import os
import sys
import traceback

import uvicorn
from sqlalchemy.exc import OperationalError


def server_options():
    if os.environ.get("ROUTER_DEPLOYMENT") != "saas":
        raise RuntimeError("Production entrypoint requires ROUTER_DEPLOYMENT=saas; use app.py locally")
    def integer(name, default, low, high):
        try:
            value = int(os.environ.get(name, default))
        except ValueError:
            raise RuntimeError(name+" must be an integer") from None
        if not low <= value <= high:
            raise RuntimeError(name+" is out of range")
        return value
    return {"host": "0.0.0.0", "port": integer("PORT", os.environ.get("ROUTER_PORT", "8765"), 1, 65535),
            "workers": integer("ROUTER_WORKERS", "2", 1, 16), "factory": True,
            "limit_concurrency": 128, "timeout_keep_alive": 5,
            "timeout_graceful_shutdown": 35, "proxy_headers": False}


def hint(error):
    """Name the operator action that resolves a startup failure."""
    message = str(error)
    if "SaaS requires" in message:
        missing = [name for name, ok in (
            ("DATABASE_URL", os.environ.get("DATABASE_URL", "").startswith("postgresql")),
            ("ROUTER_PUBLIC_URL", os.environ.get("ROUTER_PUBLIC_URL", "").startswith("https://")),
            ("ROUTER_SECRET_KEY", bool(os.environ.get("ROUTER_SECRET_KEY"))),
            ("OIDC_ISSUER", os.environ.get("OIDC_ISSUER", "").startswith("https://")),
            ("OIDC_CLIENT_ID", bool(os.environ.get("OIDC_CLIENT_ID"))),
            ("OIDC_CLIENT_SECRET", bool(os.environ.get("OIDC_CLIENT_SECRET")))) if not ok]
        return message + (" | missing or invalid: " + ", ".join(missing) if missing else "")
    if "migration required" in message:
        return message + (" | the pre-deploy migration did not reach this database;"
                          " check that DATABASE_URL resolves to the same database in both steps")
    if isinstance(error, OperationalError):
        return message + (" | the app cannot reach the database; use the private DATABASE_URL"
                          " reference rather than the public proxy URL, and confirm both services"
                          " share a network")
    return message


def preflight():
    """Resolve configuration and the database before any port is bound.

    Uvicorn binds the socket in its parent process and respawns workers, so an
    exception raised inside the application factory leaves a container that looks
    alive, holds 0.0.0.0:$PORT, and answers nothing while its children die on
    every restart. Railway never sees a 2xx from the readiness probe, fails the
    deployment and serves 502 with no cause in the logs. Exit here instead.
    """
    from .service import create_app
    try:
        application = create_app()
    except Exception as error:
        print("Router startup preflight failed: " + hint(error), file=sys.stderr, flush=True)
        traceback.print_exc()
        raise SystemExit(1) from None
    application.state.store.engine.dispose()


def main():
    options = server_options()
    preflight()
    uvicorn.run("llm_router.service:create_app", **options)


if __name__ == "__main__":
    main()
