FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.0 && useradd --create-home --uid 10001 router
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY llm_router ./llm_router
COPY web ./web
COPY catalog.json ./
RUN mkdir -p /app/data && chown router:router /app/data
USER router
ENV PATH="/opt/venv/bin:$PATH"
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8765/healthz',headers={'Host':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(os.environ['ROUTER_PUBLIC_URL']).netloc}); urllib.request.urlopen(r,timeout=3)"
CMD ["uvicorn", "llm_router.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--workers", "2", "--limit-concurrency", "128", "--timeout-keep-alive", "5", "--no-proxy-headers"]
