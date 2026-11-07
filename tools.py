"""Tools the model may call. Arguments come from the model, so they are checked before any tool runs."""
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from typing import Callable

import httpx

from memory_store import MemoryStore, ReminderStore

log = logging.getLogger("uvicorn.error")
memories = MemoryStore(Path(__file__).resolve().parent / "data" / "agent.db")
reminders = ReminderStore(memories.path)
MAX_MEMORY_CHARS = 200
# Past this, say it is late instead of pretending it is on time.
REMINDER_LATE_SECONDS = 120
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
    # read runs freely, reversible runs and is logged, confirm also needs the user's yes.
    level: str = "read"
    # Fixed here rather than written by the model, so the question asked is the action taken.
    confirm_text: str = ""


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


def no_audit(*_):
    pass


class ToolRegistry:
    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}
        self.writes = set()

    def get(self, name):
        return self.tools.get(name)

    def is_read(self, name):
        tool = self.tools.get(name)
        return tool is None or tool.level == "read"

    def schemas(self):
        return [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                  "parameters": tool.parameters}} for tool in self.tools.values()]

    async def execute(self, call, confirm=None, audit=no_audit):
        """Always return JSON text for the model. Failures become results so it can explain them.

        confirm(tool, arguments) returns (approved, detail); without it, confirm tools never run.
        """
        tool = self.tools.get(call.name)
        try:
            if tool is None:
                raise ToolError(f"没有这个工具：{call.name}")
            arguments = check_arguments(tool, call.arguments)
        except ToolError as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        if tool.level == "read":
            result = await self._attempt(tool, arguments)
        else:
            result = await self._write(tool, arguments, confirm, audit)
        return json.dumps(result, ensure_ascii=False)

    async def _attempt(self, tool, arguments):
        try:
            return await asyncio.wait_for(tool.run(**arguments), tool.timeout)
        except ToolError as error:
            return {"error": str(error)}
        except TimeoutError:
            return {"error": "查询超时"}
        except Exception:
            log.exception("tool %s failed", tool.name)
            return {"error": "工具暂时不可用"}

    async def _write(self, tool, arguments, confirm, audit):
        detail = ""
        if tool.level == "confirm":
            try:
                approved, detail = await confirm(tool, arguments) if confirm else (False, "no_confirm")
            except asyncio.CancelledError:
                audit(tool.name, arguments, "interrupted", "")
                raise
            if not approved:
                audit(tool.name, arguments, "denied", detail)
                return {"cancelled": True, "message": "用户没有确认，操作未执行"}

        async def finish():
            result = await self._attempt(tool, arguments)
            audit(tool.name, arguments, "failed" if "error" in result else "executed", detail)
            return result

        # An interruption must not leave a write half done or missing from the log.
        work = asyncio.ensure_future(finish())
        self.writes.add(work)
        work.add_done_callback(self.writes.discard)
        return await asyncio.shield(work)


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


async def demo_action():
    return {"done": True, "note": "这是演示操作，没有改动任何内容"}


DEMO_CONFIRM = Tool("demo_confirm", "演示确认流程，不做任何实际操作。只有用户说“测试确认”时才调用", {"type": "object", "properties": {}},
                    demo_action, "等待确认…", filler=False, level="confirm", confirm_text="要执行演示操作吗？")
async def remember(content):
    text = content.strip()
    if not 1 <= len(text) <= MAX_MEMORY_CHARS:
        raise ToolError(f"记忆内容要在 1 到 {MAX_MEMORY_CHARS} 字之间")
    memory_id = memories.add(text)
    return {"saved": True, "say": f"记住了：{text}", "undo": {"action": "delete", "id": memory_id}}


async def forget(memory_id):
    if not memory_id.strip().isdecimal():
        raise ToolError("memory_id 必须是记忆的编号")
    content = memories.forget(int(memory_id))
    if content is None:
        raise ToolError(f"没有编号为 {memory_id} 的记忆")
    return {"forgotten": True, "say": f"已经忘掉：{content}", "undo": {"action": "restore", "id": int(memory_id)}}


async def clear_memories():
    return {"cleared": True, "say": f"已清空全部记忆，共 {memories.clear()} 条。", "changed": True}


