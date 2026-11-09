"""Whose request is this? Each user gets a directory of their own, and code reaches it through current().

Files (knowledge files and index, voice samples, recordings, settings) live under data/users/<id>/. Memories,
reminders, the action log, tasks, English practice and chats live in that folder's agent.db with SQLite, or in
PostgreSQL scoped to the user by row-level security (see db.py). Either way, a query that forgets to filter
cannot return another user's rows.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import threading

import db
from knowledge import KnowledgeBase
from memory_store import ActionLog, ChatStore, MemoryStore, EnglishStore, ReminderStore, TaskStore
from voice_clone import VoiceStore

CURRENT = ContextVar("current_user")


@dataclass
class UserContext:
    user_id: int
    email: str
    is_admin: bool
    root: Path
    memories: MemoryStore
    reminders: ReminderStore
    action_log: ActionLog
    knowledge: KnowledgeBase
    voices: VoiceStore
    tasks: TaskStore
    english: EnglishStore
    chats: ChatStore

    def settings(self):
        """Personal settings; anything missing falls back to the server default."""
        try:
            return json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, changes):
        merged = {**self.settings(), **changes}
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "settings.tmp"
        temporary.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.root / "settings.json")
        return merged


def build(user, root, embedder=None):
    root = Path(root)
    database = db.target(root / "agent.db", user["id"])
    tasks = TaskStore(database)
    # A context is built once per user per process, so nothing of this user's can be running yet.
    tasks.recover_interrupted()
    return UserContext(user["id"], user["email"], bool(user.get("is_admin")), root, MemoryStore(database), ReminderStore(database),
                       ActionLog(database), KnowledgeBase(root / "knowledge", root / "knowledge.db", embedder), VoiceStore(root / "voices"),
                       tasks, EnglishStore(database), ChatStore(database))


class ContextCache:
    """One context per user, built on first use, so every request for a user shares the same stores."""

    def __init__(self, root, embedder_factory=lambda: None):
        self.root, self.embedder_factory, self.contexts, self.lock = Path(root), embedder_factory, {}, threading.Lock()

    def get(self, user):
        with self.lock:
            if user["id"] not in self.contexts:
                self.contexts[user["id"]] = build(user, self.root / str(int(user["id"])), self.embedder_factory())
            return self.contexts[user["id"]]


def current():
    """The signed-in user for this request. Raises LookupError outside one, rather than guessing."""
    return CURRENT.get()
