"""Container health check, using the same dynamic port as the server."""
import os
import urllib.request
from urllib.parse import urlsplit


def main():
    host = urlsplit(os.environ["ROUTER_PUBLIC_URL"]).netloc
    port = int(os.environ.get("PORT", os.environ.get("ROUTER_PORT", "8765")))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/readyz", headers={"Host": host})
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Application not ready")


if __name__ == "__main__":
    main()
