# Database upgrades and recovery

The application uses forward-only numbered migrations in `llm_router/migrations.py`, with a frozen initial schema in `llm_router/schema_v1.py`. Revision 1 creates or adopts the recognizable legacy schema and its indexes. Revision 2 widens PostgreSQL micro-dollar accounting columns to BIGINT. SQLite already stores 64-bit integers. Revision 3 adds shared request-rate buckets.

## Deployment workflow

1. Take and verify a database backup and preserve `ROUTER_SECRET_KEY` separately. Capture the image/version used before the change.
2. Stop or drain application writers for the first adoption migration and any migration requiring exclusive table locks. Review future migrations for compatibility before rolling upgrades.
3. Run `python -m llm_router.manage status` with the target `DATABASE_URL`. A fresh or legacy database reports revision 0.
4. Run `python -m llm_router.manage migrate` using the migration role. PostgreSQL migration runners serialize with a transaction-scoped advisory lock; lock acquisition is bounded to 30 seconds and statements to 120 seconds. SQLite uses BEGIN IMMEDIATE. A failed migration rolls back both DDL and the version record.
5. Run `python -m llm_router.manage status` again, start the application and check `/readyz`. SaaS workers require the exact supported revision and recognized table structure; they do not perform DDL at startup.

`init` remains an alias for `migrate`. Local SQLite development automatically migrates at startup. An unknown, incomplete or newer schema is rejected. There is no automatic destructive downgrade. Never edit an applied revision: add the next numbered migration and its upgrade, failure and data-preservation tests.

Compose runs a separate, one-shot `migrate` service and starts application workers only after it succeeds. The sample uses the same database role for both for ease of setup. Before public deployment, provision separate database owner/migration/runtime roles. The runtime needs SELECT/INSERT/UPDATE/DELETE on application tables and SELECT on `router_schema`, not CREATE/ALTER/DROP. Migration credentials should not be mounted into application workers. PostgreSQL RLS remains a separate future defense-in-depth task.

On subsequent upgrades, build the new image, run the migration job explicitly (`docker compose --env-file .env.saas run --rm migrate`), and restart/recreate application services after it succeeds. Do not infer success from an older completed migration container.

## Backup and restore runbook

Use PostgreSQL's native backup tooling or your managed database provider's backup service. Set encryption, access controls, scheduled backups and point-in-time recovery according to your recovery objectives. Avoid credentials in command arguments; use a restricted password file or the deployment secret mechanism.

An operator-controlled logical backup can use `pg_dump --format=custom --file=router.dump DATABASE_NAME`. Restore into a **new, isolated database**, using `pg_restore --exit-on-error --dbname=RESTORE_DATABASE router.dump`. These examples intentionally omit credentials and host selection: verify the destination and privileges before running them. Do not restore over the live database as a routine test.

For each restore drill:

- Record the source revision, database size, backup timestamp and application image.
- Verify `router_schema`, representative projects, memberships, conversations and usage totals in the isolated restore.
- Run the same application image against that restore with outbound provider access blocked. Check a representative credential can be decrypted using the backed-up key without logging it or calling a provider.
- Exercise authentication in an isolated identity-client configuration and check tenant authorization.
- Measure restore time and lost-write window against chosen RTO/RPO. Document the result and remove the isolated test resources.

The development verification performed a logical dump/restore of disposable PostgreSQL test data. It does not establish production RTO/RPO, verify production key recovery or replace managed backup/point-in-time-recovery drills.

If an upgrade fails, keep writers stopped, inspect the migration error and confirm the transaction rolled back. A database at an older version requires the matching application image or a repaired forward migration. If a committed migration is incompatible with the old image, restore the verified pre-upgrade backup into a separate database and explicitly switch over after validating it; account for writes since that backup. Never stamp the version backward to bypass the startup check.
