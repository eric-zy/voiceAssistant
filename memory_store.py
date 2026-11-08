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


TASK_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL, input_json TEXT NOT NULL,
    status TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '', result_json TEXT, error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, seen INTEGER NOT NULL DEFAULT 0)"""
TASK_COLUMNS = "id, kind, title, status, progress, result_json, error, created_at, started_at, finished_at, seen"
ACTIVE = ("queued", "running")


class TaskStore:
    """Background work one user started: queued, running, then done, failed or cancelled.

    The input stays here so a task can be rerun or inspected; results are JSON written by the feature.
    """

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(TASK_SCHEMA)
        return connection

    @staticmethod
    def _task(row):
        if not row:
            return None
        task = dict(zip(TASK_COLUMNS.split(", "), row))
        task["result"] = json.loads(task.pop("result_json")) if task["result_json"] else None
        task["seen"] = bool(task["seen"])
        return task

    def add(self, kind, title, payload):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("INSERT INTO agent_tasks (kind, title, input_json, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
                                      (kind, title, json.dumps(payload, ensure_ascii=False), now())).lastrowid

    def get(self, task_id):
        with contextlib.closing(self.connect()) as connection:
            return self._task(connection.execute(f"SELECT {TASK_COLUMNS} FROM agent_tasks WHERE id = ?", (task_id,)).fetchone())

    def payload(self, task_id):
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT input_json FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recent(self, limit=20):
        with contextlib.closing(self.connect()) as connection:
            return [self._task(row) for row in connection.execute(f"SELECT {TASK_COLUMNS} FROM agent_tasks ORDER BY id DESC LIMIT ?", (limit,))]

    def _move(self, task_id, sql, args, allowed):
        marks = ", ".join("?" * len(allowed))
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute(f"UPDATE agent_tasks SET {sql} WHERE id = ? AND status IN ({marks})",
                                      (*args, task_id, *allowed)).rowcount > 0

    def start(self, task_id):
        return self._move(task_id, "status = 'running', started_at = ?", (now(),), ("queued",))

    def set_progress(self, task_id, text):
        return self._move(task_id, "progress = ?", (text[:200],), ("running",))

    def finish(self, task_id, result):
        return self._move(task_id, "status = 'done', progress = '', result_json = ?, finished_at = ?",
                          (json.dumps(result, ensure_ascii=False), now()), ("running",))

    def fail(self, task_id, error):
        return self._move(task_id, "status = 'failed', progress = '', error = ?, finished_at = ?", (error[:300], now()), ACTIVE)

    def cancel(self, task_id):
        """True when it was still queued or running; a finished task stays as it is."""
        return self._move(task_id, "status = 'cancelled', progress = '', finished_at = ?", (now(),), ACTIVE)

    def mark_seen(self, task_id):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE agent_tasks SET seen = 1 WHERE id = ?", (task_id,)).rowcount > 0

    def recover_interrupted(self):
        """At startup nothing can be running yet, so anything marked active was cut off by a restart."""
        if not self.path.exists():  # a new user has nothing to recover; do not create files just to look
            return
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("UPDATE agent_tasks SET status = 'failed', progress = '', error = '服务重启，任务中断，请重新提交', "
                               "finished_at = ? WHERE status IN ('queued', 'running')", (now(),))
