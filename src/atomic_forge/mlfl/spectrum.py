"""
spectrum.py -- Language-agnostic line-level Ochiai spectrum-based fault localization.

This module contains ONLY the mathematical core:
  - Line-level Ochiai computation from any {test_id: {file: set_of_lines}} data
  - File-level Ochiai (kept for comparison/regression)
  - Formatting utilities

All language-specific coverage collection (pytest, jest, go test, cargo,
mvn) lives in the backends/ subdirectory.

Design contract (the degrade-to-{} philosophy):
  - Every public function returns a dict on success or {} on degradation.
  - Never fabricates a confident-looking score when data is insufficient.
  - Explicit skip conditions: no coverage data, no failing tests,
    flaky suite, etc.

Why line-level instead of file-level:
  File-level Ochiai with nf=1 degenerates: every file covered by the failing
  test gets the same score 1/sqrt(1+ep). If ep is uniform across files (common
  when the failing test is a focused unit test), you get an N-way tie.
  Line-level granularity breaks this because different lines within those files
  have different passing-test coverage profiles.

Ochiai formula:
  Ochiai(line) = ef / sqrt( (ef + ep) * nf )

  where:
    ef = number of FAILING tests that execute this line
    ep = number of PASSING test batches that execute this line
    nf = total number of FAILING tests
    np = total number of PASSING test batches
    N  = nf + np
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NewType


# ── Types ──────────────────────────────────────────────────────────────────

TestID = NewType("TestID", str)


@dataclass(frozen=True)
class SpectrumResult:
    """Per-line Ochiai score with full evidence."""
    file_path: str
    line: int
    function_name: str | None
    score: float
    ef: int
    ep: int
    nf: int
    np: int
    N: int

    def evidence_summary(self) -> str:
        fn = f" in {self.function_name}" if self.function_name else ""
        return (
            f"{self.file_path}:{self.line}{fn} — "
            f"Ochiai={self.score:.4f} "
            f"(ef={self.ef}, ep={self.ep}, nf={self.nf}, np={self.np}, N={self.N})"
        )


# ── Core Ochiai computation (language-agnostic) ───────────────────────────


def compute_from_per_test_coverage(
    per_test_coverage: dict[str, dict[str, set[int]]],
    failing_test_ids: list[str],
    passing_test_ids: list[str],
    project_root: str,
    *,
    func_name_map: dict[tuple[str, int], str | None] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Compute line-level Ochiai from pre-collected per-test coverage.

    This is the core algorithm. Works with ANY coverage data source
    regardless of programming language.

    Args:
        per_test_coverage: {test_id_or_batch_id: {file_path: set_of_line_numbers}}
        failing_test_ids: test IDs that failed
        passing_test_ids: test IDs (or batch IDs) that passed
        project_root: absolute path (used for display only)
        func_name_map: optional (file, line) -> function_name mapping

    Returns:
        On success: {
            "ranked_candidates": [SpectrumResult, ...],
            "score_spread": {"min", "max", "unique_scores", "total_candidates"},
            "nf", "np", "N",
        }
        On degradation: {}
    """
    failing_set = set(failing_test_ids)
    passing_set = set(passing_test_ids)
    nf = len(failing_set)
    np = len(passing_set)
    N = nf + np

    if nf == 0:
        if verbose:
            print("[spectrum] SKIP: nf=0")
        return {}
    if np == 0:
        if verbose:
            print("[spectrum] SKIP: np=0")
        return {}

    # Build line matrix: key -> {ef, ep, file, line}
    line_matrix: dict[str, dict[str, int]] = {}

    for test_id, file_lines in per_test_coverage.items():
        is_failing = test_id in failing_set
        for file_path, lines in file_lines.items():
            for line_no in lines:
                key = f"{file_path}:{line_no}"
                if key not in line_matrix:
                    line_matrix[key] = {"ef": 0, "ep": 0, "file": file_path, "line": line_no}
                if is_failing:
                    line_matrix[key]["ef"] += 1
                else:
                    line_matrix[key]["ep"] += 1

    if not line_matrix:
        if verbose:
            print("[spectrum] Empty line matrix")
        return {}

    # Use provided function name map or empty
    if func_name_map is None:
        func_name_map = {}

    # Compute Ochiai per line
    results: list[SpectrumResult] = []
    for key, counts in line_matrix.items():
        ef = counts["ef"]
        ep = counts["ep"]
        if ef == 0:
            continue
        denominator = (ef + ep) * nf
        if denominator == 0:
            continue
        score = ef / (denominator ** 0.5)

        func_name = func_name_map.get((counts["file"], counts["line"]))
        results.append(SpectrumResult(
            file_path=counts["file"],
            line=counts["line"],
            function_name=func_name,
            score=score,
            ef=ef, ep=ep, nf=nf, np=np, N=N,
        ))

    if not results:
        if verbose:
            print("[spectrum] No lines with ef>0")
        return {}

    # Sort: desc by score, then desc by ef, then asc by ep
    results.sort(key=lambda r: (-r.score, -r.ef, r.ep))

    scores = [r.score for r in results]
    unique_scores = set(round(s, 10) for s in scores)

    return {
        "ranked_candidates": results,
        "score_spread": {
            "min": min(scores),
            "max": max(scores),
            "unique_scores": len(unique_scores),
            "total_candidates": len(results),
        },
        "nf": nf, "np": np, "N": N,
    }


