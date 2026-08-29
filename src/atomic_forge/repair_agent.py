"""
State-of-the-art repair orchestrator.

    run tests ──green?──► done
        │fail
        ▼
  1. SIGNALS     extract failing tests, exception types, symbol names, traceback paths
  2. LOCALIZE    failing_context (distance-ranked) + traceback paths
                 + search_symbol + callers  → ranked suspect files with evidence
  3. SAMPLE K    K independent agentic patch attempts (greedy + temperature);
                 each attempt is a full agent session with tools, ending in a PATCH
  4. SELECT      execution-first: apply each candidate, run suite, restore;
                 winner = green patch with smallest diff; else fewest-failures
                 (only if strictly better than current state)
  5. COMMIT      land winner, re-index tools, next round with fresh signals
                 auto-revert any round that increases failures
  budget: rounds (default 3) + wall-clock; best state preserved on exhaustion
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import patch as patch_mod
from .agent import run_agent
from .checkpoint import Verdict
from .llm import ChatLLM
from .models import AtomicTask
from .repair import failure_count_for as _failure_count_for
from .sandbox import ProgressCallback, TestStack, commit, detect_test_stack, lint_gate, run_test, truncate
from .stacks import is_test_file as _is_test_file
from .symbols import SymbolIndex
from .tools import ToolBackend
from .trajectory import Trajectory

#: Kept as the default for a caller that always wants pytest and has no
#: stack to detect from (e.g. a synthetic eval suite). The live path
#: doesn't rely on this — see repair_loop_agentic's test_cmd=None
#: handling, which detects the actual stack instead.
DEFAULT_TEST_CMD = "python -m pytest -q --continue-on-collection-errors"

REPAIR_SYSTEM = """You are an autonomous repair agent. A test suite is failing. Your job: find the root cause and produce the MINIMAL correct fix.
Workflow that works:
1. Start from the failure: TOOL failing_context on the failing test, and read the suspect files with TOOL file_skeleton then TOOL view_file.
2. Form ONE root-cause hypothesis from the failing assertion/traceback — not from guessing.
3. PATCH with the minimal SEARCH/REPLACE fix (unique match). Never edit tests to make them pass.
4. RUN the failing test to verify your reasoning about behavior, then SUBMIT.
You have a limited turn budget — spend it reading code, not repeating actions.
Never weaken or edit the tests to make them pass. Fix the source, not the check.

IMPORT ERRORS (`ModuleNotFoundError`, `ImportError: cannot import name 'X'
from 'Y'`, a TS/JS "Cannot find module"): never guess the corrected import
path or hand-derive a dotted module path yourself. TOOL resolve_import
with the missing symbol's name (and this file's own path as
`importing_file`) returns the exact import statement to write, resolved
against where the symbol ACTUALLY lives right now. If resolve_import
reports no match at all, the symbol genuinely doesn't exist yet anywhere —
that's a missing-definition bug in the target module, not an import-path
bug; add the definition there instead of inventing an import for it.

PATH-DEPTH BUGS (`Path(__file__).resolve().parents[N]` resolving to the
wrong directory): do NOT try to fix these by incrementing/decrementing N
and re-running — that's guessing arithmetic. Instead COMPUTE it: read the
failing file's own path and the target file's real path (TOOL view_file /
a directory listing), count the actual directory levels between them, or
replace the hardcoded `parents[N]` entirely with a marker-based walk that
finds a known ancestor directory by name instead of by a magic index.

