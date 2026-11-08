"""Stream replies from DeepSeek's chat endpoint as events, independent of speech and sockets."""
import asyncio
from dataclasses import dataclass
import json

import httpx

from api_config import auth_error

MAX_REPLY_TOKENS = 512
# The API docs ask clients to wait and retry these. Nothing has streamed yet, so a retry is invisible.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_DELAYS = (1, 2)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """The model chose a tool; its arguments are still streaming, so waiting speech can start now."""
    name: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


async def stream_chat(base_url, api_key, model, messages, tools=None, tool_choice=None, on_usage=None,
                      max_tokens=MAX_REPLY_TOKENS):
    """Yield events as they arrive; raises RuntimeError with a message safe to show the user.

    on_usage(usage) runs once for a request the server accepted: with DeepSeek's token counts, or with None
    when the stream ended early (an interrupted reply) and the counts never arrived. Retried and refused
    requests are not billed, so they are not reported.
    """
    body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "thinking": {"type": "disabled"}, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    state = {"accepted": False, "usage": None}
    try:
        async for event in _events(base_url, api_key, body, state):
            yield event
    finally:
        if on_usage and state["accepted"]:
            on_usage(state["usage"])


async def _events(base_url, api_key, body, state):
    calls, announced, finish_reason = {}, set(), None
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5)) as client:
        for delay in (*RETRY_DELAYS, None):
            async with client.stream("POST", base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key}, json=body) as response:
                retry = response.status_code in RETRY_STATUSES and delay is not None
                if not retry:
                    if response.status_code != 200:
                        raise RuntimeError(auth_error(response.status_code))
                    state["accepted"] = True
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        chunk = json.loads(payload)
                        if chunk.get("error"):
                            raise RuntimeError("DeepSeek 返回错误，请检查账户及请求配置")
                        # The last chunk carries the whole request's token counts, sometimes beside its final delta.
                        state["usage"] = chunk.get("usage") or state["usage"]
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        finish_reason = choices[0].get("finish_reason") or finish_reason
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            yield TextDelta(delta["content"])
                        # Only the first piece of a call carries its id and name; later ones add arguments.
                        for piece in delta.get("tool_calls") or []:
                            index = piece.get("index", 0)
                            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            function = piece.get("function") or {}
                            call["id"] = piece.get("id") or call["id"]
                            call["name"] = function.get("name") or call["name"]
                            call["arguments"] += function.get("arguments") or ""
                            if call["name"] and index not in announced:
                                announced.add(index)
                                yield ToolCallStarted(call["name"])
            if not retry:
                break
            await asyncio.sleep(delay)
    if not calls:
        return
    # Cut-off arguments can still parse as JSON, so never run a call the model did not finish.
    if finish_reason != "tool_calls":
        raise RuntimeError("工具调用不完整，请再说一次")
    for index in sorted(calls):
        yield ToolCall(**calls[index])
