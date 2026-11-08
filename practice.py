"""Free English conversation: a partner talks in the foreground at the learner's level, a corrector marks each
learner sentence in the background, every correction goes into the mistake book, and a report agent sums up
the session when it ends.

The partner is the live voice loop with another prompt, an English voice, English recognition and no tools,
so it stays as quick as a normal reply. Everything else runs beside it and never delays an answer.
"""
import difflib
import json
import re

import agents
from agents import AgentSpec

ENGLISH_VOICE = "en-US-JennyNeural"
HISTORY_MESSAGES = 12
MAX_TOPIC_CHARS = 100
MAX_SAY_CHARS = 300
LEVELS = {
    "beginner": ("初级", "a beginner (about CEFR A2): use very common words and short simple sentences, speak slowly and clearly"),
    "intermediate": ("中级", "an intermediate learner (about CEFR B1-B2): everyday vocabulary, some idioms, natural pace"),
    "advanced": ("高级", "an advanced learner (about CEFR C1): speak naturally as to a native speaker, idioms welcome"),
}
OPENING = "(The learner has just joined. Greet them warmly and open the conversation with one short question.)"


def used_words(sentence, words_to_find):
    """Which of the given words or phrases appear in the sentence, as whole words, whatever the case."""
    return [word for word in words_to_find if re.search(r"(?<![A-Za-z])" + re.escape(word.strip()) + r"(?![A-Za-z])", sentence, re.I)]


def partner_prompt(level, topic="", review_words=()):
    about = (f"The learner would like to talk about: {topic}. Start there, and follow wherever they take it. "
             if topic else "Talk about anything the learner likes; if they have no idea, suggest an everyday topic. ")
    if review_words:
        about += ("The learner is reviewing these words; use one now and then where it fits naturally, and invite them to "
                  "use it too, without turning the chat into a lesson: " + ", ".join(review_words) + ". ")
    return ("You are a friendly English conversation partner for a Chinese speaker. "
            f"The learner is {LEVELS[level][1]}. " + about +
            "Speak only English, at most three short sentences per turn, and usually end with a question so the "
            "conversation keeps going. If the learner speaks Chinese or seems stuck, first help in one short Chinese "
            "sentence, then continue in English. If they ask what a word or sentence means, explain it simply and "
            "give one example. Do not correct their mistakes; another teacher does that quietly. "
            "No lists, no markdown, no emojis.")


def history_for(turns):
    """The partner's chat history, rebuilt from the saved turns: taking turns back is just a shorter list."""
    return [{"role": "user", "content": OPENING}] + [
        {"role": "assistant" if turn["who"] == "tutor" else "user", "content": turn["text"]} for turn in turns]


def is_learner_turn(turns, index):
    """Only the learner's own sentences can be said again; the partner's opening cannot."""
    return isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(turns) and turns[index]["who"] == "learner"


def split_english(pending):
    """A sentence is done once the next one has started, so 3.5 is never cut; the last one is flushed at the end."""
    match = re.search(r"[.!?;]\s+(?=\S)|\n", pending)
    if match:
        return pending[:match.end()], pending[match.end():]
    if len(pending) >= 160:
        return pending[:160], pending[160:]
    return None, pending


CORRECTOR = AgentSpec("corrector", (
    "你是英语口语老师。看学生说的这一句英语（陪练上一句只是上下文），做两件事。"
    "一、语言：只指出最重要的一个语法、用词或表达问题。这句话来自语音识别，明显是识别错误的不算学生的错；"
    "学生说中文、或者在问某个词的意思时，不算错误。"
    "二、内容：这句话是否回应了陪练、意思是否清楚、前后是否合乎逻辑，给 1 到 5 分（5 最好）和中文一句话评语。"
    "只输出 JSON，不要其他文字：语言没有问题时 {\"ok\": true, \"logic\": {\"score\": 分数, \"comment\": \"评语\"}}；"
    "有问题时 {\"ok\": false, \"type\": \"语法\" 或 \"用词\" 或 \"表达\", \"original\": \"原句中有问题的部分\", "
    "\"corrected\": \"改正后的写法\", \"better\": \"更地道的完整说法，可为空\", \"explain\": \"中文一句话说明\", "
    "\"logic\": {\"score\": 分数, \"comment\": \"评语\"}}。"), max_yuan=0.005, max_tokens=400)


def parse_logic(text):
    """The content judgement: a 1-5 score and a comment, or None when the answer had none usable."""
    logic = (parse_object(text) or {}).get("logic")
    if not isinstance(logic, dict):
        return None
    try:
        score = int(logic.get("score"))
    except (TypeError, ValueError):
        return None
    if not 1 <= score <= 5:
        return None
    return {"score": score, "comment": str(logic.get("comment") or "").strip()[:200]}


def wav16k(pcm):
    """A WAV file around 16 kHz mono PCM16, which is what the recognizer and the recordings use."""
    import io
    import wave
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    return data.getvalue()


PAUSE_SECONDS = .5


