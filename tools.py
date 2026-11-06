"""Tools the model may call. Arguments come from the model, so they are checked before any tool runs."""
import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Callable

import httpx

log = logging.getLogger("uvicorn.error")
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_NAMES = {0: "晴", 1: "基本晴朗", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
                 56: "冻毛毛雨", 57: "冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪",
                 75: "大雪", 77: "雪粒", 80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪", 95: "雷雨", 96: "雷雨伴冰雹",
                 99: "强雷雨伴冰雹"}


class ToolError(Exception):
    """A failure worth telling the model, such as an unknown city."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable
    status: str
    # Whether the wait is long enough to deserve spoken filler.
    filler: bool = True
    timeout: float = 8


def check_arguments(tool, raw):
    """Return the parsed arguments, or raise ToolError saying what the model should fix."""
    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise ToolError("参数不是合法 JSON") from error
    if not isinstance(arguments, dict):
        raise ToolError("参数必须是对象")
    known = tool.parameters["properties"]
    unknown = sorted(set(arguments) - set(known))
    missing = sorted(set(tool.parameters.get("required", [])) - set(arguments))
    if unknown or missing:
        raise ToolError(f"参数有误，未知：{unknown}，缺少：{missing}")
    if not all(isinstance(value, str) for value in arguments.values()):
        raise ToolError("参数必须是字符串")
    return arguments


class ToolRegistry:
    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}

    def get(self, name):
        return self.tools.get(name)

    def schemas(self):
        return [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                  "parameters": tool.parameters}} for tool in self.tools.values()]

    async def execute(self, call):
        """Always return JSON text for the model. Failures become results so it can explain them."""
        try:
            tool = self.tools.get(call.name)
            if tool is None:
                raise ToolError(f"没有这个工具：{call.name}")
            arguments = check_arguments(tool, call.arguments)
            result = await asyncio.wait_for(tool.run(**arguments), tool.timeout)
        except ToolError as error:
            result = {"error": str(error)}
        except TimeoutError:
            result = {"error": "查询超时"}
        except Exception:
            log.exception("tool %s failed", call.name)
            result = {"error": "工具暂时不可用"}
        return json.dumps(result, ensure_ascii=False)


async def get_weather(city, date=""):
    # "上海市" geocodes to Shanghai, Illinois, while "上海" finds the Chinese city.
    query = city.strip().removesuffix("市") or city
    async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3)) as client:
        found = await client.get(GEOCODING_URL, params={"name": query, "count": 1, "language": "zh", "format": "json"})
        found.raise_for_status()
        places = found.json().get("results")
        if not places:
            raise ToolError(f"找不到地点：{city}")
        place = places[0]
        forecast = await client.get(FORECAST_URL, params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "past_days": 1, "forecast_days": 7,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        forecast.raise_for_status()
    daily = forecast.json()["daily"]
    # past_days=1 puts the place's own today second, which may differ from this machine's date.
    index = daily["time"].index(date) if date in daily["time"] else 1
    if date and date not in daily["time"]:
        raise ToolError(f"只能查询 {daily['time'][0]} 到 {daily['time'][-1]} 的天气")
    return {"place": "，".join(part for part in (place.get("country"), place.get("admin1"), place.get("name")) if part),
            "date": daily["time"][index], "weather": WEATHER_NAMES.get(daily["weather_code"][index], "未知"),
            "high_c": daily["temperature_2m_max"][index], "low_c": daily["temperature_2m_min"][index],
            "rain_chance_percent": daily["precipitation_probability_max"][index]}


WEATHER = Tool("get_weather", "查询某地某天的天气预报，可查今天起 7 天内", {
    "type": "object", "required": ["city"], "properties": {
        "city": {"type": "string", "description": "城市名，如北京"},
        "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD，不填表示今天"}}},
    get_weather, "正在查询天气…")
default_registry = ToolRegistry([WEATHER])
