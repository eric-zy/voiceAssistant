"""Local SQLite storage: what the user asked to remember, reminders, and a log of every write."""
import contextlib
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import time

log = logging.getLogger("uvicorn.error")
SCHEMA = """CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY, tool TEXT NOT NULL, args_json TEXT NOT NULL, source TEXT NOT NULL,
    outcome TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)"""
MEMORY_SCHEMA = """CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT)"""

REMINDER_SCHEMA = """CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, claimed_at TEXT, delivered_at TEXT)"""
# A page that claimed a reminder and then vanished must not lose it.
CLAIM_EXPIRES_SECONDS = 60


def local(moment):
    """Fixed-width local time, so text comparison in SQL is time comparison."""
    return moment.isoformat(timespec="seconds")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class ActionLog:
    """Every write a tool attempts, including refused ones. Stays on this machine."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(SCHEMA)
        return connection

    def record(self, tool, arguments, source, outcome, detail=""):
        # A full disk should cost the log entry, not the user's reply.
        try:
            with contextlib.closing(self.connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO action_log (tool, args_json, source, outcome, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (tool, json.dumps(arguments, ensure_ascii=False), source, outcome, detail, now()))
        except sqlite3.Error:
            log.exception("could not write the action log")

    def recent(self, limit=50):
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute("SELECT tool, args_json, source, outcome, detail FROM action_log ORDER BY id DESC LIMIT ?", (limit,))
            return [{"tool": tool, "arguments": json.loads(args), "source": source, "outcome": outcome, "detail": detail}
                    for tool, args, source, outcome, detail in rows]


class MemoryStore:
    """Facts the user asked to keep. Forgetting is soft so it can be undone; clearing is final."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(MEMORY_SCHEMA)
        return connection

    def add(self, content):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("INSERT INTO memories (content, created_at) VALUES (?, ?)", (content, now())).lastrowid

    def recent(self, limit=50):
        """Newest last, reading like a list. A broken database means no memories, not a failed reply."""
        try:
            with contextlib.closing(self.connect()) as connection:
                rows = connection.execute("SELECT id, content, created_at FROM memories WHERE deleted_at IS NULL "
                                          "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            log.exception("could not read memories")
            return []
        return [{"id": row[0], "content": row[1], "created_at": row[2]} for row in reversed(rows)]

    def forget(self, memory_id):
        """Return what was forgotten, or None if there is no such memory."""
        with contextlib.closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT content FROM memories WHERE id = ? AND deleted_at IS NULL", (memory_id,)).fetchone()
            if row:
                connection.execute("UPDATE memories SET deleted_at = ? WHERE id = ?", (now(), memory_id))
        return row[0] if row else None

    def restore(self, memory_id):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE memories SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
                                      (memory_id,)).rowcount > 0

    def clear(self):
        """Delete everything, including soft-deleted rows; returns how many live memories were lost."""
        with contextlib.closing(self.connect()) as connection, connection:
            count = connection.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
            connection.execute("DELETE FROM memories")
        return count


class ReminderStore:
    """pending -> claimed -> delivered, or cancelled. Times are naive local time."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(REMINDER_SCHEMA)
        return connection

    def add(self, content, due_at):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("INSERT INTO reminders (content, due_at, status, created_at) VALUES (?, ?, 'pending', ?)",
                                      (content, local(due_at), now())).lastrowid

    def pending(self, limit=20):
        try:
            with contextlib.closing(self.connect()) as connection:
                rows = connection.execute("SELECT id, content, due_at FROM reminders WHERE status IN ('pending', 'claimed') "
                                          "ORDER BY due_at LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            log.exception("could not read reminders")
            return []
        return [{"id": row[0], "content": row[1], "due_at": row[2]} for row in rows]

    def claim_due(self, moment=None):
        """Take one due reminder for this page, or None. The conditional update lets only one page win."""
        moment = moment or datetime.now()
        expired = local(moment - timedelta(seconds=CLAIM_EXPIRES_SECONDS))
        available = "(status = 'pending' OR (status = 'claimed' AND claimed_at <= ?))"
        with contextlib.closing(self.connect()) as connection, connection:
            row = connection.execute(f"SELECT id, content, due_at FROM reminders WHERE due_at <= ? AND {available} ORDER BY due_at LIMIT 1",
                                     (local(moment), expired)).fetchone()
            if not row:
                return None
            won = connection.execute(f"UPDATE reminders SET status = 'claimed', claimed_at = ? WHERE id = ? AND {available}",
                                     (local(moment), row[0], expired)).rowcount
        return {"id": row[0], "content": row[1], "due_at": row[2]} if won else None

    def mark_delivered(self, reminder_id):
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("UPDATE reminders SET status = 'delivered', delivered_at = ? WHERE id = ?", (now(), reminder_id))

    def cancel(self, reminder_id):
        """Return the cancelled reminder, or None if there is nothing left to cancel."""
        with contextlib.closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT id, content, due_at FROM reminders WHERE id = ? AND status IN ('pending', 'claimed')",
                                     (reminder_id,)).fetchone()
            if row:
                connection.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ?", (reminder_id,))
        return {"id": row[0], "content": row[1], "due_at": row[2]} if row else None

    def restore(self, reminder_id):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE reminders SET status = 'pending', claimed_at = NULL WHERE id = ? AND status = 'cancelled'",
                                      (reminder_id,)).rowcount > 0