DEEP LOCALIZATION IN LONG FUNCTIONS: when a suspect file is large, or the
traceback line sits deep inside one function and the first read doesn't
reveal the bug, call TOOL statement_graph(file, traceback_line) — it
returns that statement's def-use context: which statement DEFINED the
value you're seeing, and which later statements use it. Prefer it over
re-reading a long function line-by-line; it also resolves a variable's
origin when name shadowing makes the wrong definition look like the
right one. Not useful for one-line functions or non-Python blocks."""


# --------------------------------------------------------------------------
# 1. signals
# --------------------------------------------------------------------------

@dataclass
class FailureSignals:
    test_files: list[str] = field(default_factory=list)
    test_nodes: list[str] = field(default_factory=list)     # tests/x.py::test_y
    symbol_names: list[str] = field(default_factory=list)   # from import/name/attr errors
    traceback_paths: list[str] = field(default_factory=list)
    exception_types: list[str] = field(default_factory=list)
    #: (specifier, requiring_file) pairs from Node/Jest's "Cannot find
    #: module 'X' from 'Y'" — the target genuinely doesn't exist on disk
    #: yet (needs to be created, not patched).
    missing_modules: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (f"tests={self.test_files} symbols={self.symbol_names} "
                f"paths={self.traceback_paths} exceptions={self.exception_types} "
                f"missing_modules={self.missing_modules}")


#: Colored test-runner output leaks SGR escape codes into raw text; strip
#: before any pattern runs so every signal sees the same text a human
#: reads on a color-stripped terminal.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def extract_signals(output: str) -> FailureSignals:
    output = _ANSI_ESCAPE_RE.sub("", output)
    sig = FailureSignals()
    sig.test_files = sorted(set(
        re.findall(r"(tests/[^\s:]+\.py)", output)
        + re.findall(r"(src/test/java/[^\s:]+\.java)", output)
        + re.findall(r"([^\s:]+\.spec\.ts)", output)
        + re.findall(r"(tests/[^\s:'\"]+\.test\.(?:tsx|ts|jsx|js))", output)
        # Gradle's own console reporter prints a bare "ClassName >
        # methodName() FAILED" summary — never a path at all.
        + re.findall(r"(\w+) > \S+\(\) FAILED", output)
    ))
    sig.test_nodes = sorted(set(re.findall(r"(tests/[^\s:]+\.py::\w+)", output)))
    sig.traceback_paths = sorted(set(
        re.findall(r"((?:src|lib|app)/[^\s:]+\.(?:py|ts|tsx|js|jsx|java))", output)
        # pytest's own literal frame format names a real project file
        # relative to pytest's rootdir regardless of prefix.
        + re.findall(r"^([\w./-]+\.py):\d+: in <\w+>", output, re.MULTILINE)
    ))
    sig.exception_types = sorted(set(re.findall(r"(\w+(?:Error|Exception|AssertionError))\b", output)))
    names = re.findall(r"cannot import name '(\w+)'", output)
    names += re.findall(r"name '(\w+)' is not defined", output)
    names += re.findall(r"attribute '(\w+)'", output)
    names += re.findall(r"has no attribute '(\w+)'", output)
    # A real JVM stack trace never carries a src/main/java/... relative
    # path — route the class name into symbol_names via search_symbol.
    names += re.findall(r"\b(\w+)\.java:\d+", output)
    sig.symbol_names = sorted(set(names))
    sig.missing_modules = sorted(set(
        (specifier, requiring)
        for specifier, requiring in re.findall(
            r"Cannot find module '([^']+)' from '([^']+)'", output)
    ))
    return sig


def _resolve_node_missing_module(project_dir: Path, specifier: str, requiring_rel: str) -> Optional[str]:
    """Resolve a Jest "Cannot find module 'specifier' from 'requiring_rel'"
    pair into a project_dir-relative candidate path for the missing file —
    or None if this isn't actually a missing-file problem."""
    if not specifier.startswith("."):
        return None  # bare package specifier (npm dependency) — not ours to create
    for prefix in ("backend", "frontend", ""):
        requiring_abs = project_dir / prefix / requiring_rel if prefix else project_dir / requiring_rel
        if not requiring_abs.is_file():
            continue
        joined = os.path.normpath(str(Path(prefix or ".") / Path(requiring_rel).parent / specifier))
        joined = joined.replace(os.sep, "/")
        if "tests" in Path(joined).parts[:-1]:
            return None
        req_ext = Path(requiring_rel).suffix
        exts = [".ts", ".tsx", ".js", ".jsx"] if req_ext in (".ts", ".tsx") else [".js", ".jsx", ".ts", ".tsx"]
        for ext in [""] + exts + [f"/index{e}" for e in exts]:
            if (project_dir / f"{joined}{ext}").exists():
                return None  # resolves fine under some extension — not actually missing
        return f"{joined}{exts[0]}"
    return None


