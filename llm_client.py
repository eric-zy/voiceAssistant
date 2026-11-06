"""Stream replies from DeepSeek's chat endpoint as events, independent of speech and sockets."""
from dataclasses import dataclass
import json

import httpx

from api_config import auth_error

MAX_REPLY_TOKENS = 512


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


async def stream_chat(base_url, api_key, model, messages, tools=None, tool_choice=None):
    """Yield events as they arrive; raises RuntimeError with a message safe to show the user."""
    body = {"model": model, "messages": messages, "stream": True,
            "thinking": {"type": "disabled"}, "max_tokens": MAX_REPLY_TOKENS}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    calls, announced, finish_reason = {}, set(), None
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5)) as client:
        async with client.stream("POST", base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key}, json=body) as response:
            if response.status_code != 200:
                raise RuntimeError(auth_error(response.status_code))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("error"):
                    raise RuntimeError("DeepSeek 返回错误，请检查账户及请求配置")
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
    if not calls:
        return
    # Cut-off arguments can still parse as JSON, so never run a call the model did not finish.
    if finish_reason != "tool_calls":
        raise RuntimeError("工具调用不完整，请再说一次")
    for index in sorted(calls):
        yield ToolCall(**calls[index])
