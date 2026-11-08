import json

import pytest
from fastapi.testclient import TestClient

import accounts
import app as backend
import manage
from accounts import AccountStore, check_password, hash_password

LOCAL = {"origin": "http://127.0.0.1:8765", "host": "127.0.0.1:8765"}


def test_passwords_are_salted_and_checked(tmp_path):
    first, second = hash_password("correct horse"), hash_password("correct horse")
    assert first != second and first.startswith("scrypt$")
    assert check_password("correct horse", first) and not check_password("wrong horse", first)
    assert not check_password("anything", "not a hash")


def test_register_then_sign_in(tmp_path):
    store = AccountStore(tmp_path / "a.db")
    user = store.register(" You@Example.com ", "password123")
    assert user["email"] == "you@example.com"
    assert store.authenticate("YOU@example.com", "password123")["id"] == user["id"]
    assert store.authenticate("you@example.com", "wrong-pass") is None
    assert store.authenticate("nobody@example.com", "password123") is None


@pytest.mark.parametrize("email,password,hint", [("bad", "password123", "邮箱"), ("a@b.co", "short", "8"), ("a@b.co", "x" * 129, "8")])
def test_bad_registrations_are_refused(tmp_path, email, password, hint):
    with pytest.raises(ValueError, match=hint):
        AccountStore(tmp_path / "a.db").register(email, password)


def test_an_email_registers_once(tmp_path):
    store = AccountStore(tmp_path / "a.db")
    store.register("a@b.co", "password123")
    with pytest.raises(ValueError, match="已经注册"):
        store.register("A@B.co", "password456")


def test_sessions_resolve_expire_and_end(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "a.db")
    user = store.register("a@b.co", "password123")
    token = store.start_session(user["id"])
    assert store.user_for_token(token)["email"] == "a@b.co"
    assert store.user_for_token("forged") is None and store.user_for_token(None) is None
    store.end_session(token)
    assert store.user_for_token(token) is None
    late = store.start_session(user["id"])
    monkeypatch.setattr(accounts.time, "time", lambda: 10 ** 12)
    assert store.user_for_token(late) is None


def test_only_a_hash_of_the_token_is_stored(tmp_path):
    import sqlite3
    store = AccountStore(tmp_path / "a.db")
    token = store.start_session(store.register("a@b.co", "password123")["id"])
    stored = sqlite3.connect(store.path).execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert token not in stored and stored == accounts.token_hash(token)


def test_repeated_wrong_passwords_are_throttled(tmp_path):
    store = AccountStore(tmp_path / "a.db")
    store.register("a@b.co", "password123")
    for _ in range(10):
        assert store.authenticate("a@b.co", "wrong-pass") is None
    with pytest.raises(PermissionError):
        store.authenticate("a@b.co", "password123")


@pytest.fixture
def real_sessions(monkeypatch):
    """Sign-in tests use real cookies, not the test user."""
    monkeypatch.setattr(backend, "session_user", backend.default_session_user)


def sign_up(client, email):
    response = client.post("/api/register", headers=LOCAL, json={"email": email, "password": "password123"})
    assert response.status_code == 200, response.text
    return response.cookies["session"]


