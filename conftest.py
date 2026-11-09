import pytest

from accounts import AccountStore
import app
import llm_client
import ops
import usage
import user_context


@pytest.fixture(autouse=True)
def user(tmp_path, monkeypatch):
    """Every test runs as a fresh signed-in user with empty stores in a temporary directory.

    Only the session lookup is replaced; host and origin checks still run, so tests of those stay honest.
    Tests of signing in itself put default_session_user back.
    """
    monkeypatch.setattr(llm_client, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(app, "LATENCY_FILE", tmp_path / "latency.jsonl")
    monkeypatch.setattr(usage, "LEDGER", usage.UsageLedger(tmp_path / "usage.db"))
    # Rate limits and metrics count per process; every test starts with none used.
    monkeypatch.setattr(app, "limiter", ops.RateLimiter())
    monkeypatch.setattr(app, "metrics", ops.Metrics())
    monkeypatch.setattr(app, "accounts", AccountStore(tmp_path / "accounts.db"))
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
