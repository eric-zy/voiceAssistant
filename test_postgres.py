"""PostgreSQL-only checks: row-level security keeps users apart, and SQLite data moves over intact.

Skipped unless VOICE_TEST_DATABASE_URL points at a test database (see conftest.py). The SQL translation tests
need no database and always run.
"""
import contextlib
from datetime import datetime, timedelta
import sqlite3

import pytest

from accounts import AccountStore
import db
from memory_store import ChatStore, EnglishStore, LookupCache, MemoryStore, ReminderStore
import pg_migrate
import usage


def test_placeholders_and_literal_percent_are_translated():
    sql, returns_id = db.translate("INSERT INTO memories (content) VALUES (?)", True)
    assert sql == "INSERT INTO memories (content) VALUES (%s) RETURNING id" and returns_id
    assert db.translate("SELECT 1 WHERE a LIKE '5%' AND b = ?", False)[0] == "SELECT 1 WHERE a LIKE '5%%' AND b = %s"
    assert db.translate("SELECT '5%'", False, False)[0] == "SELECT '5%'"


def test_a_users_upsert_key_includes_the_user_but_shared_tables_do_not_change():
    sql, returns_id = db.translate("INSERT INTO english_cache (key) VALUES (?) ON CONFLICT (key) DO NOTHING", True)
    assert "ON CONFLICT (user_id, key)" in sql and not returns_id
    assert "ON CONFLICT (key)" in db.translate("INSERT INTO x (key) VALUES (?) ON CONFLICT (key) DO NOTHING", False)[0]


@pytest.fixture
def pg(postgres):
    if postgres is None:
        pytest.skip("VOICE_TEST_DATABASE_URL not set")
    return postgres


