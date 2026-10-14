"""Tenant-scoped persistence and admission control (PostgreSQL or local SQLite)."""
import hashlib
import json
import secrets
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (Column, Float, Integer, BigInteger, JSON, MetaData, String, Table, Text, Index,
                        create_engine, delete, func, insert, select, update)

metadata = MetaData()
projects = Table("projects", metadata, Column("id", String(64), primary_key=True), Column("name", String(120)), Column("daily_micro", BigInteger, nullable=False, default=20_000_000))
members = Table("members", metadata, Column("user_id", String(256), primary_key=True), Column("project_id", String(64), primary_key=True), Column("role", String(16)))
sessions = Table("sessions", metadata, Column("id", String(64), primary_key=True), Column("user_id", String(256)), Column("project_id", String(64)), Column("csrf", String(64)), Column("expires", Float), Column("thread_id", String(64)))
documents = Table("documents", metadata, Column("project_id", String(64), primary_key=True), Column("kind", String(32), primary_key=True), Column("id", String(128), primary_key=True), Column("data", JSON), Column("updated", Float))
keys = Table("api_keys", metadata, Column("digest", String(64), primary_key=True), Column("id", String(32)), Column("project_id", String(64)), Column("name", String(100)), Column("expires", Float), Column("created", Float))
runs = Table("runs", metadata, Column("project_id", String(64), primary_key=True), Column("id", String(128), primary_key=True), Column("fingerprint", String(64)), Column("thread_id", String(64)), Column("status", String(24)), Column("created", Float), Column("lease", Float), Column("charge_micro", BigInteger), Column("result", JSON), Column("telemetry", JSON))
Index("runs_project_created", runs.c.project_id, runs.c.created)
Index("documents_project_kind_updated", documents.c.project_id, documents.c.kind, documents.c.updated)
Index("sessions_expires", sessions.c.expires)


