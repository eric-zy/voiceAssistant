"""Switching to the backup model: only before the first word, quickly, and not again and again into a dead service."""
import asyncio
import json

import httpx
import pytest

import app
import llm_client
from llm_client import ProviderUnavailable, TextDelta, ToolCallStarted
import llm_router
from llm_router import Policy, Provider, Router
import ops

PRIMARY = Provider("deepseek", "https://primary.test", "k1", "deepseek-flash", {"thinking": {"type": "disabled"}})
BACKUP = Provider("backup", "https://backup.test", "k2", "backup-model")
FAST = Policy(first_event=0.2, stall=0.3, retry_delays=(1, 2))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def scripted(**scripts):
    """A stand-in for stream_chat: each provider (by model) plays its script of waits, events and errors."""
    calls = []

    async def stream(base_url, api_key, model, messages, retry_delays=None, extra_body=None, on_usage=None, **options):
        calls.append({"model": model, "retry_delays": retry_delays, "extra_body": extra_body, "options": options})
        try:
            for step in scripts[model]:
                if isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                elif isinstance(step, BaseException):
                    raise step
                else:
                    yield step
        finally:
            if on_usage:
                on_usage({"model": model})
    return stream, calls


def run(router, policy=FAST, **options):
    async def go():
        return [event async for event in router.chat([{"role": "user", "content": "你好"}], policy, **options)]
    return asyncio.run(go())


def make(stream, providers=(PRIMARY, BACKUP), clock=None):
    metrics = ops.Metrics()
    return Router(lambda: list(providers), metrics, stream, clock or Clock()), metrics


def test_a_silent_primary_hands_over_to_the_backup():
    stream, calls = scripted(**{"deepseek-flash": [5, TextDelta("太迟")], "backup-model": [TextDelta("备用"), TextDelta("回答")]})
    router, metrics = make(stream)
    assert run(router) == [TextDelta("备用"), TextDelta("回答")]
    assert metrics.counts == {"llm_failover_deepseek": 1, "llm_answered_by_backup": 1}


@pytest.mark.parametrize("status", [429, 500, 503, 401, 402])
def test_a_refusing_primary_hands_over_at_once_without_retrying(status):
    stream, calls = scripted(**{"deepseek-flash": [ProviderUnavailable(f"HTTP {status}")], "backup-model": [TextDelta("好")]})
    router, _ = make(stream)
    assert run(router) == [TextDelta("好")]
    # The primary must not sit through its 1 s + 2 s retries while a backup waits; the backup, being last, may retry.
    assert calls[0]["retry_delays"] == () and calls[1]["retry_delays"] == (1, 2)


def test_a_request_that_is_wrong_everywhere_is_not_sent_to_the_backup():
    stream, calls = scripted(**{"deepseek-flash": [RuntimeError("HTTP 400")], "backup-model": [TextDelta("不该")]})
    router, _ = make(stream)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        run(router)
    assert [call["model"] for call in calls] == ["deepseek-flash"]


def test_once_words_are_out_a_stall_is_reported_never_answered_twice():
    stream, calls = scripted(**{"deepseek-flash": [TextDelta("我想"), 5, TextDelta("太迟")], "backup-model": [TextDelta("另一个回答")]})
    router, _ = make(stream)
    seen = []

    async def go():
        async for event in router.chat([], FAST):
            seen.append(event)
    with pytest.raises(RuntimeError, match="回复中断"):
        asyncio.run(go())
    assert seen == [TextDelta("我想")] and [call["model"] for call in calls] == ["deepseek-flash"]


def test_a_tool_call_announcement_counts_as_the_reply_starting():
    stream, calls = scripted(**{"deepseek-flash": [ToolCallStarted("get_weather"), ProviderUnavailable("断了")],
                                "backup-model": [TextDelta("不该")]})
    router, _ = make(stream)
    with pytest.raises(ProviderUnavailable):
        run(router)
    assert [call["model"] for call in calls] == ["deepseek-flash"]


def test_the_last_provider_is_not_cut_short_by_the_first_word_timeout():
    stream, _ = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [0.5, TextDelta("慢但到了")]})
    router, _ = make(stream)
    assert run(router) == [TextDelta("慢但到了")]


