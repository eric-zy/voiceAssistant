"""Stage 0 probe: does the chat model stream tool calls, and what do tools cost in latency?

Sends about 25 short requests. Prints no credentials and saves only fixed test
prompts, model replies and timings to outputs/tool_calling_spike.json.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api_config import auth_error, fingerprint, normalize_key  # noqa: E402

RESULT = ROOT / "outputs" / "tool_calling_spike.json"
# Same wording as app.py SYSTEM, plus the rule the agent will need.
SYSTEM = ("你是中文语音助手。请用自然口语回答，直接说重点，不要用‘好的’‘当然’‘没问题’等应答语开场。"
          "首句不超过15字，再按需补充一两句，通常总字数不超过80字，不使用Markdown。用户要求详细解释时可以展开。"
          "只有需要实时信息或用户明确要求记录、提醒时才调用工具，闲聊和常识问题直接回答。")


def tool(name, description, **params):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {key: {"type": "string", "description": text} for key, text in params.items()},
        "required": list(params)}}}


# The full first batch from the plan, so prompt size and latency are realistic.
TOOLS = [
    tool("get_time", "查询当前本地时间"),
    tool("get_weather", "查询城市天气", city="城市名", date="日期，如今天、明天"),
    tool("web_search", "联网搜索实时信息", query="搜索词"),
    tool("remember", "保存用户明确要求记住的事", content="要记住的内容"),
    tool("forget", "删除一条记忆", content="要忘掉的内容"),
    tool("set_reminder", "新建提醒", content="提醒内容", due_at="ISO 8601 到期时间"),
    tool("list_reminders", "查询提醒"),
    tool("cancel_reminder", "取消一条提醒", content="要取消的提醒内容"),
    tool("clear_memories", "清空全部记忆"),
]
CALL_CASES = [
    ("天气", "北京明天会下雨吗？", {"get_weather"}),
    ("时间", "现在几点了？", {"get_time"}),
    ("记忆", "记住我不吃香菜。", {"remember"}),
    ("提醒", "明天下午三点提醒我开会。", {"set_reminder"}),
    ("多城市", "北京和上海今天哪个更热？", {"get_weather"}),
]
CHAT_CASES = ["你好呀", "讲个很短的笑话", "你觉得猫可爱吗？", "用一句话解释什么是递归"]
LATENCY_PROMPTS = ["你喜欢春天吗？", "茶和咖啡哪个好？", "猫为什么爱睡觉？", "星星为什么会闪？", "为什么天是蓝的？", "怎么才能早起？",
                   "下雨天适合做什么？", "什么是好朋友？", "为什么要读书？", "海水为什么是咸的？"]
# Tool syntax must never reach text-to-speech.
LEAK = re.compile(r"tool_call|get_weather|get_time|set_reminder|<[|｜]|\{\s*\"")


def system_message():
    return {"role": "system", "content": SYSTEM + "当前时间：" + time.strftime("%Y-%m-%d %H:%M %A")}


def stream_turn(client, base_url, key, model, messages, tools=None, max_tokens=256):
    body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "thinking": {"type": "disabled"}, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    result = {"content": "", "calls": [], "finish_reason": None, "usage": None, "first_text": None,
              "first_tool": None, "total": None, "text_before_tool": False, "arg_chunks": 0, "error": None}
    calls = {}
    started = time.perf_counter()
    with client.stream("POST", base_url + "/chat/completions", json=body,
                       headers={"Authorization": "Bearer " + key}) as response:
        if response.status_code != 200:
            detail = response.read().decode("utf-8", "replace")[:300]
            result["error"] = f"{auth_error(response.status_code)} {detail}"
            return result
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("error"):
                result["error"] = str(chunk["error"])[:300]
                return result
            result["usage"] = chunk.get("usage") or result["usage"]
            if not chunk.get("choices"):
                continue
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}
            result["finish_reason"] = choice.get("finish_reason") or result["finish_reason"]
            if delta.get("content"):
                if result["first_text"] is None:
                    result["first_text"] = time.perf_counter() - started
                result["content"] += delta["content"]
            for piece in delta.get("tool_calls") or []:
                if result["first_tool"] is None:
                    result["first_tool"] = time.perf_counter() - started
                    result["text_before_tool"] = bool(result["content"])
                call = calls.setdefault(piece.get("index", 0), {"id": "", "name": "", "arguments": ""})
                function = piece.get("function") or {}
                call["id"] = piece.get("id") or call["id"]
                call["name"] = function.get("name") or call["name"]
                if function.get("arguments"):
                    call["arguments"] += function["arguments"]
                    result["arg_chunks"] += 1
    result["total"] = time.perf_counter() - started
    result["calls"] = [calls[index] for index in sorted(calls)]
    return result


def valid_arguments(call):
    try:
        return isinstance(json.loads(call["arguments"] or "{}"), dict)
    except json.JSONDecodeError:
        return False


def tool_case(client, target, label, prompt, expected):
    result = stream_turn(client, *target, [system_message(), {"role": "user", "content": prompt}], TOOLS)
    names = {call["name"] for call in result["calls"]}
    passed = (not result["error"] and result["finish_reason"] == "tool_calls" and bool(names & expected)
              and all(valid_arguments(call) and call["id"] for call in result["calls"]) and not LEAK.search(result["content"]))
    return {"label": label, "prompt": prompt, "passed": passed, **result}


def chat_case(client, target, prompt):
    result = stream_turn(client, *target, [system_message(), {"role": "user", "content": prompt}], TOOLS)
    passed = not result["error"] and not result["calls"] and bool(result["content"]) and not LEAK.search(result["content"])
    return {"prompt": prompt, "passed": passed, **result}


def round_trip(client, target, first):
    """Send fake weather back: proves the follow-up request is accepted and spoken as plain text."""
    assistant = {"role": "assistant", "content": first["content"] or None, "tool_calls": [
        {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
        for call in first["calls"]]}
    results = [{"role": "tool", "tool_call_id": call["id"], "content": json.dumps(
        {"weather": "中雨", "high_c": 18, "low_c": 11}, ensure_ascii=False)} for call in first["calls"]]
    messages = [system_message(), {"role": "user", "content": first["prompt"]}, assistant, *results]
    result = stream_turn(client, *target, messages, TOOLS)
    passed = not result["error"] and not result["calls"] and "雨" in result["content"] and not LEAK.search(result["content"])
    return {"passed": passed, **result}


def latency(client, target, rounds):
    arms = {"without_tools": [], "with_tools": []}
    prompts = {"without_tools": [], "with_tools": []}
    failures = []
    for index in range(rounds):
        prompt = LATENCY_PROMPTS[index % len(LATENCY_PROMPTS)]
        # Alternate which arm goes first so warm-up and drift do not favor one side.
        order = ("with_tools", "without_tools") if index % 2 else ("without_tools", "with_tools")
        for arm in order:
            tools = TOOLS if arm == "with_tools" else None
            result = stream_turn(client, *target, [system_message(), {"role": "user", "content": prompt}], tools, max_tokens=48)
            if result["first_text"] is None:
                failures.append({"arm": arm, "prompt": prompt, "error": result["error"], "calls": result["calls"]})
                continue
            arms[arm].append(result["first_text"])
            prompts[arm].append((result["usage"] or {}).get("prompt_tokens"))

    def summary(values):
        return {"samples": [round(value, 3) for value in values], "median": round(statistics.median(values), 3) if values else None,
                "min": round(min(values), 3) if values else None, "max": round(max(values), 3) if values else None}

    report = {arm: summary(values) for arm, values in arms.items()}
    known = {arm: [value for value in values if value is not None] for arm, values in prompts.items()}
    extra = statistics.median(known["with_tools"]) - statistics.median(known["without_tools"]) if all(known.values()) else None
    report.update(extra_prompt_tokens=extra, failures=failures)
    if report["with_tools"]["median"] is not None and report["without_tools"]["median"] is not None:
        report["median_delta_seconds"] = round(report["with_tools"]["median"] - report["without_tools"]["median"], 3)
    return report


def line(ok, text):
    print(("PASS  " if ok else "FAIL  ") + text)


def run(client, base_url, key, model, rounds):
    target = (base_url, key, model)
    listing = client.get(base_url + "/models", headers={"Authorization": "Bearer " + key})
    if listing.status_code != 200:
        print(auth_error(listing.status_code))
        return None
    models = sorted(item.get("id", "") for item in listing.json().get("data", []))
    print("Models offered:", ", ".join(models))
    line(model in models, f"model {model} is listed")

    calls = [tool_case(client, target, *case) for case in CALL_CASES]
    for case in calls:
        names = [call["name"] for call in case["calls"]]
        line(case["passed"], f"{case['label']}: tools={names} finish={case['finish_reason']} "
                             f"arg_chunks={case['arg_chunks']} text_first={case['text_before_tool']} {case['error'] or ''}")
    chats = [chat_case(client, target, prompt) for prompt in CHAT_CASES]
    for case in chats:
        line(case["passed"], f"闲聊不调用工具: {case['prompt']} tools={[call['name'] for call in case['calls']]} {case['error'] or ''}")
    weather = next((case for case in calls if case["calls"]), None)
    follow_up = round_trip(client, target, weather) if weather else {"passed": False, "error": "no tool call to return"}
    line(follow_up["passed"], f"工具结果回填: {follow_up.get('content', '')!r} {follow_up.get('error') or ''}")
    delays = latency(client, target, rounds)
    print("first_text seconds:", {arm: delays[arm]["median"] for arm in ("without_tools", "with_tools")},
          "delta:", delays.get("median_delta_seconds"), "extra prompt tokens:", delays["extra_prompt_tokens"])
    report = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model, "models_offered": models,
              "model_listed": model in models, "tool_cases": calls, "chat_cases": chats, "follow_up": follow_up, "latency": delays}
    report["passed"] = (report["model_listed"] and follow_up["passed"] and all(case["passed"] for case in calls + chats)
                        and not delays["failures"])
    return report


def main():
    # A reply with an emoji must not crash the run on a GBK console.
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=6, help="latency pairs to measure")
    parser.add_argument("--output", type=Path, default=RESULT)
    args = parser.parse_args()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    if urlsplit(base_url).scheme != "https":
        print("Refusing to send the key to a non-HTTPS URL.")
        return 1
    key = normalize_key(os.getenv("DEEPSEEK_API_KEY", "") or getpass.getpass("DeepSeek API Key (hidden): "))
    print(f"Host: {urlsplit(base_url).hostname}  Model: {model}  Key fingerprint: {fingerprint(key)}")
    with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
        report = run(client, base_url, key, model, args.rounds)
    if report is None:
        return 1
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved", args.output)
    print("ALL PASSED" if report["passed"] else "SOME CHECKS FAILED")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
