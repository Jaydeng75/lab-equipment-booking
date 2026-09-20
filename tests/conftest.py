import pytest
from app.lab_db import LabDb
from app.memory import RunStore
@pytest.fixture
def db():
    d=LabDb(":memory:"); d.migrate(); return d
@pytest.fixture
def store():
    s=RunStore(":memory:"); s.migrate(); return s
