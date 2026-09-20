import inspect
import os
import tempfile
import threading

from app.agents import SUPERVISOR_SYSTEM, SupervisorTools, run_tool
from app.idempotency import idempotency_key
from app.lab_db import LabDb
from app.memory import RunStore
from app.providers import AgentError, ModelTurn, ScriptedProvider, ToolCall, demo_providers
from app.tools.lab_tools import BookingTools, EquipmentTools
from app.worker import Worker


class ManualClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_business_rule_booking_limit_is_enforced_by_tool(db):
    t = BookingTools(db, "22CS045")
    assert t.book_slot(2, "2026-09-20 09:00")["status"] == "booked"
    assert t.book_slot(4, "2026-09-20 10:00")["status"] == "booked"
    blocked = t.book_slot(2, "2026-09-20 11:00")
    assert blocked["error"] == "booking_limit"


def test_return_is_safe_to_repeat(db):
    student = db.get_student("22CS045")
    assert student is not None
    assert db.book(student["id"], 2, "2026-09-20 12:00") == "booked"
    booking_id = db.conn.execute(
        "SELECT id FROM booking WHERE student_id=? AND equipment_id=2 AND slot=?",
        (student["id"], "2026-09-20 12:00"),
    ).fetchone()[0]
    t = BookingTools(db, "22CS045")
    assert t.return_item(booking_id)["status"] == "returned"
    assert t.return_item(booking_id)["status"] == "already_returned"


def test_booking_specialist_cannot_act_for_another_student(db):
    # Least privilege is structural: none of its callable tool signatures accepts roll_no/student_id.
    for name in BookingTools.TOOL_NAMES:
        params = inspect.signature(getattr(BookingTools, name)).parameters
        assert "roll_no" not in params
        assert "student_id" not in params


def test_every_side_effect_goes_through_once_when_run_tool_is_used(db):
    tools = BookingTools(db, "22CS045")
    calls = [
        ("book_slot", {"equipment_id": 2, "slot": "2026-09-20 13:00"}),
        ("notify_student", {"message": "Equipment 2 booking confirmed."}),
    ]
    for i, (name, args) in enumerate(calls, start=1):
        key = idempotency_key("run-1", i, name, args)
        first, replayed1 = run_tool(tools, db, key, name, args)
        second, replayed2 = run_tool(tools, db, key, name, args)
        assert "error" not in first
        assert second == first
        assert replayed1 is False
        assert replayed2 is True
    assert db.count("idempotency") == 2
    assert db.count("notification") == 1


def test_supervisor_only_has_delegation_tools(db):
    tools = SupervisorTools(db, demo_providers(), "22CS045")
    assert set(tools.TOOL_NAMES) == {"ask_equipment", "ask_booking"}
    assert set(tools.DELEGATES) == set(tools.TOOL_NAMES)
    assert tools.SIDE_EFFECTS == ()


def test_two_database_schemas_are_separate(tmp_path):
    store = RunStore(str(tmp_path / "agent.db"))
    db = LabDb(str(tmp_path / "lab.db"))
    store.migrate()
    db.migrate()
    agent_tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    lab_tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"thread", "run", "run_step", "tool_call"} <= agent_tables
    assert {"student", "equipment", "booking", "notification", "idempotency"} <= lab_tables
    assert "booking" not in agent_tables
    assert "run" not in lab_tables


def test_expired_lease_is_requeued_and_claimed_by_another_worker():
    clock = ManualClock()
    store = RunStore(":memory:", clock=clock)
    store.migrate()
    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "hello", "mock")
    a = store.claim_next("worker-A", lease_seconds=1.0)
    assert a and a.run_id == rid
    clock.advance(2.0)
    assert rid in store.reap_expired()
    b = store.claim_next("worker-B", lease_seconds=1.0)
    assert b and b.run_id == rid and b.attempts == 2


def test_retry_uses_backoff_then_dead_letters():
    clock = ManualClock()
    store = RunStore(":memory:", clock=clock)
    store.migrate()
    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "hello", "mock", max_attempts=2)
    assert store.claim_next("w1", 10)
    assert store.fail_attempt(rid, "w1", "provider_unavailable", True, backoff_seconds=2.0) == "queued"
    assert store.claim_next("too-early", 10) is None
    clock.advance(2.0)
    assert store.claim_next("w2", 10)
    assert store.fail_attempt(rid, "w2", "provider_unavailable", True, backoff_seconds=2.0) == "dead"
    run = store.get_run(rid)
    assert run["status"] == "dead" and run["error_code"] == "provider_unavailable"


