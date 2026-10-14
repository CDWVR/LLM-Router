"""Production-only entrypoint; reads platform PORT without shell expansion."""
import os
import uvicorn


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


def main():
    uvicorn.run("llm_router.service:create_app", **server_options())


if __name__ == "__main__":
    main()
