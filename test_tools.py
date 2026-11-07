import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

import tools
from llm_client import ToolCall
from tools import Tool, ToolError, ToolRegistry, check_arguments

SCHEMA = {"type": "object", "required": ["city"], "properties": {"city": {"type": "string"}, "date": {"type": "string"}}}


def make(run, **options):
    return ToolRegistry([Tool("t", "说明", SCHEMA, run, "查询中", **options)])


async def ok(**arguments):
    return arguments


def run_tool(registry, arguments='{"city":"北京"}', name="t"):
    return json.loads(asyncio.run(registry.execute(ToolCall("c", name, arguments))))


def test_good_arguments_reach_the_tool():
    assert run_tool(make(ok), '{"city":"北京","date":"2026-11-07"}') == {"city": "北京", "date": "2026-11-07"}


@pytest.mark.parametrize("raw,hint", [('{"city":"北', "JSON"), ('["北京"]', "对象"), ('{}', "缺少"), ('{"city":"北京","x":"1"}', "未知"),
                                      ('{"city":3}', "字符串")])
def test_bad_arguments_never_run_the_tool(raw, hint):
    ran = []

    async def run(**arguments):
        ran.append(arguments)
    result = run_tool(make(run), raw)
    assert hint in result["error"] and not ran


def test_empty_arguments_mean_an_empty_object():
    assert check_arguments(Tool("t", "", {"properties": {}}, ok, ""), "") == {}


def test_an_unknown_tool_is_explained():
    assert "没有这个工具" in run_tool(make(ok), name="delete_everything")["error"]


def test_a_slow_tool_times_out():
    async def slow(**arguments):
        await asyncio.sleep(5)
    assert run_tool(make(slow, timeout=.01)) == {"error": "查询超时"}


def test_a_tool_error_keeps_its_message_and_other_errors_do_not():
    async def known(**arguments):
        raise ToolError("找不到地点")

    async def unknown(**arguments):
        raise OSError("C:\\secret\\path")
    assert run_tool(make(known)) == {"error": "找不到地点"}
    assert run_tool(make(unknown)) == {"error": "工具暂时不可用"}


def test_schemas_use_the_api_format():
    function = make(ok).schemas()[0]
    assert function["type"] == "function" and function["function"]["name"] == "t"
    assert function["function"]["parameters"] == SCHEMA


DAILY = {"time": ["2026-11-05", "2026-11-06", "2026-11-07"], "weather_code": [0, 2, 63], "temperature_2m_max": [16.4, 13.1, 5.9],
         "temperature_2m_min": [4.3, 3.2, .9], "precipitation_probability_max": [0, 10, 100]}


@pytest.fixture
def open_meteo(monkeypatch):
    queries = []

    def handler(request):
        if "geocoding" in request.url.host:
            name = request.url.params["name"]
            queries.append(name)
            results = [] if name == "不存在" else [{"name": name, "admin1": name + "市", "country": "中国", "latitude": 39.9, "longitude": 116.4}]
            return httpx.Response(200, json={"results": results} if results else {})
        return httpx.Response(200, json={"daily": DAILY})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return queries


def test_weather_for_a_chosen_day(open_meteo):
    result = asyncio.run(tools.get_weather("北京", "2026-11-07"))
    assert result == {"place": "中国，北京市，北京", "date": "2026-11-07", "weather": "中雨", "high_c": 5.9, "low_c": .9, "rain_chance_percent": 100}


def test_no_date_means_the_places_own_today(open_meteo):
    assert asyncio.run(tools.get_weather("北京"))["date"] == "2026-11-06"


def test_the_city_suffix_is_dropped_because_it_finds_the_wrong_country(open_meteo):
    asyncio.run(tools.get_weather("上海市"))
    assert open_meteo == ["上海"]


def test_an_unknown_place_says_so(open_meteo):
    with pytest.raises(ToolError, match="找不到地点"):
        asyncio.run(tools.get_weather("不存在"))


def test_a_date_out_of_range_names_the_range(open_meteo):
    with pytest.raises(ToolError, match="2026-11-05 到 2026-11-07"):
        asyncio.run(tools.get_weather("北京", "2027-01-01"))


def audited():
    rows = []
    return rows, lambda tool, arguments, outcome, detail: rows.append((tool, outcome, detail))


def writer(level, run=None, **options):
    ran = []

    async def default(**arguments):
        ran.append(arguments)
        return {"done": True}
    return ToolRegistry([Tool("t", "说明", SCHEMA, run or default, "处理中", level=level, **options)]), ran


