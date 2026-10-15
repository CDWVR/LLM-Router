FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv ROUTER_DEPLOYMENT=saas
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
# Railway may override PORT at runtime; the server and healthcheck both read it.
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-m", "llm_router.healthcheck"]
CMD ["python", "-m", "llm_router.serve"]
