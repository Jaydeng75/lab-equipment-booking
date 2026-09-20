import inspect
from app.tools.lab_tools import EquipmentTools, BookingTools
from app.providers import demo_providers
from app.worker import Worker


def test_migrate_twice(db):
    db.migrate(); assert db.count("student")==3 and db.count("equipment")==4

def test_seed_contains_rule_breaker_and_nearly_gone_item(db):
    # Arjun lacks oscilloscope training; Thermal Camera has a seeded occupied slot.
    assert EquipmentTools(db,"22IT017").check_training(1)["trained"] is False
    assert db.slot_available(3,"2026-09-19 10:00") is False

def test_read_only_list(db):
    r=EquipmentTools(db,"22CS045").list_equipment("embedded")
    assert r["equipment"][0]["name"]=="Raspberry Pi 5 Kit"

def test_read_only_training(db):
    assert EquipmentTools(db,"22CS045").check_training(1)["trained"] is True

def test_read_only_specialist_has_no_side_effects(db):
    assert EquipmentTools(db,"22CS045").SIDE_EFFECTS==()

def test_tools_have_full_descriptions():
    for cls in (EquipmentTools,BookingTools):
        for n in cls.TOOL_NAMES:
            assert len(inspect.getdoc(getattr(cls,n)) or "") >= 120

def test_booking_enforces_training_without_model_check(db):
    assert BookingTools(db,"22IT017").book_slot(1,"2026-09-19 12:00")["error"]=="training_required"

def test_safe_write_refuses_slot_clash(db):
    a=BookingTools(db,"22CS045").book_slot(2,"2026-09-19 14:00")
    b=BookingTools(db,"22IT017").book_slot(2,"2026-09-19 14:00")
    assert a["status"]=="booked" and b["error"]=="slot_taken"

def test_same_booking_is_safe_to_repeat(db):
    t=BookingTools(db,"22CS045")
    assert t.book_slot(2,"2026-09-19 15:00")["status"]=="booked"
    assert t.book_slot(2,"2026-09-19 15:00")["status"]=="already_booked"

def test_notification_dedupes(db):
    t=BookingTools(db,"22CS045")
    a=t.notify_student("Booked."); b=t.notify_student("Booked.")
    assert a["notification_id"]==b["notification_id"] and b["duplicate"]

def test_demo_question_end_to_end(store,db):
    thread=store.create_thread("22CS045"); rid=store.enqueue(thread,"Show me an oscilloscope and check whether I am trained for it.","mock")
    Worker(store,db,demo_providers(),worker_id="w").run_until_idle()
    assert store.get_run(rid)["status"]=="succeeded" and "Digital Oscilloscope" in store.load_history(thread)[-1]["text"]
