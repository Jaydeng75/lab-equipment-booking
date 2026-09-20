"""lab.db: students, equipment, training, bookings and notifications. Backend-agnostic via
``app.storage.Conn``: SQLite by default, Supabase Postgres when ``SUPABASE_DB_URL`` is set."""
import json
import time
from collections.abc import Callable
from pathlib import Path

from app.seeds import EQUIPMENT, POLICY, STUDENTS, TRAINING
from app.storage import Conn, open_lab_store

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "lab.sql"
SCHEMA_PG = Path(__file__).resolve().parent.parent / "schema" / "lab.pg.sql"


class LabDb:
    def __init__(self, dsn_or_path: object = None, clock: Callable[[], float] = time.time):
        """``dsn_or_path`` is either a psycopg/Supabase ``postgresql://`` URL (Postgres
        backend) or a file path / ``:memory:`` (SQLite backend). Pass ``None`` to use
        ``app.config`` defaults (``SUPABASE_DB_URL`` or ``LAB_DB``)."""
        if dsn_or_path is None:
            self.conn = open_lab_store()
        elif isinstance(dsn_or_path, str) and (dsn_or_path.startswith("postgresql://")
                                               or dsn_or_path.startswith("postgres://")):
            self.conn = Conn(dsn=dsn_or_path)
        else:
            self.conn = Conn(path=str(dsn_or_path))
        self.clock = clock

    def transaction(self):
        return self.conn.transaction()

    def migrate(self) -> None:
        schema = SCHEMA_PG if self.conn.backend == "postgres" else SCHEMA
        self.conn.executescript(schema.read_text())
        if self.conn.cursor().execute("SELECT EXISTS (SELECT 1 FROM student) AS has").fetchone()["has"]:
            return
        with self.transaction() as c:
            c.executemany("INSERT INTO student (id, roll_no, name, dept, max_active_bookings) VALUES (?, ?, ?, ?, ?)",
                          STUDENTS)
            c.executemany(
                "INSERT INTO equipment (id, name, category, requires_training, version) VALUES (?, ?, ?, ?, 0)",
                EQUIPMENT)
            c.executemany("INSERT INTO training (student_id, course, completed) VALUES (?, ?, ?)", TRAINING)
            c.executemany("INSERT INTO policy (name, value) VALUES (?, ?)", POLICY)
            c.execute("INSERT INTO booking (student_id, equipment_id, slot, returned, created_at)"
                      " VALUES (3, 3, '2026-09-19 10:00', 0, ?)", (self.clock(),))

    def get_student(self, roll_no: str) -> dict | None:
        row = self.conn.cursor().execute("SELECT * FROM student WHERE roll_no = ?", (roll_no,)).fetchone()
        return dict(row) if row else None

    def list_equipment(self, text: str = "") -> list[dict]:
        like = f"%{text.strip()}%"
        rows = self.conn.cursor().execute(
            "SELECT * FROM equipment WHERE name LIKE ? OR category LIKE ? ORDER BY name", (like, like)).fetchall()
        return [dict(r) for r in rows]

    def get_equipment(self, equipment_id: int) -> dict | None:
        row = self.conn.cursor().execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        return dict(row) if row else None

    def training_status(self, student_id: int, course: str | None) -> bool:
        if not course:
            return True
        row = self.conn.cursor().execute(
            "SELECT completed FROM training WHERE student_id = ? AND course = ?", (student_id, course)).fetchone()
        return bool(row and row["completed"])

    def active_bookings(self, student_id: int) -> list[dict]:
        rows = self.conn.cursor().execute(
            "SELECT b.id booking_id, b.equipment_id, e.name, b.slot FROM booking b "
            "JOIN equipment e ON e.id=b.equipment_id WHERE b.student_id=? AND b.returned=0 ORDER BY b.id",
            (student_id,)).fetchall()
        return [dict(r) for r in rows]

    def slot_available(self, equipment_id: int, slot: str) -> bool:
        return self.conn.cursor().execute(
            "SELECT 1 FROM booking WHERE equipment_id=? AND slot=?", (equipment_id, slot)).fetchone() is None

    def book(self, student_id: int, equipment_id: int, slot: str) -> str:
        """Safe write: exactly one booking can own an equipment/slot pair. In a race both
        students may reach the write, but the UNIQUE(equipment_id, slot) constraint lets only
        one insert win; the other returns 'slot_taken'."""
        if self.conn.backend == "postgres":
            return self._book_pg(student_id, equipment_id, slot)
        with self.conn.transaction() as c:
            if c.execute("SELECT 1 FROM booking WHERE student_id=? AND equipment_id=? AND slot=?",
                         (student_id, equipment_id, slot)).fetchone():
                return "already_booked"
            try:
                c.execute("INSERT INTO booking (student_id,equipment_id,slot,returned,created_at)"
                          " VALUES (?,?,?,0,?)", (student_id, equipment_id, slot, self.clock()))
                c.execute("UPDATE equipment SET version=version+1 WHERE id=?", (equipment_id,))
                return "booked"
            except Exception as e:  # noqa: BLE001
                if "UNIQUE constraint failed" in str(e):
                    return "slot_taken"
                raise

    def _book_pg(self, student_id: int, equipment_id: int, slot: str) -> str:
        import psycopg

        with self.conn.transaction() as c:
            if c.execute("SELECT 1 FROM booking WHERE student_id=? AND equipment_id=? AND slot=?",
                         (student_id, equipment_id, slot)).fetchone():
                return "already_booked"
            try:
                c.execute("INSERT INTO booking (student_id,equipment_id,slot,returned,created_at)"
                          " VALUES (?,?,?,0,?)", (student_id, equipment_id, slot, self.clock()))
                c.execute("UPDATE equipment SET version=version+1 WHERE id=?", (equipment_id,))
                return "booked"
            except psycopg.errors.UniqueViolation:
                self.conn.raw.rollback()
                return "slot_taken"

    def return_booking(self, student_id: int, booking_id: int) -> str:
        with self.conn.transaction() as c:
            row = c.execute("SELECT returned FROM booking WHERE id=? AND student_id=?",
                            (booking_id, student_id)).fetchone()
            if row is None:
                return "not_found"
            if row["returned"]:
                return "already_returned"
            c.execute("UPDATE booking SET returned=1 WHERE id=?", (booking_id,))
            return "returned"

    def record_notification(self, roll_no: str, message: str, dedupe_key: str) -> tuple[int, bool]:
        """Insert with `ON CONFLICT (dedupe_key) DO NOTHING`: returns (id, inserted_flag). No driver
        specific conflict text is inspected."""
        cur = self.conn.cursor()
        row = cur.execute("INSERT INTO notification (roll_no,message,dedupe_key,created_at)"
                          " VALUES (?,?,?,?) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
                          (roll_no, message, dedupe_key, self.clock())).fetchone()
        if row:
            nid = row["id"] if isinstance(row, dict) else row[0]
            return int(nid), True
        existing = self.conn.cursor().execute("SELECT id FROM notification WHERE dedupe_key=?",
                                              (dedupe_key,)).fetchone()
        return int(existing["id"]) if isinstance(existing, dict) else int(existing[0]), False

    def once(self, key: str, tool_name: str, effect: Callable[[], dict]) -> tuple[dict, bool]:
        with self.conn.transaction() as c:
            row = c.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row["result"]), False
            result = effect()
            c.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (key, tool_name, json.dumps(result), self.clock()))
            return result, True

    def count(self, table: str) -> int:
        assert table.isidentifier()
        row = self.conn.cursor().execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])