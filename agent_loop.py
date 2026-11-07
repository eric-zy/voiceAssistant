"""One user turn: ask the model, run the tools it picks, feed results back until it answers."""
import asyncio
import contextlib
import time

from llm_client import TextDelta, ToolCall
from tools import no_audit

MAX_TOOL_ROUNDS = 3
TOOL_RULES = ("需要实时信息或用户要求执行操作时调用工具，闲聊和常识问题直接回答。调用工具之前不要说话，也不要声称已经完成。"
              "查天气时用户没说地点就先问，温度读成口语，不要逐项罗列。")


def build_system_prompt(base, default_city=""):
    # The clock goes last: it changes every minute, and a stable prefix keeps the prompt cache warm.
    city = f"用户默认城市：{default_city}。" if default_city else ""
    return base + TOOL_RULES + city + "当前时间：" + time.strftime("%Y-%m-%d %H:%M %A")


def assistant_message(content, calls):
    return {"role": "assistant", "content": content or None, "tool_calls": [
        {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}} for call in calls]}


async def demo_chat(messages, **options):
    """Stands in for the model on the test phrase, so a measurement covers only speech and confirmation."""
    if messages[-1]["role"] == "tool":
        yield TextDelta("演示结束，没有改动任何内容。")
    else:
        yield ToolCall("demo", "demo_confirm", "{}")


async def run_calls(calls, registry, confirm, audit):
    if all(registry.is_read(call.name) for call in calls):
        return await asyncio.gather(*(registry.execute(call) for call in calls))
    # One at a time: the user can only answer one confirmation, and writes keep their order.
    return [await registry.execute(call, confirm, audit) for call in calls]


async def run_turn(messages, registry, chat, on_event, confirm=None, audit=no_audit):
    """Return the messages this turn added. Text and tool starts reach on_event as they stream."""
    added = []
    for round_number in range(MAX_TOOL_ROUNDS + 1):
        out_of_rounds = round_number == MAX_TOOL_ROUNDS
        options = {"tools": registry.schemas(), **({"tool_choice": "none"} if out_of_rounds else {})}
        content, calls = "", []
        async with contextlib.aclosing(chat(messages + added, **options)) as events:
            async for event in events:
                if isinstance(event, ToolCall):
                    calls.append(event)
                    continue
                if isinstance(event, TextDelta):
                    content += event.text
                await on_event(event)
        if not calls or out_of_rounds:
            added.append({"role": "assistant", "content": content})
            return added
        added.append(assistant_message(content, calls))
        results = await run_calls(calls, registry, confirm, audit)
        added.extend({"role": "tool", "tool_call_id": call.id, "content": result} for call, result in zip(calls, results))
