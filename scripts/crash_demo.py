"""Crash-and-replay proof for the weekend project.

Runs entirely with scripted models and temporary SQLite files. It deliberately
simulates worker-A dying after the booking specialist has committed its side
effects but before the supervisor's delegation result is recorded in agent.db.
After the lease expires, worker-B picks up the same run. Stable parent/child
idempotency keys make the booking and notification replay instead of duplicate.

Run with:
    python -m scripts.crash_demo
"""
from __future__ import annotations

import os
import tempfile

from app.agents import SUPERVISOR_SYSTEM, SupervisorTools, run_tool
from app.idempotency import idempotency_key
from app.lab_db import LabDb
from app.memory import RunStore
from app.providers import demo_providers
from app.worker import Worker
from scripts._term import CYAN, DIM, GREEN, RESET, print_step


class ManualClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="lab-crash-demo-")
    clock = ManualClock()
    store = RunStore(os.path.join(tmp, "agent.db"), clock=clock)
    db = LabDb(os.path.join(tmp, "lab.db"), clock=clock)
    store.migrate()
    db.migrate()

    question = "Book equipment 2 for 2026-09-19 14:00 and send me a confirmation."
    thread_id = store.create_thread("22CS045")
    run_id = store.enqueue(thread_id, question, "mock")
    providers_a = demo_providers()

    print(f"{CYAN}queued run {run_id[:8]}{RESET}")
    claimed = store.claim_next("worker-A", lease_seconds=1.0)
    assert claimed is not None and claimed.run_id == run_id

    # Reproduce the first supervisor step exactly as execute_run would.
    history = [{"role": m["role"], "text": m["text"]} for m in store.load_history(thread_id)]
    supervisor_tools = SupervisorTools(db, providers_a, "22CS045", on_step=print_step)
    turn = providers_a["supervisor"].generate(
        SUPERVISOR_SYSTEM.format(roll_no="22CS045"),
        history,
        list(supervisor_tools.functions().values()),
    )
    calls = [{"name": c.name, "args": c.args} for c in turn.tool_calls]
    assert len(calls) == 1 and calls[0]["name"] == "ask_booking"
    store.record_model_step(run_id, 1, turn.tokens_in, turn.tokens_out, turn.text, calls)

    # Execute the delegation and its child side effects, but intentionally do
    # NOT record supervisor step 2. This is the crash window from the slides.
    call = calls[0]
    parent_key = idempotency_key(run_id, 2, call["name"], call["args"])
    result, _ = run_tool(supervisor_tools, db, parent_key, call["name"], call["args"])
    assert "error" not in result
    print(f"{DIM}worker-A died after side effects, before recording supervisor step 2{RESET}")
    print(
        f"before replay: bookings={db.count('booking')} "
        f"notifications={db.count('notification')} idempotency_keys={db.count('idempotency')}"
    )

    # Let A's lease expire. B reaps and claims it, rebuilds the pending
    # delegation from the stored model step, and executes it with the same key.
    clock.advance(2.0)
    touched = store.reap_expired()
    assert run_id in touched and store.get_run(run_id)["status"] == "queued"

    replay_events: list[dict] = []

    def on_step(event: dict) -> None:
        replay_events.append(event)
        print_step(event)

    worker_b = Worker(
        store,
        db,
        demo_providers(),
        worker_id="worker-B",
        lease_seconds=1.0,
        on_step=on_step,
    )
    done = worker_b.run_until_idle()
    assert done and done[-1] == (run_id, "succeeded")

    run = store.get_run(run_id)
    booking_rows = db.count("booking")
    notification_rows = db.count("notification")
    idempotency_rows = db.count("idempotency")
    replayed = [e for e in replay_events if e.get("replayed")]

    print(
        f"after replay: bookings={booking_rows} notifications={notification_rows} "
        f"idempotency_keys={idempotency_rows}"
    )

    # One booking existed in seed data. Exactly one new booking + one message
    # must exist after replay; the child side effects should have replayed.
    assert run["status"] == "succeeded"
    assert booking_rows == 2
    assert notification_rows == 1
    assert idempotency_rows == 2
    assert {e.get("tool") for e in replayed} >= {"book_slot", "notify_student"}

    print(f"{GREEN}PASS: dead worker replay produced one new booking and one notification{RESET}")


if __name__ == "__main__":
    main()
