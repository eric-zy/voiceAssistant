import pytest

import app
import tools
from memory_store import ActionLog, MemoryStore, ReminderStore


@pytest.fixture(autouse=True)
def memories(tmp_path, monkeypatch):
    """Every test gets empty stores, so a test run can never touch real data or the real log."""
    store = MemoryStore(tmp_path / "memories.db")
    monkeypatch.setattr(tools, "memories", store)
    monkeypatch.setattr(tools, "reminders", ReminderStore(tmp_path / "reminders.db"))
    monkeypatch.setattr(app, "action_log", ActionLog(tmp_path / "isolated-actions.db"))
    return store


@pytest.fixture
def reminders():
    return tools.reminders
