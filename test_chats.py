import json

import httpx
from fastapi.testclient import TestClient
import pytest

import app as backend

ORIGIN = {"origin": "http://127.0.0.1:8765"}


def test_conversations_are_listed_newest_first_and_found_by_what_was_said(user):
    store = user.chats
    weather = store.create("今天   天气怎么样？这是一个很长很长很长很长很长很长很长的问题")
    store.add_turn(weather, "今天天气怎么样", "北京晴，15 度")
    percent = store.create("100% 完成了吗")
    store.add_turn(percent, "100% 完成了吗", "是的")
    assert [chat["id"] for chat in store.list()] == [percent, weather]
    assert len(store.get(weather)["title"]) == 30 and "  " not in store.get(weather)["title"]
    assert [chat["id"] for chat in store.list("晴")] == [weather]           # found in the answer
    assert [chat["id"] for chat in store.list("%")] == [percent]            # a typed % is not a wildcard
    assert store.list("_") == []
    assert store.get(weather)["messages"] == [
        {"role": "user", "text": "今天天气怎么样", "at": store.get(weather)["messages"][0]["at"]},
        {"role": "assistant", "text": "北京晴，15 度", "at": store.get(weather)["messages"][1]["at"]}]
    assert store.rename(weather, "天气") and store.get(weather)["title"] == "天气"
    assert store.delete(weather) and store.get(weather) is None and not store.delete(weather)
    assert not store.add_turn(weather, "x", "y")


def sse(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"


@pytest.fixture
def model(user, monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, text=sse(f"第 {len([m for m in body['messages'] if m['role'] == 'user'])} 个回答。"))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")

    async def synthesize(text, choice=None):
        return b"ID3fake"
    monkeypatch.setattr(backend, "synthesize", synthesize)
    return requests


def until(socket, kind):
    seen = []
    while True:
        message = socket.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return message, seen


def ask(socket, rid, text):
    socket.send_json({"type": "text", "request_id": rid, "text": text})
    _, seen = until(socket, "done")
    return next((m for m in seen if m["type"] == "chat_saved"), None)


def test_each_turn_is_saved_into_one_conversation_and_a_new_one_starts_on_request(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        first = ask(socket, 1, "今天天气怎么样")
        second = ask(socket, 2, "那明天呢")
        assert first["chat_id"] == second["chat_id"] and second["turns"] == 2 and first["title"] == "今天天气怎么样"
        socket.send_json({"type": "chat_new"})
        assert until(socket, "chat_started")[0]["chat_id"] is None
        third = ask(socket, 3, "讲个笑话")
        assert third["chat_id"] != first["chat_id"] and third["turns"] == 1
        assert [m["role"] for m in model[-1]["messages"]][1:] == ["user"]    # the new one starts with no history
    chats = user.chats.list()
    assert [chat["title"] for chat in chats] == ["讲个笑话", "今天天气怎么样"]
    assert [m["text"] for m in user.chats.get(first["chat_id"])["messages"]] == ["今天天气怎么样", "第 1 个回答。", "那明天呢", "第 2 个回答。"]


def test_opening_an_earlier_conversation_shows_it_and_continues_with_its_context(user, model):
    earlier = user.chats.create("周末计划")
    user.chats.add_turn(earlier, "周末去哪玩", "可以去爬山。")
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "chat_open", "chat_id": earlier})
        opened, _ = until(socket, "chat_started")
        assert opened["chat_id"] == earlier and opened["title"] == "周末计划"
        assert [m["text"] for m in opened["messages"]] == ["周末去哪玩", "可以去爬山。"]
        saved = ask(socket, 2, "那要带什么")
        assert saved["chat_id"] == earlier and saved["turns"] == 2
        said = [m["content"] for m in model[-1]["messages"][1:]]
        assert said == ["周末去哪玩", "可以去爬山。", "那要带什么"]
        for bad in (999, "1", None):
            socket.send_json({"type": "chat_open", "chat_id": bad})
            assert until(socket, "error")[0]["text"] == "没有这次对话"


def test_a_deleted_conversation_is_not_added_to_again(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        first = ask(socket, 1, "第一句")
        user.chats.delete(first["chat_id"])
        again = ask(socket, 2, "第二句")
        assert again["chat_id"] != first["chat_id"] and again["turns"] == 1


def test_english_practice_turns_are_not_saved_as_assistant_conversations(user, model):
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, "level": "beginner"})
        until(socket, "done")
        socket.send_json({"type": "text", "request_id": 2, "text": "Hello there"})
        _, seen = until(socket, "done")
        assert not any(m["type"] == "chat_saved" for m in seen)
    assert user.chats.list() == []


def test_the_page_lists_reads_renames_and_deletes_conversations(user):
    chat = user.chats.create("学习计划")
    user.chats.add_turn(chat, "帮我做个学习计划", "好的，先……")
    with TestClient(backend.app) as client:
        assert [c["title"] for c in client.get("/api/chats").json()["chats"]] == ["学习计划"]
        assert client.get("/api/chats?q=先").json()["chats"][0]["id"] == chat
        assert client.get("/api/chats?q=没有的").json()["chats"] == []
        assert len(client.get(f"/api/chats/{chat}").json()["messages"]) == 2
        assert client.post(f"/api/chats/{chat}/title", json={"title": "  新标题 "}, headers=ORIGIN).json() == {"ok": True}
        assert client.post(f"/api/chats/{chat}/title", json={"title": " "}, headers=ORIGIN).status_code == 400
        assert client.get(f"/api/chats/{chat}").json()["title"] == "新标题"
        assert client.delete(f"/api/chats/{chat}").status_code == 403
        assert client.delete(f"/api/chats/{chat}", headers=ORIGIN).json() == {"ok": True}
        assert client.get(f"/api/chats/{chat}").status_code == 404
        assert client.delete(f"/api/chats/{chat}", headers=ORIGIN).status_code == 404
