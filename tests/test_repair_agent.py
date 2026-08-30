from pathlib import Path

from atomic_forge.repair_agent import (
    _attempt_patch, _blast_radius_violations, _diff_size, extract_signals, localize,
    repair_loop_agentic,
)
from atomic_forge.sandbox import ensure_repo
from atomic_forge.tools import GraphToolBackend, LocalToolBackend
from atomic_forge.trajectory import Trajectory

from _helpers import ScriptedToolCallLLM, TurnByPositionScriptedLLM


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


def test_localize_survives_a_timing_out_structural_backend(tmp_path):
    """Confirmed live on astroid#3258 (2026-08-30, three `fix` runs launched
    in parallel): a contended CIE backend timing out inside tools.callers()
    propagated as an unhandled TimeoutError and killed the ENTIRE repair
    loop outright. failing_context/search_symbol/callers are structural
    signals exactly like hybrid_search — losing one must degrade
    localization (fall back to whatever other signals fired), never crash
    the whole run."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def run():\n    raise ValueError()\n")

    class _TimingOutBackend(LocalToolBackend):
        def callers(self, symbol):
            raise TimeoutError("CIE backend contended — simulated")

    tools = _TimingOutBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["src/service.py"], symbol_names=["run"])
    # Must not raise — the traceback signal should still land even though
    # callers() blew up for the symbol-name signal.
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


def test_localize_uses_hybrid_search_for_dynamic_dispatch_bugs(tmp_path):
    """A file with zero static call-graph signal (no traceback hit, no
    failing_context distance, no symbol-name match) must still surface as
    a suspect when hybrid_search's semantic/lexical ranking finds it —
    this is exactly the astroid#769 gap: the real fix site
    (`rebuilder.py::check_type_comment`) never appeared in either live
    round's suspects because nothing on the failing test's static call
    path reaches it. Backends without hybrid_search (LocalToolBackend,
    GraphToolBackend) must keep working unaffected — the signal is
    additive and optional, gated by hasattr()."""
    (tmp_path / "dispatch.py").write_text("def handler():\n    pass\n")

    class _HybridSearchOnlyBackend(LocalToolBackend):
        def hybrid_search(self, query, top_k=10):
            assert query  # must be built from the failure, never empty
            return {"ok": True, "results": [
                {"name": "handler", "kind": "function", "source_file": "dispatch.py",
                 "score": 0.8, "lexical_score": 0.6, "dense_score": 0.0, "graph_score": 0.2},
            ]}

    tools = _HybridSearchOnlyBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(exception_types=["Uninferable"])
    suspects = localize(sig, tools, traj, tmp_path,
                         output="AssertionError: Uninferable inferred during attribute access")
    assert any(s.file == "dispatch.py" for s in suspects)


def test_localize_excludes_vendored_hybrid_search_hits(tmp_path):
    """Confirmed live on astroid#769 (2026-08-30): hybrid_search's lexical
    leg matched a vendored `.venv/.../_pytest/terminal.py` on pytest's own
    "short test summary" wording and it became the ONLY suspect for two
    full rounds, since no static signal fired at all — every sample burned
    its turn budget on a dependency file instead of project source. A hit
    under a vendor dir (`.venv`, `node_modules`, etc. — see
    `symbols._SKIP_DIRS`) must never become a suspect, hybrid_search or
    not."""
    (tmp_path / "real_fix.py").write_text("def check_type_comment():\n    pass\n")
    venv_pytest = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages" / "_pytest"
    venv_pytest.mkdir(parents=True)
    (venv_pytest / "terminal.py").write_text("def short_test_summary():\n    pass\n")

    class _VendorHitBackend(LocalToolBackend):
        def hybrid_search(self, query, top_k=10):
            return {"ok": True, "results": [
                {"name": "short_test_summary", "kind": "function",
                 "source_file": ".venv/lib/python3.14/site-packages/_pytest/terminal.py",
                 "score": 0.9, "lexical_score": 0.9, "dense_score": 0.0, "graph_score": 0.0},
            ]}

    tools = _VendorHitBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(exception_types=["AssertionError"])
    suspects = localize(sig, tools, traj, tmp_path, output="short test summary info")
    assert not any(".venv" in s.file for s in suspects)


def test_localize_without_hybrid_search_is_unaffected(tmp_path):
    """A plain backend with no hybrid_search method (the pre-existing
    LocalToolBackend/GraphToolBackend contract) must not raise and must
    behave exactly as before — the new signal is additive, not required."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def run():\n    raise ValueError()\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["src/service.py"])
    suspects = localize(sig, tools, traj, tmp_path, output="ValueError: bad")
    assert suspects
    assert suspects[0].file == "src/service.py"