def test_with_no_backup_the_primary_keeps_its_retries_and_its_own_message():
    stream, calls = scripted(**{"deepseek-flash": [ProviderUnavailable("账户余额不足（HTTP 402）")]})
    router, _ = make(stream, providers=(PRIMARY,))
    with pytest.raises(ProviderUnavailable, match="余额不足"):
        run(router)
    assert calls[0]["retry_delays"] == (1, 2)


def test_when_both_fail_the_user_is_told_plainly():
    stream, _ = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [ProviderUnavailable("502")]})
    router, _ = make(stream)
    with pytest.raises(RuntimeError, match="模型服务暂时不可用"):
        run(router)


def test_a_failing_primary_is_paused_then_tried_again():
    clock = Clock()
    stream, calls = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [TextDelta("好")]})
    router, _ = make(stream, clock=clock)
    for _ in range(llm_router.FAILURES_TO_OPEN):
        run(router)
    assert router.status()["deepseek"]["state"] == "open"
    calls.clear()
    run(router)
    assert [call["model"] for call in calls] == ["backup-model"]  # paused: no waiting on it
    clock.now += llm_router.COOLDOWN_SECONDS
    assert router.status()["deepseek"]["state"] == "half-open"
    calls.clear()
    run(router)  # one try; it fails, so it is paused again at once
    assert [call["model"] for call in calls] == ["deepseek-flash", "backup-model"]
    assert router.status()["deepseek"]["state"] == "open"


def test_a_recovered_primary_takes_over_again():
    clock = Clock()
    scripts = {"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [TextDelta("备用")]}
    stream, calls = scripted(**scripts)
    router, _ = make(stream, clock=clock)
    for _ in range(llm_router.FAILURES_TO_OPEN):
        run(router)
    clock.now += llm_router.COOLDOWN_SECONDS
    scripts["deepseek-flash"][:] = [TextDelta("主模型回来了")]
    assert run(router) == [TextDelta("主模型回来了")]
    assert router.status()["deepseek"]["state"] == "closed"


def test_if_every_provider_is_paused_the_last_is_still_tried():
    clock = Clock()
    stream, calls = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [ProviderUnavailable("503")]})
    router, _ = make(stream, clock=clock)
    for _ in range(llm_router.FAILURES_TO_OPEN):
        with pytest.raises(RuntimeError):
            run(router)
    calls.clear()
    with pytest.raises(RuntimeError, match="暂时不可用"):
        run(router)
    assert [call["model"] for call in calls] == ["backup-model"]


def test_spend_is_billed_to_the_provider_that_answered():
    stream, _ = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [TextDelta("好")]})
    router, _ = make(stream)
    billed = []
    run(router, on_usage=lambda provider, used: billed.append(provider.model))
    assert billed == ["deepseek-flash", "backup-model"]  # the refused primary reports too; stream_chat skips unbilled ones


def test_an_agents_stronger_model_replaces_only_the_primarys():
    stream, calls = scripted(**{"deepseek-v4-pro": [ProviderUnavailable("503")], "backup-model": [TextDelta("好")]})
    router, _ = make(stream)
    run(router, primary_model="deepseek-v4-pro")
    assert [call["model"] for call in calls] == ["deepseek-v4-pro", "backup-model"]


def test_options_and_each_providers_own_fields_are_passed_through():
    stream, calls = scripted(**{"deepseek-flash": [ProviderUnavailable("503")], "backup-model": [TextDelta("好")]})
    router, _ = make(stream)
    run(router, max_tokens=99, tools=["t"])
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}} and calls[1]["extra_body"] == {}
    assert all(call["options"] == {"max_tokens": 99, "tools": ["t"]} for call in calls)


def test_an_unconfigured_backup_is_left_out():
    router, _ = make(scripted()[0], providers=(PRIMARY, Provider("backup", "", "", "")))
    assert [provider.name for provider in router.candidates()] == ["deepseek"]


# Through the real HTTP client: what actually goes over the wire.

def serve_hosts(monkeypatch, responses):
    sent = []

    def handler(request):
        sent.append(request)
        return responses[request.url.host]()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return sent


OK = 'data: {"choices":[{"delta":{"content":"好"}}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'


def test_over_http_a_503_moves_to_the_backup_with_one_request_each(monkeypatch):
    sent = serve_hosts(monkeypatch, {"primary.test": lambda: httpx.Response(503), "backup.test": lambda: httpx.Response(200, text=OK)})
    router = Router(lambda: [PRIMARY, BACKUP])
    assert run(router, policy=llm_router.VOICE) == [TextDelta("好")]
    assert [request.url.host for request in sent] == ["primary.test", "backup.test"]
    assert "thinking" in json.loads(sent[0].content) and "thinking" not in json.loads(sent[1].content)
    assert json.loads(sent[1].content)["model"] == "backup-model"


