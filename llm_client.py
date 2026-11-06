"""Stream replies from DeepSeek's chat endpoint as events, independent of speech and sockets."""
from dataclasses import dataclass
import json

import httpx

from api_config import auth_error

MAX_REPLY_TOKENS = 512


@dataclass(frozen=True)
class TextDelta:
    text: str


async def stream_chat(base_url, api_key, model, messages):
    """Yield events as they arrive; raises RuntimeError with a message safe to show the user."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5)) as client:
        async with client.stream("POST", base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key}, json={
            "model": model, "messages": messages, "stream": True,
            "thinking": {"type": "disabled"}, "max_tokens": MAX_REPLY_TOKENS}) as response:
            if response.status_code != 200:
                raise RuntimeError(auth_error(response.status_code))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                chunk = json.loads(payload)
                if chunk.get("error"):
                    raise RuntimeError("DeepSeek 返回错误，请检查账户及请求配置")
                choices = chunk.get("choices", [])
                token = (choices[0].get("delta", {}).get("content") or "") if choices else ""
                if token:
                    yield TextDelta(token)
