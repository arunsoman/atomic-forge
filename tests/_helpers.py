import threading

from atomic_forge.llm import ChatTurn, ToolCall


class ScriptedToolCallLLM:
    """A minimal `chat_with_tools`-capable scripted LLM: returns each entry
    in `turns` in order, one per call. Each entry is either a plain string
    (content only, no tool calls) or a `(tool_name, arguments_dict)` pair /
    list of such pairs (one or more tool calls that turn, arguments
    JSON-encoded automatically). Needed to drive `run_agent`'s real
    function-calling path (`use_fc`), which the plain-text
    `ScriptedChatLLM`/`TurnByPositionScriptedLLM` helpers can't reach —
    `patch`'s optional `path` argument only exists on that path."""

    def __init__(self, turns: list):
        self.turns = list(turns)
        self.calls = 0
        self._lock = threading.Lock()

    def chat_with_tools(self, messages, tools, temperature=0.0, max_tokens=8192):
        import json
        with self._lock:
            idx = self.calls
            self.calls += 1
        entry = self.turns[idx] if idx < len(self.turns) else self.turns[-1]
        if isinstance(entry, str):
            return ChatTurn(content=entry, tool_calls=[])
        calls = entry if isinstance(entry, list) else [entry]
        tool_calls = [ToolCall(id=f"call_{idx}_{i}", name=name, arguments=json.dumps(args))
                     for i, (name, args) in enumerate(calls)]
        return ChatTurn(content="", tool_calls=tool_calls)


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
