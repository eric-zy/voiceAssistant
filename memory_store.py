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



ENGLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS english_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
    ended_at TEXT, transcript_json TEXT NOT NULL DEFAULT '[]', notes_json TEXT NOT NULL DEFAULT '[]', report TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS english_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, text TEXT NOT NULL, key TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '', phonetic TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
    session_id INTEGER, created_at TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
    box INTEGER NOT NULL DEFAULT 0, due_at TEXT NOT NULL, tested INTEGER NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
    mastered INTEGER NOT NULL DEFAULT 0, UNIQUE (kind, key));
"""
ITEM_KINDS = ("word", "phrase", "sentence", "mistake")
ITEM_COLUMNS = "id, kind, text, meaning, phonetic, note, context, session_id, created_at, seen_count, box, due_at, tested, correct, mastered"
SESSION_COLUMNS = "id, level, topic, started_at, ended_at, transcript_json, notes_json, report"


def item_key(text):
    """Same item whatever the case, spacing or trailing punctuation."""
    return " ".join(text.lower().replace("’", "'").split()).strip(" .,!?;:")


class EnglishStore:
    """Free English conversation: sessions, and the learning items they produce (mistakes now; words, phrases and
    sentences once annotations arrive). Review progress lives on each item, ready for spaced-repetition quizzes."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(ENGLISH_SCHEMA)
        return connection

    @staticmethod
    def _session(row):
        if not row:
            return None
        session_id, level, topic, started, ended, transcript, notes, report = row
        return {"id": session_id, "level": level, "topic": topic, "started_at": started, "ended_at": ended,
                "transcript": json.loads(transcript), "notes": json.loads(notes), "report": report}

    def start(self, level, topic=""):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("INSERT INTO english_sessions (level, topic, started_at) VALUES (?, ?, ?)", (level, topic, now())).lastrowid

    def save_progress(self, session_id, transcript, notes):
        """Save the conversation so far, while it is still going."""
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("UPDATE english_sessions SET transcript_json = ?, notes_json = ? WHERE id = ?",
                               (json.dumps(transcript, ensure_ascii=False), json.dumps(notes, ensure_ascii=False), session_id))

    def finish(self, session_id, transcript, notes):
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("UPDATE english_sessions SET ended_at = ?, transcript_json = ?, notes_json = ? WHERE id = ?",
                               (now(), json.dumps(transcript, ensure_ascii=False), json.dumps(notes, ensure_ascii=False), session_id))

    def set_report(self, session_id, report):
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("UPDATE english_sessions SET report = ? WHERE id = ?", (report, session_id))

    def session(self, session_id):
        with contextlib.closing(self.connect()) as connection:
            return self._session(connection.execute(f"SELECT {SESSION_COLUMNS} FROM english_sessions WHERE id = ?", (session_id,)).fetchone())

    def sessions(self, limit=10):
        with contextlib.closing(self.connect()) as connection:
            return [self._session(row) for row in connection.execute(
                f"SELECT {SESSION_COLUMNS} FROM english_sessions WHERE transcript_json != '[]' ORDER BY id DESC LIMIT ?", (limit,))]

    def add_item(self, kind, text, meaning="", phonetic="", note="", context="", session_id=None):
        """Save a learning item; one already there is counted again, gets the newer details, and is due again."""
        if kind not in ITEM_KINDS or not item_key(text):
            raise ValueError("条目类型或内容无效")
        stamp = now()
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO english_items (kind, text, key, meaning, phonetic, note, context, session_id, created_at, due_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (kind, key) DO UPDATE SET seen_count = seen_count + 1, "
                "meaning = excluded.meaning, note = excluded.note, context = excluded.context, session_id = excluded.session_id, "
                "mastered = 0, due_at = MIN(due_at, excluded.due_at)",
                (kind, text.strip()[:300], item_key(text)[:300], meaning[:300], phonetic[:60], note[:300], context[:300], session_id, stamp, local(datetime.now())))  # due now, in the format review queries compare
            return connection.execute("SELECT id FROM english_items WHERE kind = ? AND key = ?", (kind, item_key(text)[:300])).fetchone()[0]

    def items(self, kind=None, include_mastered=False, limit=200):
        clauses, args = [], []
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if not include_mastered:
            clauses.append("mastered = 0")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute(f"SELECT {ITEM_COLUMNS} FROM english_items {where} ORDER BY id DESC LIMIT ?", (*args, limit))
            return [dict(zip(ITEM_COLUMNS.split(", "), row)) for row in rows]

    def counts(self):
        with contextlib.closing(self.connect()) as connection:
            return dict(connection.execute("SELECT kind, COUNT(*) FROM english_items WHERE mastered = 0 GROUP BY kind"))

    def set_mastered(self, item_id, mastered=True):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE english_items SET mastered = ? WHERE id = ?", (int(mastered), item_id)).rowcount > 0

    def delete_item(self, item_id):
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("DELETE FROM english_items WHERE id = ?", (item_id,)).rowcount > 0


CACHE_SCHEMA = """CREATE TABLE IF NOT EXISTS english_cache (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at TEXT NOT NULL)"""