def write(registry, confirm=None, audit=None):
    return json.loads(asyncio.run(registry.execute(ToolCall("c", "t", '{"city":"北京"}'), confirm, audit or (lambda *_: None))))


def answer(approved, detail):
    async def confirm(tool, arguments):
        return approved, detail
    return confirm


def test_a_read_tool_needs_no_confirmation_and_leaves_no_log():
    registry, ran = writer("read")
    rows, audit = audited()
    write(registry, audit=audit)
    assert ran and not rows


def test_a_reversible_tool_runs_without_asking_but_is_logged():
    registry, ran = writer("reversible")
    rows, audit = audited()

    async def never(tool, arguments):
        raise AssertionError("reversible tools do not ask")
    assert write(registry, never, audit) == {"done": True}
    assert ran and rows == [("t", "executed", "")]


def test_a_confirm_tool_runs_only_after_a_yes():
    registry, ran = writer("confirm")
    rows, audit = audited()
    assert write(registry, answer(True, "voice:确认"), audit) == {"done": True}
    assert ran and rows == [("t", "executed", "voice:确认")]


def test_a_no_leaves_the_tool_unrun_and_says_so():
    registry, ran = writer("confirm")
    rows, audit = audited()
    assert write(registry, answer(False, "timeout"), audit)["cancelled"] is True
    assert not ran and rows == [("t", "denied", "timeout")]


def test_without_a_way_to_ask_a_confirm_tool_never_runs():
    registry, ran = writer("confirm")
    rows, audit = audited()
    assert write(registry, None, audit)["cancelled"] is True
    assert not ran and rows == [("t", "denied", "no_confirm")]


def test_bad_arguments_are_refused_before_asking():
    registry, ran = writer("confirm")
    asked = []

    async def confirm(tool, arguments):
        asked.append(tool)
        return True, ""
    result = json.loads(asyncio.run(registry.execute(ToolCall("c", "t", "{}"), confirm)))
    assert "缺少" in result["error"] and not asked and not ran


def test_interrupting_a_question_runs_nothing_and_is_logged():
    registry, ran = writer("confirm")
    rows, audit = audited()

    async def run():
        async def hang(tool, arguments):
            await asyncio.sleep(30)
        task = asyncio.create_task(registry.execute(ToolCall("c", "t", '{"city":"北京"}'), hang, audit))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert not ran and rows == [("t", "interrupted", "")]


def test_an_interrupted_write_still_finishes_and_is_logged():
    finished = []

    async def slow(**arguments):
        await asyncio.sleep(.05)
        finished.append(arguments)
        return {"done": True}
    registry, _ = writer("reversible", slow)
    rows, audit = audited()

    async def run():
        task = asyncio.create_task(registry.execute(ToolCall("c", "t", '{"city":"北京"}'), None, audit))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.1)
    asyncio.run(run())
    assert finished and rows == [("t", "executed", "")]


def test_a_failed_write_is_logged_as_failed():
    async def broken(**arguments):
        raise OSError("disk")
    registry, _ = writer("reversible", broken)
    rows, audit = audited()
    assert write(registry, audit=audit) == {"error": "工具暂时不可用"}
    assert rows == [("t", "failed", "")]


def test_the_demo_tool_is_off_unless_asked_for():
    assert tools.default_registry.get("demo_confirm") is None
    assert tools.DEMO_CONFIRM.level == "confirm"


def memory_call(name, **arguments):
    return json.loads(asyncio.run(tools.default_registry.execute(ToolCall("c", name, json.dumps(arguments, ensure_ascii=False)))))


def test_remember_saves_and_offers_undo(memories):
    result = memory_call("remember", content=" 不吃香菜 ")
    assert memories.recent()[0]["content"] == "不吃香菜"
    assert result["say"] == "记住了：不吃香菜"
    assert result["undo"] == {"action": "delete", "id": memories.recent()[0]["id"]}


@pytest.mark.parametrize("content", ["", "   ", "字" * 201])
def test_remember_refuses_empty_or_oversized_text(memories, content):
    assert "1 到 200" in memory_call("remember", content=content)["error"]
    assert memories.recent() == []


def test_forget_uses_the_id_and_offers_undo(memories):
    keep, drop = memories.add("喜欢美式"), memories.add("不吃香菜")
    result = memory_call("forget", memory_id=str(drop))
    assert result["say"] == "已经忘掉：不吃香菜" and result["undo"] == {"action": "restore", "id": drop}
    assert [m["id"] for m in memories.recent()] == [keep]


