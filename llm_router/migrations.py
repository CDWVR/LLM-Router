"""Forward-only, transactional schema revisions for SQLite and PostgreSQL.

Runtime workers only check the version in SaaS. Operators run migrate separately.
Revision functions and the v1 snapshot are immutable once released.
"""
from sqlalchemy import Column, Integer, String, MetaData, Table, inspect, select, insert, update
from .schema_v1 import metadata as baseline

LATEST = 3
version_metadata = MetaData()
versions = Table("router_schema", version_metadata,
                 Column("id", Integer, primary_key=True),
                 Column("revision", Integer, nullable=False))


class SchemaError(RuntimeError):
    pass


def _version(connection):
    if not inspect(connection).has_table("router_schema"):
        return 0
    rows = list(connection.execute(select(versions)))
    if len(rows) != 1 or rows[0].id != 1:
        raise SchemaError("Invalid schema version record; restore or investigate the database")
    return rows[0].revision


def _validate_baseline(connection):
    inspector = inspect(connection)
    for name, table in baseline.tables.items():
        if not inspector.has_table(name):
            raise SchemaError("Incomplete schema; missing table: "+name)
        actual = {c["name"] for c in inspector.get_columns(name)}
        if actual != set(table.c.keys()):
            raise SchemaError("Unrecognized schema columns in "+name)
        if set(inspector.get_pk_constraint(name)["constrained_columns"]) != {c.name for c in table.primary_key}:
            raise SchemaError("Unrecognized primary key in "+name)


def _revision_one(connection):
    present = set(inspect(connection).get_table_names()) - {"router_schema"}
    if present:
        # Adopt only the recognizable pre-migration schema. Never silently stamp
        # an unrelated or partially-created database as current.
        if present != set(baseline.tables):
            raise SchemaError("Unrecognized unversioned database; use a dedicated Router database")
        _validate_baseline(connection)
    else:
        baseline.create_all(connection)
    for table in baseline.tables.values():
        for index in table.indexes:
            index.create(connection, checkfirst=True)


def _revision_two(connection):
    # PostgreSQL INTEGER overflows at about $2,147 in micro-dollar accounting.
    # SQLite INTEGER already stores signed 64-bit values.
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("ALTER TABLE projects ALTER COLUMN daily_micro TYPE BIGINT")
        connection.exec_driver_sql("ALTER TABLE runs ALTER COLUMN charge_micro TYPE BIGINT")


rate_metadata = MetaData()
rate_buckets = Table("rate_buckets", rate_metadata,
                     Column("id", String(64), primary_key=True),
                     Column("window", Integer, nullable=False),
                     Column("count", Integer, nullable=False))


def _revision_three(connection):
    rate_metadata.create_all(connection)


REVISIONS = {1: _revision_one, 2: _revision_two, 3: _revision_three}


def migrate(engine):
    with engine.connect() as connection:
        try:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            elif engine.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '30s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '120s'")
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(746392018241)")
            else:
                raise SchemaError("Only SQLite and PostgreSQL are supported")
            current = _version(connection)
            if current < 0 or current > LATEST:
                raise SchemaError("Database version is newer than this application; do not downgrade")
            for revision in range(current+1, LATEST+1):
                REVISIONS[revision](connection)
                if revision == 1:
                    version_metadata.create_all(connection)
                    connection.execute(insert(versions).values(id=1, revision=revision))
                else:
                    connection.execute(update(versions).where(versions.c.id == 1).values(revision=revision))
            _validate_baseline(connection)
            if LATEST >= 3:
                _validate_rate_buckets(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return LATEST


def schema_status(engine):
    with engine.connect() as connection:
        return {"current": _version(connection), "required": LATEST}


def check_schema(engine):
    with engine.connect() as connection:
        if _version(connection) != LATEST:
            raise SchemaError("Database migration required: python -m llm_router.manage migrate")
        _validate_baseline(connection)
        _validate_rate_buckets(connection)


def _validate_rate_buckets(connection):
    inspector = inspect(connection)
    if not inspector.has_table("rate_buckets") or {column["name"] for column in inspector.get_columns("rate_buckets")} != {"id", "window", "count"}:
        raise SchemaError("Missing or incompatible request-rate schema")
