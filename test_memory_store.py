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
