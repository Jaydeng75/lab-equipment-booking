"""Shared seed rows for the domain database: SQLite migrations, Postgres migrations
and ``scripts/apply_supabase.py`` all use the same data."""
from __future__ import annotations

STUDENTS = [
    (1, "22CS045", "Priya Raman", "CSE", 2),
    (2, "22IT017", "Arjun Kumar", "IT", 2),
    (3, "22EC031", "Divya Sekar", "ECE", 1),
]

EQUIPMENT = [
    (1, "Digital Oscilloscope", "electronics", "oscilloscope_safety"),
    (2, "Raspberry Pi 5 Kit", "embedded systems", None),
    (3, "Thermal Camera", "instrumentation", "thermal_camera_safety"),
    (4, "FPGA Development Board", "digital systems", "fpga_lab"),
]

TRAINING = [
    (1, "oscilloscope_safety", 1),
    (1, "fpga_lab", 1),
    (2, "oscilloscope_safety", 0),
    (3, "thermal_camera_safety", 1),
]

POLICY = [
    ("max_active_bookings_default", 2),
]