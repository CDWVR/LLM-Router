# Deploy LLM Router on Railway

Use one application service built from the repository Dockerfile and one PostgreSQL service. Railway terminates HTTPS; the compose Caddy service is for self-hosted deployments and is not needed here. Application data is stored in PostgreSQL; the app container does not need a persistent volume. Back up the database and encryption key separately.

## Connect the repository

1. Create a Railway project and add PostgreSQL. Keep its network access private unless an explicitly controlled maintenance workflow needs otherwise.
2. Add an application service from `CDWVR/LLM-Router`. Select **codex/saas-foundation** until the implementation is merged into your default branch. Use the repository root (the directory containing Dockerfile and railway.json) as the service root.
3. Generate a Railway HTTPS domain or attach your own domain. Add the service variables below before attempting the full deployment. An initial deploy without required configuration will intentionally fail closed.
4. Railway reads `railway.json`: Dockerfile build, pre-deploy migration, production start command, `/readyz` healthcheck and bounded restart policy. Do not override these with the local `app.py` command.

## Required application variables

| Variable | Value |
| --- | --- |
| `ROUTER_DEPLOYMENT` | `saas` (also the Docker image default) |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` using the actual database service name in Railway's variable-reference picker |
| `ROUTER_PUBLIC_URL` | Your exact HTTPS origin, e.g. `https://router-production.up.railway.app`, with no path |
| `ROUTER_SECRET_KEY` | A generated Fernet key; keep the same value across replicas and deployments |
| `OIDC_ISSUER` | Your identity provider's HTTPS issuer, matching its discovery document |
| `OIDC_CLIENT_ID` | Client registered for this application |
| `OIDC_CLIENT_SECRET` | The registered client secret |

Generate the encryption key locally with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and paste it directly into Railway's secret variable interface. Do not put it in Git, screenshots, logs or this document. Back it up securely; replacing it without a rotation procedure makes saved provider credentials unreadable and invalidates OIDC state cookies.

The application and migration CLI normalize standard `postgresql://` / `postgres://` URLs to the installed psycopg driver, preserving encoded credentials and query options. Use the reference to the private database URL, not a copied external TCP proxy URL.

Register **`ROUTER_PUBLIC_URL/auth/callback`** as the exact callback URL at the identity provider, substituting the actual origin. Enable the desired signup policy and MFA there. New users create their own empty workspace; provider credentials are added after login under Workspace > Models. Never enable local deployment mode to get around missing authentication configuration.

## Optional variables

- `ROUTER_WORKERS=1`: a conservative initial process count for a small instance; the default is 2. Increase after measuring memory, latency and database connections. Each worker can hold up to 10 database connections.
- `ROUTER_GENERATION_ENABLED=0`: pause new model requests while testing identity and workspace setup; change to `1` and redeploy when ready.
- `ROUTER_METRICS_TOKEN`: a separate random secret if an authenticated monitoring scraper is used.
- `ROUTER_PROVIDER_HOSTS`: comma-separated additional approved HTTPS provider hosts. Keep this narrow.
- `ROUTER_CLIENT_IP_HEADER=x-real-ip`: choose Railway's documented client-IP header. It is only trusted for peers in `ROUTER_TRUSTED_PROXY_CIDRS`.
- `ROUTER_TRUSTED_PROXY_CIDRS`: explicitly verified platform proxy peer addresses/networks. Leave unset until the trust boundary is verified. Without it, clients behind a proxy share its conservative quota. Never configure all networks merely to make forwarded headers work; verify the edge and private-service access paths first. See [Abuse controls](ABUSE_CONTROLS.md).

Railway supplies `PORT`; the production command reads it directly and binds `0.0.0.0`. Avoid hard-coding a different target port. The app permits Railway's `healthcheck.railway.app` hostname only for `/readyz` and `/healthz` when Railway's project environment is present. Other requests still require the public hostname.

## Migration and deployment behavior

The pre-deploy command is `python -m llm_router.manage migrate`. It connects to PostgreSQL, applies numbered transactional revisions and exits. The serving command is `python -m llm_router.serve`; workers only validate the schema. Failed migrations stop the rollout. Pre-deploy tasks need the same database connectivity and variables as the application.

Migrations run against the same database as the previous deployment. Future schema changes must remain compatible with the previous workers during overlap, or require a planned maintenance window. See [Database operations](DATABASE_OPERATIONS.md). The initial variable-reference setup uses the same database role for migration and serving; separate these privileges before a larger public deployment.

The container healthcheck and Railway probe both use readiness, including database access. Railway's deployment healthcheck is not ongoing uptime monitoring: configure an external monitor and alerts separately. Test an intentional database failure in staging and confirm readiness returns 503.

## Administration after upgrading

Refresh browser tabs after a deployment to load the latest workspace forms. Model, connection, routing and report edits carry revisions; if another administrator saves first, your save is rejected without overwriting their changes. Copy any draft values you need before choosing **Reload latest (discard draft)**.

## Staging acceptance checklist

- Pre-deploy exits successfully and reports current schema equal to required schema.
- `/readyz` returns 200 on the public origin; unknown hosts and unauthenticated `/v1/models` are denied.
- Login, callback, secure session cookies, logout and membership removal work through the real HTTPS domain.
- Connect a provider, verify model metadata, then make one intentionally budgeted provider call. Verify streaming and cancellation through the edge.
- Generate a project API key and test `/v1/models` and a limited request from your consuming application.
- Re-deploy; confirm conversations, reports and encrypted provider credentials survive.
- Test IP/header trust from outside and from private services; inspect throttling behavior before opening signup.
- Exercise backup restore and key recovery, set spending alerts, and verify continuous monitoring.

Repository checks exercise a local container with Railway-like PORT, database URL, probe hostname and environment variables. They do not establish that a real Railway project, OIDC client, domain or certificate is configured. Those checks require your deployment.

Official references: [config as code](https://docs.railway.com/config-as-code/reference), [pre-deploy commands](https://docs.railway.com/deployments/pre-deploy-command), [healthchecks](https://docs.railway.com/deployments/healthchecks), [network headers and limits](https://docs.railway.com/networking/public-networking/specs-and-limits).