class LookupCache:
    """Translations and explanations already paid for, so asking again costs nothing."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(CACHE_SCHEMA)
        return connection

    def get(self, key):
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT value FROM english_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO english_cache (key, value, created_at) VALUES (?, ?, ?)",
                               (key, json.dumps(value, ensure_ascii=False), now()))


# Spaced repetition: days until an item is asked again, by box. A right answer moves it up a box, a wrong one
# back to box 1 (tomorrow); a right answer in the last box means it is mastered.
REVIEW_DAYS = (0, 1, 2, 4, 7, 15)
QUIZ_SCHEMA = """CREATE TABLE IF NOT EXISTS english_quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, questions_json TEXT NOT NULL,
    answers_json TEXT, results_json TEXT, score INTEGER, finished_at TEXT)"""


def next_review(box, correct, moment):
    """(new box, due time, mastered) after one answer."""
    if not correct:
        return 1, local(moment + timedelta(days=REVIEW_DAYS[1])), False
    if box >= len(REVIEW_DAYS) - 1:
        return box, local(moment), True
    box += 1
    return box, local(moment + timedelta(days=REVIEW_DAYS[box])), False


def _due_order(connection, moment, limit):
    # Mistakes first, then whatever has waited longest.
    return connection.execute(
        f"SELECT {ITEM_COLUMNS} FROM english_items WHERE mastered = 0 AND due_at <= ? "
        "ORDER BY kind = 'mistake' DESC, due_at ASC, id ASC LIMIT ?", (local(moment), limit)).fetchall()


def _english_review(cls):
    def due(self, limit=10, moment=None, kinds=None):
        moment = moment or datetime.now()
        with contextlib.closing(self.connect()) as connection:
            rows = [dict(zip(ITEM_COLUMNS.split(", "), row)) for row in _due_order(connection, moment, 500)]
        rows = [row for row in rows if not kinds or row["kind"] in kinds]
        return rows[:limit]

    def due_count(self, moment=None):
        moment = moment or datetime.now()
        with contextlib.closing(self.connect()) as connection:
            count = connection.execute("SELECT COUNT(*) FROM english_items WHERE mastered = 0 AND due_at <= ?", (local(moment),)).fetchone()[0]
            upcoming = connection.execute("SELECT MIN(due_at) FROM english_items WHERE mastered = 0 AND due_at > ?", (local(moment),)).fetchone()[0]
        return count, upcoming

    def record_review(self, item_id, correct, moment=None):
        """Apply one answer to an item's schedule; returns its new box, due time and whether it is now mastered."""
        moment = moment or datetime.now()
        with contextlib.closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT box FROM english_items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                return None
            box, due_at, mastered = next_review(row[0], correct, moment)
            connection.execute("UPDATE english_items SET box = ?, due_at = ?, mastered = ?, tested = tested + 1, correct = correct + ? WHERE id = ?",
                               (box, due_at, int(mastered), int(correct), item_id))
        return {"box": box, "due_at": due_at, "mastered": mastered}

    def get_item(self, item_id):
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(f"SELECT {ITEM_COLUMNS} FROM english_items WHERE id = ?", (item_id,)).fetchone()
        return dict(zip(ITEM_COLUMNS.split(", "), row)) if row else None

    def create_quiz(self, questions):
        with contextlib.closing(self.connect()) as connection, connection:
            connection.execute(QUIZ_SCHEMA)
            return connection.execute("INSERT INTO english_quizzes (created_at, questions_json) VALUES (?, ?)",
                                      (now(), json.dumps(questions, ensure_ascii=False))).lastrowid

    def quiz(self, quiz_id):
        with contextlib.closing(self.connect()) as connection:
            connection.execute(QUIZ_SCHEMA)
            row = connection.execute("SELECT id, created_at, questions_json, answers_json, results_json, score, finished_at FROM english_quizzes WHERE id = ?",
                                     (quiz_id,)).fetchone()
        if not row:
            return None
        quiz_id, created, questions, answers, results, score, finished = row
        return {"id": quiz_id, "created_at": created, "questions": json.loads(questions), "answers": json.loads(answers) if answers else None,
                "results": json.loads(results) if results else None, "score": score, "finished_at": finished}

    def finish_quiz(self, quiz_id, answers, results, score):
        """Only once: returns False if the quiz was already handed in."""
        with contextlib.closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE english_quizzes SET answers_json = ?, results_json = ?, score = ?, finished_at = ? WHERE id = ? AND finished_at IS NULL",
                                      (json.dumps(answers, ensure_ascii=False), json.dumps(results, ensure_ascii=False), score, now(), quiz_id)).rowcount > 0

    def quizzes(self, limit=10):
        with contextlib.closing(self.connect()) as connection:
            connection.execute(QUIZ_SCHEMA)
            return [{"id": row[0], "created_at": row[1], "score": row[2], "finished_at": row[3], "count": len(json.loads(row[4]))}
                    for row in connection.execute("SELECT id, created_at, score, finished_at, questions_json FROM english_quizzes "
                                                  "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,))]

    for function in (due, due_count, record_review, get_item, create_quiz, quiz, finish_quiz, quizzes):
        setattr(cls, function.__name__, function)
    return cls


_english_review(EnglishStore)
