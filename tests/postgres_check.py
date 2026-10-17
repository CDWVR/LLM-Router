"""Run only against a dedicated test database supplied by ROUTER_TEST_DATABASE_URL."""
import concurrent.futures
import os
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_router.store import Store, ServiceError, digest

url = os.environ["ROUTER_TEST_DATABASE_URL"]
if not url.endswith("/router_test"):
    raise RuntimeError("Use the disposable router_test database")
from sqlalchemy import create_engine
from llm_router.migrations import migrate
migration_engine = create_engine(url)
with concurrent.futures.ThreadPoolExecutor(4) as pool:
    assert list(pool.map(lambda _: migrate(migration_engine), range(4))) == [3]*4
migration_engine.dispose()
store = Store(url)
store.budget(store.new_project("large-"+uuid.uuid4().hex), 10000)
project = store.new_project("test-"+uuid.uuid4().hex)
store.budget(project, 1)
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    assert sum(pool.map(lambda _: store.allow_request("auth:postgres-concurrency", 5), range(20))) == 5, "Shared HTTP rate limit exceeded"
def reserve(index):
    try:
        store.admit(project, str(index), digest(str(index)), .6)
        return True
    except ServiceError:
        return False
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    assert sum(pool.map(reserve, range(8))) == 1, "Concurrent reservations exceeded the budget"
store.put(project, "thread", "persist", {"title":"Survives restart"})
store.put(project, "model", "concurrent-edit", {"enabled": True})
def edit(index):
    try:
        store.put(project, "model", "concurrent-edit", {"winner": index}, expected_revision=0)
        return True
    except ServiceError as exc:
        assert exc.code == "configuration_conflict"
        return False
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    assert sum(pool.map(edit, range(8))) == 1, "Concurrent edits overwrote each other"
# PostgreSQL types SUM(bigint) as NUMERIC. A Decimal micro-dollar total cannot be
# divided by a float, so usage() raised and the whole workspace panel returned
# 500 for every signed-in SaaS user. Assert the boundary returns plain floats.
usage = store.usage(project)
assert isinstance(usage["reserved_24h_usd"], float), type(usage["reserved_24h_usd"])
assert isinstance(usage["daily_limit_usd"], float), type(usage["daily_limit_usd"])
store.engine.dispose()
reopened = Store(url)
assert reopened.get(project, "thread", "persist")["title"] == "Survives restart"
assert reopened.get("different-tenant", "thread", "persist") is None
reopened.engine.dispose()
print("PostgreSQL: concurrent budget admission, restart persistence, and tenant scope passed")