def spoken_time(moment):
    """Read the full date back, so a misheard or misparsed time is caught when it is set."""
    hour = moment.hour
    period = "凌晨" if hour < 6 else "上午" if hour < 12 else "中午" if hour < 13 else "下午" if hour < 19 else "晚上"
    clock = hour if hour <= 12 else hour - 12
    minutes = f"{moment.minute}分" if moment.minute else ""
    return f"{moment.month}月{moment.day}日{period}{clock}点{minutes}"


def reminder_speech(reminder, moment=None):
    due = datetime.fromisoformat(reminder["due_at"])
    late = ((moment or datetime.now()) - due).total_seconds() > REMINDER_LATE_SECONDS
    return f"你错过了一条提醒，原定{spoken_time(due)}：{reminder['content']}" if late else f"提醒你：{reminder['content']}"


def parse_due(text):
    try:
        due = datetime.fromisoformat(text.strip())
    except ValueError as error:
        raise ToolError("due_at 必须是 YYYY-MM-DDTHH:MM:SS 格式") from error
    return due.astimezone().replace(tzinfo=None) if due.tzinfo else due


async def set_reminder(content, due_at):
    text, due = content.strip(), parse_due(due_at)
    if not 1 <= len(text) <= MAX_MEMORY_CHARS:
        raise ToolError(f"提醒内容要在 1 到 {MAX_MEMORY_CHARS} 字之间")
    if due <= datetime.now():
        raise ToolError(f"提醒时间 {due_at} 已经过去了，请和用户确认时间")
    reminder_id = reminders.add(text, due)
    return {"saved": True, "say": f"提醒设好了，{spoken_time(due)}：{text}", "undo": {"kind": "reminder", "action": "delete", "id": reminder_id}}


async def cancel_reminder(reminder_id):
    if not reminder_id.strip().isdecimal():
        raise ToolError("reminder_id 必须是提醒的编号")
    reminder = reminders.cancel(int(reminder_id))
    if reminder is None:
        raise ToolError(f"没有编号为 {reminder_id} 的待办提醒")
    return {"cancelled_reminder": True, "say": f"已取消提醒：{reminder['content']}", "undo": {"kind": "reminder", "action": "restore", "id": reminder["id"]}}


REMEMBER = Tool("remember", "保存一条长期记忆。只有用户明确说要记住时才调用", {
    "type": "object", "required": ["content"], "properties": {"content": {"type": "string", "description": "要记住的事，一句话"}}},
    remember, "正在记录…", filler=False, level="reversible")
FORGET = Tool("forget", "删除一条长期记忆。用系统提示里记忆前面的编号", {
    "type": "object", "required": ["memory_id"], "properties": {"memory_id": {"type": "string", "description": "记忆编号，如 3"}}},
    forget, "正在删除…", filler=False, level="reversible")
CLEAR_MEMORIES = Tool("clear_memories", "清空全部长期记忆，无法恢复。只有用户明确要求清空全部时才调用", {"type": "object", "properties": {}},
                      clear_memories, "等待确认…", filler=False, level="confirm", confirm_text="要清空全部记忆吗？清空后无法恢复。")
SET_REMINDER = Tool("set_reminder", "新建一个到时间主动播报的提醒", {
    "type": "object", "required": ["content", "due_at"], "properties": {
        "content": {"type": "string", "description": "提醒的事项，不含时间，如开会"},
        "due_at": {"type": "string", "description": "本地时间，格式 YYYY-MM-DDTHH:MM:SS，根据系统提示的当前时间计算"}}},
    set_reminder, "正在设置提醒…", filler=False, level="reversible")
CANCEL_REMINDER = Tool("cancel_reminder", "取消一个待办提醒。用系统提示里待办提醒前面的编号", {
    "type": "object", "required": ["reminder_id"], "properties": {"reminder_id": {"type": "string", "description": "提醒编号，如 2"}}},
    cancel_reminder, "正在取消提醒…", filler=False, level="reversible")
# Off by default: it exists to measure confirmation with a real microphone.
default_registry = ToolRegistry([WEATHER, REMEMBER, FORGET, CLEAR_MEMORIES, SET_REMINDER, CANCEL_REMINDER]
                                + ([DEMO_CONFIRM] if os.getenv("VOICE_DEMO_CONFIRM") == "1" else []))
