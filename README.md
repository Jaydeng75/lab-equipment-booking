# Lab Equipment Booking — Weekend Project

A complete no-key multi-agent service for booking campus lab equipment. It follows the Day 4 weekend brief: separate domain/agent databases, durable queue execution, least-privilege specialists, idempotent side effects, crash replay, and scripted proof that runs without Gemini.

## Domain

Students can discover equipment, check required training, book a specific equipment/time slot, return a booking, and receive confirmation notifications. The central clash is **two students trying to book the same equipment for the same slot**.

## Architecture

```text
student
  |
  v
scripts.ask / scripts.demo
  |
  v
agent.db: thread -> queued run -> leased worker -> recorded supervisor steps
  |
  v
supervisor
  |-----------------------------|
  v                             v
equipment specialist          booking specialist
READ ONLY                     WRITE-CAPABLE, bound to one roll number
  |                             |
  | list_equipment              | book_slot
  | check_training              | return_item
  |                             | notify_student
  |-----------------------------|
                |
                v
lab.db: students, training, equipment, bookings, notifications,
        business rules and idempotency results
```

The supervisor has only delegation tools. `EquipmentTools` has **no side-effect tools at all**. `BookingTools` is created for one roll number, and none of its callable tool signatures accepts another student's roll number or student id.

## Weekend requirement checklist

| Requirement | Implementation / proof |
|---|---|
| Two SQLite databases | `agent.db` via `RunStore`; `lab.db` via `LabDb`; both have seed/schema support |
| 5+ tools, at least 2 read-only + 2 side effects | `list_equipment`, `check_training`, `book_slot`, `return_item`, `notify_student` |
| Rule in data enforced by a tool | required training and per-student `max_active_bookings` live in tables; `book_slot` checks them itself |
| Queue + worker lease | `app/memory.py`, `app/worker.py`; expired leases are requeued/dead-lettered |
| Idempotency | every specialist side effect goes through `LabDb.once(key, ...)`; notifications also dedupe on their own |
| 2+ agents | supervisor + read-only equipment specialist + booking specialist |
| No-key proof | `python -m scripts.demo`, `python -m scripts.crash_demo`, `pytest -q` |
| 12+ tests incl. crash/replay | **25 tests** |

## Safe writes and clashes

`booking` has `UNIQUE(equipment_id, slot)`. Two students can race, but only one insert can win; the other gets `slot_taken`. The database is authoritative rather than relying on the model to remember a prompt rule.

The booking tool also enforces training and the student's active-booking limit from database rows. A repeated return is intrinsically safe (`already_returned`), and identical same-day notifications are deduplicated.

## Idempotency and crash recovery

The supervisor's delegation gets a stable key from `(run_id, step, tool, args)`. A specialist derives child keys from that parent key and its own step/tool/args. Every write tool runs through `LabDb.once`, which stores the result with the key in `lab.db`.

If a worker dies after a specialist side effect but before the supervisor's tool step is recorded, the lease expires. Another worker rebuilds the pending delegation from `agent.db`, derives the same keys, and replays the stored side-effect results instead of performing them twice.

## Run on a clean machine

Python 3.10+ is recommended.

```bash
python -m pip install -r requirements.txt
python -m scripts.demo
python -m scripts.crash_demo
pytest -q
```

Expected final lines include:

```text
PASS: scripted demo completed
PASS: dead worker replay produced one new booking and one notification
25 passed
```

No API key is required for any of the commands above. Scripted models are the default proof path.

## Extra / higher-grade features

This submission includes **three offline higher-grade features**, so a real Gemini run is not needed to satisfy the "add one" option:

1. **Cancel from a second terminal** — `scripts.cancel` plus cancellation checks between worker steps.
2. **Retry with exponential backoff and dead-lettering** — implemented by `RunStore.fail_attempt`; demonstrated by `python -m scripts.retry_demo`.
3. **Race test with threads** — `tests/test_weekend.py::test_thread_race_has_exactly_one_slot_winner`; also runnable as `python -m scripts.race_demo`.

