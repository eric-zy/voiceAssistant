import pytest

import app
import llm_client
import tools
from knowledge import KnowledgeBase
from memory_store import ActionLog, MemoryStore, ReminderStore


@pytest.fixture(autouse=True)
def memories(tmp_path, monkeypatch):
    """Every test gets empty stores, so a test run can never touch real data or the real log."""
    monkeypatch.setattr(llm_client, "RETRY_DELAYS", (0, 0))
    store = MemoryStore(tmp_path / "memories.db")
    monkeypatch.setattr(tools, "memories", store)
    monkeypatch.setattr(tools, "reminders", ReminderStore(tmp_path / "reminders.db"))
    monkeypatch.setattr(tools, "knowledge", KnowledgeBase(tmp_path / "knowledge-isolated", tmp_path / "knowledge.db"))
    monkeypatch.setattr(app, "action_log", ActionLog(tmp_path / "isolated-actions.db"))
    monkeypatch.setattr(app, "LATENCY_FILE", tmp_path / "latency.jsonl")
    return store


@pytest.fixture
def reminders():
    return tools.reminders
