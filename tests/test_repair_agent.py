from pathlib import Path

from atomic_forge.repair_agent import (
    _blast_radius_violations, _diff_size, extract_signals, localize, repair_loop_agentic,
)
from atomic_forge.sandbox import ensure_repo
from atomic_forge.tools import GraphToolBackend, LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import TurnByPositionScriptedLLM


def test_extract_signals_pytest_traceback():
    out = (
        "src/app/service.py:12: in run\n"
        "    raise ValueError('bad')\n"
        "ValueError: bad\n"
        "tests/test_service.py::test_run FAILED\n"
    )
    sig = extract_signals(out)
    assert "src/app/service.py" in sig.traceback_paths
    assert "tests/test_service.py::test_run" in sig.test_nodes
    assert "ValueError" in sig.exception_types


def test_extract_signals_import_error_symbol():
    out = "ImportError: cannot import name 'helper' from 'app.utils'"
    sig = extract_signals(out)
    assert "helper" in sig.symbol_names


def test_extract_signals_node_missing_module():
    out = "Cannot find module '../lib/util' from 'tests/app.test.js'"
    sig = extract_signals(out)
    assert ("../lib/util", "tests/app.test.js") in sig.missing_modules


def test_extract_signals_strips_ansi():
    out = "\x1b[31mValueError\x1b[0m: bad"
    sig = extract_signals(out)
    assert "ValueError" in sig.exception_types


def test_localize_ranks_traceback_hit_highest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def run():\n    raise ValueError()\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["src/service.py"])
    suspects = localize(sig, tools, traj, tmp_path)
    assert suspects
    assert suspects[0].file == "src/service.py"


def test_localize_excludes_test_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["tests/test_x.py"])
    suspects = localize(sig, tools, traj, tmp_path)
    assert suspects == []


def test_diff_size_counts_changed_lines():
    old = "a\nb\nc\n"
    new = "a\nB\nc\n"
    assert _diff_size(old, new) == 2  # one removed, one added


def test_blast_radius_flags_removed_function_with_external_caller(tmp_path):
    (tmp_path / "lib.py").write_text("def shared():\n    return 1\n")
    (tmp_path / "app.py").write_text("from lib import shared\n\n\ndef run():\n    return shared()\n")
    tools = LocalToolBackend(tmp_path)
    old_content = "def shared():\n    return 1\n"
    new_content = "def renamed():\n    return 1\n"
    violations = _blast_radius_violations("lib.py", old_content, new_content, tools)
    assert violations
    assert "removed" in violations[0]


def test_blast_radius_allows_internal_only_change(tmp_path):
    tools = LocalToolBackend(tmp_path)
    old_content = "def helper():\n    return 1\n"
    new_content = "def helper():\n    return 2\n"  # same signature, no callers indexed
    violations = _blast_radius_violations("lib.py", old_content, new_content, tools)
    assert violations == []


_FIX_TURNS = [
    "PATCH\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n>>>>>>> REPLACE",
    "SUBMIT",
]


