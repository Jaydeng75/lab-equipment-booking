"""agent.db: conversations, runs and the job queue. Backend-agnostic: uses ``app.storage.Conn``,
which is local SQLite unless ``SUPABASE_DB_URL`` is set. Postgres rows are ``dict_row``; SQLite
rows are ``sqlite3.Row``, and a tiny shim keeps ``execute(...).rowcount/.lastrowid`` uniform."""
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.storage import Conn, open_agent_store

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "agent.sql"
SCHEMA_PG = Path(__file__).resolve().parent.parent / "schema" / "agent.pg.sql"


@dataclass(frozen=True)
class Claimed:
    run_id: str
    thread_id: str
    attempts: int


class RunStore:  # noqa: PLR0904  (methods match the assignment spec)
    def __init__(self, dsn_or_path: object = None, clock: Callable[[], float] = time.time):
        """``dsn_or_path`` is either a psycopg/Supabase ``postgresql://`` URL (Postgres
        backend) or a file path / ``:memory:`` (SQLite backend). Pass ``None`` to use
        ``app.config`` defaults (``SUPABASE_DB_URL`` or ``AGENT_DB``)."""
        if dsn_or_path is None:
            self.conn = open_agent_store()
        elif isinstance(dsn_or_path, str) and (dsn_or_path.startswith("postgresql://")
                                               or dsn_or_path.startswith("postgres://")):
            self.conn = Conn(dsn=dsn_or_path)
        else:
            self.conn = Conn(path=str(dsn_or_path))
        self.clock = clock

    def migrate(self) -> None:
        schema = SCHEMA_PG if self.conn.backend == "postgres" else SCHEMA
        self.conn.executescript(schema.read_text())

    def transaction(self):
        return self.conn.transaction()

    def create_thread(self, student_id: str) -> str:
        thread_id = str(uuid.uuid4())
        self.conn.cursor().execute("INSERT INTO thread (id, student_id) VALUES (?, ?)", (thread_id, student_id))
        return thread_id

    def get_thread(self, thread_id: str) -> dict | None:
        r = self.conn.cursor().execute("SELECT * FROM thread WHERE id = ?", (thread_id,)).fetchone()
        return dict(r) if r else None

    def append_message(self, thread_id: str, role: str, text: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO message (thread_id, seq, role, text)"
            " VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM message WHERE thread_id = ?), ?, ?)"
            " RETURNING id",
            (thread_id, thread_id, role, text))
        seq = cur.execute("SELECT seq FROM message WHERE id = ?", (cur.lastrowid,)).fetchone()
        return int(seq["seq"])

    def load_history(self, thread_id: str) -> list[dict]:
        rows = self.conn.cursor().execute(
            "SELECT seq, role, text FROM message WHERE thread_id = ? ORDER BY seq", (thread_id,)).fetchall()
        return [dict(r) for r in rows]

    def record_model_step(self, run_id: str, seq: int, tokens_in: int, tokens_out: int,
                          text: str | None, tool_calls: list[dict]) -> int:
        with self.conn.transaction() as c:
            c.execute(
                "INSERT INTO run_step (run_id, seq, kind, tokens_in, tokens_out, text, tool_calls)"
                " VALUES (?, ?, 'model', ?, ?, ?, ?) RETURNING id",
                (run_id, seq, tokens_in, tokens_out, text, json.dumps(tool_calls)))
            step_id = c.lastrowid
            c.execute("UPDATE run SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id = ?",
                      (tokens_in, tokens_out, run_id))
            return step_id

    def record_tool_call(self, run_id: str, seq: int, name: str, args: dict, result: dict,
                         ok: bool, latency_ms: int, idempotency_key: str | None = None) -> int:
        with self.conn.transaction() as c:
            c.execute("INSERT INTO run_step (run_id, seq, kind) VALUES (?, ?, 'tool') RETURNING id", (run_id, seq))
            step_id = c.lastrowid
            c.execute("INSERT INTO tool_call (run_step_id, tool_name, args, result, ok, latency_ms, idempotency_key)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (step_id, name, json.dumps(args, default=str), json.dumps(result, default=str),
                       int(ok), latency_ms, idempotency_key))
            return step_id

    def load_steps(self, run_id: str) -> list[dict]:
        """Every recorded step of a run, in order, with JSON decoded. Used to resume after a crash."""
        rows = self.conn.cursor().execute(
            """SELECT s.seq, s.kind, s.text, s.tool_calls, t.tool_name, t.args, t.result, t.ok
                 FROM run_step s LEFT JOIN tool_call t ON t.run_step_id = s.id
                WHERE s.run_id = ? ORDER BY s.seq""", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("tool_calls", "args", "result"):
                d[k] = json.loads(d[k]) if d[k] is not None else None
            out.append(d)
        return out

    def get_run(self, run_id: str) -> dict | None:
        r = self.conn.cursor().execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        return {**dict(r), "steps": self.load_steps(run_id)} if r else None

    def enqueue(self, thread_id: str, text: str, model: str, max_attempts: int = 3) -> str:
        run_id = str(uuid.uuid4())
        with self.conn.transaction() as c:
            self.append_message(thread_id, "user", text)
            c.execute("INSERT INTO run (id, thread_id, status, model, max_attempts, available_at)"
                      " VALUES (?, ?, 'queued', ?, ?, ?)", (run_id, thread_id, model, max_attempts, self.clock()))
        return run_id

    def claim_next(self, worker_id: str, lease_seconds: float) -> Optional[Claimed]:
        """Atomically take the oldest claimable run: queued and available now.
        Postgres and SQLite share the same visible behaviour: the run becomes running, leased
        to this worker until now + lease_seconds, with attempts + 1."""
        now = self.clock()
        with self.conn.transaction() as c:
            row = c.execute(
                "SELECT id, thread_id, attempts FROM run WHERE status = 'queued' AND available_at <= ?"
                " ORDER BY available_at, created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE run SET status = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1,"
                " started_at = COALESCE(started_at, {ts})"
                " WHERE id = ?".format(ts=self.conn.now_sql()),
                (worker_id, now + lease_seconds, row["id"]))
            return Claimed(row["id"], row["thread_id"], int(row["attempts"]) + 1)

    def heartbeat(self, run_id: str, worker_id: str, lease_seconds: float) -> bool:
        """Extend the lease. False means this worker no longer owns the run: stop working on it."""
        return self.conn.cursor().execute(
            "UPDATE run SET lease_until = ? WHERE id = ? AND status = 'running' AND lease_owner = ?",
            (self.clock() + lease_seconds, run_id, worker_id)).rowcount == 1

    def reap_expired(self) -> list[str]:
        """Runs whose worker died: lease expired while running. Requeue them, or dead-letter the ones
        that have used all their attempts (error_code 'lease_expired'). Return the ids touched."""
        now = self.clock()
        with self.conn.transaction() as c:
            rows = c.execute("SELECT id, attempts, max_attempts FROM run WHERE status = 'running'"
                             " AND lease_until < ?", (now,)).fetchall()
            for r in rows:
                if r["attempts"] >= r["max_attempts"]:
                    c.execute("UPDATE run SET status = 'dead', error_code = 'lease_expired', lease_owner = NULL,"
                              " lease_until = NULL, finished_at = {ts}"
                              " WHERE id = ?".format(ts=self.conn.now_sql()), (r["id"],))
                else:
                    c.execute("UPDATE run SET status = 'queued', error_code = 'lease_expired', lease_owner = NULL,"
                              " lease_until = NULL, available_at = ? WHERE id = ?", (now, r["id"]))
            return [r["id"] for r in rows]

    def complete(self, run_id: str, worker_id: str, reply: str) -> bool:
        """Save the model's reply and mark the run succeeded, together, only if this worker still owns it."""
        with self.conn.transaction() as c:
            row = c.execute("SELECT thread_id FROM run WHERE id = ? AND status = 'running' AND lease_owner = ?",
                            (run_id, worker_id)).fetchone()
            if row is None:
                return False
            self.append_message(row["thread_id"], "model", reply)
            c.execute("UPDATE run SET status = 'succeeded', lease_owner = NULL, lease_until = NULL,"
                      " error_code = NULL, finished_at = {ts}"
                      " WHERE id = ?".format(ts=self.conn.now_sql()), (run_id,))
            return True

    def request_cancel(self, run_id: str) -> str | None:
        """A queued run is cancelled at once. A running run is flagged; its worker stops after the current
        step. Finished runs are left alone. Return the run's status after the call, or None if unknown."""
        with self.conn.transaction() as c:
            row = c.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                c.execute("UPDATE run SET status = 'cancelled', finished_at ="
                          " {ts} WHERE id = ?".format(ts=self.conn.now_sql()), (run_id,))
                return "cancelled"
            if row["status"] == "running":
                c.execute("UPDATE run SET cancel_requested = 1 WHERE id = ?", (run_id,))
            return row["status"]

    def cancel_requested(self, run_id: str) -> bool:
        row = self.conn.cursor().execute("SELECT cancel_requested FROM run WHERE id = ?", (run_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def mark_cancelled(self, run_id: str, worker_id: str) -> bool:
        return self.conn.cursor().execute(
            "UPDATE run SET status = 'cancelled', lease_owner = NULL, lease_until = NULL, finished_at ="
            " {ts}"
            " WHERE id = ? AND status = 'running' AND lease_owner = ?".format(ts=self.conn.now_sql()),
            (run_id, worker_id)).rowcount == 1

    def fail_attempt(self, run_id: str, worker_id: str, error_code: str, retryable: bool,
                     backoff_seconds: float = 2.0) -> str | None:
        """An attempt failed. Not retryable: 'failed'. Retryable with attempts left: back to 'queued',
        available after backoff_seconds * 2 ** (attempts - 1). Retryable with none left: 'dead'.
        Only if this worker owns the run. Return the new status, or None if it didn't own it."""
        with self.conn.transaction() as c:
            row = c.execute("SELECT attempts, max_attempts FROM run WHERE id = ? AND status = 'running'"
                            " AND lease_owner = ?", (run_id, worker_id)).fetchone()
            if row is None:
                return None
            if retryable and row["attempts"] < row["max_attempts"]:
                delay = backoff_seconds * 2 ** (row["attempts"] - 1)
                c.execute("UPDATE run SET status = 'queued', error_code = ?, lease_owner = NULL, lease_until = NULL,"
                          " available_at = ? WHERE id = ?", (error_code, self.clock() + delay, run_id))
                return "queued"
            status = "dead" if retryable else "failed"
            c.execute("UPDATE run SET status = ?, error_code = ?, lease_owner = NULL, lease_until = NULL,"
                      " finished_at = {ts} WHERE id = ?".format(ts=self.conn.now_sql()),
                      (status, error_code, run_id))
            return status

    def count(self, table: str) -> int:
        assert table.isidentifier()
        row = self.conn.cursor().execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])