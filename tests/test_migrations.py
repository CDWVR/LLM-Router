import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import create_engine, inspect
from llm_router.migrations import migrate, check_schema, schema_status, SchemaError
from llm_router.schema_v1 import metadata


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///'+str(Path(self.folder.name)/'schema.db'), connect_args={'timeout': 10})

    def tearDown(self):
        self.engine.dispose()
        self.folder.cleanup()

    def test_fresh_database_and_repeat(self):
        with self.assertRaises(SchemaError):
            check_schema(self.engine)
        migrate(self.engine)
        migrate(self.engine)
        check_schema(self.engine)
        self.assertEqual(schema_status(self.engine), {'current': 2, 'required': 2})

    def test_adopt_legacy_preserves_data(self):
        metadata.create_all(self.engine)
        with self.engine.begin() as db:
            db.exec_driver_sql("INSERT INTO projects (id, name, daily_micro) VALUES ('legacy', 'Keep me', 1000000)")
        migrate(self.engine)
        with self.engine.connect() as db:
            self.assertEqual(db.exec_driver_sql("SELECT name FROM projects WHERE id='legacy'").scalar(), 'Keep me')

    def test_reject_partial_legacy(self):
        with self.engine.begin() as db:
            db.exec_driver_sql('CREATE TABLE projects (id TEXT PRIMARY KEY)')
        with self.assertRaises(SchemaError):
            migrate(self.engine)
        self.assertNotIn('router_schema', inspect(self.engine).get_table_names())

    def test_reject_future_revision(self):
        migrate(self.engine)
        with self.engine.begin() as db:
            db.exec_driver_sql('UPDATE router_schema SET revision=999')
        with self.assertRaises(SchemaError):
            migrate(self.engine)
        with self.assertRaises(SchemaError):
            check_schema(self.engine)

    def test_failure_rolls_back_schema_and_version(self):
        def fail(db):
            db.exec_driver_sql('CREATE TABLE should_rollback (id INTEGER)')
            raise RuntimeError('injected failure')
        with patch.dict('llm_router.migrations.REVISIONS', {2: fail}):
            with self.assertRaises(RuntimeError):
                migrate(self.engine)
        self.assertEqual(inspect(self.engine).get_table_names(), [])
        migrate(self.engine)
        check_schema(self.engine)

    def test_concurrent_migrators(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            self.assertEqual(list(pool.map(lambda _: migrate(self.engine), range(4))), [2]*4)

    def test_current_version_still_checks_structure(self):
        migrate(self.engine)
        with self.engine.begin() as db:
            db.exec_driver_sql('DROP TABLE sessions')
        with self.assertRaises(SchemaError):
            check_schema(self.engine)
