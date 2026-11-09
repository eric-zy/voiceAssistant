"""Where the stores keep their rows: SQLite files (development, tests) or one PostgreSQL database (deployment).

The stores write their SQL once, in the dialect both understand, and ask this module for a connection:

  - A Path means SQLite. Each user has their own agent.db, so isolation is by file, as before.
  - A Scope means PostgreSQL. Every user's rows share one set of tables with a user_id column; each connection is
    told whose request it serves (app.user_id) and row-level security returns and accepts only that user's rows.
    A query that forgets to filter still cannot reach anyone else's data, and a connection that names no user
    sees no per-user rows at all.

Set VOICE_DATABASE_URL (postgresql://voice_app@db:5432/voice; the password comes from PGPASSWORD) to use
PostgreSQL. The role must not be a superuser or have BYPASSRLS, because those skip row-level security.
"""
from dataclasses import dataclass
import functools
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading

try:
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:  # SQLite-only installs (development) need not have the driver.
    psycopg = ConnectionPool = None

log = logging.getLogger("uvicorn.error")
Error = (sqlite3.Error, psycopg.Error) if psycopg else (sqlite3.Error,)
IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError) if psycopg else (sqlite3.IntegrityError,)

# Per-user tables. Each gets user_id, filled in from the connection, and a policy limiting rows to that user.
USER_TABLES = ("action_log", "memories", "reminders", "agent_tasks", "english_sessions", "english_items", "english_cache",
               "english_quizzes", "chats", "chat_messages")
