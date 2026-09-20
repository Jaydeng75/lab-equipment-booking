"""One small connection layer for two backends: local SQLite (the default, no key) and
Supabase Postgres (opt-in via env). Both backends speak the same SQL the storage classes
use; only the migration SQL and the way transactions are started differ. Rows are always
dict-like, and a tiny cursor shim gives ``rowcount`` / ``lastrowid`` the same semantics on
Postgres that sqlite3 provides natively.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator


def _split_statements(script: str) -> list[str]:
    """Split a Postgres script on `;`, keeping plpgsql `$$ ... $$` bodies intact:
    the body is neutralised first so the semicolons inside it never count as separators."""
    registry: dict[str, str] = {}
    def protect(m):  # returns a token; remembers the real body
        token = f"__PLPGSQL_{len(registry)}__"
        registry[token] = m.group(0)
        return token
    import re
    neutral = re.sub(r"\$\$.*?\$\$", protect, script, flags=re.DOTALL)
    out = []
    for stmt in (s.strip() for s in neutral.split(";")):
        stmt = "\n".join(line for line in stmt.splitlines() if not line.lstrip().startswith("--")).strip()
        if not stmt:
            continue
        for token, body in registry.items():
            stmt = stmt.replace(token, body)
        out.append(stmt)
    return out


class Conn:
    """A wrapped sqlite3 or psycopg connection with a very small, shared cursor API.

    - ``cursor()`` -> a ``Cursor`` whose ``execute`` returns itself so you can read
      ``.rowcount`` or ``.lastrowid`` right after.
    - ``transaction()`` -> context manager. Begins a real transaction if none is open
      (SQLite ``BEGIN IMMEDIATE``, Postgres ``BEGIN``), rolls back if an exception escaped,
      otherwise commits.
    - ``executescript(script)`` -> applies a schema; for Postgres it splits on ``;``.
    - ``now_sql()`` -> the dialect's "current timestamp", for the schema's text columns.
    """
    def __init__(self, dsn: str | None = None, *, path: str | None = None):
        if dsn:
            import psycopg
            self.backend = "postgres"
            self.raw = psycopg.connect(dsn, autocommit=True,
                                       row_factory=psycopg.rows.dict_row,
                                       prepare_threshold=None)
            self.raw.execute("SET search_path TO public")
        else:
            import sqlite3
            self.backend = "sqlite"
            self.raw = sqlite3.connect(path if path is not None else ":memory:",
                                       isolation_level=None, check_same_thread=False, timeout=5.0)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA foreign_keys = ON")
            self.raw.execute("PRAGMA busy_timeout = 5000")

    def cursor(self) -> "Cursor":
        return Cursor.create(self.raw, self.backend)

    def execute(self, sql: str, params: tuple | dict | list | None = None) -> "Cursor":
        """One-shot: convenience for tests and callers that use ``conn.execute(...)``."""
        return self.cursor().execute(sql, params)

    def in_transaction(self) -> bool:
        if self.backend == "sqlite":
            return bool(self.raw.in_transaction)
        try:
            from psycopg.pq import TransactionStatus
            return self.raw.info.transaction_status in (TransactionStatus.INTRANS,
                                                        TransactionStatus.INERROR)
        except Exception:
            return False

    @contextmanager
    def transaction(self) -> Iterator["Cursor"]:
        if self.in_transaction():
            yield self.cursor()
            return
        self.cursor().execute("BEGIN IMMEDIATE" if self.backend == "sqlite" else "BEGIN")
        cur = self.cursor()
        try:
            yield cur
        except BaseException:
            self.raw.rollback()
            raise
        self.raw.commit()

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self.raw.executescript(script)
        else:
            for stmt in _split_statements(script):
                self.cursor().execute(stmt)

    def now_sql(self) -> str:
        """A dialect-neutral SQL expression for "the current timestamp as text", used as
        the value of the schema's ``*_at`` TEXT columns."""
        return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')" if self.backend == "sqlite" \
            else "to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            pass


def _pg_params(sql: str) -> str:
    """Rewrite ``?`` placeholders (sqlite style, used everywhere in the storage classes)
    into the ``%s`` placeholders psycopg (v3) accepts, without touching text inside quotes."""
    out, i, quote = [], 0, None
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == "'":
                out.append(ch)
                i += 1
                if i < len(sql):
                    out.append(sql[i])
                i += 1
                continue
            out.append(ch)
            i += 1
        elif ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
        elif ch == "?":
            out.append("%s")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class Cursor:
    """Uniform cursor. The result of the last statement is buffered so ``lastrowid``,
    ``fetchone`` and ``fetchall`` all agree even though psycopg exposes no ``lastrowid``."""

    def __init__(self, raw, backend: str):
        self.raw = raw
        self.backend = backend
        self._rows: list | None = None
        self.lastrowid: int | None = None

    @classmethod
    def create(cls, conn, backend: str) -> "Cursor":
        return cls(conn.cursor(), backend)

    def _load(self) -> list:
        if self._rows is None:
            try:
                self._rows = self.raw.fetchall() or []
            except Exception:
                self._rows = []
        return self._rows

    def _exec(self, sql: str, params) -> None:
        if self.backend == "postgres":
            sql = _pg_params(sql)
        if params is None:
            self.raw.execute(sql)
        else:
            self.raw.execute(sql, params)

    def execute(self, sql: str, params: tuple | dict | list | None = None) -> "Cursor":
        self._exec(sql, params)
        self._rows = None
        if self.backend == "sqlite":
            self.lastrowid = getattr(self.raw, "lastrowid", None)
        else:
            rows = self._load()
            self.lastrowid = None
            if rows:
                self.lastrowid = next(iter(rows[0].values())) if isinstance(rows[0], dict) else rows[0][0]
        return self

    def executemany(self, sql: str, seq: list) -> "Cursor":
        if self.backend == "sqlite":
            self.raw.executemany(sql, seq)
        else:
            pg = _pg_params(sql)
            import psycopg
            self.raw.executemany(pg, seq)
        self._rows = None
        return self

    @property
    def rowcount(self) -> int:
        if self.backend == "sqlite":
            return self.raw.rowcount
        rows = self._load()
        return len(rows)

    def fetchone(self):
        rows = self._load()
        return rows[0] if rows else None

    def fetchall(self):
        return self._load()


def open_agent_store(dsn: str | None = None) -> Conn:
    if dsn or os.environ.get("SUPABASE_DB_URL"):
        return Conn(dsn or os.environ["SUPABASE_DB_URL"])
    return Conn(path=os.environ.get("AGENT_DB", "agent.db"))


def open_lab_store(dsn: str | None = None) -> Conn:
    if dsn or os.environ.get("SUPABASE_DB_URL"):
        return Conn(dsn or os.environ["SUPABASE_DB_URL"])
    return Conn(path=os.environ.get("LAB_DB", "lab.db"))