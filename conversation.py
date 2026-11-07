"""Chat history kept as whole turns, so trimming can never separate a tool call from its result."""
MAX_TURNS = 6


class Conversation:
    def __init__(self):
        self.turns = []

    def add_turn(self, messages):
        self.turns.append(list(messages))
        del self.turns[:-MAX_TURNS]

    def messages(self):
        return [message for turn in self.turns for message in turn]
