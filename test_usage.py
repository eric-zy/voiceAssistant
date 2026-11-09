import asyncio
from datetime import date, datetime

import httpx
from fastapi.testclient import TestClient
import pytest

import app as backend
import llm_client
import tools
import usage
from usage import BEIJING, UsageLedger, chat_cost, is_peak


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=BEIJING)  # 2026-11-09 is a Monday


@pytest.mark.parametrize("moment,peak", [
    (at(9, 9), True), (at(9, 11, 59), True), (at(9, 12), False), (at(9, 13, 59), False),
    (at(9, 14), True), (at(9, 17, 59), True), (at(9, 18), False), (at(9, 8, 59), False),
    (at(13, 10), True), (at(14, 10), False), (at(15, 15), False)])
def test_peak_is_weekday_office_hours_in_beijing(moment, peak):
    assert is_peak(moment) is peak


def test_the_same_moment_in_another_timezone_is_judged_in_beijing():
    # 02:00 UTC on a Monday is 10:00 in Beijing.
    assert is_peak(datetime.fromisoformat("2026-11-09T02:00:00+00:00"))


def test_cost_follows_the_price_table_and_off_peak_is_half():
    assert chat_cost("deepseek-flash", 0, 1_000_000, 0, at(9, 10)) == pytest.approx(2.0)
    assert chat_cost("deepseek-flash", 1_000_000, 1_000_000, 1_000_000, at(9, 10)) == pytest.approx(10.04)
    assert chat_cost("deepseek-flash", 1_000_000, 1_000_000, 1_000_000, at(14, 10)) == pytest.approx(5.02)
    assert chat_cost("deepseek-v4-pro", 0, 0, 1_000_000, at(9, 10)) == pytest.approx(27.0)


def test_an_unknown_model_is_unpriced_not_free():
    assert chat_cost("some-new-model", 10, 10, 10, at(9, 10)) is None


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.db")


def deepseek_usage(hit, miss, output):
    return {"prompt_tokens": hit + miss, "completion_tokens": output, "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}


def test_summary_adds_up_today_month_days_and_agents(ledger):
    ledger.record_chat(1, "deepseek-flash", deepseek_usage(1000, 1000, 500), moment=at(9, 10))
    ledger.record_chat(1, "deepseek-flash", deepseek_usage(3000, 0, 100), agent="research", moment=at(9, 20))
    ledger.record_search(1, moment=at(9, 10))
    ledger.record_chat(1, "deepseek-flash", deepseek_usage(0, 1000, 0), moment=at(2, 10))
    summary = ledger.summary(1, today=date(2026, 11, 9))
    today = summary["today"]
    assert today["calls"] == 3 and today["searches"] == 1
    assert today["hit_tokens"] == 4000 and today["miss_tokens"] == 1000 and today["output_tokens"] == 600
    assert today["cache_rate"] == .8
    # 10:00 is peak: (1000*.04 + 1000*2 + 500*8)/1e6. 20:00 is off-peak, half of (3000*.04 + 100*8)/1e6. One search.
    assert today["yuan"] == pytest.approx(.00604 + .00046 + .005)
    assert summary["month"]["calls"] == 4
    assert [row["day"] for row in summary["days"]] == ["2026-11-02", "2026-11-09"]
    assert {row["agent"]: row["calls"] for row in summary["agents"]} == {"main": 3, "research": 1}


def test_a_user_sees_only_their_own_spend_and_the_admin_sees_everyone(ledger):
    ledger.record_chat(1, "deepseek-flash", deepseek_usage(0, 100, 10), moment=at(9, 10))
    ledger.record_chat(2, "deepseek-flash", deepseek_usage(0, 100, 10), moment=at(9, 10))
    ledger.record_chat(2, "deepseek-flash", deepseek_usage(0, 100, 10), moment=at(9, 10))
    assert ledger.summary(1, today=date(2026, 11, 9))["month"]["calls"] == 1
    assert "users" not in ledger.summary(1, today=date(2026, 11, 9))
    everyone = ledger.summary(None, today=date(2026, 11, 9))
    assert everyone["month"]["calls"] == 3
    assert {row["user_id"]: row["calls"] for row in everyone["users"]} == {1: 1, 2: 2}


def test_a_cut_off_call_is_counted_as_missing_not_as_free(ledger):
    ledger.record_chat(1, "deepseek-flash", None, moment=at(9, 10))
    ledger.record_chat(1, "unknown-model", deepseek_usage(0, 10, 10), moment=at(9, 10))
    month = ledger.summary(1, today=date(2026, 11, 9))["month"]
    assert month["incomplete"] == 1 and month["unpriced"] == 1 and month["yuan"] == 0


def test_a_broken_ledger_never_breaks_a_reply(tmp_path):
    (tmp_path / "usage.db").mkdir()  # a directory where the database should be
    UsageLedger(tmp_path / "usage.db").record_chat(1, "deepseek-flash", deepseek_usage(0, 1, 1))


# The client: usage arrives in the last chunk, and only accepted requests count.

USAGE_CHUNK = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":' \
              '{"prompt_tokens":7,"completion_tokens":11,"prompt_cache_hit_tokens":3,"prompt_cache_miss_tokens":4}}'


def respond(monkeypatch, *responses):
    queue = iter(responses)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(lambda request: next(queue)), **kw))


