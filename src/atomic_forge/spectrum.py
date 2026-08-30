"""
Spectrum-Based Fault Localization (SBFL) — Ochiai suspiciousness scoring.

Every other localization signal in `repair_agent.py` (`failing_context`
call-graph distance, `search_symbol`, CIE's `hybrid_search`) models code
STRUCTURE — what calls what, what looks related. None of them model
CAUSATION — what actually ran to produce this specific failure. That's
the reason astroid#769's real fix site (`rebuilder.py::check_type_comment`,
reached only through a decorator-registered dispatch table, not a static
call edge) never appeared in any suspect list across a full campaign run
(2026-08-30): every signal available was structural, and this bug's fix
is invisible to structure.

Ochiai (Abreu et al. 2007) fixes this the direct way: actually run the
code and see what executes. Given one failing test and a sample of
passing tests, a file's suspiciousness is how disproportionately it's
covered by the failing test versus the passing ones:

    susp(f) = ef / sqrt((ef + nf) * (ef + ep))

ef/nf: whether the FAILING test did/didn't execute f (always exactly one
failing test here, so nf = 1 - ef). ep: how many of the sampled PASSING
tests also executed f. A file the failing test touches that almost no
passing test touches scores near 1.0; a file touched by nearly everything
(ubiquitous helpers, base classes) scores near 0 — exactly separating
"this is what's special about the failing case" from "this is just
infrastructure everything goes through."

Precedent, not a fresh idea: AutoCodeRover (arXiv:2404.05427) tested this
exact "SBFL as one additive signal alongside LLM-driven search" pattern
on SWE-bench and measured +9 resolved out of 300 (19%->22%) when added.
The reason most SWE-bench agents skip it isn't cost — it's that most
real issues arrive with no reliable failing test to instrument. This
project's F1/testgen precondition (a validated, reproducing failing test
exists before repair ever starts) is exactly what SBFL needs and what
those baselines are missing.

Python/pytest only for now (coverage.py is Python-specific, and
pytest-cov is already installed in every Python stack's bootstrap venv —
see stacks.py). Every failure mode here (coverage not installed,
collection fails, a sampled "passing" test turns out flaky) degrades to
an empty dict, never an exception — same optional-signal contract as
`hybrid_search` in `cie_backend.py`.

GRANULARITY (confirmed live on astroid#769, 2026-08-30): the formula
above is applied per LINE, not per file. An earlier file-level version
of this module — "does this file's coverage report mention the file at
all" — measured ALL ~90 candidate files in astroid tied at the identical
score 1/sqrt(7)=0.3780, because astroid.parse() cascades into importing
nearly the whole package (~15 decorator-self-registering `brain/` plugin
modules), so every sampled passing test touches every candidate file's
MODULE-LEVEL code (class/def statements, decorator registrations —
executed once at import time, by construction, for every test) even
when its actual FUNCTION-BODY code never runs. File-level "touched at
all" can't distinguish "imported" from "this specific runtime path was
exercised" — the two research passes that diagnosed this proved
algebraically that switching to line-level granularity (using
coverage.py's `executed_lines`, already collected but previously
discarded here) restores real discriminating power, and that `max`
(not sum or mean) is the only sound file-level rollup of per-line
scores: sum can literally invert the ranking in favor of a bigger,
innocent file (proven by a concrete counterexample — an innocent file
with more module-level lines can out-sum a smaller file containing the
actual fault line); mean's margin shrinks as O(1/file_size). Re-running
this exact bug with line-level scoring (`sbfl_probe3.py`, real astroid
checkout, 8 real passing samples) measured the tie collapse from ~90
files to 2 files at the top score, with two independently-hypothesized
candidate fix files (`rebuilder.py`, `scoped_nodes.py`/`protocols.py`,
from two unrelated prior sessions) both landing in the real top 10 of
92 touched files — see campaign notes for the full before/after.

Formula CHOICE was separately proven irrelevant here, not merely
untested: with exactly one failing test, ef=1 and nf=0 are fixed
constants for every candidate, so Ochiai/Jaccard/Tarantula/DStar/Op2 all
reduce algebraically to strictly monotonic functions of `ep` alone and
produce IDENTICAL rankings — confirmed both symbolically and by
computing all five on the same worked numbers. Ochiai is kept as the
default with no loss versus the alternatives in this single-failing-test
regime.
"""
from __future__ import annotations

import json
import math
import random
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .sandbox import run_test


@dataclass(frozen=True)
class SpectrumHit:
    """One file's line-level spectrum evidence, rolled up by max.

    `score`: Ochiai suspiciousness of the single most-suspicious line in
    this file that the failing test touched (see module docstring for
    why max, not sum/mean, is the sound rollup).
    `line`: the 1-based line number that achieved `score` — surfaced so a
    downstream agent can jump straight to it instead of re-deriving it
    (e.g. via `view_file(file, line-10, line+10)` or `statement_graph`).
    `ep`: how many of the sampled passing tests also executed that exact
    line — the raw count behind `score`, since with a small sample (see
    `DEFAULT_PASSING_SAMPLES`) the derived float alone can be misleading:
    `ep=0` is a clean, maximally-trustworthy signal; `ep` near the sample
    count is a much weaker one even at the same rounded score."""
    score: float
    line: int
    ep: int

