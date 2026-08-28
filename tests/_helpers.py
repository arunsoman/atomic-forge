class ScriptedChatLLM:
    """Returns each entry in `turns` in order, one per chat() call."""

    def __init__(self, turns: list[str]):
        self.turns = list(turns)
        self.calls = 0

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        out = self.turns[self.calls] if self.calls < len(self.turns) else self.turns[-1]
        self.calls += 1
        return out