def _make_buggy_project(project_dir: Path) -> None:
    ensure_repo(project_dir)
    (project_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (project_dir / "tests").mkdir()
    (project_dir / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )


def test_repair_loop_parallel_samples_fixes_real_bug(tmp_path):
    """K=3 concurrent candidate attempts (parallel_samples=True, the new
    default), against a real GraphToolBackend (exercises codegraph.py's
    SQLite thread-safety fix) and a position-indexed scripted LLM (safe
    for concurrent conversations — see TurnByPositionScriptedLLM's
    docstring for why a global-counter mock would NOT be safe here)."""
    _make_buggy_project(tmp_path)
    tools = GraphToolBackend(tmp_path)
    try:
        traj = Trajectory(tmp_path)
        llm = TurnByPositionScriptedLLM(_FIX_TURNS)
        report = repair_loop_agentic(
            tmp_path, llm, tools, traj,
            test_cmd="python -m pytest -q --continue-on-collection-errors",
            samples=3, max_rounds=2, tasks_by_file={"calc.py": "add"},
            parallel_samples=True,
        )
        assert report["success"], report
        assert report["final_failures"] == 0
        assert (tmp_path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    finally:
        tools.__exit__(None, None, None)


class _ArchitectAwareLLM:
    """Routes on message CONTENT (the `_PLAN_MARKER` phrase), not
    position or call order — the one extra `_plan_repair` call happens
    sequentially before the K-sampled attempts' own conversations begin,
    so a content check is the simplest correct way to give it a distinct
    scripted response without disturbing TurnByPositionScriptedLLM's
    per-conversation indexing for the actual patch attempts."""

    def __init__(self, plan_text: str, patch_turns: list[str]):
        from atomic_forge.repair_agent import _PLAN_MARKER
        self._marker = _PLAN_MARKER
        self.plan_text = plan_text
        self._patch_llm = TurnByPositionScriptedLLM(patch_turns)
        self.plan_calls = 0

    def chat(self, messages, temperature=0.0, max_tokens=8192):
        if any(self._marker in (m.get("content") or "") for m in messages):
            self.plan_calls += 1
            return self.plan_text
        return self._patch_llm.chat(messages, temperature, max_tokens)


def test_repair_loop_architect_mode_plans_then_fixes(tmp_path):
    """architect_mode=True: one extra planning call happens before
    K-sampling, its text is folded into every attempt's task_prompt, and
    the repair still succeeds — proves the opt-in path is wired end to
    end (not proof it improves outcomes; see req-planner-executor-split.md
    for why that validation needs a live LLM benchmark this test can't
    provide)."""
    _make_buggy_project(tmp_path)
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = _ArchitectAwareLLM(
        plan_text="TARGET: add\nCHANGE: fix the operator\nCONSTRAINTS: keep the signature",
        patch_turns=_FIX_TURNS,
    )
    report = repair_loop_agentic(
        tmp_path, llm, tools, traj,
        test_cmd="python -m pytest -q --continue-on-collection-errors",
        samples=2, max_rounds=2, tasks_by_file={"calc.py": "add"},
        parallel_samples=False, architect_mode=True,
    )
    assert report["success"], report
    assert llm.plan_calls == 1
    events = traj.read_all()
    assert any(e.get("event") == "repair_plan" and e.get("ok") for e in events)


def test_repair_loop_architect_mode_survives_planning_failure(tmp_path):
    """A broken/empty planning call must not block the repair attempt it
    was meant to help — _plan_repair degrades to the unplanned prompt."""
    _make_buggy_project(tmp_path)
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)

    class _EmptyPlanLLM:
        def __init__(self):
            from atomic_forge.repair_agent import _PLAN_MARKER
            self._marker = _PLAN_MARKER
            self._patch_llm = TurnByPositionScriptedLLM(_FIX_TURNS)

        def chat(self, messages, temperature=0.0, max_tokens=8192):
            if any(self._marker in (m.get("content") or "") for m in messages):
                return ""  # empty plan — must not crash or block the attempt
            return self._patch_llm.chat(messages, temperature, max_tokens)

    report = repair_loop_agentic(
        tmp_path, _EmptyPlanLLM(), tools, traj,
        test_cmd="python -m pytest -q --continue-on-collection-errors",
        samples=1, max_rounds=2, tasks_by_file={"calc.py": "add"},
        parallel_samples=False, architect_mode=True,
    )
    assert report["success"], report


def test_repair_loop_sequential_samples_still_works(tmp_path):
    """parallel_samples=False (the pre-parallelization behavior) must
    still produce the same outcome — the flag is an opt-out, not a
    removed code path."""
    _make_buggy_project(tmp_path)
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    llm = TurnByPositionScriptedLLM(_FIX_TURNS)
    report = repair_loop_agentic(
        tmp_path, llm, tools, traj,
        test_cmd="python -m pytest -q --continue-on-collection-errors",
        samples=3, max_rounds=2, tasks_by_file={"calc.py": "add"},
        parallel_samples=False,
    )
    assert report["success"], report
    assert report["final_failures"] == 0
    assert (tmp_path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
