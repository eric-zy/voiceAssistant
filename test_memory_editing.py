import asyncio
import contextlib
from datetime import datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient
import pytest

import app as backend
import memory_store
from memory_store import ReminderStore, next_occurrence
import tools

ORIGIN = {"origin": "http://127.0.0.1:8765"}
FRIDAY = datetime(2026, 11, 13, 9, 0)


@pytest.mark.parametrize("due,repeat,moment,expected", [
    (FRIDAY, "daily", FRIDAY, datetime(2026, 11, 14, 9)),
    (FRIDAY, "weekdays", FRIDAY, datetime(2026, 11, 16, 9)),       # over the weekend to Monday
    (FRIDAY, "weekly", FRIDAY, datetime(2026, 11, 20, 9)),
    (datetime(2026, 11, 1, 9), "daily", FRIDAY, datetime(2026, 11, 14, 9)),  # missed for days: the next one still ahead
    (datetime(2026, 11, 13, 8), "weekdays", FRIDAY + timedelta(hours=12), datetime(2026, 11, 16, 8))])
def test_a_repeating_reminder_comes_round_at_the_same_time_of_day(due, repeat, moment, expected):
    assert next_occurrence(due, repeat, moment) == expected


def test_a_delivered_repeating_reminder_is_set_for_its_next_time_and_a_one_off_is_done(tmp_path):
    store = ReminderStore(tmp_path / "agent.db")
    pills = store.add("吃药", FRIDAY, "weekdays")
    meeting = store.add("开会", FRIDAY)
    store.mark_delivered(pills, moment=FRIDAY)
    store.mark_delivered(meeting, moment=FRIDAY)
    assert store.pending() == [{"id": pills, "content": "吃药", "due_at": "2026-11-16T09:00:00", "repeat": "weekdays"}]
    with pytest.raises(ValueError):
        store.add("x", FRIDAY, "hourly")


def test_a_database_from_before_repeating_reminders_gets_the_column(tmp_path):
    path = tmp_path / "agent.db"
    with contextlib.closing(sqlite3.connect(path)) as old, old:
        old.execute(memory_store.REMINDER_SCHEMA)
        old.execute("INSERT INTO reminders (content, due_at, status, created_at) VALUES ('旧提醒', '2099-01-01T08:00:00', 'pending', 'x')")
    assert ReminderStore(path).pending()[0]["repeat"] == ""


def test_a_pending_reminder_can_be_changed_but_a_delivered_one_cannot(tmp_path):
    store = ReminderStore(tmp_path / "agent.db")
    item = store.add("开会", FRIDAY)
    assert store.update(item, "开周会", FRIDAY + timedelta(days=3), "weekly")
    assert store.pending()[0] == {"id": item, "content": "开周会", "due_at": "2026-11-16T09:00:00", "repeat": "weekly"}
    other = store.add("交报告", FRIDAY)
    store.mark_delivered(other, moment=FRIDAY)
    assert not store.update(other, "交报告", FRIDAY, "")


def test_the_voice_tool_sets_a_repeating_reminder(user):
    later = (datetime.now() + timedelta(hours=1)).replace(microsecond=0).isoformat()
    result = asyncio.run(tools.set_reminder("吃药", later, "daily"))
    assert result["say"].endswith("吃药（每天重复）")
    assert user.reminders.pending()[0]["repeat"] == "daily"
    with pytest.raises(tools.ToolError, match="repeat"):
        asyncio.run(tools.set_reminder("吃药", later, "hourly"))
    assert "repeat" in tools.SET_REMINDER.parameters["properties"] and tools.check_arguments(
        tools.SET_REMINDER, '{"content": "x", "due_at": "2099-01-01T08:00:00", "repeat": "weekly"}')["repeat"] == "weekly"


def test_the_page_adds_and_edits_memories(user):
    with TestClient(backend.app) as client:
        added = client.post("/api/memories", json={"content": "  我不吃   香菜 "}, headers=ORIGIN).json()
        assert added["content"] == "我不吃 香菜"
        assert client.put(f"/api/memories/{added['id']}", json={"content": "我不吃香菜和葱"}, headers=ORIGIN).status_code == 200
        assert client.post("/api/memories", json={"content": " "}, headers=ORIGIN).status_code == 400
        assert client.post("/api/memories", json={"content": "长" * 201}, headers=ORIGIN).status_code == 400
        assert client.put("/api/memories/999", json={"content": "x"}, headers=ORIGIN).status_code == 404
        assert client.post("/api/memories", json={"content": "x"}).status_code == 403
    assert [memory["content"] for memory in user.memories.recent()] == ["我不吃香菜和葱"]
    assert [entry["tool"] for entry in user.action_log.recent()][:2] == ["edit_memory", "remember"]


def test_the_page_adds_and_edits_reminders_with_the_same_checks_as_the_voice_tool(user):
    later = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()
    earlier = (datetime.now() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    with TestClient(backend.app) as client:
        reminder = client.post("/api/reminders", json={"content": "吃药", "due_at": later, "repeat": "daily"}, headers=ORIGIN).json()
        assert client.get("/api/reminders").json()["reminders"][0]["repeat"] == "daily"
        changed = client.put(f"/api/reminders/{reminder['id']}", json={"content": "吃维生素", "due_at": later, "repeat": ""}, headers=ORIGIN)
        assert changed.status_code == 200 and user.reminders.pending()[0]["content"] == "吃维生素"
        for body, message in [({"content": "x", "due_at": earlier}, "已经过去"), ({"content": "x", "due_at": "明天"}, "时间格式"),
                              ({"content": "x", "due_at": later, "repeat": "hourly"}, "重复方式"), ({"content": " ", "due_at": later}, "提醒内容")]:
            response = client.post("/api/reminders", json=body, headers=ORIGIN)
            assert response.status_code == 400 and message in response.json()["detail"], body
        assert client.put("/api/reminders/999", json={"content": "x", "due_at": later}, headers=ORIGIN).status_code == 404
