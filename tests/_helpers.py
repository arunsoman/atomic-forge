import threading


class ScriptedChatLLM:
    """Returns each entry in `turns` in order, one per chat() call.

    Thread-safe (the read-and-increment of `self.calls` is lock-guarded):
    needed since repair_agent.py's K sampled repair attempts now run
    concurrently by default (see repair_loop_agentic's `parallel_samples`),
    so more than one thread can call `.chat()` on the same instance at
    once in a test that exercises a real (non-fast-path) repair round.
    """

    def __init__(self, turns: list[str]):
        self.turns = list(turns)
        self.calls = 0
        self._lock = threading.Lock()

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        with self._lock:
            idx = self.calls
            self.calls += 1
        return self.turns[idx] if idx < len(self.turns) else self.turns[-1]


class TurnByPositionScriptedLLM:
    """Like ScriptedChatLLM, but indexes `turns` by how many prior
    assistant turns are already in the conversation passed to `chat()`,
    not by a shared global call counter.

    Needed for tests that run MULTIPLE independent agent conversations
    concurrently against ONE shared LLM instance (e.g. repair_agent.py's
    parallel K-sampling, `parallel_samples=True`) — a global-counter mock
    like ScriptedChatLLM hands out turns in whatever order threads happen
    to call in, which breaks as soon as more than one conversation is in
    flight (thread A's first call could receive the response scripted for
    thread B's second call). Reading position from `messages` itself is
    correct regardless of interleaving, since each conversation's own
    history only ever grows from that same conversation's own prior
    calls."""

    def __init__(self, turns: list[str]):
        self.turns = list(turns)

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        idx = sum(1 for m in messages if m.get("role") == "assistant")
        return self.turns[idx] if idx < len(self.turns) else self.turns[-1]
