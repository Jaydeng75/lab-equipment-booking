"""Apply (or refresh) the schema + seed data in a Supabase Postgres project.

Usage:
    python -m scripts.apply_supabase "postgresql://postgres:...@db.xxx.supabase.co:5432/postgres"

The URL can also be supplied via `SUPABASE_DB_URL` so it is not pasted on the command line:
    SUPABASE_DB_URL="..." python -m scripts.apply_supabase

The apply is a clean rebuild: existing tables are dropped first, so re-running is safe.
"""
from __future__ import annotations

import os
import sys

from app.lab_db import LabDb
from app.memory import RunStore

TABLES = [
    "thread", "message", "run_step", "tool_call", "run",
    "student", "equipment", "training", "policy", "booking", "notification", "idempotency",
]


def _reset(conn) -> None:
    for t in TABLES:
        try:
            conn.cursor().execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    dsn = args[0] if args else os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("Usage: python -m scripts.apply_supabase <postgresql://...>  (or set SUPABASE_DB_URL)")
        return 2
    if not (dsn.startswith("postgresql://") or dsn.startswith("postgres://")):
        print("That does not look like a Supabase connection string ('postgresql://...').")
        return 2

    store = RunStore(dsn)
    db = LabDb(dsn)
    try:
        _reset(store.conn)
        _reset(db.conn)
        store.migrate()
        db.migrate()
    except Exception as e:  # noqa: BLE001
        print(f"Migration failed: {e}")
        return 1

    print("Schema applied and seeded.")
    print("  lab.db tables:")
    for t in ("student", "equipment", "training", "policy", "booking", "notification", "idempotency"):
        print(f"    {t}: {db.count(t)} rows")
    print("  agent.db tables:")
    for t in ("thread", "message", "run", "run_step", "tool_call"):
        print(f"    {t}: {store.count(t)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())