def test_localize_spectrum_signal_can_outrank_static_distance(tmp_path):
    """A file with a strong Ochiai score (only the failing test touches
    it) must be able to outrank a file that merely sits close in the
    static call graph — this is the whole point of the causal signal:
    astroid#769's real fix site never scored ANY static-distance points
    at all, so without this it can never win regardless of weighting."""
    (tmp_path / "near_test.py").write_text("def helper():\n    pass\n")
    (tmp_path / "real_cause.py").write_text("def check_type_comment():\n    pass\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    from atomic_forge.spectrum import SpectrumHit
    sig = FailureSignals(traceback_paths=["near_test.py"])  # only static hit
    suspects = localize(sig, tools, traj, tmp_path,
                        spectrum={"real_cause.py": SpectrumHit(score=1.0, line=1, ep=0)})
    assert suspects
    assert suspects[0].file == "real_cause.py"


def test_attempt_patch_targets_a_different_file_via_optional_path(tmp_path):
    """Confirmed live on astroid#3257 (2026-08-30): a sample correctly
    investigated and produced a genuinely correct patch for a file OTHER
    than the round's assigned suspect — and every attempt was rejected as
    "SEARCH block not found" because the patch gate always validated
    against the assigned suspect's content, not the file the SEARCH text
    actually named. The `patch` tool's optional `path` argument must let
    the model redirect, and the resulting Candidate must carry the file
    it was ACTUALLY validated against, not the originally assigned one."""
    suspect = tmp_path / "wrong_suspect.py"
    suspect.write_text("def unrelated():\n    pass\n")
    real_fix_file = tmp_path / "real_target.py"
    real_fix_file.write_text("def broken():\n    return None\n")

    llm = ScriptedToolCallLLM([
        [("patch", {
            "content": (
                "<<<<<<< SEARCH\n    return None\n=======\n    return 1\n>>>>>>> REPLACE"
            ),
            "path": "real_target.py",
        })],
        [("submit", {})],
    ])
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    cand = _attempt_patch("wrong_suspect.py", "fix the bug", llm, tools, tmp_path, traj,
                          temperature=0.0, max_turns=5, sample_no=0,
                          tool_manifest_text="", tool_manifest=manifest)
    assert cand is not None
    assert cand.file == "real_target.py"
    assert "return 1" in cand.new_content
    # the original wrong-suspect file must be completely untouched
    assert suspect.read_text() == "def unrelated():\n    pass\n"


def test_attempt_patch_rejects_path_targeting_a_test_file(tmp_path):
    """The `path` override must not become a backdoor around 'never patch
    the test' — a model redirecting to a test file must be rejected, not
    silently allowed just because it named a path explicitly."""
    (tmp_path / "wrong_suspect.py").write_text("def f():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")

    llm = ScriptedToolCallLLM([
        [("patch", {
            "content": (
                "<<<<<<< SEARCH\ndef test_x():\n    assert True\n=======\n"
                "def test_x():\n    assert False\n>>>>>>> REPLACE"
            ),
            "path": "tests/test_x.py",
        })],
        [("submit", {})],
        [("submit", {})],  # a 2nd submit after rejection — still nothing to accept
    ])
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    manifest = [{"name": "view_file", "signature": "(path)", "doc": "view a file"}]

    cand = _attempt_patch("wrong_suspect.py", "fix the bug", llm, tools, tmp_path, traj,
                          temperature=0.0, max_turns=5, sample_no=0,
                          tool_manifest_text="", tool_manifest=manifest)
    assert cand is None
    assert (tmp_path / "tests" / "test_x.py").read_text() == "def test_x():\n    assert True\n"


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


def test_repair_loop_promotes_next_suspect_after_exhaustion(tmp_path, monkeypatch):
    """When round 1's top suspect burns its full sample budget with no
    usable candidate, round 2 must route to the next-ranked suspect
    instead of re-serving the identical top suspect. Regression test for
    the astroid#769 campaign run (2026-08-30): localize() is a pure
    function of the (unchanged, since nothing landed) failing-test
    signals, so without promotion round 2 just repeats round 1's failed
    investigation on the same file verbatim."""
    ensure_repo(tmp_path)
    (tmp_path / "wrong.py").write_text("def noop():\n    return None\n")
    (tmp_path / "right.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from right import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )

    from atomic_forge import repair_agent
    from atomic_forge.repair_agent import Suspect

    fixed_suspects = [Suspect("wrong.py", 10.0, ["looks likely"]),
                       Suspect("right.py", 5.0, ["actual bug"])]
    monkeypatch.setattr(repair_agent, "localize", lambda *a, **k: list(fixed_suspects))

    class _RouteOnTargetLLM:
        """Never patches while wrong.py is the prime suspect (identical
        RUN 5x -> stuck-abort, well inside the turn budget); fixes
        right.py once the loop promotes it."""

        def __init__(self):
            self._fix = TurnByPositionScriptedLLM(_FIX_TURNS)

        def chat(self, messages, temperature=0.0, max_tokens=8192):
            task = next((m["content"] for m in messages if m.get("role") == "user"), "")
            if "Prime suspect: wrong.py" in task:
                return "RUN echo stuck"
            return self._fix.chat(messages, temperature, max_tokens)

    traj = Trajectory(tmp_path)
    tools = LocalToolBackend(tmp_path)
    report = repair_loop_agentic(
        tmp_path, _RouteOnTargetLLM(), tools, traj,
        test_cmd="python -m pytest -q --continue-on-collection-errors",
        samples=1, max_rounds=2, tasks_by_file={"right.py": "add"},
        parallel_samples=False,
    )
    assert report["success"], report
    assert (tmp_path / "right.py").read_text() == "def add(a, b):\n    return a + b\n"
    events = traj.read_all()
    assert any(e.get("event") == "repair_localize_promote" and e.get("to_file") == "right.py"
               for e in events)


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