def test_over_http_an_unreachable_primary_moves_to_the_backup(monkeypatch):
    def refuse():
        raise httpx.ConnectError("refused")
    serve_hosts(monkeypatch, {"primary.test": refuse, "backup.test": lambda: httpx.Response(200, text=OK)})
    assert run(Router(lambda: [PRIMARY, BACKUP]), policy=llm_router.VOICE) == [TextDelta("好")]


def test_an_unreachable_service_alone_says_so_in_words(monkeypatch):
    def refuse():
        raise httpx.ConnectError("refused")
    serve_hosts(monkeypatch, {"primary.test": refuse})

    async def go():
        return [event async for event in llm_client.stream_chat("https://primary.test", "k", "m", [], retry_delays=())]
    with pytest.raises(ProviderUnavailable, match="连不上模型服务"):
        asyncio.run(go())


def test_background_agents_bill_the_backup_model_they_were_answered_by(monkeypatch):
    serve_hosts(monkeypatch, {"api.deepseek.com": lambda: httpx.Response(503), "backup.test": lambda: httpx.Response(200, text=OK)})
    monkeypatch.setattr(app, "DEEPSEEK_URL", "https://api.deepseek.com")
    monkeypatch.setattr(app, "API_KEY", "k1")
    monkeypatch.setattr(app, "BACKUP_URL", "https://backup.test")
    monkeypatch.setattr(app, "BACKUP_KEY", "k2")
    monkeypatch.setattr(app, "BACKUP_MODEL", "backup-model")
    monkeypatch.setattr(llm_router, "BACKGROUND", Policy(first_event=2, stall=2, retry_delays=()))
    billed = []

    async def go():
        return [event async for event in app.agent_chat("deepseek-flash", [], on_usage=lambda used, model: billed.append((model, used)))]
    assert asyncio.run(go()) == [TextDelta("好")]
    assert billed == [("backup-model", {"prompt_tokens": 3, "completion_tokens": 1})]
    assert app.llm.status()["deepseek"]["state"] == "closed" and app.metrics.counts["llm_answered_by_backup"] == 1


def test_abandoning_a_reply_midway_closes_the_providers_stream():
    """Barge-in: the listener stops reading; the HTTP stream underneath must be closed, not left running."""
    stream, _ = scripted(**{"deepseek-flash": [TextDelta("一"), TextDelta("二"), TextDelta("三")]})
    router, _ = make(stream)
    closed = []

    async def go():
        reply = router.chat([], FAST, on_usage=lambda provider, used: closed.append(provider.name))
        async for event in reply:
            break
        await reply.aclose()
    asyncio.run(go())
    assert closed == ["deepseek"]


def test_a_spoken_turn_is_answered_by_the_backup_when_deepseek_is_down(monkeypatch):
    from fastapi.testclient import TestClient
    serve_hosts(monkeypatch, {"api.deepseek.com": lambda: httpx.Response(503), "backup.test": lambda: httpx.Response(
        200, text='data: {"choices":[{"delta":{"content":"备用模型在。"}}]}\n\ndata: [DONE]\n\n')})
    monkeypatch.setattr(app, "DEEPSEEK_URL", "https://api.deepseek.com")
    monkeypatch.setattr(app, "API_KEY", "test-key")
    monkeypatch.setattr(app, "BACKUP_URL", "https://backup.test")
    monkeypatch.setattr(app, "BACKUP_KEY", "backup-key")
    monkeypatch.setattr(app, "BACKUP_MODEL", "backup-model")

    async def silent(text):
        return b"fake-wav"
    monkeypatch.setattr(app, "synthesize", silent)
    with TestClient(app.app) as client, client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
        socket.send_json({"type": "text", "request_id": 1, "text": "你好"})
        tokens = []
        while (message := socket.receive_json())["type"] != "done":
            assert message["type"] != "error", message
            if message["type"] == "token":
                tokens.append(message["text"])
    assert "".join(tokens) == "备用模型在。"
    spend = app.usage.LEDGER.summary(1)
    assert [row["agent"] for row in spend["agents"]] == ["main"] and spend["today"]["incomplete"] == 1  # billed, counts missing
