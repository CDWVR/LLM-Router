from dotenv import load_dotenv
import os

load_dotenv()

if __name__ == "__main__":
    import uvicorn
    from llm_router.service import create_app
    mode = os.environ.get("ROUTER_DEPLOYMENT", "local")
    host = os.environ.get("ROUTER_HOST", "127.0.0.1")
    if mode != "saas" and host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Local mode binds only to loopback. Configure SaaS mode for public access.")
    uvicorn.run(create_app(), host=host, port=int(os.environ.get("ROUTER_PORT", "8765")), limit_concurrency=128, timeout_keep_alive=5, proxy_headers=False)
