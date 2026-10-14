# Production readiness and next improvements

Status: **implemented SaaS foundation; public launch still requires the release gates below**. Reviewed 2026-10-14. This assessment describes the default ASGI app, not the retained legacy server.

## Implemented

| Area | Delivered |
| --- | --- |
| Identity and tenancy | OIDC authorization-code login with PKCE; secure sessions; project membership; single-use invitations; administrator boundaries; scoped exports and API keys |
| Credentials | Server-side Fernet encryption for project provider keys; one-time display and hashes for Router keys; endpoint changes require a new credential |
| Routing | Scoped catalogs/connections; capability filtering; configurable profiles and allowlists; deadlines, output limits, conservative reservations, fallback, cancellation and required-check failures |
| API | Chat Completions subset, text SSE, inline images, function-call translation, scoped model listing and idempotent replay |
| Durable state | PostgreSQL SaaS and SQLite local; projects, threads, reports, keys, settings, usage, invitations and evaluation prompts |
| Admission | Transactional project budgets, three active generations, 30 starts/minute, same-thread exclusion and expired-request leases |
| Product | Provider onboarding/discovery, comparison, history search/archive/export, usage dashboard, report triage and account controls |
| UI | Animated report border and preview, screenshot redaction/removal, responsive workspace, custom controls, focus handling and reduced-motion support |
| Operations | Non-root container, HTTPS proxy configuration, liveness/readiness, protected metrics, request IDs, retention cleanup and CI regression/advisory checks, numbered transactional migrations and schema-version startup checks |

## Verification and practical limits

The regression suite currently has 113 passing tests, including tenant isolation, revocation, CSRF, encrypted credentials, context separation, concurrent budget admission, report-rate concurrency, streaming replay and partial-stream failures. PostgreSQL concurrency/persistence checks have also passed against a disposable database. Browser checks use mock providers so they do not establish real provider compatibility or incur generation charges. The Python dependency advisory scan covered 53 installed packages and reported no known vulnerabilities at scan time; that is not a security audit. The image built successfully; a read-only, non-root container with two workers passed PostgreSQL readiness and unauthenticated API/metrics denial checks. Browser comparison, screenshot reporting, triage and a 390-pixel mobile layout were checked with mock responses; the browser recorded no warnings or errors. JavaScript syntax checks and `git diff --check` also passed. Real OIDC and HTTPS certificate issuance were not exercised.

The application retains metadata and result records for 90 days; report/attachment cleanup uses a 30-day idle window, and sessions/keys expire. Conversation history remains until an explicit future deletion workflow is implemented. Archive is reversible and does not erase data. Cleanup runs every five minutes. Administrators should document these semantics before onboarding customers.

Provider keys are encrypted; messages, answers, screenshots and metadata are not application-encrypted. Protect database volumes, backups and operator access. Workspace members intentionally share conversations. Report screenshots can contain sensitive visible text even after automatic input masking; users must inspect/redact/remove them.

Metrics are basic retained-record gauges, not a complete historical monitoring system. Set `ROUTER_METRICS_TOKEN`; `/metrics` is forbidden without it. Scrape through a private monitoring path. Audit records are best-effort and bounded; failure is logged without making an already-completed mutation appear to fail. This is not a tamper-evident compliance audit trail.

## Concurrent administration

Connection, model and report edits now compare revisions under the same project transaction that writes the data. A stale editor receives HTTP 409 and cannot overwrite a newer configuration or credential. Existing records start at revision zero; the first protected save advances them, so no schema migration is required. The workspace preserves the unsaved form on conflict and offers an explicit reload action that discards that draft. Refresh open browser tabs after upgrading; older clients cannot update a record once its revision advances.

## Required before public launch

