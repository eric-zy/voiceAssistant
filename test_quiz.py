from datetime import datetime, timedelta
import json
import random

import httpx
from fastapi.testclient import TestClient
import pytest

import app as backend
import memory_store
import practice
import quiz

ORIGIN = {"origin": "http://127.0.0.1:8765"}
NOW = datetime(2026, 11, 9, 10, 0)


# The schedule

@pytest.mark.parametrize("box,correct,expected", [
    (0, True, (1, "2026-11-10T10:00:00", False)), (1, True, (2, "2026-11-11T10:00:00", False)),
    (4, True, (5, "2026-11-24T10:00:00", False)), (5, True, (5, "2026-11-09T10:00:00", True)),
    (3, False, (1, "2026-11-10T10:00:00", False)), (0, False, (1, "2026-11-10T10:00:00", False))])
def test_right_answers_space_reviews_out_and_a_wrong_one_brings_it_back_tomorrow(box, correct, expected):
    assert memory_store.next_review(box, correct, NOW) == expected


def test_due_items_are_mistakes_first_then_the_longest_waiting(user):
    store = user.english
    word = store.add_item("word", "rough", "难熬的")
    mistake = store.add_item("mistake", "He go", "He goes")
    later = store.add_item("phrase", "sounds rough", "听起来很糟")
    mastered = store.add_item("word", "cinema", "电影院")
    store.set_mastered(mastered)
    store.record_review(later, True)                         # now due tomorrow
    assert [item["id"] for item in store.due()] == [mistake, word]
    assert store.due(kinds=("word", "phrase")) == [store.get_item(word)]
    count, upcoming = store.due_count()
    assert count == 2 and upcoming
    assert [item["id"] for item in store.due(moment=datetime.now() + timedelta(days=2))] == [mistake, word, later]
    item = store.get_item(later)
    assert item["box"] == 1 and item["tested"] == 1 and item["correct"] == 1
    assert store.record_review(999, True) is None


# Building and grading

def item(id, kind, text, meaning="", context="", phonetic="", note=""):
    return {"id": id, "kind": kind, "text": text, "meaning": meaning, "context": context, "phonetic": phonetic, "note": note}


def test_each_kind_of_item_gets_its_kind_of_question():
    assert quiz.plan(item(1, "mistake", "He go", "He goes")) == "correct"
    assert quiz.plan(item(1, "sentence", "Tell me more", "多说点")) == "translate"
    assert quiz.plan(item(2, "sentence", "Tell me more", "多说点")) == "dictation"
    assert quiz.plan(item(3, "word", "cinema")) == "dictation"                       # no meaning: a pronunciation word
    assert quiz.plan(item(4, "word", "rough", "难熬的", "That sounds rough.")) == "cloze"
    assert quiz.plan(item(5, "word", "rough", "难熬的")) == "choice"
    assert quiz.plan(item(6, "word", "rough", "难熬的")) == "spell"
    assert quiz.blank_out("That sounds rough today.", "rough") == "That sounds _____ today."
    assert quiz.blank_out("roughly speaking", "rough") is None


def test_questions_carry_their_answers_and_the_page_never_sees_them():
    items = [item(1, "mistake", "He go to school", "He goes to school", note="第三人称"), item(5, "word", "rough", "难熬的", phonetic="/rʌf/"),
             item(7, "word", "bold", "大胆的"), item(4, "word", "rough", "难熬的", "That sounds rough.")]
    questions = quiz.build(items, {5: ["光滑的", "安静的", "明亮的"]}, random.Random(1))
    assert [q["type"] for q in questions] == ["correct", "choice", "spell", "cloze"]   # bold had no wrong options: spell
    choice = questions[1]
    assert len(choice["options"]) == 4 and choice["options"][choice["expected"]] == "难熬的"
    assert questions[3]["prompt"] == "That sounds _____." and questions[3]["expected"] == "rough"
    assert all("expected" not in q for q in quiz.for_page(questions))
    assert len(quiz.build([item(i, "word", f"w{i}", "义") for i in range(20)], {})) == quiz.MAX_QUESTIONS


def test_rules_grade_what_they_can_and_leave_judgement_to_the_grader():
    choice = {"type": "choice", "expected": 2}
    assert quiz.grade_locally(choice, 2) is True and quiz.grade_locally(choice, 1) is False and quiz.grade_locally(choice, True) is False
    spell = {"type": "spell", "expected": "sounds rough"}
    assert quiz.grade_locally(spell, "Sounds  rough!") is True and quiz.grade_locally(spell, "sound rough") == quiz.NEAR
    assert quiz.grade_locally(spell, "looks rough") is False
    assert quiz.grade_locally(spell, "") is False
    dictation = {"type": "dictation", "expected": "Tell me what's going on."}
    assert quiz.grade_locally(dictation, "tell me what's going on") is True and quiz.grade_locally(dictation, "tell me going") is False
    assert quiz.grade_locally({"type": "correct", "expected": "He goes"}, "He goes") is None
    assert quiz.grade_locally({"type": "translate", "expected": "多说点"}, " ") is False
    assert quiz.parse_grades('{"1": {"ok": true, "comment": "好"}, "2": {"ok": "yes"}, "x": {}}') == {1: {"ok": True, "comment": "好"}}
    assert quiz.parse_distractors('{"5": ["a", "b", "c", "d"], "6": ["a"], "y": ["a", "b", "c"]}') == {5: ["a", "b", "c"]}