# --------------------------------------------------------------------------
# 2. localization
# --------------------------------------------------------------------------

@dataclass
class Suspect:
    file: str
    score: float
    evidence: list[str] = field(default_factory=list)


def _resolve_traceback_path(project_dir: Path, path: str) -> Optional[str]:
    """A path parsed directly out of raw test-output TEXT is relative to
    wherever the test command's cwd was — try project-root-relative
    first, then each split-layout prefix."""
    if (project_dir / path).exists():
        return path
    for prefix in ("backend", "frontend"):
        if (project_dir / prefix / path).exists():
            return f"{prefix}/{path}"
    return None


def localize(signals: FailureSignals, tools: ToolBackend, traj: Trajectory,
             project_dir: Path, known_files: Optional[list[str]] = None) -> list[Suspect]:
    """known_files: the current round's own file set — investigated first
    when a crash's raw output happens to name unrelated files, and used as
    a last-resort suspect when every other signal comes up empty."""
    suspects: dict[str, Suspect] = {}
    known_files = known_files or []
    known_set = set(known_files)

    def bump(file: str, points: float, why: str, allow_missing: bool = False) -> None:
        if Path(file).is_absolute():
            try:
                file = str(Path(file).resolve().relative_to(project_dir.resolve()))
            except ValueError:
                pass
        if _is_test_file(file):
            return
        if not allow_missing and not (project_dir / file).exists():
            return
        s = suspects.setdefault(file, Suspect(file, 0.0))
        s.score += points
        s.evidence.append(why)

    for p in signals.traceback_paths:
        resolved = _resolve_traceback_path(project_dir, p)
        if resolved:
            bump(resolved, 3.0, "named in traceback")

    for specifier, requiring_rel in signals.missing_modules:
        resolved = _resolve_node_missing_module(project_dir, specifier, requiring_rel)
        if resolved:
            bump(resolved, 5.0, f"required by {requiring_rel} but does not exist on disk",
                 allow_missing=True)

    test_candidates = signals.test_nodes or signals.test_files
    ordered_tests = sorted(test_candidates, key=lambda f: f not in known_set)[:3]
    for test in ordered_tests:
        ctx = tools.failing_context(test)
        for r in ctx.get("results", []):
            pts = 4.0 if r["distance"] == 1 else 2.0
            bump(r["file"], pts, f"distance-{r['distance']} from failing test ({r['symbol']})")

    for name in signals.symbol_names[:5]:
        hits = tools.search_symbol(name)
        for h in hits.get("results", [])[:2]:
            bump(h["source_file"], 3.0, f"defines symbol '{name}' from error message")
        for c in tools.callers(name).get("results", [])[:3]:
            bump(c["caller_file"], 1.5, f"calls '{name}' (possible import/usage site)")

    ranked = sorted(suspects.values(), key=lambda s: -s.score)

    if not ranked and known_files:
        for f in known_files:
            bump(f, 0.5, "batch's own file — no other localization signal found")
        ranked = sorted(suspects.values(), key=lambda s: -s.score)

    traj.log("localize", signals=signals.summary(),
             suspects=[{"file": s.file, "score": s.score, "why": s.evidence[:2]} for s in ranked])
    return ranked


# --------------------------------------------------------------------------
# 3+4. candidate attempts & execution-based selection
# --------------------------------------------------------------------------

