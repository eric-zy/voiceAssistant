"""Stage 2 probe: how often does the model talk before a write tool has run, with and without the rule?

Also runs one real weather question through run_turn. Sends about 45 short requests and saves
fixed prompts, replies and counts to outputs/preamble_spike.json. Prints no credentials.
"""
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "work"))
from agent_loop import TOOL_RULES, build_system_prompt, run_turn  # noqa: E402
from api_config import fingerprint, normalize_key  # noqa: E402
from llm_client import TextDelta, stream_chat  # noqa: E402
from spike_tool_calling import TOOLS, stream_turn  # noqa: E402
from tools import default_registry  # noqa: E402

RESULT = ROOT / "outputs" / "preamble_spike.json"
BASE = ("你是中文语音助手。请用自然口语回答，直接说重点，不要用‘好的’‘当然’‘没问题’等应答语开场。"
        "首句不超过15字，再按需补充一两句，通常总字数不超过80字，不使用Markdown。用户要求详细解释时可以展开。")
OLD_RULE = "只有需要实时信息或用户明确要求记录、提醒时才调用工具，闲聊和常识问题直接回答。"
PROMPTS = {"remember": ["记住我不吃香菜。", "记住我的车牌尾号是七。", "帮我记一下我对花生过敏。", "记住我妈妈生日是五月三号。", "记住我喜欢喝美式。"],
           "set_reminder": ["明天下午三点提醒我开会。", "一小时后提醒我喝水。", "后天早上八点提醒我交房租。", "今晚九点提醒我吃药。", "周一上午十点提醒我打电话。"]}
REPEATS = 2


def measure(client, target, rule):
    system = {"role": "system", "content": BASE + rule + "当前时间：" + time.strftime("%Y-%m-%d %H:%M %A")}
    report = {}
    for tool, prompts in PROMPTS.items():
        rows = []
        for prompt in prompts * REPEATS:
            result = stream_turn(client, *target, [system, {"role": "user", "content": prompt}], TOOLS)
            rows.append({"prompt": prompt, "called": tool in [call["name"] for call in result["calls"]], "spoke_first": result["text_before_tool"],
                         "content": result["content"], "error": result["error"]})
        report[tool] = {"samples": len(rows), "called": sum(row["called"] for row in rows),
                        "spoke_first": sum(row["spoke_first"] for row in rows), "rows": rows}
    return report


async def weather_turn(base_url, key, model):
    text, tools_seen = [], []

    async def on_event(event):
        if isinstance(event, TextDelta):
            text.append(event.text)
        else:
            tools_seen.append(event.name)

    def chat(messages, **options):
        return stream_chat(base_url, key, model, messages, **options)

    messages = [{"role": "system", "content": build_system_prompt(BASE, "")}, {"role": "user", "content": "北京明天天气怎么样？"}]
    started = time.perf_counter()
    added = await run_turn(messages, default_registry, chat, on_event)
    return {"reply": "".join(text), "tools": tools_seen, "seconds": round(time.perf_counter() - started, 2),
            "tool_result": next((message["content"] for message in added if message["role"] == "tool"), None)}


def main():
    sys.stdout.reconfigure(errors="replace")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    key = normalize_key(os.getenv("DEEPSEEK_API_KEY", "") or getpass.getpass("DeepSeek API Key (hidden): "))
    print(f"Model: {model}  Key fingerprint: {fingerprint(key)}")
    target = (base_url, key, model)
    with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
        report = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model,
                  "old_rule": measure(client, target, OLD_RULE), "new_rule": measure(client, target, TOOL_RULES)}
    # A failed weather turn must not lose the counts already measured.
    try:
        report["weather_turn"] = asyncio.run(weather_turn(base_url, key, model))
    except Exception as error:
        report["weather_turn"] = {"error": f"{type(error).__name__}: {error}"}
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for arm in ("old_rule", "new_rule"):
        for tool in PROMPTS:
            row = report[arm][tool]
            errors = [item["error"] for item in row["rows"] if item["error"]]
            print(f"{arm} {tool}: called {row['called']}/{row['samples']}, spoke first {row['spoke_first']}/{row['samples']}, "
                  f"errors {len(errors)}{' first: ' + errors[0] if errors else ''}")
    print("weather:", report["weather_turn"])
    print("Saved", RESULT)


if __name__ == "__main__":
    main()
