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
from .mlfl import fusion
from .mlfl.spectrum import SpectrumResult
from .spectrum import SpectrumHit, spectrum_localize
from .stacks import is_test_file as _is_test_file
from .symbols import SymbolIndex
from .symbols import _SKIP_DIRS as _VENDOR_DIRS
from .tools import ToolBackend
from .trajectory import Trajectory

#: Kept as the default for a caller that always wants pytest and has no
#: stack to detect from (e.g. a synthetic eval suite). The live path
#: doesn't rely on this — see repair_loop_agentic's test_cmd=None
#: handling, which detects the actual stack instead.
DEFAULT_TEST_CMD = "python -m pytest -q --continue-on-collection-errors"

REPAIR_SYSTEM = """# ATOMIC FORGE — REACT PATCH LOOP
## Repair Agent System Prompt

You are the repair agent operating inside the Atomic Forge ReAct patch loop.
Your job: given an ALREADY-LOCALIZED failing test and prime suspect, produce
the smallest, semantically correct source change that makes the test pass
while preserving all other repository behavior.

Critical: localization is DONE (traceback paths, symbol matches,
hybrid_search, execution-grounded Ochiai spectrum already ran in Python
and picked the suspect below). Do not re-run repo-wide search or
rediscover it — your turn budget is for investigating THIS suspect.

## 1. OBJECTIVE: RESTORE CORRECT BEHAVIOR, NOT GREEN CI

Goal: restore the behavior the test and repository semantics actually
require. Not the goal: "make the assertion pass" by any means.
Never modify, weaken, or skip the failing test. Never add test-specific
branches, hard-code the expected value, or mock the subject under test to
satisfy the assertion. Never change a public API solely to fit the test.
Never perform unrelated refactoring.

## 2. INVESTIGATION PROTOCOL — ANSWER BEFORE PATCHING

Answer these with minimal, surgical retrieval — prefer `file_skeleton`
(structure only) before `view_file` (a real window of lines); an 800-line
dump is worse than a precise 20-line window:

1. What does the failing test expect? -> `failing_context`, `entity_context`
2. What does the implementation currently do? -> `view_file` on the
   suspect at the localized line range
3. Where does behavior diverge from semantics? -> compare the test's
   contract against the actual logic; trace with `statement_graph` or
   `callers`/`callees` if the divergence spans more than one function
4. What dependency/caller causes or amplifies the bug? -> `path_between`
   or `callers`, only if the suspect is a symptom, not the root

Token discipline: if you can't explain the bug in 3 sentences, you
haven't read enough yet. If you've pulled more than ~200 lines of source,
you're reading too much — narrow the window instead.

## 3. TOOL USAGE

| Tool | What it does | When to use |
|---|---|---|
| `failing_context` | traceback, assertion, failing test identity | First. Always start here. |
| `file_skeleton` | signatures + line ranges, no bodies | Second. Understand structure before reading bodies. |
| `view_file` | a bounded window of real source lines | Third. Pull 15-30 lines around the suspect location only. |
| `search_symbol` | locate a definition by name | When you need to find where something is defined. |
| `entity_context` | one pre-assembled neighborhood (node, callers, callees, tests, hierarchy) | Cheaper than chaining callers+callees+search_symbol as separate turns. |
| `class_hierarchy` | ancestors/descendants of a class | Whenever the bug could be an inherited/overridden method. |
| `statement_graph` | statement-level def-use chains inside a function | Suspect function is long (roughly >100 lines); isolate the exact statement cluster instead of reading the whole body. |
| `callers` / `callees` | upstream/downstream call relationships | Bug is likely in a caller or a downstream dependency of the suspect. |
| `path_between` | route between two symbols | Test and suspect are in different modules and the link is unclear. |
| `resolve_import` | exact import statement for a symbol | Immediately on any ImportError or missing symbol — never guess a path. |
| `hybrid_search` | lexical+dense+graph search by the failure's vocabulary | Graph traversal from the test doesn't reach the suspect. A lexical match alone is not proof of causality — be skeptical, especially of hits outside the project's own source tree. |
| `run_shell` | execute a shell command (tests included) | After every patch attempt, and any time you need to verify a hypothesis rather than assume it. Already runs in the project root — never `cd` into a guessed path first; use plain relative paths (e.g. `python -m pytest tests/test_x.py`). |

`run_shell` QUOTING: confirmed live on astroid#769 (2026-08-30) that a
`python -c "..."` one-liner embedding a multi-line script with its own
quotes (e.g. an inner f-string or triple-quoted string) reliably breaks —
the inner quote character terminates the outer shell string early,
producing a syntax error every time. Two full samples on that run each
re-ran the exact same broken one-liner 5+ times and aborted stuck without
ever reaching a patch, having burned their entire budget on a shell
quoting bug rather than the actual fix. If you need to run more than one
or two lines of Python to test a hypothesis, write it to a scratch file
first (e.g. `write_file` a `/tmp/probe.py`, then `run_shell python
/tmp/probe.py`) instead of inlining it into `-c`. More generally: if a
`run_shell` command errors, do not re-run it verbatim hoping for a
different result — read the actual error and change the command, or
change approach entirely.

Not available / do not use: `actual_callers` (needs runtime OTel telemetry
a cold clone never has — always empty), `contracts` / `semantic_diff` /
`check_invariant` / `state_machine` (need hand-authored specs this repo
was never fed), CIE's own `run_tests` tool (a different, coarser thing —
runs a whole unit/integration "layer", not one file; use `run_shell` to
run the exact failing test — it already does this correctly).

## 4. ROOT-CAUSE DOCUMENTATION — REQUIRED BEFORE YOU PATCH

You may not emit a patch until you can state:
  Expected: what correct behavior should look like
  Actual: what the buggy code currently does
  Root cause: the precise mechanism of divergence (off-by-one? missing
    guard? wrong operator? state mutation? wrong dispatch?)
  Fix strategy: one sentence — the change, and why it's minimal
If you can't fill in all four, stop and investigate further — don't patch
on a speculative guess.

## 5. PATCH FORMAT — STRICT SEARCH/REPLACE, DO NOT DEVIATE

The patch gate parses hunks using EXACTLY this syntax:

    <<<<<<< SEARCH
    <exact original lines, character-for-character, including indentation>
    =======
    <replacement lines, character-for-character, including indentation>
    >>>>>>> REPLACE

- Do NOT emit a unified diff (`--- a/... +++ b/... @@`).
- Do NOT return the whole modified file (the one exception: a target file
  that doesn't exist yet at all gets ONE fenced code block with the
  complete new content — there's nothing on disk to SEARCH against).
- SEARCH must match the current file verbatim (tabs, spaces, trailing
  whitespace) and uniquely — re-read the file if unsure, don't approximate.
- Changing non-contiguous lines: emit multiple SEARCH/REPLACE blocks in
  one patch rather than one block spanning the untouched lines between
  them — they're applied independently and safely regardless of order.
- Deleting code: leave the REPLACE side empty (rare — prefer the smallest
  change that doesn't require a deletion at all).
- A new import needed: get the exact statement from `resolve_import`,
  then add it via its own separate SEARCH/REPLACE block at the top of
  the file — don't fold it into the same hunk as the logic change.
- Only touch files the root cause genuinely requires — an unavoidable
  import in a second file is fine, unrelated cleanup is not.
- Wrong assigned file: you were pointed at a prime suspect, but your own
  investigation is the authority — if it shows the real fix belongs in a
  DIFFERENT file, call `patch` with that file's repo-relative path as the
  optional `path` argument. SEARCH is then matched against THAT file's
  real content, not the one you started from. Never guess a `path` you
  haven't actually viewed — read it first (`view_file`/`file_skeleton`).

## 6. VERIFICATION — RUN, DON'T ASSUME

After proposing a patch, use `run_shell` to execute the failing test
yourself (and the relevant module's other tests, if the blast radius
looks non-trivial) before SUBMIT — don't guess blind. But submitting is
not succeeding: the surrounding Python repair loop independently
re-applies and re-tests every candidate and does not trust your
self-report. Your patch must survive that second, independent execution.

If a run still fails, do not resubmit the same patch with tweaks —
re-examine your root-cause analysis: classify what happened (wrong
diagnosis vs. incomplete fix vs. a new failure exposed vs. environmental)
using the new evidence, re-read only what that evidence implicates, then
patch again. Never repeat an identical action.

## 7. TEST = EVIDENCE, IMPLEMENTATION = VARIABLE

The test is immutable evidence of a contract; the implementation is the
only thing that may change. Treat every assertion as a specification, not
a target to game. Only touch the test if repository evidence conclusively
shows the test itself is wrong, and even then, do so visibly, never
silently.

## 8. ANTI-PATTERNS — AUTOMATIC REJECTION

| Anti-pattern | Why it fails |
|---|---|
| Changing the test assertion to match buggy output | Violates the immutability contract. |
| Adding test-only branches or `if TESTING:` guards | Pollutes production code; detects test execution instead of fixing behavior. |
| Hard-coding the expected value from the test | Fixes the symptom, not the bug. |
| Whole-file reformatting | Obscures the actual fix and breaks blame. |
| Hallucinating imports or utility functions | Introduces NameError or a hidden, nonexistent dependency. |
| Guessing an import path instead of using `resolve_import` | Fails the independent re-verification step. |

## 9. NO HALLUCINATION

Never invent a function, import, class, API, or file's contents. If you
need information you don't have, retrieve it with the tools above; if a
tool can't provide it, say what's missing rather than guessing.

REMINDER: you are not a code generator. You are a surgical repair agent.
Investigate minimally. Reason explicitly. Patch precisely. Verify always.

---

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


def _semantic_query(signals: FailureSignals, output: str) -> str:
    """A short natural-language query for `hybrid_search`, built entirely
    from what's already extracted/observed — no extra LLM call. Pure
    call-graph-distance signals (failing_context/search_symbol/callers)
    only find files reachable by a STATIC call edge from the failing
    test; a bug reached through dynamic dispatch (decorator/registry-based
    handlers, astroid's `inference_tip()` pattern and similar) has no such
    edge and never surfaces no matter how many rounds run. The failure's
    own vocabulary — exception type + the assertion/error text pytest
    prints — is what actually names the concept involved, which is what
    hybrid_search's lexical+dense+graph ranking searches on instead of
    graph proximity."""
    tail = [l.strip() for l in output.strip().splitlines()[-6:]
            if l.strip() and not l.strip().startswith(("File ", "at "))]
    query = " ".join(list(signals.exception_types) + tail)[:300]
    if query:
        return query
    return signals.test_nodes[0] if signals.test_nodes else (signals.test_files[0] if signals.test_files else "")


def _spectrum_to_fusion_input(spectrum: dict[str, "SpectrumHit"]) -> dict:
    """Wrap file-rolled-up SpectrumHits into mlfl.spectrum's SpectrumResult/
    score_spread shape so fusion.compute_fusion (built against mlfl's
    line-level output) can consume localize()'s file-granularity data —
    each file's hit.line is already spectrum.py's own most-suspicious-line
    rollup, so one SpectrumResult per file is the correct working
    granularity here (Suspect has no line field)."""
    if not spectrum:
        return {}
    results = [
        SpectrumResult(file_path=f, line=hit.line, function_name=None,
                       score=hit.score, ef=1, ep=hit.ep, nf=1, np=hit.ep, N=1 + hit.ep)
        for f, hit in spectrum.items()
    ]
    results.sort(key=lambda r: -r.score)
    scores = [r.score for r in results]
    unique_scores = set(round(s, 10) for s in scores)
    return {
        "ranked_candidates": results,
        "score_spread": {
            "min": min(scores), "max": max(scores),
            "unique_scores": len(unique_scores), "total_candidates": len(results),
        },
    }


def localize(signals: FailureSignals, tools: ToolBackend, traj: Trajectory,
             project_dir: Path, known_files: Optional[list[str]] = None,
             output: str = "", spectrum: Optional[dict[str, "SpectrumHit"]] = None) -> list[Suspect]:
    """known_files: the current round's own file set — investigated first
    when a crash's raw output happens to name unrelated files, and used as
    a last-resort suspect when every other signal comes up empty.

    output: the failing test run's raw output (`RunResult.full_output`),
    used only to build a `hybrid_search` query (see `_semantic_query`).
    Optional and additive — every existing signal source above is
    unaffected when omitted.

    spectrum: Ochiai suspiciousness per file from `spectrum.spectrum_localize`
    (computed once per repair session, not per round — see the call site
    in `repair_loop_agentic`). The only signal here grounded in what
    actually EXECUTED for this failure rather than what looks structurally
    or lexically related; scaled to compete with the stacked static-graph
    bumps above (a file the failing test uniquely touches, ep=0, scores
    the full 10.0). Optional and additive, same as every other signal.

    FUSION: when `spectrum` covers 2+ files with distinct scores (real
    discriminating power — see mlfl/fusion.py's variance gate), the CIE-
    sourced signals below (failing_context, search_symbol, callers,
    hybrid_search, traceback) are ALSO run through `mlfl.fusion.
    compute_fusion` for those specific files: a bounded, principled
    combination (the Spectrum-Dominance Lemma) that REPLACES the plain
    additive bump() score for spectrum-covered files only, guaranteeing a
    real spectrum lead can't be buried under noisy aux bumps the way an
    unbounded sum could. Files with no spectrum coverage keep their plain
    bump() score, untouched — fusion only re-scores files spectrum has an
    opinion on, never removes a suspect. With <2 distinct spectrum scores
    (e.g. exactly one file touched — the common case, and this function's
    own existing test coverage) fusion degrades by design and every file
    keeps its bump() score exactly as before this was added."""
    suspects: dict[str, Suspect] = {}
    aux_signals: list[fusion.AuxiliarySignal] = []
    known_files = known_files or []
    known_set = set(known_files)

    def _resolve(file: str, allow_missing: bool = False) -> Optional[str]:
        if Path(file).is_absolute():
            try:
                file = str(Path(file).resolve().relative_to(project_dir.resolve()))
            except ValueError:
                pass
        if _is_test_file(file):
            return None
        # Confirmed live on astroid#769 (2026-08-30): hybrid_search's
        # lexical leg matched a vendored `.venv/.../_pytest/terminal.py`
        # on pytest's own "short test summary" wording and made it the
        # ONLY suspect for two full rounds — every sample burned its turn
        # budget patching a dependency instead of project source.
        if set(Path(file).parts) & _VENDOR_DIRS:
            return None
        if not allow_missing and not (project_dir / file).exists():
            return None
        return file

    def bump(file: str, points: float, why: str, allow_missing: bool = False) -> None:
        resolved = _resolve(file, allow_missing)
        if resolved is None:
            return
        s = suspects.setdefault(resolved, Suspect(resolved, 0.0))
        s.score += points
        s.evidence.append(why)

    def add_aux(file: str, name: str, points: float, confidence: float, why: str) -> None:
        """Record the same evidence bump() just applied as a fusion
        AuxiliarySignal too, scored on fusion's [0,1] scale (points/5.0,
        capped — 5.0 is the current max non-spectrum bump weight, missing-
        module) and keyed to spectrum's own most-suspicious line for this
        file when spectrum covers it, since fusion matches signals to
        spectrum candidates by exact file:line (mlfl/fusion.py)."""
        resolved = _resolve(file)
        if resolved is None:
            return
        line = spectrum[resolved].line if spectrum and resolved in spectrum else 0
        aux_signals.append(fusion.AuxiliarySignal(
            name=name, file_path=resolved, line=line,
            score=min(points / 5.0, 1.0), confidence=confidence, evidence=why,
        ))

    for p in signals.traceback_paths:
        resolved = _resolve_traceback_path(project_dir, p)
        if resolved:
            bump(resolved, 3.0, "named in traceback")
            add_aux(resolved, "traceback", 3.0, 0.9, "named in traceback")

    for specifier, requiring_rel in signals.missing_modules:
        resolved = _resolve_node_missing_module(project_dir, specifier, requiring_rel)
        if resolved:
            bump(resolved, 5.0, f"required by {requiring_rel} but does not exist on disk",
                 allow_missing=True)

    # Structural signals (failing_context/search_symbol/callers) — best-
    # effort like hybrid_search below, NOT previously guarded: confirmed
    # live (astroid#3258, 2026-08-30, three `fix` runs launched in
    # parallel) that a slow/contended CIE backend timing out inside
    # `tools.callers()` propagated as an unhandled TimeoutError and killed
    # the entire repair loop outright — losing a structural signal should
    # degrade localization, not crash the whole run.
    test_candidates = signals.test_nodes or signals.test_files
    ordered_tests = sorted(test_candidates, key=lambda f: f not in known_set)[:3]
    for test in ordered_tests:
        try:
            ctx = tools.failing_context(test)
        except Exception:  # noqa: BLE001 — structural signal, optional, never fatal
            continue
        for r in ctx.get("results", []):
            pts = 4.0 if r["distance"] == 1 else 2.0
            why = f"distance-{r['distance']} from failing test ({r['symbol']})"
            bump(r["file"], pts, why)
            add_aux(r["file"], "failing_context", pts, 0.8 if r["distance"] == 1 else 0.5, why)

    for name in signals.symbol_names[:5]:
        try:
            hits = tools.search_symbol(name)
        except Exception:  # noqa: BLE001 — structural signal, optional, never fatal
            continue
        for h in hits.get("results", [])[:2]:
            why = f"defines symbol '{name}' from error message"
            bump(h["source_file"], 3.0, why)
            add_aux(h["source_file"], "search_symbol", 3.0, 0.6, why)
        try:
            callers = tools.callers(name).get("results", [])
        except Exception:  # noqa: BLE001 — structural signal, optional, never fatal
            callers = []
        for c in callers[:3]:
            why = f"calls '{name}' (possible import/usage site)"
            bump(c["caller_file"], 1.5, why)
            add_aux(c["caller_file"], "callers", 1.5, 0.4, why)

    # Semantic signal — optional (only CIE-backed tool backends implement
    # this) and best-effort: an embeddings/index outage must never break
    # localization, it just loses this one extra signal.
    if hasattr(tools, "hybrid_search"):
        try:
            query = _semantic_query(signals, output)
            if query:
                hits = tools.hybrid_search(query, top_k=8)
                for h in hits.get("results", [])[:6]:
                    src = h.get("source_file")
                    if src:
                        why = f"semantically relevant to failure ({h.get('name', '?')})"
                        bump(src, 3.5, why)
                        add_aux(src, "hybrid_search", 3.5, 0.5, why)
        except Exception:  # noqa: BLE001 — semantic search is optional, never fatal
            pass

    # Causal signal — grounded in actual test execution, not structure or
    # lexical similarity. See spectrum.py's module docstring. Line-level,
    # not file-level (confirmed live on astroid#769: file-level tied ALL
    # ~90 candidate files at the identical score — see spectrum.py) — the
    # specific line + raw ep count are surfaced so the agent can jump
    # straight to it instead of re-deriving what's already known.
    for f, hit in (spectrum or {}).items():
        bump(f, hit.score * 10.0,
             f"spectrum: line {hit.line} uniquely suspicious (Ochiai={hit.score:.2f}, ep={hit.ep})")

    # Bounded fusion (mlfl/fusion.py): re-score spectrum-covered files by
    # combining their spectrum score with the aux signals collected above,
    # REPLACING the plain additive bumps just applied to those specific
    # files. Degrades to a no-op — see docstring — whenever spectrum
    # doesn't have 2+ distinct scores to protect, or has no aux overlap at
    # all (falls back to pure spectrum ranking internally, same *10 scale
    # as the bump above); every non-spectrum-covered file is untouched.
    if spectrum:
        spectrum_output = _spectrum_to_fusion_input(spectrum)
        fusion_output = fusion.compute_fusion(spectrum_output, aux_signals) if spectrum_output else {}
        for fc in (fusion_output or {}).get("ranked_candidates", []):
            s = Suspect(fc.file_path, fc.fused_score * 10.0)  # same x10 scale as the bump above
            s.evidence.append(
                f"fused: spectrum={fc.spectrum_score:.3f} + aux={fc.auxiliary_bonus:.3f}"
                + (" [spectrum-protected]" if fc.is_spectrum_protected else ""))
            s.evidence.extend(f"[{sig.name}] {sig.evidence}" for sig in fc.auxiliary_signals)
            suspects[fc.file_path] = s

    ranked = sorted(suspects.values(), key=lambda s: -s.score)

    if not ranked and known_files:
        for f in known_files:
            bump(f, 0.5, "batch's own file — no other localization signal found")
        ranked = sorted(suspects.values(), key=lambda s: -s.score)

    traj.log("localize", signals=signals.summary(),
             suspects=[{"file": s.file, "score": s.score, "why": s.evidence[:2]} for s in ranked])
    return ranked


def _format_exhaustion_note(exhausted_files: dict[str, str]) -> str:
    """One task_prompt paragraph summarizing files earlier rounds already
    burned their full sample budget on, reusing data already collected in
    `exhausted_files` — no extra LLM call, no message-scraping. Empty
    string when nothing has been exhausted yet."""
    if not exhausted_files:
        return ""
    tried_list = "; ".join(f"{f} ({why})" for f, why in exhausted_files.items())
    return (f"\n\nPrior round(s) already exhausted their turn budget on: {tried_list}. "
            "Do not re-investigate those files unless this suspect list gives you no "
            "better option — the earlier attempt(s) already read them closely and could "
            "not find or land a fix there.")


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
    #: this candidate's own pre-patch content/existence, captured at
    #: evaluation time. Confirmed live on astroid#3257 (2026-08-30): the
    #: round loop used to assume every candidate targeted the SAME file
    #: (the round's assigned `top.file`) and shared one `target`/`original`
    #: for apply/test/revert — but a sample's own investigation can
    #: correctly determine the real fix belongs in a DIFFERENT file (see
    #: the `patch` tool's optional `path` argument), so each candidate
    #: needs its own original/existed_before to revert correctly.
    original: str = ""
    existed_before: bool = False


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

    def check(patch: Optional[str], path: Optional[str] = None) -> tuple[bool, str]:
        # `path`: confirmed live on astroid#3257 (2026-08-30) — a sample's
        # own investigation can correctly determine the real fix belongs
        # in a file OTHER than `file_rel` (the round's assigned suspect),
        # and previously had no way to say so: every patch was diffed
        # against `current` (file_rel's content) regardless, so a
        # genuinely correct patch for a different file was rejected every
        # time as "SEARCH block not found" — it was being matched against
        # the wrong file entirely. None (the common case) means "the
        # file this sample was pointed at," preserving old behavior.
        if not patch:
            return False, "You SUBMITted without a PATCH. Produce PATCH first."
        target_rel = (path or file_rel).strip().lstrip("/")
        target_path = (project_dir / target_rel).resolve()
        try:
            target_path.relative_to(project_dir.resolve())
        except ValueError:
            return False, f"'{path}' is outside the project — patch a file inside the repo."
        if _is_test_file(target_rel):
            return False, (
                f"'{path}' is a test file — never patch the test to make it pass. "
                "Fix the implementation the test is checking, in a different file."
            )
        if target_rel == file_rel:
            target_content = current  # avoid a redundant re-read for the common case
        else:
            target_content = target_path.read_text(errors="replace") if target_path.exists() else ""
        new, err = _apply_patch(target_content, patch)
        if new is None:
            if not target_path.exists():
                return False, (
                    f"{target_rel} does not exist yet — there is nothing for SEARCH/REPLACE "
                    "to match. Submit a PATCH containing ONE fenced code block with the "
                    "file's COMPLETE content instead."
                )
            return False, f"Patch does not apply: {err}. Re-read the file (view_file) and match SEARCH exactly."
        ok, why = lint_gate(project_dir, target_rel, new)
        if not ok:
            return False, f"Patch fails syntax gate: {why}. Fix and PATCH again."
        holder["new"] = new
        holder["path"] = target_rel
        holder["original"] = target_content
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
    actual_file = holder.get("path", file_rel)
    original = holder.get("original", current)
    return Candidate(file=actual_file, new_content=new, diff_size=_diff_size(original, new), turns=result.turns)


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

    # Ochiai spectrum, computed ONCE for the whole session (not per round):
    # the causal relationship between files and the ORIGINAL failing test
    # doesn't change round to round, and each computation costs ~1 + N
    # extra test subprocess runs — see spectrum.py. Best-effort: any
    # failure (non-pytest stack, no pytest-cov, nothing to sample) just
    # yields {}, and localize() treats an empty spectrum as no signal.
    initial_signals = extract_signals(res.full_output)
    failing_test_id = (initial_signals.test_nodes[0] if initial_signals.test_nodes
                       else initial_signals.test_files[0] if initial_signals.test_files else None)
    spectrum_scores: dict[str, SpectrumHit] = {}
    if failing_test_id:
        try:
            spectrum_scores = spectrum_localize(project_dir, test_cmd, image, failing_test_id)
        except Exception:  # noqa: BLE001 — spectrum is optional, never fatal
            spectrum_scores = {}

    best_failures = initial
    repaired: list[str] = []
    suspects: list[Suspect] = []
    #: Set when a round's winner is rejected by the blast-radius gate —
    #: fed into the NEXT round's task_prompt as an explicit constraint.
    pending_violations: list[str] = []
    #: file -> short reason a full round's sample budget was already spent
    #: on it with nothing usable to show. localize() is a pure function of
    #: the (unchanged, since no patch has landed) failing-test signals, so
    #: without this a round whose samples all fail on the top suspect just
    #: hands the NEXT round the identical ranked list and repeats the same
    #: failed investigation — confirmed live on astroid#769 (2026-08-30):
    #: round 2's localize() suspects were byte-identical to round 1's, and
    #: both rounds spent their full sample budget re-investigating the same
    #: file. Once a suspect is exhausted, route to the next untried one.
    exhausted_files: dict[str, str] = {}

    for round_no in range(1, max_rounds + 1):
        if time.time() - t0 > per_issue_seconds:
            traj.log("repair", result="wall-clock exhausted", round=round_no)
            break

        signals = extract_signals(res.full_output)
        suspects = localize(signals, tools, traj, project_dir,
                             known_files=list((tasks_by_file or {}).keys()),
                             output=res.full_output, spectrum=spectrum_scores)
        if not suspects:
            traj.log("repair", result="localization found no suspects", round=round_no)
            break

        before = _failure_count_for(res)
        top = next((s for s in suspects if s.file not in exhausted_files), suspects[0])
        if exhausted_files and top.file != suspects[0].file:
            traj.log("repair_localize_promote", round=round_no, from_file=suspects[0].file,
                     to_file=top.file, exhausted=list(exhausted_files.keys()))
        emit("repair_round", "running", f"round {round_no}: investigating {top.file}")
        task_prompt = (
            f"# Failing test output (truncated)\n```\n{truncate(res.output, 3500)}\n```\n\n"
            f"# Localization (ranked suspects)\n" +
            "\n".join(f"- {s.file} (score {s.score:.1f}): {'; '.join(s.evidence[:2])}"
                      + (f"  [ALREADY TRIED — {exhausted_files[s.file]}]" if s.file in exhausted_files else "")
                      for s in suspects[:4]) +
            f"\n\nPrime suspect: {top.file}. Investigate, find the root cause, PATCH it, verify, SUBMIT."
            + _format_exhaustion_note(exhausted_files)
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
            exhausted_files[top.file] = "no candidate produced within turn budget"
            continue

        # --- execution-based selection: apply → test → restore, per candidate.
        # Each candidate gets ITS OWN target/original — a candidate's file
        # may differ from `top.file` (the model's `patch` call can name a
        # different, correctly-investigated path; see Candidate.original's
        # docstring) — never assume every candidate shares one target.
        for cand in candidates:
            cand_target = project_dir / cand.file
            cand.existed_before = cand_target.exists()
            cand.original = cand_target.read_text(errors="replace") if cand.existed_before else ""
            cand_target.parent.mkdir(parents=True, exist_ok=True)
            cand_target.write_text(cand.new_content)
            r = run_test(test_cmd, image, project_dir, timeout=timeout)
            cand.failures = _failure_count_for(r)
            traj.log("candidate_eval", round=round_no, file=cand.file,
                     diff=cand.diff_size, failures=cand.failures, turns=cand.turns)
            if cand.existed_before:
                cand_target.write_text(cand.original)
            else:
                cand_target.unlink(missing_ok=True)

        green = [c for c in candidates if c.failures == 0]
        if green:
            winner = min(green, key=lambda c: c.diff_size)
        else:
            winner = min(candidates, key=lambda c: (c.failures if c.failures is not None else 1e9))
            if winner.failures is None or winner.failures >= before:
                traj.log("repair", result="no candidate improved state", round=round_no,
                         best=[c.failures for c in candidates])
                emit("repair_round", "failed", f"round {round_no}: no candidate improved on {before} failing")
                exhausted_files[top.file] = "candidates produced but none improved on the failing state"
                continue

        blast_violations = _blast_radius_violations(winner.file, winner.original, winner.new_content, tools)
        if blast_violations:
            traj.log("repair", result="winner rejected by blast-radius gate", round=round_no,
                     file=winner.file, violations=blast_violations)
            emit("repair_round", "failed", f"round {round_no}: winner rejected (blast-radius violation)")
            pending_violations = blast_violations
            continue

        winner_target = project_dir / winner.file
        winner_target.parent.mkdir(parents=True, exist_ok=True)
        winner_target.write_text(winner.new_content)
        commit(project_dir, f"forge: repair {winner.file} (round {round_no}, "
                            f"{'green' if winner.failures == 0 else 'best-effort'}, "
                            f"diff {winner.diff_size})")
        if winner.file not in repaired:
            repaired.append(winner.file)
        tools.reindex_file(winner.file)

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