def _apply_patch(current: str, patch: str) -> tuple[Optional[str], str]:
    """Prefer real SEARCH/REPLACE hunks; fall back to treating the whole
    payload as one fenced full-file rewrite."""
    new, err = patch_mod.apply_search_replace(current, patch)
    if new is not None:
        return new, ""
    m = re.search(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", patch, re.DOTALL)
    if m:
        return m.group(1).rstrip() + "\n", ""
    return None, f"unusable patch ({err})"


def _diff_size(old: str, new: str) -> int:
    return sum(1 for l in difflib.unified_diff(old.splitlines(), new.splitlines())
               if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))


def _extract_symbols(rel_path: str, text: str) -> dict:
    """Function/method/class symbols keyed by name, reusing SymbolIndex's
    per-language parsers against ONE file's raw content — not a
    whole-project scan, since candidate content here may not even be on
    disk yet."""
    idx = SymbolIndex(project_dir=Path("."))
    parser = {
        ".py": idx._parse_py, ".ts": idx._parse_ts, ".tsx": idx._parse_ts,
        ".js": idx._parse_ts, ".jsx": idx._parse_ts, ".java": idx._parse_java,
    }.get(Path(rel_path).suffix)
    if parser is None:
        return {}
    try:
        syms = parser(rel_path, text)
    except Exception:  # noqa: BLE001 — unparseable content: lint_gate already covers syntax
        return {}
    return {s.name: s for s in syms}


def _blast_radius_violations(file_rel: str, old_content: str, new_content: str,
                             tools: ToolBackend) -> list[str]:
    """Static structural gate: reject a candidate that changes the
    signature of, or removes, a function/method actually called from
    OUTSIDE the file being patched."""
    old_syms = _extract_symbols(file_rel, old_content)
    if not old_syms:
        return []  # unparseable/unsupported language — nothing to gate on
    new_syms = _extract_symbols(file_rel, new_content)
    violations = []
    for name, old_sym in old_syms.items():
        new_sym = new_syms.get(name)
        if new_sym is not None and new_sym.signature == old_sym.signature:
            continue
        why = ("removed" if new_sym is None
               else f"signature changed: {old_sym.signature!r} -> {new_sym.signature!r}")
        try:
            hits = tools.callers(name).get("results", [])
        except Exception:  # noqa: BLE001 — caller lookup is best-effort, never fatal
            hits = []
        external = sorted({h["caller_file"] for h in hits if h.get("caller_file") != file_rel})
        if external:
            violations.append(f"{file_rel}:{name} {why} — called from {', '.join(external)}")
    return violations


@dataclass
class Candidate:
    file: str
    new_content: str
    diff_size: int
    failures: Optional[int] = None
    turns: int = 0


_PLAN_MARKER = "state your fix plan"

_ARCHITECT_PROMPT_SUFFIX = f"""

Before patching, {_PLAN_MARKER}: which symbol(s) you will change, and what
must NOT change (constraints). Respond with EXACTLY 3 short lines, no other
text:
TARGET: <symbol or region you will change>
CHANGE: <one-line description of the fix>
CONSTRAINTS: <what must keep working / not change>"""


