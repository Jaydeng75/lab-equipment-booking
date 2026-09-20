"""Tools for the Lab Equipment Booking project, split by least privilege."""
from datetime import datetime, timezone

from app.idempotency import notification_dedupe_key
from app.lab_db import LabDb
from app.tools.dispatch import dispatch


class Toolset:
    SIDE_EFFECTS: tuple[str, ...] = ()
    DELEGATES: tuple[str, ...] = ()
    TOOL_NAMES: tuple[str, ...] = ()
    def functions(self): return {n: getattr(self, n) for n in self.TOOL_NAMES}
    def call(self, name, args): return dispatch(self.functions(), name, args)


class EquipmentTools(Toolset):
    """Strictly read-only specialist tools."""
    TOOL_NAMES = ("list_equipment", "check_training")

    def __init__(self, db: LabDb, roll_no: str):
        self.db, self.roll_no = db, roll_no

    def list_equipment(self, text: str) -> dict:
        """List lab equipment matching a name or category and show whether each item needs training.

        Use when a student asks what equipment exists, searches by category, or needs an equipment id.
        Read-only: changes no booking or inventory data. Do not use this to reserve a slot; booking is
        handled by the booking specialist. Pass an empty string to list everything.

        Args:
            text: Search words such as "oscilloscope", "embedded", or an empty string for all items.

        Returns:
            {"equipment": [{"equipment_id", "name", "category", "requires_training"}]}.
        """
        items = self.db.list_equipment(text)
        return {"equipment": [{"equipment_id": e["id"], "name": e["name"], "category": e["category"],
                                "requires_training": e["requires_training"]} for e in items]}

    def check_training(self, equipment_id: int) -> dict:
        """Check whether the current student has the training required for one equipment item.

        Use before recommending a bookable item or when the student asks whether they are eligible.
        Read-only: it never creates a booking. Do not guess from the prompt; eligibility comes from the
        training table. An item with no required training is automatically allowed.

        Args:
            equipment_id: Integer id returned by list_equipment.

        Returns:
            {"equipment_id", "required_course", "trained": bool}, or unknown_equipment.
        """
        e = self.db.get_equipment(equipment_id)
        if e is None:
            return {"error": "unknown_equipment", "hint": "Use list_equipment to find the id first."}
        s = self.db.get_student(self.roll_no)
        if s is None:
            return {"error": "unknown_student", "hint": "The current roll number is not registered."}
        return {"equipment_id": equipment_id, "required_course": e["requires_training"],
                "trained": self.db.training_status(s["id"], e["requires_training"])}


class BookingTools(Toolset):
    """Write-capable tools bound to one student; no roll-number argument is exposed to the model."""
    TOOL_NAMES = ("book_slot", "return_item", "notify_student")
    SIDE_EFFECTS = TOOL_NAMES

    def __init__(self, db: LabDb, roll_no: str, clock=lambda: datetime.now(timezone.utc)):
        self.db, self.roll_no, self.clock = db, roll_no, clock

    def _student(self):
        s = self.db.get_student(self.roll_no)
        if s is None: raise LookupError("student not found")
        return s

    def book_slot(self, equipment_id: int, slot: str) -> dict:
        """Book one equipment item for the current student in a named time slot. CHANGES DATA.

        Use only after the student has asked to book a specific equipment id and slot. The tool itself
        enforces training and active-booking limits from data even if the model forgot to check them.
        It also refuses a slot already taken by another student. Repeating the same booking is safe.

        Args:
            equipment_id: Integer id from list_equipment.
            slot: A concrete slot string, e.g. "2026-09-19 14:00".

        Returns:
            booked/already_booked, or an error such as training_required, booking_limit, slot_taken.
        """
        s = self._student(); e = self.db.get_equipment(equipment_id)
        if e is None:
            return {"error": "unknown_equipment", "hint": "Find the equipment id first."}
        if not self.db.training_status(s["id"], e["requires_training"]):
            return {"error": "training_required", "course": e["requires_training"], "hint": "Complete the required training first."}
        if len(self.db.active_bookings(s["id"])) >= s["max_active_bookings"]:
            return {"error": "booking_limit", "hint": "Return an active item before booking another."}
        status = self.db.book(s["id"], equipment_id, slot)
        if status == "slot_taken":
            return {"error": "slot_taken", "hint": "Choose another time slot."}
        return {"equipment_id": equipment_id, "slot": slot, "status": status}

    def return_item(self, booking_id: int) -> dict:
        """Mark one of the current student's equipment bookings as returned. CHANGES DATA.

        Use when the student explicitly reports returning an item. It can act only on the current
        student's booking because no roll number is accepted. Repeating a completed return is safe.

        Args:
            booking_id: Booking id belonging to the current student.

        Returns:
            {"booking_id", "status": "returned"|"already_returned"}, or not_found.
        """
        status = self.db.return_booking(self._student()["id"], booking_id)
        if status == "not_found":
            return {"error": "not_found", "hint": "That booking does not belong to this student."}
        return {"booking_id": booking_id, "status": status}

    def notify_student(self, message: str) -> dict:
        """Queue a short confirmation message to the current student. CHANGES DATA by creating a notification.

        Use only to confirm a completed booking or return; do not use it for ordinary chat answers.
        Identical messages on the same day are deduplicated so replay does not send two notifications.

        Args:
            message: Non-empty confirmation text of at most 160 characters.

        Returns:
            {"notification_id", "status": "queued", "duplicate": bool}.
        """
        if not message.strip() or len(message) > 160:
            return {"error": "invalid_message", "hint": "message must be 1 to 160 characters."}
        key = notification_dedupe_key(self.roll_no, message, self.clock().date())
        nid, created = self.db.record_notification(self.roll_no, message, key)
        return {"notification_id": nid, "status": "queued", "duplicate": not created}