def test_each_user_sees_only_their_own_rows(pg):
    alice, bob = MemoryStore(db.Scope(pg, 1)), MemoryStore(db.Scope(pg, 2))
    alice.add("Alice 不吃香菜")
    bob.add("Bob 喜欢咖啡")
    assert [m["content"] for m in alice.recent()] == ["Alice 不吃香菜"]
    assert [m["content"] for m in bob.recent()] == ["Bob 喜欢咖啡"]
    # Even a query with no user filter at all returns only the caller's rows.
    with contextlib.closing(alice.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    # And one user cannot change or remove another's row by its id.
    bob_id = bob.recent()[0]["id"]
    assert not alice.update(bob_id, "改掉") and alice.forget(bob_id) is None
    assert bob.recent()[0]["content"] == "Bob 喜欢咖啡"


def test_a_connection_naming_no_user_sees_and_writes_nothing(pg):
    MemoryStore(db.Scope(pg, 1)).add("秘密")
    with contextlib.closing(db.PgConnection(db.Scope(pg))) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        with pytest.raises(db.Error):  # refused: no user means no row passes the policy (and user_id would be NULL)
            connection.execute("INSERT INTO memories (content, created_at) VALUES (?, ?)", ("x", "now"))


def test_a_row_cannot_be_written_for_someone_else(pg):
    with contextlib.closing(db.PgConnection(db.Scope(pg, 1))) as connection:
        with pytest.raises(db.Error, match="row-level security"):
            connection.execute("INSERT INTO memories (user_id, content, created_at) VALUES (?, ?, ?)", (2, "伪造", "now"))


def test_the_previous_users_scope_does_not_leak_into_the_next_checkout(pg):
    MemoryStore(db.Scope(pg, 1)).add("一号的")
    for _ in range(3):  # the pool hands the same connections round again
        assert MemoryStore(db.Scope(pg, 2)).recent() == []
        with contextlib.closing(db.PgConnection(db.Scope(pg))) as connection:
            assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_the_same_word_and_cache_key_are_separate_per_user(pg):
    alice, bob = EnglishStore(db.Scope(pg, 1)), EnglishStore(db.Scope(pg, 2))
    first = alice.add_item("word", "Apple", meaning="苹果")
    assert alice.add_item("word", "apple.", meaning="苹果（再次）") == first
    bob.add_item("word", "apple", meaning="bob 的")
    assert alice.items()[0]["seen_count"] == 2 and bob.items()[0]["seen_count"] == 1
    LookupCache(db.Scope(pg, 1)).set("zh:k", {"text": "一"})
    LookupCache(db.Scope(pg, 1)).set("zh:k", {"text": "二"})
    assert LookupCache(db.Scope(pg, 1)).get("zh:k") == {"text": "二"}
    assert LookupCache(db.Scope(pg, 2)).get("zh:k") is None


def test_only_one_page_claims_a_due_reminder(pg):
    reminders = ReminderStore(db.Scope(pg, 1))
    reminders.add("喝水", datetime.now() - timedelta(minutes=1))
    assert reminders.claim_due() is not None and reminders.claim_due() is None


def test_spend_summary_adds_up_across_users(pg):
    ledger = usage.UsageLedger(db.Scope(pg))
    ledger.record_search(1)
    ledger.record_chat(2, "deepseek-flash", None)
    everyone = ledger.summary()
    assert everyone["today"]["calls"] == 2 and everyone["today"]["searches"] == 1 and everyone["today"]["incomplete"] == 1
    assert ledger.summary(1)["today"]["calls"] == 1


def make_sqlite_data(data):
    accounts = AccountStore(data / "accounts.db")
    first, second = accounts.register("a@example.com", "password-a1"), accounts.register("b@example.com", "password-b2")
    token = accounts.start_session(first["id"])
    usage.UsageLedger(data / "usage.db").record_search(first["id"])
    for user, word in ((first, "apple"), (second, "banana")):
        database = data / "users" / str(user["id"]) / "agent.db"
        MemoryStore(database).add(f"{word} 的记忆")
        ReminderStore(database).add(f"{word} 提醒", datetime(2030, 1, 1, 8), "daily")
        chats = ChatStore(database)
        chats.create("占位")  # so the real chat's old id is 2, not the 1 PostgreSQL will reuse
        chat = chats.create(f"{word} 对话")
        chats.add_turn(chat, "你好", "你好呀")
        english = EnglishStore(database)
        session = english.start("B1", "food")
        english.add_item("word", word, meaning="水果", session_id=session)
        item = english.add_item("phrase", f"an {word} a day", session_id=session)
        english.create_quiz([{"id": 1, "item_id": item, "type": "spell"}])
    return first, second, token


def test_sqlite_data_moves_over_with_links_intact(pg, tmp_path):
    first, second, token = make_sqlite_data(tmp_path)
    assert pg_migrate.migrate(tmp_path, pg, report=lambda line: None) == 2
    accounts = AccountStore(db.Scope(pg))
    assert accounts.authenticate("b@example.com", "password-b2")["id"] == second["id"]
    assert accounts.user_for_token(token)["id"] == first["id"]
    assert accounts.register("c@example.com", "password-c3")["id"] == second["id"] + 1  # the id sequence moved on
    assert usage.UsageLedger(db.Scope(pg)).summary(first["id"])["today"]["searches"] == 1
    for user, word in ((first, "apple"), (second, "banana")):
        scope = db.Scope(pg, user["id"])
        assert [m["content"] for m in MemoryStore(scope).recent()] == [f"{word} 的记忆"]
        assert ReminderStore(scope).pending()[0]["repeat"] == "daily"
        chats = ChatStore(scope).list()
        real = next(chat for chat in chats if chat["title"] == f"{word} 对话")
        assert [m["text"] for m in ChatStore(scope).get(real["id"])["messages"]] == ["你好", "你好呀"]
        english = EnglishStore(scope)
        items = {item["kind"]: item for item in english.items()}
        assert items["word"]["session_id"] == items["phrase"]["session_id"] is not None
        with contextlib.closing(english.connect()) as connection:
            quiz_id = connection.execute("SELECT id FROM english_quizzes").fetchone()[0]
        assert english.quiz(quiz_id)["questions"][0]["item_id"] == items["phrase"]["id"]


def test_migrating_twice_is_refused(pg, tmp_path):
    make_sqlite_data(tmp_path)
    pg_migrate.migrate(tmp_path, pg, report=lambda line: None)
    with pytest.raises(SystemExit, match="已经有账号"):
        pg_migrate.migrate(tmp_path, pg, report=lambda line: None)
    with contextlib.closing(sqlite3.connect(tmp_path / "accounts.db")) as source:  # the SQLite files are untouched
        assert source.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_import_legacy_puts_the_old_single_user_data_into_postgres(pg, tmp_path):
    import manage
    (tmp_path / "legacy" / "data").mkdir(parents=True)
    MemoryStore(tmp_path / "legacy" / "data" / "agent.db").add("旧的记忆")
    store = AccountStore(db.Scope(pg))
    user = store.register("old@example.com", "password-old1")
    copied = manage.import_legacy(store, "old@example.com", tmp_path / "legacy", tmp_path / "users")
    assert "记忆、提醒和操作日志" in copied
    assert [m["content"] for m in MemoryStore(db.Scope(pg, user["id"])).recent()] == ["旧的记忆"]
    assert not (tmp_path / "users" / str(user["id"]) / "agent.db").exists()  # nothing written to a SQLite file
    manage.import_legacy(store, "old@example.com", tmp_path / "legacy", tmp_path / "users")  # a second run keeps, not duplicates
    assert len(MemoryStore(db.Scope(pg, user["id"])).recent()) == 1
