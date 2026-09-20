"""lab.db: students, equipment, training, bookings and notifications."""
import json
import time
from collections.abc import Callable
from pathlib import Path

from app.db import connect, transaction

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "lab.sql"


class LabDb:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self.conn = connect(path)
        self.clock = clock

    def transaction(self):
        return transaction(self.conn)

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA.read_text())
        if self.conn.execute("SELECT count(*) FROM student").fetchone()[0]:
            return
        with self.transaction() as c:
            c.executemany("INSERT INTO student VALUES (?, ?, ?, ?, ?)", [
                (1, "22CS045", "Priya Raman", "CSE", 2),
                (2, "22IT017", "Arjun Kumar", "IT", 2),
                (3, "22EC031", "Divya Sekar", "ECE", 1),
            ])
            c.executemany("INSERT INTO equipment VALUES (?, ?, ?, ?, 0)", [
                (1, "Digital Oscilloscope", "electronics", "oscilloscope_safety"),
                (2, "Raspberry Pi 5 Kit", "embedded systems", None),
                (3, "Thermal Camera", "instrumentation", "thermal_camera_safety"),
                (4, "FPGA Development Board", "digital systems", "fpga_lab"),
            ])
            c.executemany("INSERT INTO training VALUES (?, ?, ?)", [
                (1, "oscilloscope_safety", 1), (1, "fpga_lab", 1),
                (2, "oscilloscope_safety", 0),
                (3, "thermal_camera_safety", 1),
            ])
            c.executemany("INSERT INTO policy VALUES (?, ?)", [
                ("max_active_bookings_default", 2),
            ])
            # Seed one occupied slot so clashes are visible immediately.
            c.execute("INSERT INTO booking (student_id, equipment_id, slot, returned, created_at) VALUES (3, 3, '2026-09-19 10:00', 0, ?)", (self.clock(),))

    def get_student(self, roll_no: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM student WHERE roll_no = ?", (roll_no,)).fetchone()
        return dict(row) if row else None

    def list_equipment(self, text: str = "") -> list[dict]:
        like = f"%{text.strip()}%"
        rows = self.conn.execute(
            "SELECT * FROM equipment WHERE name LIKE ? OR category LIKE ? ORDER BY name", (like, like)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_equipment(self, equipment_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        return dict(row) if row else None

    def training_status(self, student_id: int, course: str | None) -> bool:
        if not course:
            return True
        row = self.conn.execute(
            "SELECT completed FROM training WHERE student_id = ? AND course = ?", (student_id, course)
        ).fetchone()
        return bool(row and row[0])

    def active_bookings(self, student_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT b.id booking_id, b.equipment_id, e.name, b.slot FROM booking b "
            "JOIN equipment e ON e.id=b.equipment_id WHERE b.student_id=? AND b.returned=0 ORDER BY b.id",
            (student_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def slot_available(self, equipment_id: int, slot: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM booking WHERE equipment_id=? AND slot=?", (equipment_id, slot)
        ).fetchone() is None

    def book(self, student_id: int, equipment_id: int, slot: str) -> str:
        """Safe write: exactly one booking can own an equipment/slot pair."""
        with self.transaction() as c:
            if c.execute(
                "SELECT 1 FROM booking WHERE student_id=? AND equipment_id=? AND slot=?",
                (student_id, equipment_id, slot),
            ).fetchone():
                return "already_booked"
            try:
                c.execute(
                    "INSERT INTO booking (student_id,equipment_id,slot,returned,created_at) VALUES (?,?,?,0,?)",
                    (student_id, equipment_id, slot, self.clock()),
                )
            except Exception as e:
                if "UNIQUE constraint failed: booking.equipment_id, booking.slot" in str(e):
                    return "slot_taken"
                raise
            c.execute("UPDATE equipment SET version=version+1 WHERE id=?", (equipment_id,))
            return "booked"

    def return_booking(self, student_id: int, booking_id: int) -> str:
        with self.transaction() as c:
            row = c.execute("SELECT returned FROM booking WHERE id=? AND student_id=?", (booking_id, student_id)).fetchone()
            if row is None:
                return "not_found"
            if row[0]:
                return "already_returned"
            c.execute("UPDATE booking SET returned=1 WHERE id=?", (booking_id,))
            return "returned"

    def record_notification(self, roll_no: str, message: str, dedupe_key: str) -> tuple[int, bool]:
        cur = self.conn.execute(
            "INSERT INTO notification (roll_no,message,dedupe_key,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(dedupe_key) DO NOTHING", (roll_no, message, dedupe_key, self.clock()))
        if cur.rowcount == 1:
            return cur.lastrowid, True
        return self.conn.execute("SELECT id FROM notification WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0], False

    def once(self, key: str, tool_name: str, effect: Callable[[], dict]) -> tuple[dict, bool]:
        with self.transaction() as c:
            row = c.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row["result"]), False
            result = effect()
            c.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (key, tool_name, json.dumps(result), self.clock()))
            return result, True

    def count(self, table: str) -> int:
        assert table.isidentifier()
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