#: Each sampled "passing" test costs one extra pytest+coverage subprocess
#: — this is the whole latency budget for the signal (~1 + N test runs,
#: once per repair session, not per round).
DEFAULT_PASSING_SAMPLES = 6

_NODE_ID_RE = re.compile(r"^(\S+\.py(?:::\S+)+)\s*$")


def _collect_test_ids(project_dir: Path, test_cmd: str, image, timeout: int) -> list[str]:
    """Enumerate real test node ids via `pytest --collect-only` —
    nothing executes, this only lists what COULD be sampled as a passing
    comparison test. [] on any failure; collection is best-effort.

    Deliberately no extra `-q` here: `test_cmd` already carries one (every
    TestStack's cmd does), and pytest's `-q`/`-qq` levels are cumulative —
    stacking a second `-q` on top silently switches `--collect-only`'s
    output from one node id per line to an aggregated `file.py: N` count
    summary, which this function's regex can never match. Confirmed live
    against astroid#769 (2026-08-30): this returned 0 candidates and
    silently killed the whole spectrum signal every round, with no
    exception anywhere to surface it — `spectrum_localize`'s `if not
    candidates: return {}` degraded exactly as designed, just for the
    wrong reason. Single `-q` (whatever test_cmd already has) is the
    correct, well-documented format.

    An explicit trailing `.` positional arg is appended deliberately:
    `fix.py`'s own `test_cmd` (`make_test_cmd`) already bakes in ONE
    specific test file as a positional arg, scoped that way on purpose so
    every ordinary repair-round test run stays fast. Appending `--collect-
    only` alone would inherit that same scope and only ever "discover"
    the one file already being repaired — after excluding the failing
    test itself, zero candidates, every time. Confirmed live: this is
    exactly what happened before this fix (the earlier double-`-q` fix
    alone wasn't enough for the `fix` CLI path specifically). Pytest
    unions multiple positional paths, so `. ` alongside whatever test_cmd
    already specifies collects the WHOLE project regardless of how
    test_cmd itself is scoped, without needing to know or parse the
    project's actual test directory name."""
    cmd = f"{test_cmd} --collect-only ."
    res = run_test(cmd, image, project_dir, timeout=timeout)
    return [m.group(1) for line in res.full_output.splitlines()
            if (m := _NODE_ID_RE.match(line.strip()))]


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _rescope_test_cmd(test_cmd: str, test_id: str) -> str:
    """Drop any test file/node-id positional arg already baked into
    `test_cmd` and substitute `test_id` in its place.

    Confirmed live on astroid#769 (2026-08-30): `fix.py`'s `make_test_cmd`
    bakes the regression test's own path in as a positional arg (e.g.
    `... pytest tests/test_forge_769.py -q ...`). Naively appending a
    DIFFERENT test_id to that string doesn't replace the baked-in path —
    pytest unions multiple positional paths — so every sampled "passing"
    test actually ran ALONGSIDE the (still-failing) regression test in
    the same subprocess, and `--cov` measured both. Since the failing
    test's own lines were then co-executed by literally every "passing"
    sample by construction, `ep` saturated to the full sample count for
    exactly the lines that should have been most discriminating,
    reproducing the identical flat-tie symptom this whole module exists
    to fix — just one level removed from the original file-granularity
    bug. Distinguishing a positional path/node-id from a flag or a
    flag's value: a token that doesn't start with `-` and either
    contains `::` or ends in `.py` is a test path; everything else
    (interpreter, `-m`, `pytest`, `-q`, `--tb=short`, `-p`,
    `no:cacheprovider`, ...) is preserved as-is."""
    tokens = shlex.split(test_cmd)
    kept = [t for t in tokens
            if t.startswith("-") or not ("::" in t or t.endswith(".py"))]
    return " ".join(shlex.quote(t) for t in kept) + " " + shlex.quote(test_id)


