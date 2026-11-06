import asyncio
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