# Tables with a generated id: an INSERT into one of them returns it, as SQLite's lastrowid does.
ID_TABLES = {"users", "usage", *USER_TABLES} - {"english_cache"}
CURRENT_USER = "NULLIF(current_setting('app.user_id', true), '')::bigint"
OWNER = f"user_id BIGINT NOT NULL DEFAULT {CURRENT_USER}"
SCHEMA_VERSION = 1
PG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS usage (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL,
    kind TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL, hit_tokens INTEGER NOT NULL DEFAULT 0,
    miss_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    peak INTEGER NOT NULL DEFAULT 0, cost_yuan DOUBLE PRECISION, complete INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS usage_user_day ON usage (user_id, day);
CREATE INDEX IF NOT EXISTS usage_day ON usage (day);

CREATE TABLE IF NOT EXISTS action_log (
    id BIGSERIAL PRIMARY KEY, {OWNER}, tool TEXT NOT NULL, args_json TEXT NOT NULL, source TEXT NOT NULL,
    outcome TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS action_log_user ON action_log (user_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id BIGSERIAL PRIMARY KEY, {OWNER}, content TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
CREATE INDEX IF NOT EXISTS memories_user ON memories (user_id, id);
CREATE TABLE IF NOT EXISTS reminders (
    id BIGSERIAL PRIMARY KEY, {OWNER}, content TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, claimed_at TEXT, delivered_at TEXT, repeat TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS reminders_user_due ON reminders (user_id, due_at);
CREATE TABLE IF NOT EXISTS agent_tasks (
    id BIGSERIAL PRIMARY KEY, {OWNER}, kind TEXT NOT NULL, title TEXT NOT NULL, input_json TEXT NOT NULL,
    status TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '', result_json TEXT, error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, seen INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks (user_id, id);
CREATE TABLE IF NOT EXISTS english_sessions (
    id BIGSERIAL PRIMARY KEY, {OWNER}, level TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
    ended_at TEXT, transcript_json TEXT NOT NULL DEFAULT '[]', notes_json TEXT NOT NULL DEFAULT '[]', report TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS english_sessions_user ON english_sessions (user_id, id);
CREATE TABLE IF NOT EXISTS english_items (
    id BIGSERIAL PRIMARY KEY, {OWNER}, kind TEXT NOT NULL, text TEXT NOT NULL, key TEXT NOT NULL,
    meaning TEXT NOT NULL DEFAULT '', phonetic TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
    session_id BIGINT, created_at TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
    box INTEGER NOT NULL DEFAULT 0, due_at TEXT NOT NULL, tested INTEGER NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
    mastered INTEGER NOT NULL DEFAULT 0, UNIQUE (user_id, kind, key));
CREATE INDEX IF NOT EXISTS english_items_user_due ON english_items (user_id, due_at);
CREATE TABLE IF NOT EXISTS english_cache (
    {OWNER}, key TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS english_quizzes (
    id BIGSERIAL PRIMARY KEY, {OWNER}, created_at TEXT NOT NULL, questions_json TEXT NOT NULL,
    answers_json TEXT, results_json TEXT, score INTEGER, finished_at TEXT);
CREATE INDEX IF NOT EXISTS english_quizzes_user ON english_quizzes (user_id, id);
CREATE TABLE IF NOT EXISTS chats (
    id BIGSERIAL PRIMARY KEY, {OWNER}, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS chats_user_updated ON chats (user_id, updated_at);
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY, {OWNER}, chat_id BIGINT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages (user_id, chat_id, id);
""" + "".join(f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON {table};
CREATE POLICY own_rows ON {table} USING (user_id = {CURRENT_USER}) WITH CHECK (user_id = {CURRENT_USER});
""" for table in USER_TABLES)


@dataclass(frozen=True)
class Scope:
    """A PostgreSQL target: the shared pool, and whose rows (None for the shared tables: accounts, usage)."""
    pool: object
    user_id: int | None = None


class SqliteTarget:
    def __init__(self, path, schema, upgrade=None):
        self.path, self.schema, self.upgrade = Path(path), schema, upgrade

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(self.schema)
        if self.upgrade:
            self.upgrade(connection)
        return connection

    def exists(self):
        return self.path.exists()


@functools.lru_cache(maxsize=512)
def translate(sql, user_scoped, has_args=True):
    """SQLite's ? placeholders become %s, a literal % is doubled, and a user's upsert names user_id in its key.
    Without arguments psycopg sends the text as it is, so nothing is escaped."""
    if has_args:
        sql = sql.replace("%", "%%").replace("?", "%s")
    if user_scoped:
        sql = sql.replace("ON CONFLICT (", "ON CONFLICT (user_id, ")
    table = re.match(r"\s*INSERT INTO (\w+)", sql)
    returns_id = bool(table) and table.group(1) in ID_TABLES and "RETURNING" not in sql and "ON CONFLICT" not in sql
    return sql + (" RETURNING id" if returns_id else ""), returns_id


class PgCursor:
    def __init__(self, cursor, returns_id):
        self.cursor, self.lastrowid = cursor, None
        if returns_id:
            self.lastrowid = cursor.fetchone()[0]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor.fetchall() if self.cursor.description else [])


class PgConnection:
    """A pooled connection that behaves like the sqlite3 one the stores were written for.

    Like sqlite3, "with connection:" commits or rolls back; close() hands it back to the pool.
    """

    def __init__(self, scope):
        self.pool, self.user_id = scope.pool, scope.user_id
        self.raw = self.pool.getconn()
        try:
            # Session-wide and committed at once, so a later rollback cannot bring back an earlier user's value.
            # Every checkout sets it, to "" for the shared tables, so nothing carries over between users.
            self.raw.execute("SELECT set_config('app.user_id', %s, false)", ("" if self.user_id is None else str(int(self.user_id)),))
            self.raw.commit()
        except BaseException:
            self.pool.putconn(self.raw)
            raise

    def execute(self, sql, args=()):
        args = tuple(args)
        sql, returns_id = translate(sql, self.user_id is not None, bool(args))
        return PgCursor(self.raw.execute(sql, args or None), returns_id)

    def executemany(self, sql, rows):
        sql, _ = translate(sql, self.user_id is not None)
        with self.raw.cursor() as cursor:
            cursor.executemany(sql.removesuffix(" RETURNING id"), [tuple(row) for row in rows])

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if self.raw is not None:
            raw, self.raw = self.raw, None
            raw.rollback()  # anything not committed by "with connection:" is dropped, as with sqlite3
            self.pool.putconn(raw)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.raw.commit() if kind is None else self.raw.rollback()
        return False


class PgTarget:
    def __init__(self, scope):
        self.scope = scope

    def connect(self):
        return PgConnection(self.scope)

    def exists(self):
        return True


def open_target(target, schema, upgrade=None):
    """A store's way in: a Path for SQLite (schema applied on every open), or a Scope for PostgreSQL (schema applied
    once at startup by migrate())."""
    return PgTarget(target) if isinstance(target, Scope) else SqliteTarget(target, schema, upgrade)


_POOL, _POOL_LOCK = None, threading.Lock()


def database_url():
    return os.getenv("VOICE_DATABASE_URL", "").strip()


def configured():
    return bool(database_url()) or _POOL is not None


def pool():
    """The process-wide pool, opened on first use."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            if psycopg is None:
                raise RuntimeError("VOICE_DATABASE_URL 已设置，但没有安装 psycopg：pip install -r requirements.txt")
            _POOL = ConnectionPool(database_url(), min_size=1, max_size=int(os.getenv("VOICE_DB_POOL", "10")), timeout=30,
                                   open=True, name="voice")
        return _POOL


def use_pool(new_pool):
    """Point the module at a given pool (tests), or None to go back to VOICE_DATABASE_URL."""
    global _POOL
    with _POOL_LOCK:
        _POOL = new_pool


def target(path, user_id=None):
    """Where a store should live: the SQLite path unless PostgreSQL is configured."""
    return Scope(pool(), user_id) if configured() else Path(path)


def migrate(scope_pool=None):
    """Create or update the PostgreSQL tables and policies; safe to run at every start. Refuses a role that would
    skip row-level security."""
    scope_pool = scope_pool or pool()
    with scope_pool.connection() as connection:
        superuser, bypass = connection.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
        if superuser or bypass:
            raise RuntimeError("数据库账号是超级用户或带 BYPASSRLS，会绕过行级安全；请用 deploy/postgres-init.sh 建的 voice_app 账号")
        # One process migrates at a time, so two app instances starting together do not trip over each other.
        connection.execute("SELECT pg_advisory_xact_lock(7710001)")
        connection.execute(PG_SCHEMA)
        found = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if found is None:
            connection.execute("INSERT INTO schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))
        elif found > SCHEMA_VERSION:
            raise RuntimeError(f"数据库结构版本 {found} 比程序认识的 {SCHEMA_VERSION} 新，请先更新程序")


def healthy():
    """True when the database answers; always True for SQLite, whose health is the data directory's."""
    if not configured():
        return True
    try:
        with pool().connection(timeout=3) as connection:
            connection.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - any failure means "not healthy", which is all the caller asks
        log.exception("database health check failed")
        return False
