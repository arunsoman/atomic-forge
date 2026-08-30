import asyncio
import threading
from pathlib import Path

from atomic_forge.cie_backend import MCPBridge, MCPToolBackend


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


def test_mcpbridge_call_serializes_concurrent_threads():
    """Two repair samples (s0, s1) sharing one MCPBridge — the default
    parallel_samples=True in repair_agent.py — must never have two tool
    calls in flight to the CIE server at once. Regression test for
    astroid#769 (2026-08-30): concurrent access to CIE's plain
    (non-WAL) SQLite graph.db produced 10x 'OperationalError: database is
    locked' in one repair session, each one silently swapping a real
    graph query for CIE's own lower-quality heuristic-index fallback
    instead of erroring — a plausible driver of that session's dominant
    failure mode (every sample dying "stuck: identical action repeated
    5 times")."""
    bridge = object.__new__(MCPBridge)  # skip __init__'s real subprocess/stdio setup
    bridge._call_lock = threading.Lock()
    bridge.loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=bridge.loop.run_forever, daemon=True)
    loop_thread.start()

    in_flight = {"count": 0, "max": 0}
    counter_lock = threading.Lock()

    class _FakeSession:
        async def call_tool(self, name, kwargs):
            with counter_lock:
                in_flight["count"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["count"])
            await asyncio.sleep(0.05)  # stand-in for a real MCP round-trip
            with counter_lock:
                in_flight["count"] -= 1
            return _FakeResult('{"ok": true, "results": []}')

    bridge.session = _FakeSession()

    results = []

    def worker():
        results.append(bridge.call("view_file", path="x.py"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bridge.loop.call_soon_threadsafe(bridge.loop.stop)
    loop_thread.join(timeout=2)

    assert in_flight["max"] == 1, "more than one CIE call was in flight at once"
    assert len(results) == 4
    assert all(r == {"ok": True, "results": []} for r in results)


def test_statement_graph_delegates_to_local_backend_not_bridge(tmp_path):
    """CIE has no statement_graph equivalent; MCPToolBackend must serve it
    from forge's own LocalToolBackend against the on-disk checkout rather
    than relay to the MCP bridge (there's nothing to relay to). Regression
    guard: REPAIR_SYSTEM's prompt tells the model to call this tool for
    deep localization in long functions, and before this method existed
    that instruction was dead on every CIE-backed run (forge fix's only
    path) — the tool simply wasn't in the manifest the model could see."""
    (tmp_path / "mod.py").write_text("def add(a, b):\n    x = a + b\n    return x\n")

    class _Bridge:
        def call(self, tool_name, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError(f"statement_graph must not touch the MCP bridge, got {tool_name}")

    backend = MCPToolBackend(_Bridge(), tmp_path)
    env = backend.statement_graph("mod.py", line=2, radius=5)
    assert env["ok"]
    assert env["results"]


def test_statement_graph_is_in_the_manifest():
    """describe()'s introspection (what the model actually sees as
    AVAILABLE TOOLS) must include statement_graph now that it's a real
    method — this is the exact gap that made REPAIR_SYSTEM's instruction
    unreachable."""
    assert "statement_graph" in vars(MCPToolBackend)


def test_hybrid_search_relays_to_bridge_and_rewrites_absolute_paths(tmp_path):
    (tmp_path / "dispatch.py").write_text("def handler():\n    pass\n")
    abs_path = str((tmp_path / "dispatch.py").resolve())

    class _Bridge:
        def call(self, tool_name, **kwargs):
            assert tool_name == "hybrid_search"
            assert kwargs["query"] == "some query"
            return {"ok": True, "results": [{"source_file": abs_path, "name": "handler"}]}

    backend = MCPToolBackend(_Bridge(), tmp_path)
    env = backend.hybrid_search("some query", top_k=5)
    assert env["results"][0]["source_file"] == "dispatch.py"