def _run_one_with_coverage(project_dir: Path, test_cmd: str, image, test_id: str,
                           out_json: Path, timeout: int) -> tuple[bool, dict[str, set[int]]]:
    """Run ONE test node id under pytest-cov. Returns (passed,
    project-relative file -> executed 1-based line numbers) — empty dict
    on any failure to run or parse the coverage report, never raises.

    Line numbers, not just file presence: see module docstring — file-
    level "was this file touched at all" can't separate module-level
    import-time execution (every test) from function-body runtime
    execution (only tests that exercise that path), which is exactly
    what collapsed every candidate file to the same suspiciousness score
    on astroid#769. `executed_lines` was always in coverage.py's JSON
    output; this just stops throwing it away."""
    out_json.unlink(missing_ok=True)
    scoped = _rescope_test_cmd(test_cmd, test_id)
    cmd = f"{scoped} --cov=. --cov-report=json:{shlex.quote(str(out_json))}"
    res = run_test(cmd, image, project_dir, timeout=timeout)
    # `res.ok` (exit_code==0) is NOT reliable here: `test_cmd` may already
    # bake in a different, unrelated test path (fix.py's make_test_cmd
    # scopes the whole repair session to the regression test being
    # worked on) which pytest UNIONS with test_id rather than replacing —
    # so a failing test baked into test_cmd fails the WHOLE run's exit
    # code even when test_id itself passed clean. Confirmed live on
    # astroid#769 (2026-08-30): every sampled "passing" test came back
    # exit_code=1 purely from the co-run failing regression test, so
    # every sample got discarded and every file tied at the same
    # score — no discrimination left in the signal. Check test_id's own
    # outcome in the output instead of trusting the aggregate exit code.
    clean = _ANSI_ESCAPE_RE.sub("", res.full_output)
    passed = res.exit_code in (0, 1) and f"FAILED {test_id}" not in clean
    if not out_json.exists():
        return passed, {}
    try:
        data = json.loads(out_json.read_text())
    finally:
        out_json.unlink(missing_ok=True)
    file_lines: dict[str, set[int]] = {}
    for raw, info in data.get("files", {}).items():
        p = raw.replace("\\", "/").removeprefix("./")
        try:
            p = str(Path(p).resolve().relative_to(project_dir.resolve()))
        except ValueError:
            pass  # already relative, or outside project_dir (vendored/stdlib) — keep as-is
        lines = set(info.get("executed_lines") or [])
        if lines:  # a file with zero executed lines carries no signal — drop it,
            file_lines[p] = lines  # matching the old set[str] contract's implicit "touched" meaning
    return passed, file_lines


def spectrum_localize(project_dir: Path, test_cmd: str, image: Optional[str],
                      failing_test: str, timeout: int = 60,
                      max_passing_samples: int = DEFAULT_PASSING_SAMPLES,
                      collect_timeout: int = 60) -> dict[str, SpectrumHit]:
    """Line-level Ochiai suspiciousness, rolled up to one SpectrumHit per
    file by max (see module docstring for why line-level + max, not
    file-level or sum/mean), using `failing_test` as the sole failing
    spectrum and up to `max_passing_samples` other tests (sampled via
    collection, kept only if they actually pass right now) as the
    comparison spectrum.

    Returns {} whenever the signal isn't available: non-pytest stack,
    coverage/pytest-cov not installed, the failing test isn't actually
    failing right now, or nothing could be sampled. Never raises — this
    is an optional, additive signal for `localize()`, same contract as
    `hybrid_search`."""
    project_dir = Path(project_dir)
    if "pytest" not in test_cmd:
        return {}
    workdir = project_dir / ".forge"
    workdir.mkdir(parents=True, exist_ok=True)

    fail_passed, fail_lines = _run_one_with_coverage(
        project_dir, test_cmd, image, failing_test, workdir / "_spectrum_fail.json", timeout)
    if fail_passed or not fail_lines:
        # Either the failing test isn't failing right now (state moved
        # under us — a round already landed a fix) or coverage collection
        # didn't work at all (no pytest-cov, misconfigured --cov target).
        return {}

    candidates = [t for t in _collect_test_ids(project_dir, test_cmd, image, collect_timeout)
                  if t != failing_test]
    if not candidates:
        return {}
    sample = random.sample(candidates, min(max_passing_samples, len(candidates)))

    ep: dict[tuple[str, int], int] = {}
    for i, test_id in enumerate(sample):
        ok, lines_by_file = _run_one_with_coverage(
            project_dir, test_cmd, image, test_id, workdir / f"_spectrum_pass_{i}.json", timeout)
        if not ok:
            continue  # not actually passing right now — discard, don't count either way
        for f, lines in lines_by_file.items():
            for ln in lines:
                key = (f, ln)
                ep[key] = ep.get(key, 0) + 1

    # ef=1, nf=0 for every LINE the failing test touched (single failing
    # spectrum) -> susp(line) = 1 / sqrt(1 + ep(line)). Roll up to one
    # SpectrumHit per file by max over that file's own touched lines —
    # proven the only sound rollup (module docstring): invariant to file
    # size, unlike sum (can invert the ranking) or mean (margin shrinks
    # as file size grows).
    hits: dict[str, SpectrumHit] = {}
    for f, lines in fail_lines.items():
        best_score, best_line, best_ep = -1.0, 0, 0
        for ln in lines:
            e = ep.get((f, ln), 0)
            score = 1.0 / math.sqrt(1 + e)
            if score > best_score:
                best_score, best_line, best_ep = score, ln, e
        hits[f] = SpectrumHit(score=best_score, line=best_line, ep=best_ep)
    return hits