def speech_stats(pcm, text, chance):
    """A local fluency reference from one recording: speaking time, rate and pauses. Not a pronunciation score.

    chance(frame) is the speech probability of one 32 ms frame (Silero), passed in so tests can stand in for it.
    """
    from turn_detector import FRAME_BYTES, FRAME_SECONDS
    flags = [chance(pcm[i:i + FRAME_BYTES]) >= .4 for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    if not any(flags):
        return None
    first, last = flags.index(True), len(flags) - 1 - flags[::-1].index(True)
    pauses, run = [], 0
    for flag in flags[first:last + 1]:
        if flag:
            if run * FRAME_SECONDS >= PAUSE_SECONDS:
                pauses.append(run * FRAME_SECONDS)
            run = 0
        else:
            run += 1
    speaking = (last - first + 1) * FRAME_SECONDS
    count = len(words(text))
    return {"seconds": round(len(pcm) / 32000, 1), "speaking": round(speaking, 1),
            "wpm": round(count / speaking * 60) if count and speaking else 0,
            "pauses": len(pauses), "longest_pause": round(max(pauses), 1) if pauses else 0}

REPORTER = AgentSpec("practice_report", (
    "你是英语口语老师。根据一次自由英语对话的记录和逐句批注写简短总结，用中文，三部分："
    "说得好的地方；主要问题（最多三类，每类举一个本次的例子和改法）；下次可以多练的说法。语气鼓励，具体，不空泛。"),
    max_yuan=0.03, max_tokens=1000)


def parse_object(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        value = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_note(text, heard):
    """One correction, or None when the sentence was fine or the answer was not usable."""
    note = parse_object(text)
    if not note or note.get("ok") is not False:
        return None
    fields = {key: str(note.get(key) or "").strip()[:200] for key in ("original", "corrected", "better", "explain")}
    if not fields["corrected"] and not fields["better"]:
        return None
    # "category", not "type": the note travels inside a socket message whose own "type" says what it is.
    category = str(note.get("type") or "")
    return {"heard": heard[:300], "category": category if category in ("语法", "用词", "表达") else "表达", **fields}


def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))


def read_aloud(target, heard):
    """Each target word marked as heard or not, and the share heard. Order matters: words are aligned, not counted."""
    expected, said = words(target), words(heard)
    matched = set()
    for block in difflib.SequenceMatcher(None, expected, said, autojunk=False).get_matching_blocks():
        matched.update(range(block.a, block.a + block.size))
    marked = [{"word": word, "ok": index in matched} for index, word in enumerate(expected)]
    return {"heard": heard, "words": marked, "score": round(len(matched) / len(expected), 2) if expected else 0}


def transcript_text(turns):
    return "\n".join(("陪练" if turn["who"] == "tutor" else "学生") + "：" + turn["text"] for turn in turns)


def notes_text(notes):
    return "\n".join(f"- [{note['category']}] 学生说：{note['heard']} → 改为：{note['corrected'] or note['better']}（{note['explain']}）"
                     for note in notes) or "（没有批注）"


@agents.feature("practice_report", timeout=5 * 60)
async def practice_report(job):
    store, session_id = job.context.english, job.payload["session_id"]
    session = store.session(session_id)
    if not session or not session["transcript"]:
        return {"text": "这次对话没有内容，没有生成总结。", "session_id": session_id}
    job.progress("报告 agent 正在写总结…")
    topic = f"话题：{session['topic']}\n" if session["topic"] else ""
    material = f"水平：{LEVELS[session['level']][0]}\n{topic}\n对话记录：\n{transcript_text(session['transcript'])}\n\n逐句批注：\n{notes_text(session['notes'])}"
    report = await job.agent(REPORTER, material, task="请写这次对话的总结。")
    store.set_report(session_id, report)
    return {"text": report, "session_id": session_id, "mistakes": len(session["notes"])}


# Understanding the partner: a translation of any sentence, and an explanation of any selected word or phrase.
MAX_LOOKUP_CHARS = 200
TRANSLATOR = AgentSpec("translator", "把用户给的英文翻译成自然、口语化的中文。只输出译文，不要解释，不要加引号。",
                       max_yuan=0.002, max_tokens=300)
EXPLAINER = AgentSpec("explainer", (
    "你是英语老师，给中国学生解释他选中的英文单词、短语或句子，结合它所在的句子理解意思。只输出 JSON，不要其他文字："
    '{"phonetic": "美式音标，如 /ˈsɪnəmə/；句子可为空", "meaning": "在这句话里的中文意思；句子给出中文翻译", '
    '"note": "用法、搭配或句子结构，中文一句话，可为空", "example": "一个简单的英文例句，可为空"}。'), max_yuan=0.003, max_tokens=300)


def lookup_kind(text):
    """A single word, a short phrase, or a sentence; decides which list it goes into."""
    count = len(words(text))
    return "word" if count <= 1 else "phrase" if count <= 4 else "sentence"


def parse_explanation(text):
    """The explainer's answer, or None when it has no meaning in it."""
    value = parse_object(text) or {}
    result = {key: str(value.get(key) or "").strip()[:300] for key in ("phonetic", "meaning", "note", "example")}
    return result if result["meaning"] else None
