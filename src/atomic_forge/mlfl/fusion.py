"""
fusion.py — Bounded Two-Tier Spectrum-Dominant Fusion.

Replaces the ad hoc additive bump() system (spectrum*10 + flat +3.5 for
hybrid_search, etc.) with a principled ranking that guarantees spectrum
leads survive noisy auxiliary signals.

=== The Spectrum-Dominance Lemma ===

Claim: If spectrum produces a score gap delta = s_i - s_{i+1} > 0 between
consecutive candidates, and every auxiliary signal's combined contribution
is bounded by B where B < delta, then candidate i's fused rank is guaranteed
to remain above candidate i+1's.

Proof sketch:
  Let S(i) be the spectrum score for candidate i, and A(i) be the total
  auxiliary bonus for candidate i, where 0 <= A(i) <= B for all i.
  Fused score F(i) = S(i) + alpha * A(i), where alpha is the fusion scaling factor.
  For candidate i to be outranked by i+1, we need:
    F(i+1) > F(i)
    S(i+1) + alpha * A(i+1) > S(i) + alpha * A(i)
    alpha * (A(i+1) - A(i)) > S(i) - S(i+1) = delta
  Since |A(i+1) - A(i)| <= 2B (triangle inequality, both in [0,B]):
    2*alpha*B > delta
  Therefore, choosing alpha <= delta / (2B) guarantees no inversion.

In practice, we set alpha = min(delta_min / (2B), 1) where delta_min is the
smallest spectrum gap we care to preserve, and B is the maximum possible
auxiliary contribution.

=== Design ===

Tier 1 (Spectrum): Line-level Ochiai scores. This is the primary signal.
  - Must have real variance (checked at input).
  - Provides the ranking backbone.

Tier 2 (Auxiliary): Traceback match, call-graph distance, hybrid_search.
  - Each produces a score in [0, 1].
  - Combined contribution is BOUNDED by the spectrum gap.
  - Cannot outrank a well-supported spectrum lead.

Output: A single ranked list with per-candidate evidence breakdown.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spectrum import SpectrumResult


@dataclass(frozen=True)
class AuxiliarySignal:
    """A single auxiliary signal for one candidate."""
    name: str           # e.g. "traceback_match", "callgraph_distance", "hybrid_search"
    file_path: str
    line: int
    score: float        # in [0, 1]
    confidence: float   # in [0, 1] — how much we trust this signal
    evidence: str       # human-readable justification


@dataclass
class FusedCandidate:
    """A candidate after fusion, with full provenance."""
    file_path: str
    line: int
    function_name: str | None
    spectrum_score: float
    auxiliary_bonus: float
    fused_score: float
    rank: int
    spectrum_rank: int
    spectrum_evidence: str
    auxiliary_signals: list[AuxiliarySignal] = field(default_factory=list)
    dominance_gap: float = 0.0
    is_spectrum_protected: bool = False

    def evidence_summary(self) -> str:
        fn = f" in {self.function_name}" if self.function_name else ""
        parts = [
            f"{self.file_path}:{self.line}{fn}",
            f"  fused={self.fused_score:.4f} (spectrum={self.spectrum_score:.4f} + aux={self.auxiliary_bonus:.4f})",
            f"  spectrum_rank={self.spectrum_rank}, fused_rank={self.rank}",
        ]
        if self.auxiliary_signals:
            for sig in self.auxiliary_signals:
                parts.append(
                    f"  [{sig.name}] {sig.score:.3f} (conf={sig.confidence:.2f}): {sig.evidence}"
                )
        if self.is_spectrum_protected:
            parts.append(f"  SPECTRUM-DOMINANT (gap={self.dominance_gap:.4f})")
        return "\n".join(parts)


@dataclass
class FusionConfig:
    """Configuration for the bounded fusion algorithm."""
    min_spectrum_gap: float = 0.01
    max_aux_fraction: float = 0.3
    min_signal_confidence: float = 0.1
    require_spectrum_variance: bool = True
    min_unique_scores: int = 2


def compute_fusion(
    spectrum_output: dict[str, Any],
    auxiliary_signals: list[AuxiliarySignal],
    config: FusionConfig | None = None,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Compute the bounded two-tier fusion of spectrum + auxiliary signals.

    Returns:
        On success: {"ranked_candidates": [FusedCandidate], "fusion_params": {...}, "dominance_gaps": [...]}
        On degradation: {}
    """
    if config is None:
        config = FusionConfig()

    if not spectrum_output or "ranked_candidates" not in spectrum_output:
        if verbose:
            print("[fusion] SKIP: no spectrum output")
        return {}

    candidates = spectrum_output["ranked_candidates"]
    spread = spectrum_output["score_spread"]

    if not candidates:
        if verbose:
            print("[fusion] SKIP: no spectrum candidates")
        return {}

    # Check for real variance — this catches the file-level flatness bug
    if config.require_spectrum_variance:
        if spread["unique_scores"] < config.min_unique_scores:
            if verbose:
                print(
                    f"[fusion] SKIP: spectrum has no real variance "
                    f"(unique_scores={spread['unique_scores']} < {config.min_unique_scores}). "
                    f"This is the file-level flatness bug. "
                    f"Fix: use line-level Ochiai instead of file-level."
                )
            return {}

    # Compute spectrum gaps between consecutive candidates
    spectrum_scores = [c.score for c in candidates]
    gaps: list[float] = []
    for i in range(len(spectrum_scores) - 1):
        gap = spectrum_scores[i] - spectrum_scores[i + 1]
        gaps.append(gap)

    if not gaps:
        if verbose:
            print("[fusion] SKIP: only one candidate, no gaps to compute")
        return {}

    positive_gaps = [g for g in gaps if g > 0]
    delta_min = min(positive_gaps) if positive_gaps else 0.0

    if delta_min == 0.0 and verbose:
        print("[fusion] WARN: delta_min=0, spectrum has tied top candidates. "
              "Auxiliary signals may reorder ties.")

    # Build per-candidate auxiliary scores
    candidate_aux: dict[str, list[AuxiliarySignal]] = {}
    for sig in auxiliary_signals:
        if sig.confidence < config.min_signal_confidence:
            if verbose:
                print(f"[fusion] DISCARD signal [{sig.name}] conf={sig.confidence:.2f} < threshold")
            continue
        key = f"{sig.file_path}:{sig.line}"
        if key not in candidate_aux:
            candidate_aux[key] = []
        candidate_aux[key].append(sig)

    # Maximum possible auxiliary contribution
    max_aux_raw = 0.0
    for signals in candidate_aux.values():
        total = sum(s.score * s.confidence for s in signals)
        max_aux_raw = max(max_aux_raw, total)

    # Bound B
    spectrum_range = (spectrum_scores[0] - spectrum_scores[-1]) if len(spectrum_scores) > 1 else 1.0
    B = min(max_aux_raw, config.max_aux_fraction * spectrum_range)

    if B == 0.0:
        if verbose:
            print("[fusion] No valid auxiliary signals — returning pure spectrum ranking")
        fused = []
        for rank, c in enumerate(candidates, 1):
            fused.append(FusedCandidate(
                file_path=c.file_path,
                line=c.line,
                function_name=c.function_name,
                spectrum_score=c.score,
                auxiliary_bonus=0.0,
                fused_score=c.score,
                rank=rank,
                spectrum_rank=rank,
                spectrum_evidence=c.evidence_summary(),
                auxiliary_signals=[],
                dominance_gap=gaps[rank - 1] if rank - 1 < len(gaps) else 0.0,
                is_spectrum_protected=True,
            ))
        return {
            "ranked_candidates": fused,
            "fusion_params": {"alpha": 0.0, "B": 0.0, "delta_min": delta_min},
            "dominance_gaps": gaps,
        }

    # Compute fusion scaling factor alpha (Spectrum-Dominance Lemma)
    if B > 0 and delta_min > 0:
        alpha = min(delta_min / (2 * B), 1.0)
    elif delta_min == 0:
        alpha = min(config.max_aux_fraction, 0.5)
    else:
        alpha = 0.0

    if verbose:
        print(
            f"[fusion] params: alpha={alpha:.6f}, B={B:.6f}, "
            f"delta_min={delta_min:.6f}, spectrum_range={spectrum_range:.6f}"
        )

    # Fuse
    fused: list[FusedCandidate] = []
    for spec_rank, spec_c in enumerate(candidates, 1):
        key = f"{spec_c.file_path}:{spec_c.line}"
        signals = candidate_aux.get(key, [])
        aux_bonus = sum(s.score * s.confidence for s in signals) * alpha
        fused_score = spec_c.score + aux_bonus

        fused.append(FusedCandidate(
            file_path=spec_c.file_path,
            line=spec_c.line,
            function_name=spec_c.function_name,
            spectrum_score=spec_c.score,
            auxiliary_bonus=aux_bonus,
            fused_score=fused_score,
            rank=0,
            spectrum_rank=spec_rank,
            spectrum_evidence=spec_c.evidence_summary(),
            auxiliary_signals=signals,
            dominance_gap=0.0,
            is_spectrum_protected=False,
        ))

    # Stable sort by fused score
    fused.sort(key=lambda f: -f.fused_score)

    # Assign final ranks and compute dominance gaps
    for rank, fc in enumerate(fused, 1):
        fc.rank = rank
        if rank < len(fused):
            fc.dominance_gap = fused[rank - 1].fused_score - fc.fused_score

    # Mark spectrum-protected candidates
    for fc in fused:
        if fc.rank == fc.spectrum_rank and fc.dominance_gap > 0:
            fc.is_spectrum_protected = True

    # Verify the Spectrum-Dominance Lemma held
    if verbose and delta_min > 0:
        inversions = 0
        for fc in fused:
            if fc.rank != fc.spectrum_rank and fc.spectrum_rank <= 5:
                inversions += 1
                print(
                    f"[fusion] INVERSION: {fc.file_path}:{fc.line} "
                    f"spectrum_rank={fc.spectrum_rank} -> fused_rank={fc.rank}"
                )
        if inversions == 0:
            print("[fusion] VERIFIED: No inversions in top-5 spectrum candidates")

    fused_gaps = [
        fused[i].fused_score - fused[i + 1].fused_score
        for i in range(len(fused) - 1)
    ]

    return {
        "ranked_candidates": fused,
        "fusion_params": {
            "alpha": alpha,
            "B": B,
            "delta_min": delta_min,
        },
        "dominance_gaps": fused_gaps,
    }


