"""
fault_localization.py -- Language-agnostic fault localization.

Usage:
  # Auto-detect language and localize:
  python fault_localization.py /path/to/project

  # Specify language:
  python fault_localization.py /path/to/project --language rust

  # Specify failing test:
  python fault_localization.py /path/to/project --failing-test tests/test_foo.py::test_bar

  # JSON output:
  python fault_localization.py /path/to/project --json

  # Pre-collected mode (any language):
  python fault_localization.py --mode pre-collected --json < coverage.json

Supported languages: python, javascript, go, rust, java
(plus any language via --mode pre-collected with manual coverage data)

Design: every public function returns a dict on success or {} on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# Ensure the package root and backends are importable
_pkg_root = os.path.dirname(os.path.abspath(__file__))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

import spectrum
import fusion
import backends


SUPPORTED_LANGUAGES = ["python", "javascript", "go", "rust", "java"]


def localize(
    project_root: str,
    failing_test_ids: list[str] | None = None,
    passing_test_ids: list[str] | None = None,
    per_test_coverage: dict[str, dict[str, set[int]]] | None = None,
    auxiliary_signals: list[dict[str, Any]] | None = None,
    language: str | None = None,
    *,
    use_fusion: bool = True,
    top_k: int = 20,
    batch_by_file: bool = True,
    timeout_per_batch: int = 120,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run fault localization on any project.

    Modes:
      A) Automatic: provide project_root (+ optional language).
         Auto-detects language, discovers tests, collects coverage,
         computes line-level Ochiai, optionally fuses with auxiliary.
      B) Pre-collected: provide per_test_coverage + failing/passing IDs.
         Skips test discovery and coverage collection. Works with
         any language — just provide the coverage dict.

    Supported languages (Mode A): python, javascript, go, rust, java

    Args:
        project_root: Path to the project root.
        failing_test_ids: [Mode B] Test IDs that fail.
        passing_test_ids: [Mode B] Test IDs that pass.
        per_test_coverage: [Mode B] {test_id: {file: set_of_lines}}.
        auxiliary_signals: Optional auxiliary signals for fusion.
            Each: {name, file_path, line, score, confidence, evidence}.
        language: Override auto-detection. One of: python, javascript,
            go, rust, java. None = auto-detect.
        use_fusion: Apply bounded fusion (requires spectrum variance).
        top_k: Number of top candidates in output.
        batch_by_file: [Mode A] Batch passing tests by file (faster).
        timeout_per_batch: Max seconds per test batch.
        verbose: Print diagnostics.

    Returns:
        Success: {ranked_candidates, spectrum_summary, evidence_for_llm, ...}
        Degradation: {ranked_candidates: [], degradation_reason: str}
    """
    project_root = os.path.abspath(project_root)

    func_name_map: dict[tuple[str, int], str | None] | None = None

    # ── Mode A: Automatic (backend-driven) ─────────────────────────
    if per_test_coverage is None:
        # Get the appropriate backend
        backend = backends.get_backend(language, project_root)

        if backend is None:
            detected = backends.detect_language(project_root)
            reason = (
                f"No coverage backend found for {project_root}. "
                f"Detected language: {detected or 'unknown'}. "
                f"Supported: {SUPPORTED_LANGUAGES}. "
                f"Use --mode pre-collected to provide manual coverage data."
            )
            if verbose:
                print(f"[localize] DEGRADE: {reason}")
            return {"ranked_candidates": [], "degradation_reason": reason}

        if verbose:
            print(f"[localize] Mode A: {backend.language} backend from {project_root}")

        # Build function name map
        try:
            func_name_map = backend.build_function_name_map()
            if verbose and func_name_map:
                print(f"[localize] Mapped {len(func_name_map)} lines to function names")
        except Exception as exc:
            if verbose:
                print(f"[localize] Function name mapping failed: {exc}")

        # Collect coverage via backend
        cov_data = backend.collect(
            failing_test_ids=failing_test_ids,
            timeout_per_test=timeout_per_batch,
            batch_by_file=batch_by_file,
            verbose=verbose,
        )

        if not cov_data:
            reason = (
                f"Coverage collection degraded for {project_root} "
                f"({backend.language}). Causes: no tests, no failures, "
                f"all fail, flaky suite, or coverage tool unavailable."
            )
            if verbose:
                print(f"[localize] DEGRADE: {reason}")
            return {"ranked_candidates": [], "degradation_reason": reason}

        spectrum_output = spectrum.compute_line_ochiai(
            cov_data, func_name_map=func_name_map, verbose=verbose,
        )

    # ── Mode B: Pre-collected (language-agnostic) ────────────────────
    else:
        if verbose:
            print(f"[localize] Mode B: pre-collected ({len(per_test_coverage)} entries)")

        if not failing_test_ids or not passing_test_ids:
            return {
                "ranked_candidates": [],
                "degradation_reason": "Mode B requires both failing_test_ids and passing_test_ids",
            }

        spectrum_output = spectrum.compute_from_per_test_coverage(
            per_test_coverage=per_test_coverage,
            failing_test_ids=failing_test_ids,
            passing_test_ids=passing_test_ids,
            project_root=project_root,
            func_name_map=func_name_map,
            verbose=verbose,
        )

    if not spectrum_output:
        reason = "Spectrum computation returned empty (no lines with ef>0, or nf/np=0)."
        if verbose:
            print(f"[localize] DEGRADE: {reason}")
        return {"ranked_candidates": [], "degradation_reason": reason}

    # ── Optional fusion ────────────────────────────────────────────
    fusion_output = None
    if use_fusion and auxiliary_signals:
        aux = [
            fusion.AuxiliarySignal(
                name=s["name"], file_path=s["file_path"], line=s["line"],
                score=float(s["score"]), confidence=float(s["confidence"]),
                evidence=s["evidence"],
            )
            for s in auxiliary_signals
        ]
        fusion_output = fusion.compute_fusion(spectrum_output, aux, verbose=verbose)

    # ── Build output ───────────────────────────────────────────────
    if fusion_output and fusion_output.get("ranked_candidates"):
        evidence = fusion.format_fused_results(fusion_output, top_k=top_k)
        ranked = fusion_output["ranked_candidates"][:top_k]
    else:
        evidence = spectrum.format_ranked_results(spectrum_output, top_k=top_k)
        ranked = spectrum_output["ranked_candidates"][:top_k]

    # Append detailed per-candidate evidence
    evidence += "\n\nDetailed evidence:\n"
    for i, c in enumerate(ranked, 1):
        evidence += f"\n#{i}: {c.evidence_summary()}\n"

    result: dict[str, Any] = {
        "ranked_candidates": [
            {
                "file_path": c.file_path,
                "line": c.line,
                "function_name": c.function_name,
                "score": getattr(c, 'fused_score', c.score),
                "spectrum_score": c.score,
                "ef": c.ef,
                "ep": c.ep,
                "spectrum_rank": getattr(c, 'spectrum_rank', i + 1),
            }
            for i, c in enumerate(ranked)
        ],
        "spectrum_summary": {
            "nf": spectrum_output["nf"],
            "np": spectrum_output["np"],
            "N": spectrum_output["N"],
            "score_spread": spectrum_output["score_spread"],
        },
        "evidence_for_llm": evidence,
    }

    if fusion_output:
        result["fusion_summary"] = {
            "alpha": fusion_output["fusion_params"]["alpha"],
            "B": fusion_output["fusion_params"]["B"],
            "delta_min": fusion_output["fusion_params"]["delta_min"],
        }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Language-agnostic spectrum-based fault localization. "
            f"Supported: {', '.join(SUPPORTED_LANGUAGES)}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect language:
  python fault_localization.py /path/to/project

  # Specify language explicitly:
  python fault_localization.py /path/to/project --language rust
  python fault_localization.py /path/to/project --language go
  python fault_localization.py /path/to/project --language javascript
  python fault_localization.py /path/to/project --language java

  # Specify failing test:
  python fault_localization.py /path/to/project --failing-test 'tests/test_foo.py::test_bar'

  # JSON output:
  python fault_localization.py /path/to/project --top-k 10 --json

  # Pre-collected mode (any language):
  echo '{"per_test_coverage": {...}, "failing_test_ids": [...], "passing_test_ids": [...]}' | \
    python fault_localization.py --mode pre-collected /path/to/project --json

  # Verbose diagnostics:
  python fault_localization.py /path/to/project --verbose
