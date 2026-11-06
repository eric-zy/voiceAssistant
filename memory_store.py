"""Local SQLite storage. Starts with the action log; memories and reminders join it later."""
import contextlib
import json
import logging
from pathlib import Path
import sqlite3
import time

log = logging.getLogger("uvicorn.error")
SCHEMA = """CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY, tool TEXT NOT NULL, args_json TEXT NOT NULL, source TEXT NOT NULL,
    outcome TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)"""


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
                    (tool, json.dumps(arguments, ensure_ascii=False), source, outcome, detail, time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        except sqlite3.Error:
            log.exception("could not write the action log")

    def recent(self, limit=50):
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute("SELECT tool, args_json, source, outcome, detail FROM action_log ORDER BY id DESC LIMIT ?", (limit,))
            return [{"tool": tool, "arguments": json.loads(args), "source": source, "outcome": outcome, "detail": detail}
                    for tool, args, source, outcome, detail in rows]