def _plan_repair(llm: ChatLLM, top_file: str, task_prompt: str, traj: Trajectory) -> Optional[str]:
    """One extra, cheap LLM call ahead of K-sampling: ask for a short
    structured statement of intent (target symbol, the fix, and what must
    not break) before any patch is attempted. This is the "architect" half
    of an Aider-style planner/executor split — see req-planner-executor-
    split.md for why it's a single extra call against the SAME model
    rather than a genuinely separate cheap/expensive model pair (forge has
    no per-role model configuration yet; that's real follow-up work, not
    faked here) and why it defaults OFF (`architect_mode=False`) pending a
    live-LLM benchmark comparison against plain K-sampling, per SAFEdit's
    finding that decomposition doesn't automatically improve reliability.

    Returns the plan text, or None if the call failed or came back empty
    (caller falls back to the unplanned task_prompt — a failed planning
    call should never block the repair attempt it was meant to help)."""
    try:
        plan_text = llm.chat(
            [{"role": "user", "content": task_prompt + _ARCHITECT_PROMPT_SUFFIX}],
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001 — planning is optional, never fatal
        traj.log("repair_plan", file=top_file, ok=False, reason=str(e))
        return None
    plan_text = (plan_text or "").strip()
    if not plan_text:
        traj.log("repair_plan", file=top_file, ok=False, reason="empty response")
        return None
    traj.log("repair_plan", file=top_file, ok=True, plan=plan_text)
    return plan_text


def _attempt_patch(file_rel: str, task_prompt: str, llm: ChatLLM, tools: ToolBackend,
                   project_dir: Path, traj: Trajectory, temperature: float,
                   max_turns: int, sample_no: int,
                   tool_manifest_text: str = "",
                   tool_manifest: Optional[list] = None) -> Optional[Candidate]:
    """One agentic repair attempt. `target` may not exist yet — the
    missing-module suspect path names a file other code requires but that
    was never created; `current = ""` then, and the agent must submit a
    fenced full-file block."""
    target = project_dir / file_rel
    current = target.read_text(errors="replace") if target.exists() else ""
    holder: dict = {}

    def check(patch: Optional[str]) -> tuple[bool, str]:
        if not patch:
            return False, "You SUBMITted without a PATCH. Produce PATCH first."
        new, err = _apply_patch(current, patch)
        if new is None:
            if not target.exists():
                return False, (
                    f"{file_rel} does not exist yet — there is nothing for SEARCH/REPLACE "
                    "to match. Submit a PATCH containing ONE fenced code block with the "
                    "file's COMPLETE content instead."
                )
            return False, f"Patch does not apply: {err}. Re-read the file (view_file) and match SEARCH exactly."
        ok, why = lint_gate(project_dir, file_rel, new)
        if not ok:
            return False, f"Patch fails syntax gate: {why}. Fix and PATCH again."
        holder["new"] = new
        return True, ""

    result = run_agent(llm, tools, project_dir, REPAIR_SYSTEM, task_prompt, traj,
                       submit_check=check, max_turns=max_turns, temperature=temperature,
                       tag=f"repair_s{sample_no}", tool_manifest_text=tool_manifest_text,
                       tool_manifest=tool_manifest)
    if not result.success or "new" not in holder:
        traj.log("repair_attempt", sample=sample_no, file=file_rel, produced=False,
                 reason=result.abort_reason)
        return None
    new = holder["new"]
    return Candidate(file=file_rel, new_content=new, diff_size=_diff_size(current, new), turns=result.turns)


def repair_loop_agentic(project_dir, llm: ChatLLM, tools: ToolBackend, traj: Trajectory,
                        test_cmd: str | None = None, image: Optional[str] = None, max_rounds: int = 3,
                        samples: int = 2, max_turns_per_attempt: int = 25,
                        per_issue_seconds: int = 1200, timeout: int = 300,
                        reporter=None, tasks_by_file: Optional[dict] = None,
                        required_pass_count: int = 1,
                        on_persisted: Optional["Callable[[str, str], None]"] = None,
                        tool_manifest_text: str = "",
                        tool_manifest: Optional[list] = None,
                        on_progress: Optional[ProgressCallback] = None,
                        parallel_samples: bool = True,
                        architect_mode: bool = False) -> dict:
    """The full SOTA loop. Returns a report dict.

    reporter: optional Reporter for task-graph write-back.
    tasks_by_file: {file_path: task_name} so status/events attach to the
    right task; files with no entry are reported under "unmapped".

    required_pass_count: how many consecutive green runs of the full suite
    are required before the run-ending suite-green transition is trusted,
    to absorb flaky tests. Default 1 (unchanged behavior).

    on_persisted: optional callback `(task_id, verdict) -> None`, invoked
    the instant a task's fix is confirmed and reporter.record() has been
    called with verdict="passed" — wire `checkpoint.RunCheckpointer.
    mark_persisted` here to make persistence durable per-task rather than
    deferred to the whole run's end.

    test_cmd: pass explicitly to force a specific command. Left as None,
    the actual generated stack in project_dir is detected instead of
    assuming pytest (see stacks.detect_test_stack). When nothing testable
    can be detected, this returns a skipped report rather than running
    pytest and reporting a false failure.

    on_progress: optional (phase, status, detail) callback. Emits at
    loop-level granularity only ("repairing" overall, "repair_round" per
    round) — not per candidate/per test run, which would flood a live UI.

    parallel_samples: run the K sampled agentic attempts concurrently
    (default True) instead of one after another. Safe because each
    attempt's `submit_check` only validates a candidate patch in memory
    (`_apply_patch` + `lint_gate`) — nothing writes to `project_dir` or the
    tool backend's on-disk index until AFTER all K attempts finish and one
    is selected, sequentially, in the execution-based-selection block
    below. `tools`, `llm`, and `traj` are all already safe for this: LLM
    calls are per-thread-safe (see llm.py's `_client_lock`/`UsageTracker`
    lock), `Trajectory.log` is append-only and already called from
    concurrent worker threads elsewhere (generate_agent.py), and
    `GraphToolBackend`/`CodeGraph` serialize their SQLite connection behind
    a lock (see codegraph.py). Set False to force the old sequential
    behavior (useful for deterministic trajectory-ordering in tests/
    debugging, or providers where concurrent calls aren't wanted).

    architect_mode: opt-in "planner" pass — see `_plan_repair`'s docstring.
    Default False: not yet validated against plain K-sampling on forge's
    own benchmarks (see req-planner-executor-split.md), so it costs one
    extra LLM call per round without a confirmed win. Safe to enable per
    call site once that validation exists.
    """
    project_dir = Path(project_dir)
    t0 = time.time()

    def emit(phase: str, status: str, detail: str = "") -> None:
        if on_progress is None:
            return
        try:
            on_progress(phase, status, detail)
        except Exception:  # noqa: BLE001 - reporting must never break the run
            pass

    def _report(task_file: str, status: str, failures: int) -> None:
        if reporter is None:
            return
        task_name = (tasks_by_file or {}).get(task_file, "unmapped")
        reporter.status(task_name, status, {"failures": failures, "attempts": 1})
        reporter.events(task_name, [e for e in traj.read_all()
                                    if e.get("event") in ("candidate_eval", "test_run")][-4:])

    def _mark_batch_tested_green(all_files: list[str]) -> None:
        """Promote every file in `all_files` to tested_green — called both
        when the initial check finds the suite already green and when a
        repair round makes it green, so an already-correct task's status
        doesn't silently stay unset."""
        for f in all_files:
            task_name = (tasks_by_file or {}).get(f, "unmapped")
            path = project_dir / f
            if not path.exists():
                continue
            content = path.read_text(errors="replace")
            content_ref = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
            if reporter is not None:
                reporter.record(task_name, f, content_ref, Verdict.PASSED.value)
            if on_persisted is not None:
                on_persisted(task_name, Verdict.PASSED.value)

    if test_cmd is None:
        stack = detect_test_stack(project_dir)
        if stack is None:
            reason = ("no package.json/vitest setup and no requirements.txt/pyproject.toml/"
                      "pytest.ini/setup.cfg or *_test.py files found under project_dir — "
                      "nothing to test yet")
            traj.log("test_run", round=0, ok=True, skipped=True, reason=reason)
            emit("repairing", "skipped", reason)
            return {"success": True, "rounds": 0, "initial_failures": 0, "final_failures": 0,
                    "skipped": True, "skip_reason": reason}
        test_cmd, image = stack.cmd, stack.image

    emit("repairing", "running", "confirming current test-suite state")
    res = run_test(test_cmd, image, project_dir, timeout=timeout)
    initial = _failure_count_for(res)
    traj.log("test_run", round=0, ok=res.ok, failures=initial, output_tail=res.output[-800:])
    if res.ok:
        emit("repairing", "skipped", "tests already passing — nothing to repair")
        _mark_batch_tested_green(sorted((tasks_by_file or {}).keys()))
        return {"success": True, "rounds": 0, "initial_failures": 0, "final_failures": 0}

    best_failures = initial
    repaired: list[str] = []
    suspects: list[Suspect] = []
    #: Set when a round's winner is rejected by the blast-radius gate —
    #: fed into the NEXT round's task_prompt as an explicit constraint.
    pending_violations: list[str] = []

    for round_no in range(1, max_rounds + 1):
        if time.time() - t0 > per_issue_seconds:
            traj.log("repair", result="wall-clock exhausted", round=round_no)
            break

        signals = extract_signals(res.full_output)
        suspects = localize(signals, tools, traj, project_dir,
                             known_files=list((tasks_by_file or {}).keys()))
        if not suspects:
            traj.log("repair", result="localization found no suspects", round=round_no)
            break

        before = _failure_count_for(res)
        top = suspects[0]
        emit("repair_round", "running", f"round {round_no}: investigating {top.file}")
        task_prompt = (
            f"# Failing test output (truncated)\n```\n{truncate(res.output, 3500)}\n```\n\n"
            f"# Localization (ranked suspects)\n" +
            "\n".join(f"- {s.file} (score {s.score:.1f}): {'; '.join(s.evidence[:2])}"
                      for s in suspects[:4]) +
            f"\n\nPrime suspect: {top.file}. Investigate, find the root cause, PATCH it, verify, SUBMIT."
        )
        if not (project_dir / top.file).exists():
            task_prompt += (
                f"\n\n{top.file} DOES NOT EXIST YET — it is required/imported by other "
                "files but was never created. Create it: submit a PATCH containing ONE "
                "fenced code block with the file's complete content (not SEARCH/REPLACE, "
                "there is nothing on disk yet to match against)."
            )

        try:
            affected = tools.affected_by(top.file, max_depth=3, direction="incoming")
            affected_files = sorted({r["file"] for r in affected.get("results", []) if r.get("file")})
        except Exception:  # noqa: BLE001 — blast-radius context is optional, never fatal
            affected_files = []
        if affected_files:
            task_prompt += (
                f"\n\nThis file is imported by: {', '.join(affected_files)}. Your patch "
                "must preserve all existing exports and type signatures consumed by "
                "these dependents."
            )

        if pending_violations:
            task_prompt += (
                "\n\n# Previous patch REJECTED — fix these before resubmitting\n" +
                "\n".join(f"- {v}" for v in pending_violations)
            )
            pending_violations = []

        if architect_mode:
            plan = _plan_repair(llm, top.file, task_prompt, traj)
            if plan:
                task_prompt += f"\n\n# Repair plan (architect pass)\n{plan}\n\nFollow this plan."

        # --- sample K agentic attempts on the prime suspect
        candidates: list[Candidate] = []
        if parallel_samples and samples > 1:
            with ThreadPoolExecutor(max_workers=samples) as pool:
                futures = [
                    pool.submit(_attempt_patch, top.file, task_prompt, llm, tools, project_dir, traj,
                               0.0 if k == 0 else 0.7, max_turns_per_attempt, k,
                               tool_manifest_text, tool_manifest)
                    for k in range(samples)
                ]
                for fut in as_completed(futures):
                    cand = fut.result()
                    if cand:
                        candidates.append(cand)
        else:
            for k in range(samples):
                cand = _attempt_patch(top.file, task_prompt, llm, tools, project_dir, traj,
                                      temperature=0.0 if k == 0 else 0.7,
                                      max_turns=max_turns_per_attempt, sample_no=k,
                                      tool_manifest_text=tool_manifest_text,
                                      tool_manifest=tool_manifest)
                if cand:
                    candidates.append(cand)

        if not candidates:
            traj.log("repair", result="no candidate produced", round=round_no, file=top.file)
            emit("repair_round", "failed", f"round {round_no}: no candidate patch produced")
            continue

        # --- execution-based selection: apply → test → restore, per candidate
        target = project_dir / top.file
        existed_before = target.exists()
        original = target.read_text(errors="replace") if existed_before else ""
        for cand in candidates:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(cand.new_content)
            r = run_test(test_cmd, image, project_dir, timeout=timeout)
            cand.failures = _failure_count_for(r)
            traj.log("candidate_eval", round=round_no, file=cand.file,
                     diff=cand.diff_size, failures=cand.failures, turns=cand.turns)
            if existed_before:
                target.write_text(original)
            else:
                target.unlink(missing_ok=True)

        green = [c for c in candidates if c.failures == 0]
        if green:
            winner = min(green, key=lambda c: c.diff_size)
        else:
            winner = min(candidates, key=lambda c: (c.failures if c.failures is not None else 1e9))
            if winner.failures is None or winner.failures >= before:
                traj.log("repair", result="no candidate improved state", round=round_no,
                         best=[c.failures for c in candidates])
                emit("repair_round", "failed", f"round {round_no}: no candidate improved on {before} failing")
                continue

        blast_violations = _blast_radius_violations(top.file, original, winner.new_content, tools)
        if blast_violations:
            traj.log("repair", result="winner rejected by blast-radius gate", round=round_no,
                     file=top.file, violations=blast_violations)
            emit("repair_round", "failed", f"round {round_no}: winner rejected (blast-radius violation)")
            pending_violations = blast_violations
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(winner.new_content)
        commit(project_dir, f"forge: repair {top.file} (round {round_no}, "
                            f"{'green' if winner.failures == 0 else 'best-effort'}, "
                            f"diff {winner.diff_size})")
        if top.file not in repaired:
            repaired.append(top.file)
        tools.reindex_file(top.file)

        res = run_test(test_cmd, image, project_dir, timeout=timeout)
        now = _failure_count_for(res)
        traj.log("test_run", round=round_no, ok=res.ok, failures=now, output_tail=res.output[-800:])

        if res.ok:
            stable = True
            for _ in range(required_pass_count - 1):
                confirm = run_test(test_cmd, image, project_dir, timeout=timeout)
                traj.log("test_run", round=round_no, ok=confirm.ok, confirm=True,
                         output_tail=confirm.output[-800:])
                if not confirm.ok:
                    stable = False
                    break
            if not stable:
                traj.log("repair", result="suite green once but not stably "
                                          f"({required_pass_count}x required)", round=round_no)
                emit("repair_round", "failed", f"round {round_no}: green once but flaky, not stable")
                best_failures = min(best_failures, now)
                continue

            emit("repair_round", "done", f"round {round_no}: {top.file} fixed, suite green")
            _report(top.file, "tested_green", 0)
            _mark_batch_tested_green(sorted(set(repaired) | set((tasks_by_file or {}).keys())))
            emit("repairing", "done", f"fixed after {round_no} round(s)")
            return {"success": True, "rounds": round_no, "initial_failures": initial,
                    "final_failures": 0, "repaired_files": repaired}
        if now > before:  # auto-revert rule
            target.write_text(original)
            commit(project_dir, f"forge: revert round {round_no} (failures {before} -> {now})")
            traj.log("repair", result=f"round reverted (regression {before}->{now})", round=round_no)
            emit("repair_round", "failed", f"round {round_no}: reverted (made things worse, {before} -> {now})")
            res = run_test(test_cmd, image, project_dir, timeout=timeout)
            now = _failure_count_for(res)
        best_failures = min(best_failures, now)

    if reporter is not None:
        for f in (repaired or [suspects[0].file if suspects else "unmapped"]):
            _report(f, "repair_exhausted", best_failures)
    emit("repairing", "failed", f"exhausted after {max_rounds} round(s); {best_failures} failing test(s) remain")
    return {"success": False, "rounds": max_rounds, "initial_failures": initial,
            "final_failures": best_failures, "repaired_files": repaired, "exhausted": True}
