"""Two-thread race proof for the slot UNIQUE constraint.

Run with:
    python -m scripts.race_demo
"""
from __future__ import annotations

import os
import tempfile
import threading

from app.lab_db import LabDb
from app.tools.lab_tools import BookingTools


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="lab-race-demo-")
    path = os.path.join(tmp, "lab.db")
    seed = LabDb(path)
    seed.migrate()

    barrier = threading.Barrier(2)
    results: list[dict] = []
    lock = threading.Lock()

    def attempt(roll_no: str) -> None:
        db = LabDb(path)
        barrier.wait()
        result = BookingTools(db, roll_no).book_slot(2, "2026-09-20 16:00")
        with lock:
            results.append(result)

    threads = [
        threading.Thread(target=attempt, args=("22CS045",)),
        threading.Thread(target=attempt, args=("22IT017",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [r.get("status") for r in results]
    errors = [r.get("error") for r in results]
    assert statuses.count("booked") == 1
    assert errors.count("slot_taken") == 1
    print(results)
    print("PASS: two concurrent students produced one winner and one slot_taken")


if __name__ == "__main__":
    main()