class ServiceError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, url, auto_migrate=None):
        self.engine = create_engine(url, pool_pre_ping=True, **({"connect_args": {"check_same_thread": False, "timeout": 10}} if url.startswith("sqlite") else {"pool_size": 5, "max_overflow": 5}))
        from .migrations import migrate, check_schema
        try:
            should_migrate = auto_migrate if auto_migrate is not None else self.engine.dialect.name == "sqlite"
            if should_migrate:
                migrate(self.engine)
            check_schema(self.engine)
        except BaseException:
            self.engine.dispose()
            raise

    @contextmanager
    def transaction(self):
        with self.engine.connect() as connection:
            try:
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def allow_request(self, identifier, limit, period=60):
        """Shared fixed-window counters with bounded storage and atomic increments."""
        from .migrations import rate_buckets
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        window = int(time.time() // period)
        with self.transaction() as db:
            # A fixed number of buckets bounds unauthenticated cardinality. Hash
            # collisions conservatively share a quota; they never widen one.
            bucket = digest(identifier.split(":", 1)[0])[:8]+digest(identifier)[:4]
            statement = (pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert)(rate_buckets).values(id=bucket, window=window, count=1)
            from sqlalchemy import case, or_
            statement = statement.on_conflict_do_update(index_elements=[rate_buckets.c.id],
                set_={"window": window, "count": case((rate_buckets.c.window != window, 1), else_=rate_buckets.c.count+1)},
                where=or_(rate_buckets.c.window != window, rate_buckets.c.count < limit)).returning(rate_buckets.c.id)
            return db.execute(statement).first() is not None

    def new_project(self, user, name="My workspace", project_id=None):
        if len(self.memberships(user)) >= 10:
            raise ServiceError("workspace_limit_reached", 409)
        pid = project_id or uuid.uuid4().hex
        with self.transaction() as db:
            if not db.execute(select(projects).where(projects.c.id == pid)).first():
                db.execute(insert(projects).values(id=pid, name=name, daily_micro=20_000_000))
                db.execute(insert(members).values(user_id=user, project_id=pid, role="owner"))
        return pid

    def memberships(self, user):
        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(select(projects, members.c.role).join(members, members.c.project_id == projects.c.id).where(members.c.user_id == user)).mappings()]

    def role(self, user, project):
        with self.engine.connect() as db:
            return db.execute(select(members.c.role).where(members.c.user_id == user, members.c.project_id == project)).scalar()

    def invite(self, project):
        token = secrets.token_urlsafe(32)
        self.put(project, "invite", digest(token), {"expires": time.time()+86400}, 30)
        return project+"."+token

    def join(self, user, invitation):
        try:
            project, token = invitation.split(".", 1)
        except ValueError:
            raise ServiceError("invalid_invitation") from None
        with self.transaction() as db:
            condition = (documents.c.project_id == project) & (documents.c.kind == "invite") & (documents.c.id == digest(token))
            row = db.execute(select(documents).where(condition).with_for_update()).mappings().first()
            if not row or row["data"]["expires"] < time.time():
                raise ServiceError("invalid_invitation")
            if not db.execute(select(members).where(members.c.project_id == project, members.c.user_id == user)).first():
                db.execute(insert(members).values(project_id=project, user_id=user, role="member"))
            db.execute(delete(documents).where(condition))
        return project

    def member_list(self, project):
        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(select(members.c.user_id, members.c.role).where(members.c.project_id == project)).mappings()]

    def remove_member(self, project, user):
        with self.transaction() as db:
            db.execute(delete(members).where(members.c.project_id == project, members.c.user_id == user, members.c.role != "owner"))
            db.execute(delete(sessions).where(sessions.c.project_id == project, sessions.c.user_id == user))

    def create_session(self, user, project):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute(insert(sessions).values(id=digest(token), user_id=user, project_id=project, csrf=csrf, expires=time.time()+86400, thread_id=""))
        return token

    def session(self, token):
        if not token:
            return None
        with self.engine.connect() as db:
            row = db.execute(select(sessions).where(sessions.c.id == digest(token), sessions.c.expires > time.time())).mappings().first()
        if row is None or self.role(row["user_id"], row["project_id"]) is None:
            return None
        return dict(row)

    def set_session(self, session, **values):
        with self.transaction() as db:
            db.execute(update(sessions).where(sessions.c.id == session["id"]).values(**values))

    def logout(self, session):
        with self.transaction() as db:
            db.execute(delete(sessions).where(sessions.c.id == session["id"]))

    def get(self, project, kind, key):
        with self.engine.connect() as db:
            return db.execute(select(documents.c.data).where(documents.c.project_id == project, documents.c.kind == kind, documents.c.id == key)).scalar()

    def list(self, project, kind, limit=250):
        with self.engine.connect() as db:
            return [{"id": row.id, **row.data, "updated": row.updated} for row in db.execute(select(documents).where(documents.c.project_id == project, documents.c.kind == kind).order_by(documents.c.updated.desc()).limit(limit))]

    def put(self, project, kind, key, value, limit=250, merge=False, preserve=()):
        if len(json.dumps(value)) > 4_000_000:
            raise ServiceError("document_too_large", 413)
        with self.transaction() as db:
            # Serialize cardinality checks per project on PostgreSQL as well as SQLite.
            db.execute(select(projects.c.id).where(projects.c.id == project).with_for_update()).first()
            condition = (documents.c.project_id == project) & (documents.c.kind == kind) & (documents.c.id == key)
            existing = db.execute(select(documents.c.data).where(condition)).first()
            if existing:
                if merge:
                    value = {**existing[0], **value}
                value = {**value, **{name: existing[0][name] for name in preserve if name in existing[0]}}
                db.execute(update(documents).where(condition).values(data=value, updated=time.time()))
            else:
                count = db.execute(select(func.count()).select_from(documents).where(documents.c.project_id == project, documents.c.kind == kind)).scalar()
                if count >= limit:
                    raise ServiceError("workspace_capacity_reached", 409)
                db.execute(insert(documents).values(project_id=project, kind=kind, id=key, data=value, updated=time.time()))

    def submit_report(self, project, identifier, value):
        now = time.time()
        with self.transaction() as db:
            db.execute(select(projects.c.id).where(projects.c.id == project).with_for_update()).first()
            rows = list(db.execute(select(documents.c.data, documents.c.updated).where(documents.c.project_id == project, documents.c.kind == "report")).mappings())
            if sum(row["data"].get("created", row["updated"]) > now-3600 for row in rows) >= 5:
                raise ServiceError("report_rate_limited", 429)
            if len(rows) >= 250:
                raise ServiceError("workspace_capacity_reached", 409)
            db.execute(insert(documents).values(project_id=project, kind="report", id=identifier, data={**value, "created": now}, updated=now))

    def remove(self, project, kind, key):
        with self.transaction() as db:
            db.execute(delete(documents).where(documents.c.project_id == project, documents.c.kind == kind, documents.c.id == key))

    def create_key(self, project, name):
        token = "lr_" + secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute(select(projects.c.id).where(projects.c.id == project).with_for_update()).first()
            count = db.execute(select(func.count()).select_from(keys).where(keys.c.project_id == project, keys.c.expires > time.time())).scalar()
            if count >= 20:
                raise ServiceError("too_many_keys", 409)
            db.execute(insert(keys).values(digest=digest(token), id=uuid.uuid4().hex[:16], project_id=project, name=name, created=time.time(), expires=time.time()+90*86400))
        return token

    def key_project(self, token):
        with self.engine.connect() as db:
            return db.execute(select(keys.c.project_id).where(keys.c.digest == digest(token), keys.c.expires > time.time())).scalar()

    def list_keys(self, project):
        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(select(keys.c.id, keys.c.name, keys.c.created, keys.c.expires).where(keys.c.project_id == project, keys.c.expires > time.time())).mappings()]

    def revoke_key(self, project, key):
        with self.transaction() as db:
            db.execute(delete(keys).where(keys.c.project_id == project, keys.c.id == key))

    def admit(self, project, request_id, fingerprint, ceiling, thread_id=None):
        now = time.time()
        with self.transaction() as db:
            workspace = db.execute(select(projects).where(projects.c.id == project).with_for_update()).mappings().one()
            existing = db.execute(select(runs).where(runs.c.project_id == project, runs.c.id == request_id)).mappings().first()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ServiceError("idempotency_conflict", 409)
                if existing["status"] == "running":
                    raise ServiceError("request_in_progress", 409)
                return dict(existing)
            scope = (runs.c.project_id == project) & (runs.c.created >= now-86400)
            active = list(db.execute(select(runs.c.thread_id).where(scope, runs.c.status == "running", runs.c.lease > now)).scalars())
            if len(active) >= 3 or thread_id and thread_id in active:
                raise ServiceError("router_busy", 429)
            recent_count = db.execute(select(func.count()).select_from(runs).where(scope, runs.c.created >= now-60)).scalar()
            if recent_count >= 30:
                raise ServiceError("rate_limited", 429)
            reserved = int(round(ceiling * 1_000_000))
            spent = db.execute(select(func.coalesce(func.sum(runs.c.charge_micro), 0)).where(scope)).scalar()
            if reserved + spent > workspace["daily_micro"]:
                raise ServiceError("project_budget_exceeded", 429)
            db.execute(insert(runs).values(project_id=project, id=request_id, fingerprint=fingerprint, thread_id=thread_id, status="running", created=now, lease=now+210, charge_micro=reserved, result=None, telemetry={}))
        return None

    def finish(self, project, request_id, result, telemetry):
        with self.transaction() as db:
            row = db.execute(select(runs).where(runs.c.project_id == project, runs.c.id == request_id).with_for_update()).mappings().one()
            # Missing accounting information retains the original reservation.
            charge = min(row["charge_micro"], max(0, int(round(telemetry.get("reserved_usd", row["charge_micro"]/1e6)*1e6))))
            db.execute(update(runs).where(runs.c.project_id == project, runs.c.id == request_id).values(status=telemetry.get("status", "failed"), charge_micro=charge, result=result, telemetry=telemetry, lease=0))

    def usage(self, project):
        with self.engine.connect() as db:
            rows = [dict(row) for row in db.execute(select(runs).where(runs.c.project_id == project).order_by(runs.c.created.desc()).limit(500)).mappings()]
            ceiling = db.execute(select(projects.c.daily_micro).where(projects.c.id == project)).scalar()
            reserved = db.execute(select(func.coalesce(func.sum(runs.c.charge_micro), 0)).where(runs.c.project_id == project, runs.c.created > time.time()-86400)).scalar()
        return {"daily_limit_usd": ceiling/1e6, "reserved_24h_usd": reserved/1e6, "requests": [{k:v for k,v in r.items() if k not in {"result", "fingerprint", "project_id"}} for r in rows]}

    def save_routing(self, project, settings, daily_limit, expected_revision):
        """Commit policy and budget together, rejecting stale administrator edits."""
        with self.transaction() as db:
            db.execute(select(projects.c.id).where(projects.c.id == project).with_for_update()).one()
            condition = (documents.c.project_id == project) & (documents.c.kind == "settings") & (documents.c.id == "routing")
            old = db.execute(select(documents.c.data).where(condition)).scalar()
            if (old or {}).get("revision", 0) != expected_revision:
                raise ServiceError("configuration_conflict", 409)
            value = {**settings, "revision": expected_revision+1}
            if old is None:
                db.execute(insert(documents).values(project_id=project, kind="settings", id="routing", data=value, updated=time.time()))
            else:
                db.execute(update(documents).where(condition).values(data=value, updated=time.time()))
            db.execute(update(projects).where(projects.c.id == project).values(daily_micro=int(round(daily_limit*1e6))))

    def budget(self, project, amount):
        with self.transaction() as db:
            db.execute(update(projects).where(projects.c.id == project).values(daily_micro=int(amount*1e6)))

    def cleanup(self):
        with self.transaction() as db:
            db.execute(delete(sessions).where(sessions.c.expires < time.time()))
            db.execute(delete(keys).where(keys.c.expires < time.time()))
            db.execute(update(runs).where(runs.c.status == "running", runs.c.lease < time.time()).values(status="interrupted"))
            db.execute(delete(runs).where(runs.c.created < time.time()-90*86400))
            db.execute(delete(documents).where(documents.c.kind.in_(["report", "attachment"]), documents.c.updated < time.time()-30*86400))
            db.execute(delete(documents).where(documents.c.kind.in_(["invite", "cancel"]), documents.c.updated < time.time()-86400))
            db.execute(delete(documents).where(documents.c.kind == "audit", documents.c.updated < time.time()-90*86400))