""",
    )

    parser.add_argument("project_root", help="Path to the project root")
    parser.add_argument(
        "--language", choices=SUPPORTED_LANGUAGES, default=None,
        help=(
            "Override language auto-detection. "
            f"Choices: {', '.join(SUPPORTED_LANGUAGES)}"
        ),
    )
    parser.add_argument(
        "--mode", choices=["auto", "pre-collected"], default="auto",
        help="'auto' = discover and run tests; 'pre-collected' = read coverage from stdin (JSON)",
    )
    parser.add_argument(
        "--failing-test", action="append", dest="failing_tests", default=[],
        help="Test ID that fails (repeatable)",
    )
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--no-fusion", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--per-test", action="store_true",
        help="Run each test individually (slower, more precise)",
    )
    parser.add_argument(
        "--timeout", type=int, default=120,
        help="Timeout per test batch in seconds",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if not os.path.isdir(args.project_root):
        print(f"ERROR: not a directory: {args.project_root}", file=sys.stderr)
        sys.exit(1)

    # Pre-collected mode: read coverage from stdin
    if args.mode == "pre-collected":
        try:
            pre_data = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"ERROR: invalid JSON on stdin: {exc}", file=sys.stderr)
            sys.exit(1)

        result = localize(
            project_root=args.project_root,
            failing_test_ids=pre_data.get("failing_test_ids"),
            passing_test_ids=pre_data.get("passing_test_ids"),
            per_test_coverage=pre_data.get("per_test_coverage"),
            language=args.language,
            use_fusion=not args.no_fusion,
            top_k=args.top_k,
            verbose=args.verbose,
        )
    else:
        result = localize(
            project_root=args.project_root,
            failing_test_ids=args.failing_tests or None,
            language=args.language,
            use_fusion=not args.no_fusion,
            top_k=args.top_k,
            batch_by_file=not args.per_test,
            timeout_per_batch=args.timeout,
            verbose=args.verbose,
        )

    if not result.get("ranked_candidates"):
        reason = result.get("degradation_reason", "unknown")
        if args.as_json:
            print(json.dumps({"error": reason}, indent=2))
        else:
            print(f"Degraded: {reason}")
        sys.exit(0)

    if args.as_json:
        serializable = {
            "ranked_candidates": result["ranked_candidates"],
            "spectrum_summary": result["spectrum_summary"],
        }
        if "fusion_summary" in result:
            serializable["fusion_summary"] = result["fusion_summary"]
        print(json.dumps(serializable, indent=2))
    else:
        print(result["evidence_for_llm"])


if __name__ == "__main__":
    main()