1. **Verify real authentication end to end.** Configure a real OIDC issuer, client and callback, MFA/signup controls, and HTTPS domain. Exercise first login, callback errors, logout, expired sessions and membership removal. The repository cannot verify an unconfigured identity provider.
2. **Configure edge protection and egress isolation.** Shared database-backed IP limits now protect login, management writes and dynamic reads; discovery also has a workspace limit, and operators can pause new generations. Configure the trusted proxy boundary as described in [Abuse controls](deploy/ABUSE_CONTROLS.md), then add edge volumetric protection and signup controls. Restrict outbound traffic to approved provider destinations; domain checks alone do not prevent every DNS or network-level attack.
3. **Rehearse upgrades with production privileges.** Numbered transactional migrations, legacy-schema adoption and a startup version check are implemented. Provision separate migration/runtime database roles, rehearse an upgrade and failure recovery on staging, and use expand/contract changes for future rolling upgrades. See [Database operations](deploy/DATABASE_OPERATIONS.md). Consider PostgreSQL RLS as defense in depth alongside application filters.
4. **Exercise backup and recovery.** Set database encryption, automated backups, point-in-time recovery, retention and restore drills. Back up the encryption key separately and design key rotation/KMS integration. Choose documented RPO/RTO targets and test them.
5. **Measure capacity and failure behavior.** Load-test the chosen infrastructure, concurrent tenants, slow providers, disconnects, DB outages and process restarts. Review pool sizes, admission limits and memory. Long comparisons run sequentially and lack a persistent job/cancellation system. Graceful shutdown waits up to 30 seconds for generation workers.
6. **Validate model contracts and accounting.** Check live text, image, tools and stream behavior for every supported provider. Verify pricing/capability metadata and residency contracts. Reservations are not billing reconciliation; accurate invoicing needs a separate usage ledger and provider reconciliation.
7. **Complete data lifecycle and access workflows.** Add user/project deletion, retention controls, export scope choices and a documented privacy process. Add granular key scopes/usage attribution and controlled ownership transfer. Confirm database operators' access and logging policy.
8. **Run independent security and accessibility reviews.** Test authorization across every resource, stored/DOM XSS, malformed content, dependency/container advisories, focus traps, keyboard redaction alternatives, contrast, screen readers and reduced motion. Pin release image/action digests and produce an SBOM. No claim of being vulnerability-free is made.

## Prioritized next features

| Priority | Improvement | Why / acceptance criterion |
| --- | --- | --- |
| P0 | Production-role migration rehearsal, restore drills, OIDC staging suite, edge/egress policy | Repeatable deployment with verified isolation and recoverability |
| Delivered | Optimistic versioning for configuration | Routing, models, connections and report triage reject stale revisions; conversation metadata merges transactionally |
| P0 | Reconciled usage ledger and spend alerts | Attribute every attempt to a project/key/provider; distinguish reservation, estimate and settled charge |
| P1 | Provider contract test harness | Exercise live provider fixtures on demand and record compatibility without storing secrets |
| P1 | Evaluation runner and versioned routing policy | Turn saved prompts into repeatable scored suites; compare policy versions before promotion |
| P1 | Background comparison jobs | Progress, cancellation, restart recovery and durable partial results for long comparisons |
| P1 | Credential rotation and scoped keys | Expiring credentials, overlapping rotation windows, per-key permissions, limits and attribution |
| P1 | Search and pagination at scale | Server-side paged conversation/report lists and indexed search instead of bounded full lists |
| P1 | Accessible onboarding and command palette | Guided empty states, keyboard navigation, shortcuts and clearer unsupported-capability feedback |
| P1 | Report annotations and deduplication | Undoable redaction, typed annotations, duplicate grouping and administrator-triggered tracker export |
| P2 | Provider adapters: Azure, Bedrock, Vertex and local agents | Expand integration through a common contract, with explicit auth and residency handling |
| P2 | Prompt versions and reusable workflows | Save reusable prompts, variables, approval rules and reproducible runs |
| P2 | Caching and batch processing | Reduce cost only with tenant-isolated cache keys, explicit retention and freshness controls |
| P2 | Canary routing and circuit breakers | Promote policies using measured evaluations and recover from outages using bounded shared health state |
| P2 | Regional deployments and enterprise controls | Contract-backed residency, SSO provisioning, stronger roles and immutable audit export |
| P2 | Frontend component extraction and browser CI | Split large UI modules, consistent form/error components, visual regression and automated accessibility checks |

The strongest next increment is operational reliability and measurement: establish staging, migrations, recovery and provider contract tests, then build an evaluation runner that proves a routing change improves quality or cost. Additional animation should follow usability and accessibility evidence.

## Deployment notes

Railway is the selected deployment target. [Railway setup](deploy/RAILWAY.md) documents the configuration-as-code file, pre-deploy migrations, dynamic port, PostgreSQL URL support and probe hostname. A real Railway deployment and OIDC login still need staging validation.

Use `.env.saas.example`, `compose.yaml` and the README procedure. Only the HTTPS edge exposes public ports; PostgreSQL and the application are internal compose services. The Docker image runs two ASGI workers as UID 10001. Avoid passing real credentials on a command line or committing environment files.

`python -m llm_router.manage migrate` applies numbered revisions; `init` is a compatibility alias. `status` reports the installed and required revisions. `cleanup` performs retention cleanup manually and never upgrades schemas. See [Database operations](deploy/DATABASE_OPERATIONS.md) for upgrade and recovery instructions. Legacy `llm_router.reports` CLI exports read the old local report store, not SaaS reports; use the authenticated Workspace Reports UI for the new store.
