"""Meeting notes: a recording becomes a transcript on this machine, then two agents write notes and to-dos.

Only the transcript text goes to the model. The to-do agent proposes reminders; nothing is created until the
user ticks them on the page. Speaker separation is left out of this first version; segments carry their time
so a speaker label can be attached later without changing the rest.
"""
import asyncio
from datetime import datetime
import json
import re

import av
import numpy as np

import agents
from agents import AgentSpec
from turn_detector import FRAME_BYTES, FRAME_SECONDS, SileroStream

SAMPLE_RATE = 16000
MAX_SECONDS = 3 * 60 * 60
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".webm", ".aac", ".flac", ".mp4"}
SPEECH_ON, SPEECH_OFF = .4, .35
JOIN_GAP_SECONDS = .8      # pauses shorter than this stay inside one segment
MAX_SEGMENT_SECONDS = 25   # the recognizer takes up to 30 s at a time
MIN_SEGMENT_SECONDS = .3
PAD_SECONDS = .2
TRANSCRIBE = None          # set by app.py: pcm bytes -> text, the same recognizer the live chat uses

NOTES_AGENT = AgentSpec("meeting_notes", (
    "你是会议记录员。根据会议逐字稿写纪要，用中文，分三部分：议题与结论（每个议题一段，写清定了什么）、"
    "分歧与未决问题、关键数字和时间。只写逐字稿里有的内容，不要补充或猜测。逐字稿由语音识别生成，可能有错字，"
    "按上下文理解，不确定的地方注明“（识别可能有误）”。不要写待办清单，另有人负责。"), max_yuan=0.05)
TODO_AGENT = AgentSpec("meeting_todo", (
    "你从会议逐字稿里找出明确分配或承诺的待办事项。只输出一个 JSON 数组，不要任何其他文字。每项："
    '{"task": "要做的事，一句话", "owner": "负责人，没说就空字符串", "due_text": "截止时间的原话，没说就空字符串", '
    '"due_at": "按会议日期换算的 YYYY-MM-DDTHH:MM:SS，只说日期就用当天 18:00，没说截止时间就空字符串"}。'
    "只收录有人明确要做的事，讨论中的想法不算。没有待办就输出 []。"), max_yuan=0.03)


def decode(path):
    """16 kHz mono PCM16 for any audio or video file PyAV can open; ValueError when it is not audio or too long."""
    try:
        container = av.open(str(path))
    except av.error.FFmpegError as error:
        raise ValueError("无法读取这个文件，请上传常见的音频格式") from error
    with container:
        if not container.streams.audio:
            raise ValueError("文件里没有音频")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        chunks, total = [], 0
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                data = out.to_ndarray().astype("<i2").tobytes()
                chunks.append(data)
                total += len(data)
                if total > MAX_SECONDS * SAMPLE_RATE * 2:
                    raise ValueError("录音超过 3 小时，请分段上传")
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().astype("<i2").tobytes())
    return b"".join(chunks)


def find_speech(pcm):
    """(start, end) seconds of each stretch of speech, short pauses joined, long stretches cut to fit the recognizer."""
    vad, spans, start, last_voice = SileroStream(), [], None, None
    frames = len(pcm) // FRAME_BYTES
    for index in range(frames):
        chance, moment = vad(pcm[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]), index * FRAME_SECONDS
        if chance >= SPEECH_ON or (start is not None and chance >= SPEECH_OFF):
            start = moment if start is None else start
            last_voice = moment + FRAME_SECONDS
        elif start is not None and moment - last_voice >= JOIN_GAP_SECONDS:
            spans.append((start, last_voice))
            start = None
    if start is not None:
        spans.append((start, last_voice))
    duration = len(pcm) / 2 / SAMPLE_RATE
    segments = []
    for begin, end in spans:
        if end - begin < MIN_SEGMENT_SECONDS:
            continue
        begin, end = max(0, begin - PAD_SECONDS), min(duration, end + PAD_SECONDS)
        while end - begin > MAX_SEGMENT_SECONDS:
            segments.append((begin, begin + MAX_SEGMENT_SECONDS))
            begin += MAX_SEGMENT_SECONDS
        segments.append((begin, end))
    return segments


def transcribe_pcm(pcm, transcribe, progress=lambda done, total: None, stopped=lambda: False):
    """[{start, end, text}] for every stretch of speech that produced words."""
    segments, lines = find_speech(pcm), []
    for number, (begin, end) in enumerate(segments, 1):
        if stopped():
            raise asyncio.CancelledError
        clip = pcm[int(begin * SAMPLE_RATE) * 2:int(end * SAMPLE_RATE) * 2]
        try:
            text = transcribe(clip).strip()
        except ValueError:  # too short after rounding, or the model is missing; the latter fails every clip
            text = ""
        if text:
            lines.append({"start": round(begin, 1), "end": round(end, 1), "text": text})
        progress(number, len(segments))
    return lines


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}"


def transcript_text(lines):
    return "\n".join(f"[{clock(line['start'])}] {line['text']}" for line in lines)


def parse_todos(text):
    """The agent's JSON array as clean proposals; anything malformed is dropped, never guessed at."""
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except json.JSONDecodeError:
        return []
    proposals = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("task", "")).strip():
            continue
        due_at = str(item.get("due_at") or "").strip()
        try:
            due_at = datetime.fromisoformat(due_at).replace(tzinfo=None).isoformat(timespec="seconds") if due_at else ""
        except ValueError:
            due_at = ""
        proposals.append({"task": str(item["task"]).strip()[:200], "owner": str(item.get("owner") or "").strip()[:40],
                          "due_text": str(item.get("due_text") or "").strip()[:40], "due_at": due_at})
    return proposals


@agents.feature("meeting_notes", timeout=30 * 60)
async def meeting_notes(job):
    path = job.context.root / "meetings" / job.payload["file"]
    job.progress("正在读取录音…")
    pcm = await asyncio.to_thread(decode, path)
    task_id, tasks = job.task_id, job.context.tasks

    def progress(done, total):
        if done == total or done % 5 == 0:
            job.progress(f"正在转写 {done}/{total} 段")

    # Transcription runs in a thread, which cancellation cannot interrupt; it checks between segments instead.
    lines = await asyncio.to_thread(transcribe_pcm, pcm, TRANSCRIBE, progress, lambda: tasks.get(task_id)["status"] == "cancelled")
    if not lines:
        return {"text": "录音里没有识别到说话内容。", "transcript": [], "proposals": [], "file": job.payload["file"]}
    job.progress("纪要和待办两个 agent 正在处理…")
    material = transcript_text(lines)
    meeting_date = job.payload.get("date") or datetime.now().strftime("%Y-%m-%d")
    notes, todos = await asyncio.gather(
        job.agent(NOTES_AGENT, material, task=f"会议日期：{meeting_date}。请写会议纪要。"),
        job.agent(TODO_AGENT, material, task=f"会议日期：{meeting_date}。请列出待办。"))
    return {"text": notes, "transcript": lines, "proposals": parse_todos(todos), "file": job.payload["file"],
            "seconds": round(len(pcm) / 2 / SAMPLE_RATE)}
