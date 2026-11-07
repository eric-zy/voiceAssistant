from datetime import datetime, timedelta
import sqlite3

from memory_store import ActionLog


def test_records_come_back_newest_first_with_chinese_intact(tmp_path):
    log = ActionLog(tmp_path / "nested" / "agent.db")
    log.record("clear", {"what": "记忆"}, "listen", "denied", "voice:取消")
    log.record("clear", {}, "text", "executed", "button")
    assert log.recent() == [
        {"tool": "clear", "arguments": {}, "source": "text", "outcome": "executed", "detail": "button"},
        {"tool": "clear", "arguments": {"what": "记忆"}, "source": "listen", "outcome": "denied", "detail": "voice:取消"}]


def test_nothing_is_created_until_something_is_logged(tmp_path):
    ActionLog(tmp_path / "data" / "agent.db")
    assert not (tmp_path / "data").exists()


def test_a_broken_database_does_not_break_the_reply(tmp_path, monkeypatch):
    log = ActionLog(tmp_path / "agent.db")

    def broken():
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(log, "connect", broken)
    log.record("clear", {}, "text", "executed")


def test_memories_survive_a_new_store_on_the_same_file(memories):
    memories.add("不吃香菜")
    assert [m["content"] for m in type(memories)(memories.path).recent()] == ["不吃香菜"]


def test_recent_keeps_the_newest_and_reads_oldest_first(memories):
    for text in "一二三":
        memories.add(text)
    assert [m["content"] for m in memories.recent(limit=2)] == ["二", "三"]


def test_forgetting_hides_a_memory_and_restoring_brings_it_back(memories):
    memory_id = memories.add("不吃香菜")
    assert memories.forget(memory_id) == "不吃香菜"
    assert memories.recent() == []
    assert memories.restore(memory_id) is True
    assert memories.recent()[0]["id"] == memory_id


def test_forgetting_or_restoring_nothing_says_so(memories):
    memory_id = memories.add("不吃香菜")
    assert memories.forget(999) is None
    assert memories.restore(memory_id) is False
    memories.forget(memory_id)
    assert memories.forget(memory_id) is None


def test_clearing_is_final_and_counts_live_memories(memories):
    forgotten = memories.add("旧的")
    memories.forget(forgotten)
    memories.add("新的")
    assert memories.clear() == 1
    assert memories.recent() == [] and memories.restore(forgotten) is False


def test_ids_are_not_reused_after_clearing(memories):
    old = memories.add("旧的")
    memories.clear()
    assert memories.add("新的") > old


def test_a_broken_database_reads_as_no_memories(memories, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(memories, "connect", broken)
    assert memories.recent() == []


NOON = datetime(2026, 11, 7, 12, 0, 0)


def test_reminders_list_soonest_first(reminders):
    later = reminders.add("晚的", NOON + timedelta(hours=2))
    sooner = reminders.add("早的", NOON + timedelta(hours=1))
    assert [r["id"] for r in reminders.pending()] == [sooner, later]


def test_only_due_reminders_can_be_claimed(reminders):
    reminders.add("开会", NOON)
    assert reminders.claim_due(NOON - timedelta(seconds=1)) is None
    assert reminders.claim_due(NOON)["content"] == "开会"


def test_a_reminder_is_claimed_once(reminders):
    reminders.add("开会", NOON)
    assert reminders.claim_due(NOON) is not None
    assert reminders.claim_due(NOON + timedelta(seconds=5)) is None


def test_two_stores_racing_cannot_both_win(reminders):
    reminders.add("开会", NOON)
    other = type(reminders)(reminders.path)
    assert [reminders.claim_due(NOON), other.claim_due(NOON)].count(None) == 1


def test_a_claim_from_a_vanished_page_expires(reminders):
    reminders.add("开会", NOON)
    reminders.claim_due(NOON)
    assert reminders.claim_due(NOON + timedelta(seconds=59)) is None
    assert reminders.claim_due(NOON + timedelta(seconds=61))["content"] == "开会"


def test_a_delivered_reminder_never_comes_back(reminders):
    reminder_id = reminders.add("开会", NOON)
    reminders.claim_due(NOON)
    reminders.mark_delivered(reminder_id)
    assert reminders.pending() == []
    assert reminders.claim_due(NOON + timedelta(hours=1)) is None


def test_cancelling_stops_a_reminder_and_restoring_brings_it_back(reminders):
    reminder_id = reminders.add("开会", NOON)
    assert reminders.cancel(reminder_id)["content"] == "开会"
    assert reminders.claim_due(NOON) is None and reminders.pending() == []
    assert reminders.restore(reminder_id) is True
    assert reminders.claim_due(NOON)["id"] == reminder_id


def test_cancelling_or_restoring_nothing_says_so(reminders):
    reminder_id = reminders.add("开会", NOON)
    assert reminders.cancel(999) is None and reminders.restore(reminder_id) is False
    reminders.mark_delivered(reminder_id)
    assert reminders.cancel(reminder_id) is None


def test_a_broken_database_reads_as_no_reminders(reminders, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(reminders, "connect", broken)
    assert reminders.pending() == []
