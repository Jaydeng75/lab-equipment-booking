"""Run the core store flows against Supabase Postgres, when a DSN is available.

Skipped by default. Enable with:
    SUPABASE_DB_URL="postgresql://..." python -m pytest -q
The demo/crash/race scripts are the same proof path; this pins the storage semantics
(queue lease, idempotent side effect, unique-slot clash) on the Postgres backend too.
"""
import os
import time

import pytest

from app.lab_db import LabDb
from app.memory import RunStore
from app.tools.lab_tools import BookingTools

DSN = (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_TEST_DB_URL") or "").strip()


@pytest.mark.skipif(not DSN, reason="SUPABASE_DB_URL not set")
def test_postgres_run_booking_and_race():
    dsn = DSN
    store = RunStore(dsn)
    db = LabDb(dsn)
    store.migrate()
    db.migrate()

    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "hello", "mock")
    claimed = store.claim_next("w", 60)
    assert claimed is not None and claimed.run_id == rid and claimed.attempts == 1
    assert store.complete(rid, "w", "done")
    assert store.get_run(rid)["status"] == "succeeded"

    slot = f"2026-09-20 17:{int(time.time()) % 60:02d}"
    db.conn.cursor().execute("DELETE FROM booking WHERE equipment_id=? AND slot=?", (2, slot))
    t = BookingTools(db, "22CS045")
    assert t.book_slot(2, slot)["status"] == "booked"
    assert t.book_slot(2, slot)["status"] == "already_booked"
    clash = BookingTools(db, "22IT017").book_slot(2, slot)
    assert clash["error"] == "slot_taken"

    same = BookingTools(db, "22CS045")
    a = same.notify_student(f"Postgres test {slot}.")
    b = same.notify_student(f"Postgres test {slot}.")
    assert a["notification_id"] == b["notification_id"] and b["duplicate"]