def run_chat(stop_after=None):
    reported = []

    async def go():
        stream = llm_client.stream_chat("https://api.test", "k", "m", [], on_usage=reported.append)
        count = 0
        async for _ in stream:
            count += 1
            if count == stop_after:
                await stream.aclose()
                break
    asyncio.run(go())
    return reported


def sse(*lines):
    return httpx.Response(200, text="".join(line + "\n\n" for line in lines))


def test_usage_from_the_last_chunk_is_reported_once(monkeypatch):
    respond(monkeypatch, sse('data: {"choices":[{"delta":{"content":"好"}}]}', USAGE_CHUNK, 'data: [DONE]'))
    assert run_chat() == [{"prompt_tokens": 7, "completion_tokens": 11, "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 4}]


def test_an_interrupted_reply_reports_that_its_usage_is_missing(monkeypatch):
    respond(monkeypatch, sse('data: {"choices":[{"delta":{"content":"一"}}]}', 'data: {"choices":[{"delta":{"content":"二"}}]}',
                             USAGE_CHUNK, 'data: [DONE]'))
    assert run_chat(stop_after=1) == [None]


def test_a_retried_outage_is_not_billed_twice(monkeypatch):
    respond(monkeypatch, httpx.Response(503), sse(USAGE_CHUNK, 'data: [DONE]'))
    assert len(run_chat()) == 1


def test_a_refused_request_is_not_billed(monkeypatch):
    respond(monkeypatch, httpx.Response(401))
    with pytest.raises(RuntimeError):
        run_chat()


def test_a_refused_request_reports_nothing(monkeypatch):
    respond(monkeypatch, httpx.Response(402))
    reported = []

    async def go():
        with pytest.raises(RuntimeError):
            async for _ in llm_client.stream_chat("https://api.test", "k", "m", [], on_usage=reported.append):
                pass
    asyncio.run(go())
    assert reported == []


# Wiring: a spoken turn and a web search land in the signed-in user's ledger.

def test_a_reply_is_recorded_for_the_user_who_asked(monkeypatch, user):
    respond(monkeypatch, sse('data: {"choices":[{"delta":{"content":"你好。"}}]}', USAGE_CHUNK, 'data: [DONE]'))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(backend, "TTS_PROVIDER", "edge")

    async def no_speech(text):
        return b"ID3"
    monkeypatch.setattr(backend, "synthesize", no_speech)
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
        socket.send_json({"type": "text", "request_id": 1, "text": "你好"})
        while socket.receive_json()["type"] != "done":
            pass
    month = usage.LEDGER.summary(user.user_id)["month"]
    assert month["calls"] == 1 and month["hit_tokens"] == 3 and month["miss_tokens"] == 4 and month["output_tokens"] == 11
    assert month["yuan"] > 0


def test_a_successful_web_search_is_recorded_and_a_failed_one_is_not(monkeypatch, user):
    respond(monkeypatch, httpx.Response(200, json={"results": []}), httpx.Response(500))
    asyncio.run(tools.web_search("新闻"))
    with pytest.raises(tools.ToolError):
        asyncio.run(tools.web_search("新闻"))
    assert usage.LEDGER.summary(user.user_id)["month"]["searches"] == 1


def test_the_page_gets_its_own_spend_and_only_an_admin_sees_everyone(user, monkeypatch):
    usage.LEDGER.record_chat(user.user_id, "deepseek-flash", deepseek_usage(0, 100, 10))
    usage.LEDGER.record_chat(99, "deepseek-flash", deepseek_usage(0, 100, 10))
    with TestClient(backend.app) as client:
        data = client.get("/api/usage").json()
        assert data["month"]["calls"] == 1
        assert {row["email"] for row in data["all"]["users"]} == {"#99", f"#{user.user_id}"}
        monkeypatch.setattr(user, "is_admin", False)
        assert "all" not in client.get("/api/usage").json()


def test_a_backup_models_flat_price_has_no_off_peak_discount(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(usage, "FLAT_PRICES", {})
    usage.set_flat_price("backup-model", {"hit": 1, "miss": 2, "output": 4})
    night = datetime(2026, 11, 9, 3, tzinfo=usage.BEIJING)  # off-peak for DeepSeek
    assert usage.chat_cost("backup-model", 1_000_000, 1_000_000, 1_000_000, night) == 7
    assert usage.chat_cost("deepseek-flash", 0, 1_000_000, 0, night) == 1.0  # DeepSeek still halves at night
    assert usage.chat_cost("unknown-model", 1, 1, 1, night) is None
    with pytest.raises(ValueError):
        usage.set_flat_price("bad", {"hit": 1})
