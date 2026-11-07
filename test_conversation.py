from conversation import Conversation, MAX_TURNS


def chat_turn(number):
    return [{"role": "user", "content": f"问{number}"}, {"role": "assistant", "content": f"答{number}"}]


def tool_turn(number):
    call = {"id": f"call{number}", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    return [{"role": "user", "content": f"问{number}"}, {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": f"call{number}", "content": "中雨"}, {"role": "assistant", "content": f"答{number}"}]


def test_starts_empty():
    assert Conversation().messages() == []


def test_keeps_the_same_twelve_messages_the_old_trim_kept():
    history = Conversation()
    old = []
    for number in range(20):
        history.add_turn(chat_turn(number))
        old.extend(chat_turn(number))
        del old[:-12]
    assert history.messages() == old


def test_turns_stay_in_order():
    history = Conversation()
    history.add_turn(chat_turn(1))
    history.add_turn(chat_turn(2))
    assert [message["content"] for message in history.messages()] == ["问1", "答1", "问2", "答2"]


def test_trimming_never_separates_a_tool_call_from_its_result():
    history = Conversation()
    for number in range(MAX_TURNS + 3):
        history.add_turn(tool_turn(number) if number % 2 else chat_turn(number))
    messages = history.messages()
    called = {call["id"] for message in messages for call in message.get("tool_calls", [])}
    answered = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
    assert called == answered and called
    assert messages[0]["role"] == "user"


def test_a_stored_turn_is_not_changed_by_its_caller():
    history = Conversation()
    turn = chat_turn(1)
    history.add_turn(turn)
    turn.clear()
    assert len(history.messages()) == 2