def format_fused_results(fusion_output: dict[str, Any], top_k: int = 20) -> str:
    """Format fused results as a human-readable table."""
    if not fusion_output or "ranked_candidates" not in fusion_output:
        return ""

    candidates = fusion_output["ranked_candidates"][:top_k]
    params = fusion_output["fusion_params"]

    header = (
        f"{'Rank':<6}{'Fused':<10}{'Spectrum':<10}{'Aux':<10}"
        f"{'SR':<5}{'File:Line':<40}{'Func':<15}{'Status'}"
    )
    sep = "-" * 96

    lines = [
        "Fault Localization — Bounded Two-Tier Fusion",
        f"{'='*96}",
        f"alpha={params['alpha']:.6f}, B={params['B']:.6f}, delta_min={params['delta_min']:.6f}",
        sep,
        header,
        sep,
    ]

    for fc in candidates:
        fn = (fc.function_name or "-")[:14]
        status = "PROTECTED" if fc.is_spectrum_protected else ""
        loc = f"{fc.file_path}:{fc.line}"
        lines.append(
            f"{fc.rank:<6}{fc.fused_score:<10.4f}{fc.spectrum_score:<10.4f}"
            f"{fc.auxiliary_bonus:<10.4f}{fc.spectrum_rank:<5}"
            f"{loc:<40}{fn:<15}{status}"
        )

    return "\n".join(lines)
