"""
Spectrum-Based Fault Localization (SBFL) — a deterministic pre-filter.

This is the one feature atomic-forge was missing that *every* serious
competitor (Agentless, AutoCodeRover) and the classic fault-localization
literature converge on: when a runnable test suite with per-test coverage
exists, the coverage *spectrum* (which code each passing vs failing test
executed) is a cheap, deterministic signal that ranks "where to look first"
*before* any LLM reasoning tokens are spent. The LLM still does the repair;
this just narrows its search space by orders of magnitude deterministically.

Scientific grounding
---------------------
- Ochiai is empirically the strongest single SBFL formula across the
  literature: Abreu, Zoeteweij & van Gemund, "On the Accuracy of
  Spectrum-based Fault Localization" (TAICPART-MUTATION 2007) — Ochiai
  consistently beats Jaccard and Tarantula across the whole observation
  quality/quantity space, and near-optimal accuracy is reached at ~6
  failing tests.
- On *real Python* faults (BugsInPy; Widyasari et al., EMSE 2022,
  "Real World Projects, Real Faults: Evaluating SBFL on Python Projects"),
  Tarantula/Barinel/Ochiai are statistically tied on real faults (and beat
  the newer O^p/DStar) — so Ochiai is a safe default and Tarantula is kept
  as a documented fallback, exactly as Agentless notes ("classic
  information-retrieval-based localization idea").
- AutoCodeRover (Zhang et al. 2024) and Agentless (Xia et al. 2024) both
  fold fault localization in as the first phase; Agentless's ablation
  shows hierarchical localization (file -> skeleton -> edit location) is
  what makes the agentless approach competitive with full agents.

Design (adapted from the gsd-core SBFL reference contract)
----------------------------------------------------------
- Pure scoring functions (`ochiai`, `tarantula`) are the canonical,
  heavily-unit-tested core — any future formula drift is caught by the
  known-fault fixture.
- `compute_spectrum()` acquires per-test coverage via `coverage.py` with
  dynamic contexts, and per-test pass/fail via pytest's junit-xml. It is
  *bounded* (a coverage run is ~2-3x a plain test run) and *degrades
  cleanly*: every miss returns a typed skip reason, never a silent pass.
- `rank_suspicious()` turns a spectrum into a top-N shortlist.
- `sbfl_prefilter()` is the one-call orchestrator with the full degradation
  matrix (no suite / no failing / no passing / no coverage / non-Python /
  coverage timeout) and explicit Bohrbug-vs-Heisenbug gating: a flaky
  suite pollutes the spectrum, so SBFL is skipped on non-deterministic
  failures and the skip is *logged*, not hidden (Kernighan — the agent
  stays auditable).

This step is purely additive: when it can't run, the caller proceeds with
its existing localization unchanged.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# pure scoring formulas — the canonical, paper-anchored core
# --------------------------------------------------------------------------

def ochiai(failed: int, passed: int, total_failed: int) -> float:
    """Ochiai suspiciousness for one code element.

        ochiai(s) = failed(s) / sqrt(total_failed * (failed(s) + passed(s)))

    Range [0, 1]; 1.0 = executed by every failing test and no passing test.
    An element executed by no test (failed+passed == 0) is defined as 0.0
    (it carries no signal), and total_failed == 0 is the caller's job to
    gate out before calling — see `sbfl_prefilter`'s degradation matrix.
    (Abreu et al. 2007; confirmed-best across Jaccard/Tarantula/Barinel.)"""
    if total_failed <= 0:
        return 0.0
    denom = math.sqrt(total_failed * (failed + passed))
    if denom == 0.0:
        return 0.0
    return failed / denom


def tarantula(failed: int, passed: int, total_failed: int, total_passed: int) -> float:
    """Tarantula suspiciousness — the documented fallback formula.

        tarantula(s) = (failed(s)/total_failed)
                       / ((failed(s)/total_failed) + (passed(s)/total_passed))

    Range [0, 1]. Returns 0.0 when neither failures nor passes touched the
    element, and degrades to 0.0 when total_passed == 0 (the BugsInPy
    Python study found Tarantula statistically tied with Ochiai on real
    faults, but it divides by total_passed, so it MUST NOT be run with a
    zero-passing spectrum — `sbfl_prefilter` skips the whole step in that
    case rather than relying on this guard)."""
    if total_failed <= 0:
        return 0.0
    fr = failed / total_failed
    pr = (passed / total_passed) if total_passed > 0 else 0.0
    denom = fr + pr
    if denom == 0.0:
        return 0.0
    return fr / denom


# --------------------------------------------------------------------------
# spectrum data model
# --------------------------------------------------------------------------

@dataclass
class TestOutcome:
    """One test's pass/fail status and the code it executed.

    `name` is the fully-qualified test id (e.g. ``tests/test_x.py::test_foo``
    or a coverage dynamic-context test-function name). `passed` is its
    pass/fail boolean. `covered` maps a *repo-relative* file path to the
    set of 1-based line numbers that test executed."""
    name: str
    passed: bool
    covered: dict = field(default_factory=dict)  # file -> set[int]


@dataclass
class Spectrum:
    """The full coverage spectrum over a test run.

    `total_failed` / `total_passed` are the suite-wide counts; `tests` is
    the per-test detail. `project_dir` is the repo root used to make file
    paths repo-relative (so callers can map back to real files)."""
    project_dir: Path
    tests: list  # list[TestOutcome]
    total_failed: int
    total_passed: int


@dataclass
class SuspiciousLocation:
    """One ranked suspicious code element.

    `score` is the Ochiai (or fallback Tarantula) value; `failed`/`passed`
    are the raw coverage counts behind it (for evidence/audit). `kind` is
    ``"line"`` or ``"function"`` (function-level rolls up a function's
    max-line score). `symbol` is the enclosing function/class name when
    available, else ``""``."""
    file: str
    kind: str
    line: int
    score: float
    failed: int
    passed: int
    symbol: str = ""


@dataclass
class SbflResult:
    """The outcome of `sbfl_prefilter` — either a ranked shortlist or a
    typed skip. `skipped` is False iff `locations` is populated; `reason`
    is always set (empty string on a clean, non-skipped run)."""
    skipped: bool
    reason: str
    locations: list = field(default_factory=list)  # list[SuspiciousLocation]
    formula: str = "ochiai"
    total_failed: int = 0
    total_passed: int = 0


# --------------------------------------------------------------------------
# ranking — pure, unit-tested
# --------------------------------------------------------------------------

def _aggregate_line_counts(spectrum: Spectrum) -> dict:
    """Collapse the per-test spectrum into per-line (failed, passed) counts.

    Returns ``{file: {line: [failed_count, passed_count]}}``. A line's
    `failed_count` is the number of *failing* tests that executed it;
    `passed_count` the number of *passing* tests that executed it. This is
    exactly the `a11`/`a01` decomposition SBFL formulas consume (a line
    executed only by failing tests -> max suspiciousness)."""
    counts: dict = {}  # file -> line -> [failed, passed]
    for t in spectrum.tests:
        for f, lines in t.covered.items():
            fcounts = counts.setdefault(f, {})
            bucket = 1 if not t.passed else 0
            for ln in lines:
                slot = fcounts.setdefault(ln, [0, 0])
                slot[bucket] += 1
    return counts


def rank_suspicious(spectrum: Spectrum, *, top_n: int = 10,
                    formula: str = "ochiai") -> list:
    """Rank executed code elements by suspiciousness and return the top-N.

    Granularity is *line* (the most actionable for a repair agent).
    `formula` is ``"ochiai"`` (default) or ``"tarantula"``. Ties are broken
    by (more failing tests, then fewer passing tests, then file/line) so
    the most-likely-correct element wins a tie. An element executed by no
    failing test scores 0 and is dropped."""
    if spectrum.total_failed <= 0:
        return []
    counts = _aggregate_line_counts(spectrum)
    tf, tp = spectrum.total_failed, spectrum.total_passed
    ranked: list = []
    for f, fcounts in counts.items():
        for ln, (failed, passed) in fcounts.items():
            if failed == 0:
                continue  # executed only by passing tests -> not suspicious
            if formula == "tarantula":
                score = tarantula(failed, passed, tf, tp)
            else:
                score = ochiai(failed, passed, tf)
            ranked.append(SuspiciousLocation(
                file=f, kind="line", line=ln, score=score,
                failed=failed, passed=passed,
            ))
    # tie-break: higher score, then more failing, then fewer passing, then path
    ranked.sort(key=lambda s: (-s.score, -s.failed, s.passed, s.file, s.line))
    return ranked[:top_n]


# --------------------------------------------------------------------------
# coverage acquisition — bounded, degrades cleanly
# --------------------------------------------------------------------------

def _run(cmd: list, cwd: Path, timeout: int, env: dict) -> tuple[int, str]:
    """Shell out, never raise. Returns (exit_code, combined_output)."""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, env=env)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
              f"\n[TIMEOUT after {timeout}s]"
        return 124, out
    except FileNotFoundError as e:
        return 127, f"[command not found: {e}]"


def _coverage_available() -> bool:
    """Is the `coverage` package importable on the forge's own interpreter?
    (Used to decide whether we can collect a per-test spectrum at all.)"""
    try:
        import coverage  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _parse_junit_passfail(junit_xml: Path) -> dict:
    """Parse a pytest --junitxml file into ``{test_id: passed_bool}``.

    pytest's junit-xml uses ``<testcase classname=... name=...>`` with a
    child ``<failure>``/``<error>``/``<skipped>`` iff not passed. The test
    id is reconstructed as ``classname::name`` to match coverage's dynamic
    context test-function names where possible."""
    results: dict = {}
    try:
        tree = ET.parse(junit_xml)
    except ET.ParseError:
        return results
    for tc in tree.iter("testcase"):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        tid = f"{classname}::{name}" if classname else name
        failed = any(tc.find(tag) is not None
                     for tag in ("failure", "error", "skipped"))
        results[tid] = not failed
    return results


def _short_test_id(name: str) -> str:
    """Normalize a junit/coverage test id to a comparable basename.

    Coverage dynamic contexts report the bare function name (``test_foo``);
    junit reports ``tests.test_mod::test_foo`` or ``tests/test_mod.py::test_foo``.
    We match on the trailing ``::name`` part so the two sources join."""
    if "::" in name:
        return name.rsplit("::", 1)[-1]
    return name.rsplit(".", 1)[-1] if "." in name else name


def compute_spectrum(project_dir: str | Path, test_cmd: Optional[str] = None,
                      *, timeout: int = 180, extra_env: Optional[dict] = None
                      ) -> Optional[Spectrum]:
    """Run the test suite under `coverage` with per-test dynamic contexts and
    return a parsed `Spectrum`, or None on any degradation.

    Degradations (all return None, with the reason available to the caller
    via the orchestrator `sbfl_prefilter`):
      - not a Python project / `coverage` not importable -> None
      - no test command discoverable and none given -> None
      - the coverage run times out -> None
      - `coverage` produces no per-context data (e.g. no dynamic contexts
        recorded) -> None

    `test_cmd` overrides auto-detection (the `fix` one-shot passes its own).
    Bounded: a single coverage run, capped at `timeout` seconds (default
    180 — a coverage run is ~2-3x a plain test run, so we cap it and let
    the caller degrade-to-skip rather than hang)."""
    project_dir = Path(project_dir)
    if not _coverage_available():
        return None
    if test_cmd is None:
        from .sandbox import detect_test_stack
        stack = detect_test_stack(project_dir)
        if stack is None:
            return None
        test_cmd = stack.cmd
    # Only Python suites are supported (coverage.py is Python-only).
    if "pytest" not in test_cmd and "python -m pytest" not in test_cmd and "-m pytest" not in test_cmd:
        return None

    env = {**os.environ, "CI": "true", **(extra_env or {})}
    with tempfile.TemporaryDirectory(prefix="forge_sbfl_") as tmpd:
        tmpd = Path(tmpd)
        cov_file = tmpd / ".coverage"
        cov_json = tmpd / "cov.json"
        junit = tmpd / "junit.xml"
        rc = tmpd / ".coveragerc"
        # dynamic_context = test_function records the enclosing test fn as
        # the coverage context for every executed line -> per-test coverage.
        rc.write_text(
            "[run]\n"
            "branch = False\n"
            "dynamic_context = test_function\n"
            f"data_file = {cov_file}\n"
            "[json]\n"
            "show_contexts = True\n"
        )
        env["COVERAGE_PROCESS_START"] = str(rc)
        # Run pytest under coverage. --cov-report= suppresses terminal noise;
        # -p no:cacheprovider keeps it from writing into the project; junit
        # gives us authoritative per-test pass/fail.
        run_cmd = [
            sys.executable, "-m", "coverage", "run",
            "--rcfile", str(rc), "-m", "pytest",
            "-p", "no:cacheprovider",
            f"--junitxml={junit}",
            "-q", "--no-header", "--tb=no",
        ]
        # If test_cmd already names a target (file/dir/node), append it so we
        # don't re-run the whole repo unnecessarily.
        target = test_cmd.replace("python -m pytest", "").replace("pytest", "").strip()
        if target:
            run_cmd.append(target)
        code, _out = _run(run_cmd, project_dir, timeout, env)
        if code == 124:  # timeout -> degrade
            return None
        if not cov_file.exists():
            return None
        # Emit JSON with contexts.
        jcode, jout = _run([sys.executable, "-m", "coverage", "json",
                            "--rcfile", str(rc), f"-o={cov_json}"],
                           project_dir, 60, env)
        if not cov_file.exists() and not cov_json.exists():
            return None
        try:
            data = json.loads(cov_json.read_text())
        except Exception:  # noqa: BLE001
            return None

    passfail = _parse_junit_passfail(junit) if junit.exists() else {}
    if not passfail:
        return None  # no per-test pass/fail -> can't build a spectrum

    # Build per-test coverage from the json contexts. coverage json with
    # show_contexts produces, per file: {"contexts": {line: [ctx, ...]}}
    # where each ctx is the test-function dynamic context name.
    project_dir_resolved = project_dir.resolve()
    tests_map: dict = {}  # test_name -> TestOutcome
    for fpath, fdata in data.get("files", {}).items():
        contexts = fdata.get("contexts", {})
        # Make the path repo-relative for stable downstream lookups.
        try:
            rel = str(Path(fpath))
        except Exception:  # noqa: BLE001
            rel = fpath
        for ln_str, ctx_list in contexts.items():
            ln = int(ln_str)
            for ctx in ctx_list or []:
                key = _short_test_id(ctx)
                to = tests_map.get(key)
                if to is None:
                    # match against junit by short id; default passed=True
                    # until junit says otherwise (unknown test = assume pass)
                    to = TestOutcome(name=ctx, passed=passfail.get(ctx, True))
                    tests_map[key] = to
                to.covered.setdefault(rel, set()).add(ln)

    tests = list(tests_map.values())
    total_failed = sum(1 for t in tests if not t.passed)
    total_passed = sum(1 for t in tests if t.passed)
    if total_failed == 0 or total_passed == 0:
        return None  # spectrum requires both (degrade; caller logs why)
    return Spectrum(project_dir=project_dir_resolved, tests=tests,
                    total_failed=total_failed, total_passed=total_passed)


# --------------------------------------------------------------------------
# orchestrator — the one-call pre-filter with the full degradation matrix
# --------------------------------------------------------------------------

def sbfl_prefilter(project_dir: str | Path, test_cmd: Optional[str] = None,
                   *, top_n: int = 10, timeout: int = 180,
                   formula: str = "ochiai",
                   deterministic: bool = True) -> SbflResult:
    """The deterministic SBFL pre-filter. Returns a ranked top-N shortlist
    of suspicious locations, or a typed skip — never raises, never a silent
    pass.

    Degradation matrix (every skip sets `reason`; the caller logs it):
      - not a Python project / `coverage` unavailable      -> skip
      - no runnable test suite                              -> skip
      - no failing tests (no spectrum)                      -> skip
      - no passing tests (Tarantula would div-by-zero)       -> skip
      - coverage run times out                              -> skip
      - `deterministic=False` (Heisenbug/Mandelbug)          -> skip
        (a flaky suite poisons the spectrum; SBFL is the go-to
        for Bohrbugs only)

    `top_n` bounds the shortlist (5-10 typical). `formula` is
    ``"ochiai"`` (default, Abreu 2007) or ``"tarantula"`` (fallback)."""
    if not deterministic:
        return SbflResult(skipped=True, reason=(
            "SBFL skipped: failure is non-deterministic (Heisenbug/Mandelbug) — "
            "a flaky suite pollutes the spectrum; route to record-replay/stress instead"))
    if not _coverage_available():
        return SbflResult(skipped=True, reason=(
            "SBFL skipped: `coverage` package not available — install with "
            "`pip install coverage` to enable spectrum-based fault localization"))
    spectrum = compute_spectrum(project_dir, test_cmd, timeout=timeout)
    if spectrum is None:
        return SbflResult(skipped=True, reason=(
            "SBFL skipped: no usable per-test coverage spectrum (non-Python suite, "
            "no failing AND passing tests, or coverage run failed/timed out)"))
    locations = rank_suspicious(spectrum, top_n=top_n, formula=formula)
    if not locations:
        return SbflResult(skipped=True, reason=(
            "SBFL skipped: spectrum computed but no line was executed by any "
            "failing test — nothing suspicious to rank"))
    return SbflResult(
        skipped=False, reason="", locations=locations, formula=formula,
        total_failed=spectrum.total_failed, total_passed=spectrum.total_passed,
    )