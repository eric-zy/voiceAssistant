from fastapi.testclient import TestClient
import pytest

from accounts import AccountStore
import app as backend
import ops

ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
def store(tmp_path):
    return AccountStore(tmp_path / "accounts.db")


def test_a_password_change_needs_the_current_one_and_signs_out_other_devices(store):
    user = store.register("me@example.test", "old-password-1")
    here, elsewhere = store.start_session(user["id"]), store.start_session(user["id"])
    assert store.change_password(user["id"], "wrong-password", "new-password-1", here) is False
    assert store.change_password(user["id"], "old-password-1", "new-password-1", here) is True
    assert store.user_for_token(here)["id"] == user["id"]          # this device stays signed in
    assert store.user_for_token(elsewhere) is None                 # every other one is signed out
    assert store.authenticate("me@example.test", "new-password-1")["id"] == user["id"]
    assert store.authenticate("me@example.test", "old-password-1") is None


@pytest.mark.parametrize("new,message", [("short", "8 到 128"), ("x" * 129, "8 到 128"), ("old-password-1", "不能和现在的一样")])
def test_an_unusable_new_password_is_refused_before_anything_changes(store, new, message):
    user = store.register("me@example.test", "old-password-1")
    with pytest.raises(ValueError, match=message):
        store.change_password(user["id"], "old-password-1", new)
    assert store.authenticate("me@example.test", "old-password-1")


def test_the_page_changes_the_password_and_says_why_when_it_cannot(user, monkeypatch):
    accounts = AccountStore(user.root / "accounts.db")
    account = accounts.register("test@example.com", "old-password-1")
    monkeypatch.setattr(backend, "accounts", accounts)
    monkeypatch.setattr(user, "user_id", account["id"])
    with TestClient(backend.app) as client:
        change = lambda current, new, **kw: client.post("/api/account/password", json={"current": current, "new": new}, headers=ORIGIN, **kw)
        wrong = change("nope-nope-1", "new-password-1")
        assert wrong.status_code == 403 and wrong.json()["detail"] == "现在的密码不对"
        assert change("old-password-1", "short").status_code == 400
        assert change("old-password-1", "new-password-1").json() == {"ok": True}
        assert client.post("/api/account/password", json={"current": "a", "new": "b"}).status_code == 403   # cross-site
    assert accounts.authenticate("test@example.com", "new-password-1")
    outcomes = [entry["outcome"] for entry in user.action_log.recent() if entry["tool"] == "change_password"]
    assert outcomes == ["executed", "refused"]


def test_password_attempts_count_against_the_sign_in_limit(user, monkeypatch):
    monkeypatch.setitem(ops.LIMITS, "auth", (1, 60))
    with TestClient(backend.app) as client:
        client.post("/api/account/password", json={"current": "a", "new": "b-password"}, headers=ORIGIN)
        assert client.post("/api/account/password", json={"current": "a", "new": "b-password"}, headers=ORIGIN).status_code == 429