def compute_line_ochiai(
    coverage_data: dict[str, Any],
    *,
    func_name_map: dict[tuple[str, int], str | None] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Compute line-level Ochiai from collected coverage data.

    Convenience wrapper around compute_from_per_test_coverage.
    Accepts the dict produced by any backend's collect() method.

    Returns:
        On success: {ranked_candidates, score_spread, nf, np, N}
        On degradation: {}
    """
    if not coverage_data or "per_test_coverage" not in coverage_data:
        return {}

    return compute_from_per_test_coverage(
        per_test_coverage=coverage_data["per_test_coverage"],
        failing_test_ids=coverage_data["failing_test_ids"],
        passing_test_ids=coverage_data["passing_test_ids"],
        project_root=coverage_data.get("project_root", ""),
        func_name_map=func_name_map,
        verbose=verbose,
    )


def compute_file_ochiai(
    coverage_data: dict[str, Any],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """File-level Ochiai (kept for comparison — produces ties with nf=1)."""
    if not coverage_data or "per_test_coverage" not in coverage_data:
        return {}

    failing_set = set(coverage_data["failing_test_ids"])
    passing_set = set(coverage_data["passing_test_ids"])
    nf = len(failing_set)
    np = len(passing_set)
    N = nf + np

    if nf == 0 or np == 0:
        return {}

    file_matrix: dict[str, dict[str, int]] = {}
    for test_id, file_lines in coverage_data["per_test_coverage"].items():
        is_failing = test_id in failing_set
        for file_path in file_lines:
            if file_path not in file_matrix:
                file_matrix[file_path] = {"ef": 0, "ep": 0}
            if is_failing:
                file_matrix[file_path]["ef"] += 1
            else:
                file_matrix[file_path]["ep"] += 1

    results: list[SpectrumResult] = []
    for file_path, counts in file_matrix.items():
        ef, ep = counts["ef"], counts["ep"]
        if ef == 0:
            continue
        denom = (ef + ep) * nf
        if denom == 0:
            continue
        score = ef / (denom ** 0.5)
        results.append(SpectrumResult(
            file_path=file_path, line=0, function_name=None,
            score=score, ef=ef, ep=ep, nf=nf, np=np, N=N,
        ))

    results.sort(key=lambda r: (-r.score, -r.ef, r.ep))
    scores = [r.score for r in results]
    unique_scores = set(round(s, 10) for s in scores)

    return {
        "ranked_candidates": results,
        "score_spread": {
            "min": min(scores) if scores else 0.0,
            "max": max(scores) if scores else 0.0,
            "unique_scores": len(unique_scores),
            "total_candidates": len(results),
        },
        "nf": nf, "np": np, "N": N,
    }


# ── Formatting ─────────────────────────────────────────────────────────────


def format_ranked_results(spectrum_output: dict[str, Any], top_k: int = 20) -> str:
    """Format ranked spectrum results as a readable table."""
    if not spectrum_output or "ranked_candidates" not in spectrum_output:
        return ""

    candidates = spectrum_output["ranked_candidates"][:top_k]
    spread = spectrum_output["score_spread"]

    lines = [
        "Spectrum Fault Localization — Line-Level Ochiai",
        f"{'='*80}",
        f"nf={spectrum_output['nf']}, np={spectrum_output['np']}, "
        f"N={spectrum_output['N']}, candidates={spread['total_candidates']}, "
        f"unique_scores={spread['unique_scores']}",
        f"Score range: [{spread['min']:.4f}, {spread['max']:.4f}]",
        f"{'-'*80}",
        f"{'Rank':<6}{'Score':<10}{'ef':<5}{'ep':<6}{'File:Line':<45}{'Function'}",
        f"{'-'*80}",
    ]

    for rank, c in enumerate(candidates, 1):
        fn = (c.function_name or "-")[:20]
        lines.append(
            f"{rank:<6}{c.score:<10.4f}{c.ef:<5}{c.ep:<6}{c.file_path}:{c.line:<40}{fn}"
        )

    return "\n".join(lines)
