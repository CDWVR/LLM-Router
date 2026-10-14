from contextlib import closing
import base64
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from llm_router import reports
import test_integrations as fixtures


class ReportStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "reports.db"

    def save(self, **kwargs):
        return reports.save_report({"description": "The send button is stuck", **kwargs}, "owner", self.path)

    def test_screenshot_roundtrip_and_description_only(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
        report_id = self.save(screenshot=base64.b64encode(buffer.getvalue()).decode())
        self.save()
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT screenshot FROM reports WHERE id=?", (report_id,)).fetchone()
            with Image.open(io.BytesIO(row[0])) as image:
                self.assertEqual(image.size, (8, 8))
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 2)

    def test_rejects_bad_description_and_image(self):
        for body in [{"description": "short"}, {"screenshot": "not base64"}, {"screenshot": base64.b64encode(b"not png").decode()}]:
            with self.assertRaises(reports.ReportError):
                self.save(**body)

    def test_rate_limit_and_inbox_cap(self):
        for _ in range(5):
            self.save()
        with self.assertRaisesRegex(reports.ReportError, "report_rate_limited"):
            self.save()
        with patch.object(reports, "MAX_REPORTS", 5):
            with self.assertRaisesRegex(reports.ReportError, "report_inbox_full"):
                reports.save_report({"description": "Another report here"}, "different", self.path)

    def test_old_reports_are_removed_on_next_submission(self):
        self.save()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE reports SET created=0")
        self.save()
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 1)


class ReportHTTPTests(unittest.TestCase):
    # Reuse the HTTP fixture without inheriting and rerunning its test cases.
    setUp = fixtures.HTTPBoundaryTests.setUp
    cleanup = fixtures.HTTPBoundaryTests.cleanup
    request = fixtures.HTTPBoundaryTests.request
    session = fixtures.HTTPBoundaryTests.session

    def test_expired_session_cannot_read_state(self):
        from llm_router import web
        headers = self.session()
        token = headers["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        web._sessions[token]["last_seen"] = 0
        self.assertEqual(self.request("/state", headers=headers)[0], 403)

    def test_unlock_limit_is_shared_across_new_sessions(self):
        from llm_router import web
        with patch.object(web, "_unlock_attempts", {}), patch.dict("os.environ", {"ROUTER_PASSWORD": "secret"}):
            for _ in range(10):
                self.assertEqual(self.request("/unlock", {"password": "wrong"}, self.session())[0], 403)
            self.assertEqual(self.request("/unlock", {"password": "wrong"}, self.session())[0], 429)

    def test_report_requires_csrf_and_unlock(self):
        body = {"description": "Cannot send a message"}
        self.assertEqual(self.request("/reports", body)[0], 403)
        headers = self.session()
        with patch.dict("os.environ", {"ROUTER_PASSWORD": "secret"}):
            self.assertEqual(self.request("/reports", body, headers)[0], 403)
            status, payload, _ = self.request("/state", headers=headers)
            self.assertIn(b'"locked": true', payload)
            self.assertNotIn(b'"providers"', payload)

    def test_report_saves_while_generation_is_busy(self):
        from llm_router import web
        headers = self.session()
        token = headers["Cookie"].split("router_session=", 1)[1].split(";", 1)[0]
        state = web._sessions[token]
        state["lock"].acquire()
        try:
            with patch.object(reports, "REPORT_DB", Path(self.folder.name) / "reports.db"):
                status, payload, response_headers = self.request("/reports", {"description": "Generation is stuck here"}, headers)
            self.assertEqual(status, 201)
            self.assertIn(b"BR-", payload)
            self.assertEqual(response_headers["X-Frame-Options"], "DENY")
        finally:
            state["lock"].release()