# The quiz over the API

def sse(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + \
           '\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"completion_tokens":10,' \
           '"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":50}}\n\ndata: [DONE]\n\n'


@pytest.fixture
def model(user, monkeypatch):
    replies = {"quiz_maker": None, "grader": None}

    def handler(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        material = body["messages"][-1]["content"]
        if system.startswith(quiz.DISTRACTORS.instructions[:8]):
            ids = [line.split("：")[0] for line in material.splitlines() if "正确释义" in line]
            text = json.dumps({key.strip()[-3:].strip(): ["光滑的", "安静的", "明亮的"] for key in ids}, ensure_ascii=False)
        elif system.startswith(quiz.GRADER.instructions[:8]):
            numbers = [line.split(".")[0].strip() for line in material.splitlines() if "学生答案" in line]
            text = json.dumps({n: {"ok": "goes" in material.split(f"{n}.")[1].split("学生答案：")[1].split("\n")[0], "comment": "看过了"} for n in numbers}, ensure_ascii=False)
        elif system.startswith("You are a friendly English conversation partner"):
            text = "Nice! Tell me more."
        elif system.startswith(practice.CORRECTOR.instructions[:12]):
            text = '{"ok": true, "logic": {"score": 5, "comment": "好"}}'
        else:
            text = "你好。"
        return httpx.Response(200, text=sse(text))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(backend, "API_KEY", "test-key")
    monkeypatch.setattr(backend, "LLM_MODEL", "deepseek-flash")

    async def synthesize(text, choice=None):
        return b"ID3fake"
    monkeypatch.setattr(backend, "synthesize", synthesize)
    return replies


def test_nothing_due_means_no_quiz(user, model):
    with TestClient(backend.app) as client:
        response = client.post("/api/english/quizzes", headers=ORIGIN)
        assert response.status_code == 400 and "先在对话里积累" in response.json()["detail"]
        assert client.get("/api/english/review").json() == {"due": 0, "next_due_at": None, "quizzes": []}


def test_a_quiz_is_built_answered_graded_and_moves_each_item_along(user, model):
    store = user.english
    mistake = store.add_item("mistake", "He go to school", "He goes to school", note="第三人称单数")
    word = store.add_item("word", "rough", "难熬的", phonetic="/rʌf/")            # id 2: spell
    store.add_item("word", "bold", "大胆的")                                     # id 3: choice
    with TestClient(backend.app) as client:
        assert client.get("/api/english/review").json()["due"] == 3
        started = client.post("/api/english/quizzes", headers=ORIGIN).json()
        questions = {q["type"]: q for q in started["questions"]}
        assert set(questions) == {"correct", "spell", "choice"} and all("expected" not in q for q in started["questions"])
        choice = questions["choice"]
        right = choice["options"].index("大胆的")
        answers = {str(questions["correct"]["id"]): "He goes to school.", str(questions["spell"]["id"]): "ruff", str(choice["id"]): right}
        graded = client.post(f"/api/english/quizzes/{started['id']}/submit", json={"answers": answers}, headers=ORIGIN).json()
        assert graded["score"] == 2 and graded["total"] == 3
        by_type = {q["type"]: r for q in started["questions"] for r in graded["results"] if r["id"] == q["id"]}
        assert by_type["correct"]["ok"] and by_type["correct"]["comment"] == "看过了"
        assert not by_type["spell"]["ok"] and by_type["spell"]["expected"] == "rough"
        assert by_type["choice"]["expected"] == "大胆的"
        assert client.post(f"/api/english/quizzes/{started['id']}/submit", json={"answers": answers}, headers=ORIGIN).status_code == 409
        assert client.post("/api/english/quizzes/999/submit", json={"answers": {}}, headers=ORIGIN).status_code == 404
        review = client.get("/api/english/review").json()
        assert review["due"] == 0 and review["quizzes"][0]["score"] == 2
    assert store.get_item(mistake)["box"] == 1 and store.get_item(word)["box"] == 1 and store.get_item(word)["correct"] == 0


def test_without_the_model_wrong_options_come_from_other_items_and_open_answers_by_closeness(user, model, monkeypatch):
    monkeypatch.setattr(backend, "API_KEY", "")
    store = user.english
    for i, (text, meaning) in enumerate([("a", "甲"), ("bb", "乙"), ("ccc", "丙"), ("dddd", "丁")]):
        store.add_item("word", text, meaning)
    store.add_item("mistake", "She have a cat", "She has a cat")
    with TestClient(backend.app) as client:
        started = client.post("/api/english/quizzes", headers=ORIGIN).json()
        kinds = [q["type"] for q in started["questions"]]
        assert "choice" in kinds and kinds[0] == "correct"
        correct = started["questions"][0]
        graded = client.post(f"/api/english/quizzes/{started['id']}/submit", json={"answers": {str(correct["id"]): "She has a cat"}}, headers=ORIGIN).json()
        assert graded["results"][0]["ok"] is True and graded["results"][0]["comment"] == ""


def test_due_words_go_to_the_partner_and_using_one_counts_as_a_review(user, model):
    word = user.english.add_item("word", "rough", "难熬的")
    with TestClient(backend.app) as client, client.websocket_connect("/ws", headers=ORIGIN) as socket:
        socket.send_json({"type": "practice_start", "request_id": 1, "level": "intermediate"})
        while (message := socket.receive_json())["type"] != "practice_started":
            pass
        assert message["review_words"] == ["rough"]
        while socket.receive_json()["type"] != "done":
            pass
        socket.send_json({"type": "text", "request_id": 2, "text": "My week was rough, honestly."})
        review = None
        while review is None:
            message = socket.receive_json()
            if message["type"] == "practice_review":
                review = message
        assert review["used"] == ["rough"]
    assert user.english.get_item(word)["box"] == 1 and user.english.get_item(word)["correct"] == 1
    assert practice.used_words("Rough day! Roughly fine.", ["rough", "fine", "bad"]) == ["rough", "fine"]
    assert "rough, bold" in practice.partner_prompt("beginner", "", ["rough", "bold"])


# Nearly right spelling, and looking back at finished quizzes

@pytest.mark.parametrize("a,b,expected", [("weekday", "weekend", False), ("rouh", "rough", True), ("roughh", "rough", True),
                                          ("rouhg", "rough", False), ("rough", "rough", False), ("cut", "cat", True), ("", "a", True)])
def test_one_edit_apart(a, b, expected):
    assert quiz.one_edit_apart(a, b) is expected


def test_a_spelling_one_letter_off_is_nearly_right_only_for_longer_words():
    assert quiz.grade_locally({"type": "spell", "expected": "weekend"}, "weekand") == quiz.NEAR
    assert quiz.grade_locally({"type": "cloze", "expected": "rough"}, "Rough") is True
    assert quiz.grade_locally({"type": "spell", "expected": "cat"}, "cut") is False
    assert quiz.grade_locally({"type": "spell", "expected": "weekend"}, "") is False


def test_nearly_right_keeps_the_item_where_it_was_and_asks_again_tomorrow(user):
    word = user.english.add_item("word", "weekend", "周末")
    user.english.record_review(word, True)
    user.english.record_review(word, True, moment=datetime.now() + timedelta(days=2))      # box 2
    review = user.english.record_review(word, None)
    item = user.english.get_item(word)
    assert item["box"] == 2 and item["tested"] == 3 and item["correct"] == 2 and not review["mastered"]
    assert review["due_at"][:10] == (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def test_a_finished_quiz_can_be_looked_at_again_and_a_near_miss_is_reported(user, model):
    store = user.english
    store.add_item("word", "rough", "难熬的")          # id 1 -> choice? odd ids are choice; with no other items it becomes spell
    with TestClient(backend.app) as client:
        started = client.post("/api/english/quizzes", headers=ORIGIN).json()
        question = started["questions"][0]
        assert question["type"] in ("spell", "choice")
        answer = "rugh" if question["type"] == "spell" else question["options"].index("难熬的")
        graded = client.post(f"/api/english/quizzes/{started['id']}/submit", json={"answers": {str(question["id"]): answer}}, headers=ORIGIN).json()
        if question["type"] == "spell":
            assert graded["results"][0]["near"] is True and "正确是 rough" in graded["results"][0]["comment"]
        history = client.get("/api/english/quizzes").json()["quizzes"]
        assert history[0]["id"] == started["id"] and history[0]["count"] == 1
        detail = client.get(f"/api/english/quizzes/{started['id']}").json()
        assert detail["answers"] == {str(question["id"]): answer} and "expected" not in detail["questions"][0]
        assert detail["results"][0]["expected"] in ("rough", "难熬的")
        unfinished = client.post("/api/english/quizzes", headers=ORIGIN)
        assert client.get("/api/english/quizzes/999").status_code == 404