@pytest.mark.parametrize("memory_id,hint", [("abc", "编号"), ("-1", "编号"), ("999", "没有编号")])
def test_forget_refuses_a_bad_id(memories, memory_id, hint):
    memories.add("不吃香菜")
    assert hint in memory_call("forget", memory_id=memory_id)["error"]
    assert len(memories.recent()) == 1


def test_clearing_memories_cannot_run_without_a_yes(memories):
    memories.add("不吃香菜")
    assert memory_call("clear_memories")["cancelled"] is True
    assert len(memories.recent()) == 1


def test_clearing_memories_after_a_yes(memories):
    memories.add("不吃香菜")
    result = json.loads(asyncio.run(tools.default_registry.execute(ToolCall("c", "clear_memories", "{}"), answer(True, "button"))))
    assert result["say"] == "已清空全部记忆，共 1 条。" and memories.recent() == []


def test_each_memory_tool_has_the_expected_level():
    levels = {name: tools.default_registry.get(name).level for name in ("remember", "forget", "clear_memories")}
    assert levels == {"remember": "reversible", "forget": "reversible", "clear_memories": "confirm"}


FUTURE = datetime(2099, 11, 7, 15, 0, 0)


@pytest.mark.parametrize("moment,text", [
    (datetime(2026, 11, 7, 15, 0), "11月7日下午3点"), (datetime(2026, 11, 7, 15, 30), "11月7日下午3点30分"),
    (datetime(2026, 1, 2, 9, 5), "1月2日上午9点5分"), (datetime(2026, 11, 7, 12, 0), "11月7日中午12点"),
    (datetime(2026, 11, 7, 0, 30), "11月7日凌晨0点30分"), (datetime(2026, 11, 7, 21, 0), "11月7日晚上9点")])
def test_times_are_read_back_in_full(moment, text):
    assert tools.spoken_time(moment) == text


def test_setting_a_reminder_reads_the_time_back_and_offers_undo(reminders):
    result = memory_call("set_reminder", content=" 开会 ", due_at="2099-11-07T15:00:00")
    assert result["say"] == "提醒设好了，11月7日下午3点：开会"
    assert reminders.pending()[0]["due_at"] == "2099-11-07T15:00:00"
    assert result["undo"] == {"kind": "reminder", "action": "delete", "id": reminders.pending()[0]["id"]}


def test_a_past_time_goes_back_to_the_model(reminders):
    assert "已经过去" in memory_call("set_reminder", content="开会", due_at="2020-01-01T15:00:00")["error"]
    assert reminders.pending() == []


@pytest.mark.parametrize("due_at", ["明天下午三点", "", "2099-13-45T00:00:00"])
def test_a_bad_time_is_refused(reminders, due_at):
    assert "格式" in memory_call("set_reminder", content="开会", due_at=due_at)["error"]
    assert reminders.pending() == []


def test_a_time_with_a_zone_becomes_local_time(reminders):
    memory_call("set_reminder", content="开会", due_at="2099-11-07T15:00:00+00:00")
    expected = datetime(2099, 11, 7, 15, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert reminders.pending()[0]["due_at"] == expected.isoformat(timespec="seconds")


def test_empty_reminder_text_is_refused(reminders):
    assert "1 到 200" in memory_call("set_reminder", content="  ", due_at="2099-11-07T15:00:00")["error"]


def test_cancelling_uses_the_id_and_offers_undo(reminders):
    reminder_id = reminders.add("开会", FUTURE)
    result = memory_call("cancel_reminder", reminder_id=str(reminder_id))
    assert result["say"] == "已取消提醒：开会" and reminders.pending() == []
    assert result["undo"] == {"kind": "reminder", "action": "restore", "id": reminder_id}


@pytest.mark.parametrize("reminder_id,hint", [("abc", "编号"), ("999", "没有编号")])
def test_cancelling_refuses_a_bad_id(reminders, reminder_id, hint):
    assert hint in memory_call("cancel_reminder", reminder_id=reminder_id)["error"]


def test_an_on_time_reminder_and_a_late_one_sound_different():
    reminder = {"content": "开会", "due_at": "2026-11-07T15:00:00"}
    assert tools.reminder_speech(reminder, datetime(2026, 11, 7, 15, 0, 5)) == "提醒你：开会"
    assert tools.reminder_speech(reminder, datetime(2026, 11, 7, 18, 0)) == "你错过了一条提醒，原定11月7日下午3点：开会"
