"""Frozen revision 1 schema. Never edit: add a migration for schema changes."""
from sqlalchemy import Column, Float, Integer, JSON, MetaData, String, Table, Index

metadata = MetaData()
projects = Table("projects", metadata, Column("id", String(64), primary_key=True), Column("name", String(120)), Column("daily_micro", Integer, nullable=False, default=20_000_000))
members = Table("members", metadata, Column("user_id", String(256), primary_key=True), Column("project_id", String(64), primary_key=True), Column("role", String(16)))
sessions = Table("sessions", metadata, Column("id", String(64), primary_key=True), Column("user_id", String(256)), Column("project_id", String(64)), Column("csrf", String(64)), Column("expires", Float), Column("thread_id", String(64)))
documents = Table("documents", metadata, Column("project_id", String(64), primary_key=True), Column("kind", String(32), primary_key=True), Column("id", String(128), primary_key=True), Column("data", JSON), Column("updated", Float))
keys = Table("api_keys", metadata, Column("digest", String(64), primary_key=True), Column("id", String(32)), Column("project_id", String(64)), Column("name", String(100)), Column("expires", Float), Column("created", Float))
runs = Table("runs", metadata, Column("project_id", String(64), primary_key=True), Column("id", String(128), primary_key=True), Column("fingerprint", String(64)), Column("thread_id", String(64)), Column("status", String(24)), Column("created", Float), Column("lease", Float), Column("charge_micro", Integer), Column("result", JSON), Column("telemetry", JSON))
Index("runs_project_created", runs.c.project_id, runs.c.created)
Index("documents_project_kind_updated", documents.c.project_id, documents.c.kind, documents.c.updated)
Index("sessions_expires", sessions.c.expires)
