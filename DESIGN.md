# Weekend Project Design — Lab Equipment Booking

## 1. Tables and constraints

### Domain database: `lab.db`

- `student`: roll number, department, per-student **`max_active_bookings`**.
- `equipment`: id, name, category, required training, **`version`**.
- `training`: `(student_id, course)` completion state.
- `policy`: seeded rule rows for domain policy data.
- `booking`: student + equipment + slot; **`UNIQUE(equipment_id, slot)`** settles concurrent slot races, and `(student_id, equipment_id, slot)` is also unique.
- `notification`: outbound messages with a **unique dedupe key**.
- `idempotency`: key + tool + serialized result for replay-safe side effects.

### Agent database: `agent.db`

- `thread`, append-only `message`.
- `run`: status, attempts, `available_at`, lease owner/until, cancellation and error state.
- `run_step`: recorded model/tool sequence.
- `tool_call`: arguments, results, latency and idempotency key.

The two databases deliberately separate conversational/durable-execution state from domain business state.

## 2. Rule in data

Two important rules are data-backed rather than prompt-only:

1. Equipment may require a named training course. Completion is read from the `training` table.
2. Each student has a `max_active_bookings` value in the `student` table.

`BookingTools.book_slot` checks both itself, so the rule still holds even if a model directly asks to book without first calling `check_training`.

Slot exclusivity is stronger still: the `booking` table's `UNIQUE(equipment_id, slot)` constraint is authoritative under concurrent writes.

## 3. Tools

| Tool | Kind | When to use | What it changes |
|---|---|---|---|
| `list_equipment` | read-only | discover/search equipment and ids | nothing |
| `check_training` | read-only | verify current student's eligibility for an equipment id | nothing |
| `book_slot` | side effect | student explicitly requests an equipment id + exact slot | inserts one booking, increments equipment version |
| `return_item` | side effect | student explicitly reports returning their own booking | marks booking returned |
| `notify_student` | side effect | confirm a completed booking/return | inserts a deduplicated notification |

Every callable has a full docstring saying when to use it, what not to use it for, and whether it changes data.

## 4. Agents and permissions

### Supervisor

Tools: `ask_equipment`, `ask_booking` only. It cannot directly query or mutate domain tables.

### Equipment specialist — no write tools

Tools: `list_equipment`, `check_training`. `SIDE_EFFECTS == ()`, so a confused read-only agent cannot reserve, return or notify.

### Booking specialist — bounded write authority

Tools: `book_slot`, `return_item`, `notify_student`. The toolset is constructed for exactly one `roll_no`, and no write method exposes a `roll_no` or `student_id` argument to the model.

## 5. Clash and safe write

**Clash:** two students request the same equipment and slot at the same time.

**Settlement:** both may reach the write, but SQLite serializes writers and `UNIQUE(equipment_id, slot)` permits exactly one booking. The loser receives `slot_taken`. `scripts.race_demo` and the thread race test prove this behavior.

## 6. Durable execution

A question is persisted as a queued `run`. A worker atomically claims it and receives a time-bounded lease. Heartbeats extend ownership. If the worker dies, `reap_expired()` either requeues the run or dead-letters it after the maximum attempts.

Supervisor model/tool steps are recorded in `agent.db`, allowing the next worker to rebuild the pending tool call instead of starting the whole conversation from scratch.

## 7. Idempotency and key propagation

Supervisor tool calls use a stable key derived from `(run_id, step_seq, tool_name, args)`. A delegation passes that key down as `parent_key`; each specialist write derives a child key from the parent plus its own sequence/name/args.

`run_tool` sends every side-effect tool through `LabDb.once`. `once` stores the result under the key in the same domain database. A replay with the same key returns the stored result and does not execute the side effect again.

`notify_student` also has independent same-day deduplication, and `return_item` is independently safe to repeat.

## 8. Crash scenario

Crash point used by `scripts.crash_demo`:

1. worker-A records the supervisor model step asking for `ask_booking`;
2. booking specialist commits `book_slot` and `notify_student` with child idempotency keys;
3. worker-A dies **before** the supervisor delegation result is recorded;
4. its lease expires and worker-B claims the run;
5. worker-B rebuilds the pending `ask_booking` call and derives the same parent/child keys;
6. `book_slot` and `notify_student` are replayed from stored results, creating no duplicates;
7. worker-B records the delegation result and completes the run.

The proof asserts one seeded booking + exactly one new booking, one notification, and two domain idempotency records.

## 9. Extra reliability features

- queued/running cancellation from another terminal;
- exponential retry backoff and dead-lettering;
- a two-thread race test using separate SQLite connections.

These are in addition to the required weekend features.
