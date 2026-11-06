import asyncio
import json

import httpx
import pytest

from llm_client import MAX_REPLY_TOKENS, TextDelta, stream_chat


@pytest.fixture
def serve(monkeypatch):
    """Answer every request with the given response and record what was sent."""
    def install(response):
        sent = []

        def handler(request):
            sent.append(request)
            return response

        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
        return sent
    return install


def collect(messages=()):
    async def run():
        return [event async for event in stream_chat("https://api.test", "test-key", "test-model", list(messages))]
    return asyncio.run(run())


def sse(*lines):
    return httpx.Response(200, text="".join(line + "\n\n" for line in lines))


def test_text_arrives_in_order_and_noise_is_skipped(serve):
    serve(sse(': keepalive', 'data: {"choices":[{"delta":{"content":"你"}}]}', 'data: {"choices":[{"delta":{}}]}',
              'data: {"choices":[]}', 'data: {"choices":[{"delta":{"content":"好"}}]}', 'data: [DONE]'))
    assert collect() == [TextDelta("你"), TextDelta("好")]


def test_nothing_after_done_is_read(serve):
    serve(sse('data: {"choices":[{"delta":{"content":"完"}}]}', 'data: [DONE]', 'data: {"choices":[{"delta":{"content":"多余"}}]}'))
    assert collect() == [TextDelta("完")]


def test_the_request_matches_what_the_api_expects(serve):
    sent = serve(sse('data: [DONE]'))
    messages = [{"role": "user", "content": "你好"}]
    collect(messages)
    assert sent[0].url == "https://api.test/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(sent[0].content) == {"model": "test-model", "messages": messages, "stream": True,
                                           "thinking": {"type": "disabled"}, "max_tokens": MAX_REPLY_TOKENS}


@pytest.mark.parametrize("status,hint", [(401, "认证失败"), (402, "余额不足"), (429, "请求受限"), (500, "HTTP 500")])
def test_http_errors_become_readable_messages(serve, status, hint):
    serve(httpx.Response(status, text="secret upstream detail"))
    with pytest.raises(RuntimeError, match=hint) as caught:
        collect()
    assert "secret upstream detail" not in str(caught.value)


def test_an_error_inside_the_stream_stops_it(serve):
    serve(sse('data: {"choices":[{"delta":{"content":"半"}}]}', 'data: {"error":{"message":"secret upstream detail"}}'))
    events = []

    async def run():
        async for event in stream_chat("https://api.test", "test-key", "test-model", []):
            events.append(event)

    with pytest.raises(RuntimeError, match="DeepSeek 返回错误"):
        asyncio.run(run())
    assert events == [TextDelta("半")]
