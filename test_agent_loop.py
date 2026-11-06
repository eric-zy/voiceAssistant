import asyncio
import json

import pytest

from agent_loop import MAX_TOOL_ROUNDS, build_system_prompt, run_turn
from llm_client import TextDelta, ToolCall, ToolCallStarted
from tools import Tool, ToolRegistry

USER = [{"role": "user", "content": "问题"}]


def registry(run=None, **options):
    async def echo(city):
        return {"city": city}
    return ToolRegistry([Tool("lookup", "查询", {"type": "object", "required": ["city"], "properties": {"city": {"type": "string"}}},
                              run or echo, "查询中", **options)])


def scripted(*rounds):
    """Fake model: each round is the list of events one request yields."""
    requests = []

    async def chat(messages, **options):
        requests.append({"messages": messages, **options})
        for event in rounds[min(len(requests), len(rounds)) - 1]:
            yield event
    return chat, requests


def turn(chat, tools=None):
    seen = []

    async def on_event(event):
        seen.append(event)
    return asyncio.run(run_turn(USER, tools or registry(), chat, on_event)), seen


def call(number=1, city="北京"):
    return ToolCall(f"c{number}", "lookup", json.dumps({"city": city}, ensure_ascii=False))


def test_a_plain_answer_makes_one_request():
    chat, requests = scripted([TextDelta("你"), TextDelta("好")])
    added, seen = turn(chat)
    assert added == [{"role": "assistant", "content": "你好"}]
    assert seen == [TextDelta("你"), TextDelta("好")]
    assert len(requests) == 1 and "tool_choice" not in requests[0]


def test_tool_results_go_back_to_the_model():
    chat, requests = scripted([ToolCallStarted("lookup"), call()], [TextDelta("晴")])
    added, seen = turn(chat)
    assert [message["role"] for message in added] == ["assistant", "tool", "assistant"]
    assert added[0]["content"] is None and added[0]["tool_calls"][0]["id"] == "c1"
    assert added[1] == {"role": "tool", "tool_call_id": "c1", "content": '{"city": "北京"}'}
    assert requests[1]["messages"] == USER + added[:2]
    assert seen == [ToolCallStarted("lookup"), TextDelta("晴")]


def test_parallel_calls_run_together_and_answer_in_order():
    running = []

    async def run(city):
        running.append(city)
        while len(running) < 2:
            await asyncio.sleep(0)
        return {"city": city}

    chat, _ = scripted([call(1, "北京"), call(2, "上海")], [TextDelta("好")])
    added, _ = turn(chat, registry(run))
    assert [(message["tool_call_id"], json.loads(message["content"])["city"]) for message in added[1:3]] == [("c1", "北京"), ("c2", "上海")]


def test_a_model_that_keeps_calling_tools_is_stopped():
    chat, requests = scripted([call()])
    added, _ = turn(chat)
    assert len(requests) == MAX_TOOL_ROUNDS + 1
    assert requests[-1]["tool_choice"] == "none"
    assert all("tool_choice" not in request for request in requests[:-1])
    assert added[-1]["role"] == "assistant" and "tool_calls" not in added[-1]


def test_text_before_a_tool_call_is_kept():
    chat, _ = scripted([TextDelta("我看看"), call()], [TextDelta("晴")])
    added, _ = turn(chat)
    assert added[0]["content"] == "我看看"


def test_a_failed_tool_is_reported_to_the_model_not_the_user():
    async def broken(city):
        raise OSError("disk detail")
    chat, requests = scripted([call()], [TextDelta("查不到")])
    added, _ = turn(chat, registry(broken))
    assert json.loads(added[1]["content"]) == {"error": "工具暂时不可用"}
    assert added[-1]["content"] == "查不到"


def test_cancelling_stops_a_running_tool():
    stopped = []

    async def hang(city):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(city)
            raise

    async def run():
        chat, _ = scripted([call()])

        async def on_event(event):
            pass
        task = asyncio.create_task(run_turn(USER, registry(hang), chat, on_event))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert stopped == ["北京"]


def test_the_clock_ends_the_prompt_so_the_prefix_stays_cacheable():
    prompt = build_system_prompt("基础。", "苏州")
    assert prompt.startswith("基础。") and "用户默认城市：苏州。" in prompt
    assert prompt.index("苏州") < prompt.index("当前时间")


def test_no_default_city_is_invented():
    assert "默认城市" not in build_system_prompt("基础。")
