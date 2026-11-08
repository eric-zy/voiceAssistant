"""Review quizzes: questions built from the items that are due, answers graded here where a rule can decide and by
a grader agent where it takes judgement (a translation, a corrected sentence).

Kinds of question, by item:
  mistake            -> correct:    the learner's own wrong sentence; write it right
  sentence           -> dictation:  hear it (the standard voice), type it      (alternates with translate)
                        translate:  write it in Chinese
  word / phrase      -> cloze:      the sentence it came from, with it blanked  (when that sentence holds it)
                        choice:     pick its meaning from four                  (needs a meaning and three wrong ones)
                        spell:      its meaning (and phonetic), write it
  word with no meaning (a pronunciation word) -> dictation
The answer key stays on the server; the page gets the questions without it.
"""
import difflib
import json
import random
import re

from agents import AgentSpec
import practice

MAX_QUESTIONS = 10
DICTATION_PASS = .9
OPEN_PASS = .8  # used only when no grader is available: closeness to the expected answer

DISTRACTORS = AgentSpec("quiz_maker", (
    "你给英语选择题出干扰项。对每个条目，给出 3 个看起来合理但错误的中文释义，和正确释义词性、长度相近，不能和正确释义同义。"
    "只输出 JSON，不要其他文字：{\"条目编号\": [\"干扰项1\", \"干扰项2\", \"干扰项3\"]}。"), max_yuan=0.01, max_tokens=800)
GRADER = AgentSpec("grader", (
    "你是英语老师，批改学生的答案。改错题：学生的句子只要改掉了原来的问题、语法正确、意思不变就算对，不必和参考答案一字不差。"
    "翻译题：中文意思准确、通顺就算对。对每题给出是否正确和中文一句话点评。"
    "只输出 JSON，不要其他文字：{\"题目编号\": {\"ok\": true 或 false, \"comment\": \"点评\"}}。"), max_yuan=0.01, max_tokens=800)


def normalize(text):
    return " ".join(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower().replace("’", "'")))


def blank_out(sentence, target):
    """The sentence with the target replaced by a blank, or None when the sentence does not contain it."""
    match = re.search(r"(?<![A-Za-z])" + re.escape(target.strip()) + r"(?![A-Za-z])", sentence or "", re.I)
    if not match:
        return None
    return sentence[:match.start()] + "_____" + sentence[match.end():]


def plan(item):
    """Which kind of question an item gets."""
    if item["kind"] == "mistake":
        return "correct"
    if item["kind"] == "sentence":
        return "translate" if item["meaning"] and item["id"] % 2 else "dictation"
    if not item["meaning"]:
        return "dictation"
    if blank_out(item["context"], item["text"]) and item["context"].strip().lower() != item["text"].strip().lower():
        return "cloze"
    return "choice" if item["id"] % 2 else "spell"


def build(items, distractors, rng=random):
    """Questions with their answer key. distractors: {item id: [three wrong meanings]} for choice questions;
    an item planned as choice without three of them is asked as spell instead."""
    questions = []
    for number, item in enumerate(items[:MAX_QUESTIONS], 1):
        kind = plan(item)
        if kind == "choice" and len(distractors.get(item["id"], [])) < 3:
            kind = "spell"
        question = {"id": number, "item_id": item["id"], "type": kind}
        if kind == "correct":
            question.update(prompt=item["text"], hint=item["note"], expected=item["meaning"])
        elif kind == "dictation":
            question.update(say=item["text"], hint=item["meaning"], expected=item["text"])
        elif kind == "translate":
            question.update(prompt=item["text"], expected=item["meaning"])
        elif kind == "cloze":
            question.update(prompt=blank_out(item["context"], item["text"]), hint=item["meaning"], expected=item["text"])
        elif kind == "spell":
            question.update(prompt=item["meaning"], hint=item["phonetic"], expected=item["text"])
        else:
            options = [item["meaning"], *distractors[item["id"]][:3]]
            rng.shuffle(options)
            question.update(prompt=item["text"], hint=item["phonetic"], options=options, expected=options.index(item["meaning"]))
        questions.append(question)
    return questions


def for_page(questions):
    """What the page may see: everything but the answer key."""
    return [{key: value for key, value in question.items() if key != "expected"} for question in questions]


def distractor_material(items):
    return "\n".join(f"{item['id']}：{item['text']} —— 正确释义：{item['meaning']}" for item in items)


def parse_distractors(text):
    value = practice.parse_object(text) or {}
    result = {}
    for key, options in value.items():
        if str(key).isdigit() and isinstance(options, list):
            clean = [str(option).strip()[:60] for option in options if str(option).strip()]
            if len(clean) >= 3:
                result[int(key)] = clean[:3]
    return result


def grade_locally(question, answer):
    """True or False when a rule decides; None when it takes judgement (the grader agent's job)."""
    kind = question["type"]
    if kind == "choice":
        return isinstance(answer, int) and not isinstance(answer, bool) and answer == question["expected"]
    answer = answer if isinstance(answer, str) else ""
    if kind in ("cloze", "spell"):
        return normalize(answer) == normalize(question["expected"]) and bool(normalize(answer))
    if kind == "dictation":
        return practice.read_aloud(question["expected"], answer)["score"] >= DICTATION_PASS
    if not answer.strip():
        return False
    return None


def grader_material(questions, answers):
    lines = []
    for question in questions:
        name = "改错题：原句" if question["type"] == "correct" else "翻译题：英文"
        lines.append(f"{question['id']}. {name}：{question['prompt']} | 参考答案：{question['expected']} | 学生答案：{answers.get(str(question['id']), '')}")
    return "\n".join(lines)


def parse_grades(text):
    value = practice.parse_object(text) or {}
    grades = {}
    for key, grade in value.items():
        if str(key).isdigit() and isinstance(grade, dict) and isinstance(grade.get("ok"), bool):
            grades[int(key)] = {"ok": grade["ok"], "comment": str(grade.get("comment") or "").strip()[:200]}
    return grades


def closeness(expected, answer):
    """Fallback for open questions when no grader answered: how close the answer is to the reference."""
    return difflib.SequenceMatcher(None, (expected or "").lower(), (answer or "").lower()).ratio()