### Cancel from a second terminal

Use three terminals so the run stays visible long enough to cancel:

```bash
# Terminal 1
python -m scripts.worker --mock --slow 2

# Terminal 2 (prints the run id, then waits)
python -m scripts.ask --student 22CS045 "Show me an oscilloscope and check whether I am trained for it."

# Terminal 3
python -m scripts.cancel <RUN_ID>
```

A queued run is cancelled immediately. A running run receives `cancel_requested=1`, and the worker stops at the next safe boundary between steps.

### Retry / dead-letter demonstration

```bash
python -m scripts.retry_demo
```

This proves that a retryable failure is requeued with backoff and becomes `dead` after `max_attempts`.

### Concurrent race demonstration

```bash
python -m scripts.race_demo
```

Two threads use separate SQLite connections to request the same Raspberry Pi slot. Exactly one gets `booked`; the other gets `slot_taken`.

## Optional real Gemini run

The project retains `GeminiProvider`. To try it manually, set `GEMINI_API_KEY` and run a normal worker without `--mock`. This was **not recorded as proof** in this submission because the weekend requirements explicitly allow scripted models and only one higher-grade option is required; the project already includes three offline options above.

## Optional: put the databases in Supabase

The storage layer (`app/storage.py`) supports **Supabase Postgres** as an alternative to the
two local SQLite files. Nothing is required on a clean machine for the demos/tests — SQLite
remains the default — but if you export a Supabase connection string, both `agent.db` and
`lab.db` live in Supabase instead.

```bash
export SUPABASE_DB_URL="postgresql://postgres:YOUR-PASSWORD@db.xxx.supabase.co:5432/postgres"
pip install -r requirements.txt -r requirements-supabase.txt        # psycopg is the only extra

python -m scripts.apply_supabase          # create schema + seed data (uses SUPABASE_DB_URL)
python -m scripts.demo                    # the same demo, now against Supabase
python -m scripts.crash_demo              # crash/replay proof, against Supabase
pytest -q                                 # the same tests still use local SQLite
```

A `.env.example` shows every variable; `SUPABASE_HOST/PORT/DB/USER/PASSWORD` work as a
stand-in for the full URL. The apply script is idempotent-ish (it drops and rebuilds the
tables) and both schemas have Postgres-dialect copies in `schema/lab.pg.sql` and
`schema/agent.pg.sql`. The queue/lease/idempotency mechanics are identical: `book` still
relies on the `UNIQUE(equipment_id, slot)` constraint, notifications still dedupe on a
unique `dedupe_key`, and crash replay still replays stored side-effect results.

## Important files

- `DESIGN.md` — design choices, tables, tools, agents, clash and failure behavior.
- `schema/lab.sql` / `schema/lab.pg.sql` — domain schema (SQLite / Postgres).
- `schema/agent.sql` / `schema/agent.pg.sql` — memory, queue, leases and recorded steps.
- `app/storage.py` — shared connection layer: SQLite by default, Supabase Postgres opt-in.
- `app/lab_db.py` — domain reads/safe writes/idempotency.
- `app/tools/lab_tools.py` — five documented domain tools.
- `app/agents.py` — supervisor, specialists and key propagation.
- `app/memory.py` — queue, leases, cancellation, retry and dead-lettering.
- `scripts/demo.py` — no-key end-to-end demo.
- `scripts/crash_demo.py` — dead-worker crash/replay proof.
- `scripts/apply_supabase.py` — apply the Postgres schema + seed to a Supabase project.
- `tests/` — 25 tests including crash/replay and thread race.

## Submission packaging

The submitted zip should be named `weekend-<roll-no>.zip`. Do **not** include `.venv`, `.db` files, `__pycache__`, or `.pytest_cache`. The generated package supplied with this project is already cleaned of those files; rename it with your actual roll number before uploading if necessary.
