# LLM Router

Route requests to your own OpenAI, Anthropic, Groq, OpenRouter, or compatible model connections. The app combines chat, explainable model selection, provider onboarding, conversation management, comparisons, and workspace administration.

## Run locally

Requires Python 3.12 and uv:

```powershell
uv sync --locked
Copy-Item .env.example .env
uv run python app.py
```

Open http://127.0.0.1:8765. Local mode automatically opens a trusted personal workspace and refuses a non-loopback bind through `app.py`. It is for development on your computer. Do not expose it through a tunnel or reverse proxy. It does not use the legacy `ROUTER_PASSWORD` gate.

Open **Workspace > Models** to connect a provider, optionally discover its models, and enter a model's limits, capabilities, prices, and quality estimates. Credentials added here are encrypted in the database. Local mode can also use the environment keys in `.env` and the seed `catalog.json`; SaaS projects start empty and never inherit operator provider keys.

Local data lives in `data/workspace.sqlite3`; the generated encryption key is `data/secret.key`. Back up both together and restrict access to this directory. Losing the key loses access to saved provider credentials. Existing legacy conversations and bug-report databases are not automatically migrated.

## Included product features

- Streaming chat with cancellation, image input, model-selection details, optional answer verification and safe formatted output.
- Workspace dashboard with reserved spend, response latency, recent requests and provider status.
- Saved conversations with search, rename, archive/restore and JSON export.
- Provider onboarding, connection checks, model discovery, manual model registration and capability controls.
- Balanced, speed and quality routing profiles, provider allowlists, request limits and shared workspace budgets. Local deployments also support a loopback-only private profile.
- Two- or three-model comparison and saved evaluation prompts.
- Bug-report button with a border flash, animated screenshot preview, rectangular redaction, image removal and a description field. Administrators can triage, tag and export private reports.
- Workspace membership with single-use invitations, project switching and revocable API keys.
- Responsive drawers, custom form styling, focus handling, transitions and reduced-motion support. Drafts remain in memory rather than browser storage.

The screenshot is a DOM reconstruction, not an operating-system screenshot; some effects and canvas content may differ. Review it before submitting. Reports stay in the workspace database; no GitHub issue or external message is sent.

## Deploy on Railway

Use [the Railway deployment guide](deploy/RAILWAY.md) for repository/branch selection, PostgreSQL references, OIDC configuration and required secrets. The included `railway.json` runs migrations before deployment, starts the production server on Railway's `PORT` and checks `/readyz`.

## Public multi-tenant deployment

The repository includes a non-root Docker image, PostgreSQL, a one-shot migration job and Caddy HTTPS compose configuration, plus an OIDC authentication integration. It is a deployment foundation, not a completed public launch. Read [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for the remaining release gates.

1. Copy `.env.saas.example` to `.env.saas` and fill in the domain, database password and identity-provider settings. Use a URL-safe database password or URL-encode its reserved characters in `DATABASE_URL` when adapting the deployment.
2. Generate a Fernet key with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; store it securely as `ROUTER_SECRET_KEY`. Preserve it across restarts and replicas.
3. Register `https://YOUR_DOMAIN/auth/callback` with your OIDC provider. Enable MFA and appropriate signup controls there.
4. Point the domain to the deployment, then run `docker compose --env-file .env.saas up --build -d`.
5. Follow [Database operations](deploy/DATABASE_OPERATIONS.md) for schema upgrades and recovery. Complete the authentication, backup/restore, egress, abuse-protection and load checks documented in the readiness file before accepting public traffic.

SaaS startup rejects missing OIDC/HTTPS/PostgreSQL configuration. Browser sessions use secure HTTP-only cookies and CSRF checks. API keys are project-scoped, stored as hashes, displayed once and expire after 90 days. Members share project conversations; reports and connection/key administration require an administrator role. Projects are the isolation boundary, not individual members.

## Integrate your application

Create a key under **Workspace > Account** and point your Chat Completions client to `/v1`. Use `model="auto"` or a configured Router alias. See [INTEGRATIONS.md](INTEGRATIONS.md) for examples, streaming, images, tools, idempotency and compatibility limits.

## Development checks

```powershell
uv run --locked python -m unittest discover -s tests
node --check web/app.js
node --check web/workspace.js
node --check web/report.js
node --check web/scene.js
```

`tests/preview_saas.py` starts a disposable UI preview on port 8877 using mock responses and a temporary database. It does not call paid providers. `tests/postgres_check.py` additionally checks PostgreSQL concurrent admission and persistence against a dedicated `router_test` database specified through `ROUTER_TEST_DATABASE_URL`.

The default entry point is `llm_router.service:create_app`. Legacy `llm_router.web`, `llm_router.api.chat_completion`, the CLI and report-export helpers remain for compatibility tests; their configuration and storage are separate from the new service. The broader design is in [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md).
