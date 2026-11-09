"""Copy the SQLite data (accounts, spend ledger, every user's agent.db) into an empty PostgreSQL database.

Nothing is moved or deleted: the SQLite files stay as they are, so the old setup still works if the move is undone.
Account ids are kept, because every user's folder (knowledge files, voices, recordings) is named after its id.
Row ids inside a user's data are renumbered, since PostgreSQL shares one sequence among all users; links between
rows (a chat's messages, an item's session, a quiz's items) are carried over to the new numbers.
Per-user rows are written through a connection scoped to that user, so row-level security checks every one.
"""
import contextlib
import json
from pathlib import Path
import sqlite3

import db

# Per-user tables in an order where every table comes after the ones it points to.
USER_TABLE_ORDER = ("memories", "reminders", "action_log", "agent_tasks", "english_sessions", "english_items", "english_cache",
                    "english_quizzes", "chats", "chat_messages")
LINKS = {"english_items": {"session_id": "english_sessions"}, "chat_messages": {"chat_id": "chats"}}


def _tables(connection):
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _remap_items(text, items):
    """Point a quiz's questions or results at the renumbered items."""
    if not text:
        return text
    value = json.loads(text)

    def walk(node):
        if isinstance(node, dict):
            return {key: (items.get(child, child) if key == "item_id" else walk(child)) for key, child in node.items()}
        return [walk(child) for child in node] if isinstance(node, list) else node
    return json.dumps(walk(value), ensure_ascii=False)


def copy_user_db(path, scope):
    """Copy one agent.db into PostgreSQL as scope's user. Returns {table: rows copied}."""
    copied, new_ids = {}, {}
    with contextlib.closing(sqlite3.connect(path)) as source, contextlib.closing(db.PgConnection(scope)) as target, target:
        present = _tables(source)
        for table in USER_TABLE_ORDER:
            if table not in present:
                continue
            columns = [column for column in _columns(source, table) if column not in ("id", "user_id")]
            has_id = "id" in _columns(source, table)
            order = " ORDER BY id" if has_id else ""
            rows = source.execute(f"SELECT {'id, ' if has_id else ''}{', '.join(columns)} FROM {table}{order}").fetchall()
            mapping = new_ids.setdefault(table, {})
            insert = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
            for row in rows:
                old_id, values = (row[0], dict(zip(columns, row[1:]))) if has_id else (None, dict(zip(columns, row)))
                for column, parent in LINKS.get(table, {}).items():
                    if values.get(column) is not None:
                        values[column] = new_ids.get(parent, {}).get(values[column])
                if table == "english_quizzes":
                    for column in ("questions_json", "results_json"):
                        values[column] = _remap_items(values.get(column), new_ids.get("english_items", {}))
                cursor = target.execute(insert, [values[column] for column in columns])
                if has_id:
                    mapping[old_id] = cursor.lastrowid
            copied[table] = len(rows)
    return copied


def has_user_rows(scope):
    """Whether this user already has memories or reminders in PostgreSQL."""
    with contextlib.closing(db.PgConnection(scope)) as connection:
        return any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("memories", "reminders"))


def migrate(data, pool, report=print):
    """Copy everything under data/ into PostgreSQL. Refuses if PostgreSQL already has accounts."""
    data = Path(data)
    db.migrate(pool)
    shared = db.Scope(pool)
    with contextlib.closing(db.PgConnection(shared)) as connection:
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise SystemExit("PostgreSQL 里已经有账号了，不再导入，以免重复。要重来请先清空数据库")
    accounts = data / "accounts.db"
    if not accounts.is_file():
        raise SystemExit(f"没有找到 {accounts}")
    with contextlib.closing(sqlite3.connect(accounts)) as source, contextlib.closing(db.PgConnection(shared)) as target, target:
        users = source.execute("SELECT id, email, password, is_admin, disabled, created_at FROM users ORDER BY id").fetchall()
        for row in users:
            target.execute("INSERT INTO users (id, email, password, is_admin, disabled, created_at) VALUES (?, ?, ?, ?, ?, ?)", row)
        target.execute("SELECT setval('users_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM users), false)")
        sessions = source.execute("SELECT token_hash, user_id, created_at, expires_at FROM sessions").fetchall()
        target.executemany("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                           [row for row in sessions if row[1] in {user[0] for user in users}])
    report(f"账号 {len(users)} 个，登录会话 {len(sessions)} 个")
    ledger = data / "usage.db"
    if ledger.is_file():
        with contextlib.closing(sqlite3.connect(ledger)) as source, contextlib.closing(db.PgConnection(shared)) as target, target:
            columns = [column for column in _columns(source, "usage") if column != "id"]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM usage ORDER BY id").fetchall()
            target.executemany(f"INSERT INTO usage ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows)
        report(f"花费记录 {len(rows)} 条")
    for user_id, _, *_ in users:
        path = data / "users" / str(user_id) / "agent.db"
        if path.is_file():
            copied = copy_user_db(path, db.Scope(pool, user_id))
            report(f"用户 {user_id}：" + ("，".join(f"{table} {count}" for table, count in copied.items() if count) or "没有数据"))
    return len(users)
