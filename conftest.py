import os

import pytest

from accounts import AccountStore
import app
import db
import llm_client
import llm_router
import ops
import usage
import user_context


# Set VOICE_TEST_DATABASE_URL (a voice_app-style role, never a superuser) to run every test against PostgreSQL;
# without it the stores use SQLite files under tmp_path.
TEST_DATABASE_URL = os.getenv("VOICE_TEST_DATABASE_URL", "").strip()


@pytest.fixture(scope="session")
def postgres():
    if not TEST_DATABASE_URL:
        yield None
        return
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(TEST_DATABASE_URL, min_size=1, max_size=10, open=True)
    db.migrate(pool)
    db.use_pool(pool)
    yield pool
    db.use_pool(None)
    pool.close()


@pytest.fixture(autouse=True)
def fresh_database(postgres):
    """Each test starts with empty tables and ids from 1, as a new SQLite file would."""
    if postgres is not None:
        with postgres.connection() as connection:
            connection.execute("TRUNCATE " + ", ".join(("users", "sessions", "usage", *db.USER_TABLES)) + " RESTART IDENTITY CASCADE")
    yield


@pytest.fixture(autouse=True)
def user(tmp_path, monkeypatch, fresh_database):
    """Every test runs as a fresh signed-in user with empty stores in a temporary directory.

    Only the session lookup is replaced; host and origin checks still run, so tests of those stay honest.
    Tests of signing in itself put default_session_user back.
    """
    monkeypatch.setattr(llm_client, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(app, "LATENCY_FILE", tmp_path / "latency.jsonl")
    monkeypatch.setattr(usage, "LEDGER", usage.UsageLedger(db.target(tmp_path / "usage.db")))
    # Rate limits and metrics count per process; every test starts with none used.
    monkeypatch.setattr(app, "limiter", ops.RateLimiter())
    monkeypatch.setattr(app, "metrics", ops.Metrics())
    # Circuit breakers remember failures; a test that makes DeepSeek fail must not leave it paused for the next.
    monkeypatch.setattr(app, "llm", llm_router.Router(app.llm_providers, lambda: app.metrics))
    for name in ("BACKUP_URL", "BACKUP_KEY", "BACKUP_MODEL"):
        monkeypatch.setattr(app, name, "")
    monkeypatch.setattr(app, "accounts", AccountStore(db.target(tmp_path / "accounts.db")))
    monkeypatch.setattr(app, "contexts", user_context.ContextCache(tmp_path / "users"))
    # TestClient sends Host: testserver when a test passes no headers.
    monkeypatch.setattr(app, "ALLOWED_HOSTS", app.ALLOWED_HOSTS | {"testserver"})
    context = user_context.build({"id": 1, "email": "test@example.com", "is_admin": True}, tmp_path / "user")
    monkeypatch.setattr(app, "session_user", lambda connection: context)
    token = user_context.CURRENT.set(context)
    yield context
    user_context.CURRENT.reset(token)


@pytest.fixture
def memories(user):
    return user.memories


@pytest.fixture
def reminders(user):
    return user.reminders