def test_the_cookie_is_http_only_and_same_site(real_sessions):
    with TestClient(backend.app) as client:
        response = client.post("/api/register", headers=LOCAL, json={"email": "a@b.co", "password": "password123"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_without_a_session_the_api_and_socket_refuse(real_sessions):
    with TestClient(backend.app) as client:
        assert client.get("/api/memories", headers=LOCAL).status_code == 401
        assert client.get("/api/status", headers=LOCAL).status_code == 401
        assert "注册" in client.get("/", headers=LOCAL).text
        with client.websocket_connect("/ws", headers={"origin": LOCAL["origin"]}) as socket:
            with pytest.raises(Exception) as closed:
                socket.receive_json()
        assert getattr(closed.value, "code", None) == 4401


def test_sign_in_sign_out_cycle(real_sessions):
    with TestClient(backend.app) as client:
        sign_up(client, "a@b.co")
        assert client.get("/api/me", headers=LOCAL).json()["email"] == "a@b.co"
        assert "开始说话" in client.get("/", headers=LOCAL).text
        client.post("/api/logout", headers=LOCAL)
        assert client.get("/api/me", headers=LOCAL).status_code == 401
        assert client.post("/api/login", headers=LOCAL, json={"email": "a@b.co", "password": "wrong-pass"}).status_code == 401
        assert client.post("/api/login", headers=LOCAL, json={"email": "a@b.co", "password": "password123"}).status_code == 200
        assert client.get("/api/me", headers=LOCAL).status_code == 200


def test_another_site_cannot_sign_in_or_register(real_sessions):
    foreign = {"origin": "https://evil.example.com", "host": "127.0.0.1:8765"}
    with TestClient(backend.app) as client:
        assert client.post("/api/register", headers=foreign, json={"email": "a@b.co", "password": "password123"}).status_code == 403


def test_registration_can_be_closed(real_sessions, monkeypatch):
    monkeypatch.setattr(backend, "OPEN_REGISTRATION", False)
    with TestClient(backend.app) as client:
        assert client.post("/api/register", headers=LOCAL, json={"email": "a@b.co", "password": "password123"}).status_code == 403


def test_server_details_are_for_administrators_only(real_sessions):
    with TestClient(backend.app) as client:
        sign_up(client, "a@b.co")
        status = client.get("/api/status", headers=LOCAL).json()
    assert "key_fingerprint" not in status and status["email"] == "a@b.co"


def test_import_copies_single_user_data_without_moving_or_overwriting(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "agent.db").write_bytes(b"db")
    (legacy / "knowledge").mkdir()
    (legacy / "knowledge" / "a.md").write_text("笔记", encoding="utf-8")
    (legacy / "voices").mkdir()
    (legacy / "voices" / "settings.json").write_text(json.dumps({"provider": "cosyvoice", "profile_id": "x"}), encoding="utf-8")
    store = AccountStore(tmp_path / "accounts.db")
    user = store.register("a@b.co", "password123")
    copied = manage.import_legacy(store, "a@b.co", legacy, tmp_path / "users")
    target = tmp_path / "users" / str(user["id"])
    assert (target / "agent.db").read_bytes() == b"db" and (target / "knowledge" / "a.md").exists()
    assert json.loads((target / "settings.json").read_text(encoding="utf-8"))["provider"] == "cosyvoice"
    assert (legacy / "data" / "agent.db").exists() and "音色选择" in copied


def legacy_with_memory(tmp_path):
    from memory_store import MemoryStore
    legacy = tmp_path / "legacy"
    (legacy / "data").mkdir(parents=True)
    MemoryStore(legacy / "data" / "agent.db").add("旧的记忆")
    (legacy / "voices" / "old-profile").mkdir(parents=True)
    (legacy / "voices" / "old-profile" / "profile.json").write_text("{}", encoding="utf-8")
    (legacy / "knowledge").mkdir()
    (legacy / "knowledge" / "旧.md").write_text("旧笔记", encoding="utf-8")
    (legacy / "knowledge" / "同名.md").write_text("旧版本", encoding="utf-8")
    return legacy


def test_an_empty_database_made_by_signing_in_does_not_block_the_import(tmp_path):
    from memory_store import MemoryStore
    legacy = legacy_with_memory(tmp_path)
    store = AccountStore(tmp_path / "accounts.db")
    user = store.register("a@b.co", "password123")
    target = tmp_path / "users" / str(user["id"])
    MemoryStore(target / "agent.db").recent()
    manage.import_legacy(store, "a@b.co", legacy, tmp_path / "users")
    assert [m["content"] for m in MemoryStore(target / "agent.db").recent()] == ["旧的记忆"]
    assert (target / "agent.db.before-import").exists()


def test_an_account_with_its_own_memories_keeps_them(tmp_path):
    from memory_store import MemoryStore
    legacy = legacy_with_memory(tmp_path)
    store = AccountStore(tmp_path / "accounts.db")
    user = store.register("a@b.co", "password123")
    target = tmp_path / "users" / str(user["id"])
    MemoryStore(target / "agent.db").add("新的记忆")
    manage.import_legacy(store, "a@b.co", legacy, tmp_path / "users")
    assert [m["content"] for m in MemoryStore(target / "agent.db").recent()] == ["新的记忆"]


def test_folders_merge_without_replacing_what_the_account_has(tmp_path):
    legacy = legacy_with_memory(tmp_path)
    store = AccountStore(tmp_path / "accounts.db")
    user = store.register("a@b.co", "password123")
    target = tmp_path / "users" / str(user["id"])
    (target / "voices" / "new-profile").mkdir(parents=True)
    (target / "knowledge").mkdir()
    (target / "knowledge" / "同名.md").write_text("账号里的版本", encoding="utf-8")
    manage.import_legacy(store, "a@b.co", legacy, tmp_path / "users")
    assert {p.name for p in (target / "voices").iterdir()} >= {"new-profile", "old-profile"}
    assert (target / "knowledge" / "旧.md").exists()
    assert (target / "knowledge" / "同名.md").read_text(encoding="utf-8") == "账号里的版本"
