"""Explicit migrations, version checks and retention maintenance."""
import argparse
import json
import os
from pathlib import Path
from sqlalchemy import create_engine
from dotenv import load_dotenv
from .migrations import migrate, schema_status
from .store import Store
from .configuration import database_url


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "migrate", "status", "cleanup"])
    args = parser.parse_args()
    url = database_url(os.environ.get("DATABASE_URL", "sqlite:///data/workspace.sqlite3"))
    if "DATABASE_URL" not in os.environ:
        Path("data").mkdir(exist_ok=True)
    if args.command == "cleanup":
        store = Store(url, auto_migrate=False)
        try:
            store.cleanup()
        finally:
            store.engine.dispose()
        print("Retention maintenance complete")
        return
    engine = create_engine(url)
    try:
        if args.command in {"init", "migrate"}:
            migrate(engine)
        print(json.dumps(schema_status(engine)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
