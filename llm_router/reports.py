"""Local, bounded report inbox. No external submission or public download route."""
from contextlib import closing
import base64
import binascii
import io
import sqlite3
import time
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

REPORT_DB = Path("data/reports.sqlite3")
MAX_REPORTS = 250
RETENTION_SECONDS = 30 * 86400


class ReportError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def save_report(body, owner, path=None):
    description = body.get("description")
    if not isinstance(description, str) or not 10 <= len(description.strip()) <= 4000:
        raise ReportError("description_required")
    screenshot = body.get("screenshot")
    image = None
    if screenshot is not None:
        if not isinstance(screenshot, str) or len(screenshot) > 3_000_000:
            raise ReportError("invalid_screenshot")
        try:
            raw = base64.b64decode(screenshot, validate=True)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "PNG" or source.width * source.height > 4_000_000:
                    raise ValueError
                source.load()
                output = io.BytesIO()
                # Re-encode pixels only; do not retain supplied metadata or trailing data.
                source.convert("RGB").save(output, format="PNG")
                image = output.getvalue()
                if len(image) > 2_000_000:
                    raise ValueError
        except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError):
            raise ReportError("invalid_screenshot") from None
    path = Path(path or REPORT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    report_id = "BR-" + uuid.uuid4().hex
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("PRAGMA secure_delete = ON")
        db.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL, description TEXT NOT NULL, screenshot BLOB)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM reports WHERE created < ?", (now - RETENTION_SECONDS,))
        if db.execute("SELECT count(*) FROM reports WHERE owner = ? AND created > ?", (owner, now - 3600)).fetchone()[0] >= 5:
            raise ReportError("report_rate_limited", 429)
        if db.execute("SELECT count(*) FROM reports WHERE created > ?", (now - 60,)).fetchone()[0] >= 20:
            raise ReportError("report_rate_limited", 429)
        if db.execute("SELECT count(*) FROM reports").fetchone()[0] >= MAX_REPORTS:
            raise ReportError("report_inbox_full", 503)
        db.execute("INSERT INTO reports VALUES (?, ?, ?, ?, ?)", (report_id, owner, now, description.strip(), image))
    return report_id


def main():
    """Local operator access only; report content is never served over HTTP."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Inspect the local Router bug-report inbox")
    parser.add_argument("command", choices=["list", "export"])
    parser.add_argument("report_id", nargs="?")
    parser.add_argument("--output", default="report-exports")
    args = parser.parse_args()
    if not REPORT_DB.exists():
        parser.exit(message="No reports have been saved.\n")
    with closing(sqlite3.connect(REPORT_DB)) as db:
        if args.command == "list":
            for row in db.execute("SELECT id, created FROM reports ORDER BY created DESC LIMIT 250"):
                print(row[0], time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row[1])))
            return
        if not args.report_id:
            parser.error("export requires a report ID")
        row = db.execute("SELECT id, created, description, screenshot FROM reports WHERE id=?", (args.report_id,)).fetchone()
    if row is None:
        parser.exit(1, "Report not found.\n")
    output = Path(args.output) / row[0]
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps({"id": row[0], "created": row[1], "description": row[2]}, indent=2), encoding="utf-8")
    if row[3]:
        (output / "screenshot.png").write_bytes(row[3])
    print(output.resolve())


if __name__ == "__main__":
    main()