def test_queued_run_can_be_cancelled():
    store = RunStore(":memory:")
    store.migrate()
    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "hello", "mock")
    assert store.request_cancel(rid) == "cancelled"
    assert store.get_run(rid)["status"] == "cancelled"
    assert store.claim_next("worker", 10) is None


def test_running_run_observes_cancel_between_steps():
    store = RunStore(":memory:")
    store.migrate()
    db = LabDb(":memory:")
    db.migrate()
    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "Show me an oscilloscope and check whether I am trained for it.", "mock")
    claimed = store.claim_next("worker", 60)
    assert claimed is not None
    assert store.request_cancel(rid) == "running"
    from app.runner import execute_run

    outcome = execute_run(
        claimed,
        store=store,
        db=db,
        providers=demo_providers(),
        worker_id="worker",
        lease_seconds=60,
    )
    assert outcome == "cancelled"
    assert store.get_run(rid)["status"] == "cancelled"


def test_booking_demo_end_to_end_has_side_effects(store, db):
    thread = store.create_thread("22CS045")
    rid = store.enqueue(
        thread,
        "Book equipment 2 for 2026-09-19 14:00 and send me a confirmation.",
        "mock",
    )
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert store.get_run(rid)["status"] == "succeeded"
    assert db.count("booking") == 2  # one seed + one new
    assert db.count("notification") == 1
    assert db.count("idempotency") == 2


def test_crash_after_effect_then_replay_does_not_duplicate():
    clock = ManualClock()
    store = RunStore(":memory:", clock=clock)
    db = LabDb(":memory:", clock=clock)
    store.migrate()
    db.migrate()
    thread = store.create_thread("22CS045")
    question = "Book equipment 2 for 2026-09-19 14:00 and send me a confirmation."
    rid = store.enqueue(thread, question, "mock")
    providers_a = demo_providers()
    claimed = store.claim_next("worker-A", lease_seconds=1.0)
    assert claimed is not None

    history = [{"role": m["role"], "text": m["text"]} for m in store.load_history(thread)]
    supervisor_tools = SupervisorTools(db, providers_a, "22CS045")
    turn = providers_a["supervisor"].generate(
        SUPERVISOR_SYSTEM.format(roll_no="22CS045"),
        history,
        list(supervisor_tools.functions().values()),
    )
    calls = [{"name": c.name, "args": c.args} for c in turn.tool_calls]
    store.record_model_step(rid, 1, turn.tokens_in, turn.tokens_out, turn.text, calls)

    call = calls[0]
    parent_key = idempotency_key(rid, 2, call["name"], call["args"])
    result, _ = run_tool(supervisor_tools, db, parent_key, call["name"], call["args"])
    assert "error" not in result
    assert db.count("booking") == 2 and db.count("notification") == 1

    # Crash: no supervisor tool record is written. After expiry, B resumes.
    clock.advance(2.0)
    assert rid in store.reap_expired()
    events = []
    Worker(store, db, demo_providers(), worker_id="worker-B", lease_seconds=1.0, on_step=events.append).run_until_idle()

    assert store.get_run(rid)["status"] == "succeeded"
    assert db.count("booking") == 2
    assert db.count("notification") == 1
    assert db.count("idempotency") == 2
    replayed = {e.get("tool") for e in events if e.get("replayed")}
    assert {"book_slot", "notify_student"} <= replayed


def test_thread_race_has_exactly_one_slot_winner(tmp_path):
    path = str(tmp_path / "race.db")
    seed = LabDb(path)
    seed.migrate()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    results = []

    def attempt(roll_no):
        db = LabDb(path)
        barrier.wait()
        result = BookingTools(db, roll_no).book_slot(2, "2026-09-20 16:00")
        with lock:
            results.append(result)

    a = threading.Thread(target=attempt, args=("22CS045",))
    b = threading.Thread(target=attempt, args=("22IT017",))
    a.start(); b.start(); a.join(); b.join()

    assert sum(r.get("status") == "booked" for r in results) == 1
    assert sum(r.get("error") == "slot_taken" for r in results) == 1


def test_nonretryable_agent_error_fails_run():
    store = RunStore(":memory:")
    db = LabDb(":memory:")
    store.migrate(); db.migrate()
    thread = store.create_thread("22CS045")
    rid = store.enqueue(thread, "anything", "mock")
    bad = ScriptedProvider([AgentError("bad_request", "no", retryable=False)])
    providers = {"supervisor": bad, "equipment": bad, "booking": bad}
    Worker(store, db, providers, worker_id="w").run_until_idle()
    run = store.get_run(rid)
    assert run["status"] == "failed" and run["error_code"] == "bad_request"
