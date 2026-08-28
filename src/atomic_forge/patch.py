"""
The one canonical SEARCH/REPLACE parser.

Before this module existed, forge had the same Aider-style SEARCH/REPLACE
idea hand-rolled THREE separate times — `repair.py` (`SR_BLOCK_RE`,
anchored regex), `generate_agent.py` (`_HUNK_RE`, non-anchored), and
`repair_agent.py` (imported repair.py's copy, then fell back to a bespoke
fenced-code regex of its own). The exact same defect (a hardcoded 7-char
marker regex that real model output doesn't reliably match — a model turn
emitted 6 `<` characters instead of 7) was independently discovered and
independently fixed in two of the three copies on two different days
(`generate_agent.py` in abb4075, `repair.py` in 26fa1e3) because there was
no shared implementation to fix once. This module is that shared
implementation; the other three call into it now (see their own module
docstrings for the retirement note).

Public API:
    HUNK_RE                    the marker regex (module constant, for callers
                                that want to sniff "does this look like an
                                S/R patch" without a full parse)
    Hunk                        one parsed SEARCH/REPLACE block, in the order
                                the model emitted it
    HunkOutcome                 the result of trying to locate/apply one hunk
    PatchResult                 the result of applying a whole patch
    parse_hunks(text)           text -> list[Hunk], in LLM-output order
    looks_like_search_replace(text)
    normalize_for_match(text, strictness)
    validate_hunk_disjointness(outcomes)
    apply_hunks(content, hunks, *, allow_partial=False)
    apply_search_replace(content, llm_output, *, allow_partial=False)
                                 the convenience one-shot entry point every
                                 other module should call

Fixes baked in here (all confirmed-live bug classes):

1. Empty/whitespace-only SEARCH blocks are rejected BEFORE they ever reach
   a string search. `"".count(x)` and `content.replace("", replace, 1)`
   both silently "succeed" against an empty needle — the former by
   matching everywhere, the latter by inserting at position 0. Neither is
   the SEARCH/REPLACE contract; both would previously fall through to
   whatever ambient behavior `str.count`/`str.replace` happen to have for
   an empty string, which is corruption, not a real match.

2. Hunks are located against the ORIGINAL (pre-patch) content, all at
   once, before any text is rewritten — not applied one at a time in
   LLM-output order via sequential `str.replace` calls. The latter is the
   exact shape of a second real bug: hunk 2 (in output order) can
   accidentally match text that hunk 1's REPLACE just introduced, because
   each `str.replace` re-scans the mutated string from scratch. Since
   every hunk's position is resolved against the untouched original
   content up front, output order is irrelevant to what a hunk matches —
   the string surgery itself then proceeds highest-offset-first purely so
   earlier (lower-offset) spans stay valid while later ones are being
   rewritten; the result is exactly as if every hunk had been applied in
   file-position order.

3. `validate_hunk_disjointness()` is a preflight: if two hunks' matched
   regions overlap in the original content, the WHOLE patch is rejected
   (or, in `allow_partial=True` mode, just those conflicting hunks are)
   rather than letting the second hunk's replacement silently clobber
   text the first hunk's replacement already wrote (or vice versa,
   depending on iteration order — which is exactly the kind of
   order-dependent bug this module exists to make impossible).

4. `normalize_for_match()` gives the whitespace/CRLF fallback an explicit,
   documented contract instead of an ambiguous "try to be lenient" one:

       exact  ->  normalize_ws  ->  normalize_all  ->  strip_line_numbers

   `exact` is a literal substring match (matches the pre-existing
   behavior of all three retired implementations). If that finds zero
   matches, `normalize_ws` retries on a per-line basis after unifying
   line endings (CRLF/CR -> LF) and stripping trailing whitespace per
   line — the common case of a model emitting LF while the on-disk file
   has CRLF, or omitting trailing whitespace it didn't "see". If that
   ALSO finds zero matches, `normalize_all` retries once more, this time
   with each line fully trimmed and internal whitespace runs collapsed.
   If THAT still finds zero matches, `strip_line_numbers` retries once
   more, additionally stripping a leading `view_file`-tool-shaped line-
   number prefix (`"  123\t"`) from each line — confirmed live as a real
   failure mode even with an explicit system-prompt warning against it:
   a model copying a file-viewing tool's numbered output verbatim into a
   SEARCH block. This is the most permissive level, used only as the
   very last fallback.

   If a level finds more than one match, that is treated as AMBIGUOUS
   immediately — looser matching cannot resolve ambiguity, only worsen
   it, so there is no point escalating further.

   If strip_line_numbers ALSO finds nothing, the hunk is rejected with
   reason NO_MATCH. It is NEVER applied at position 0. This is written
   down explicitly (and asserted in tests/test_patch.py) because "insert
   at position 0 when nothing matches" is the literal shape of the
   original marker-length bug's failure mode, and a vaguely-specified
   fallback would silently reintroduce something equivalent to it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: Marker length is a RANGE, not a hardcoded 7 — confirmed live 2026-07-29:
#: a real model turn emitted 6 "<" characters instead of the documented 7,
#: and a strict `{7}` match silently failed to recognize the block at all.
#: This is the non-anchored variant (no `^`/`$`/MULTILINE) — the one
#: previously in generate_agent.py's `_HUNK_RE`, proven correct by that
#: incident; repair.py's independently-written anchored variant is the one
#: retired in favor of this shared copy.
HUNK_RE = re.compile(
    r"<{3,10}\s*SEARCH\n(.*?)\n={3,10}\n(.*?)\n>{3,10}\s*REPLACE",
    re.DOTALL,
)

_STRICTNESS_LEVELS = ("exact", "normalize_ws", "normalize_all", "strip_line_numbers")

#: A common `view_file`-tool per-line content format —
#: `f"{n:>5}\t{line}"`, a right-aligned line number then a literal tab.
#: Confirmed live: a model given a
#: real function-calling `view_file` result (not the old free-text
#: convention) STILL copied that numbering verbatim into a SEARCH block
#: despite an explicit system-prompt warning not to — prompting alone
#: isn't reliable for a weaker model, so this is the defensive fallback:
#: strip a leading line-number-and-tab from each SEARCH (and content)
#: line before the final, most-permissive comparison, rather than leaving
#: an otherwise-correct patch to fail NO_MATCH over a prefix that was
#: never part of the real file.
_LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\t")


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

@dataclass
class Hunk:
    """One parsed SEARCH/REPLACE block.

    `index` is the hunk's position in LLM-OUTPUT order (0-based) — kept
    only for messaging/debugging ("hunk 2: ..."). It is deliberately NOT
    used to decide application order; see `apply_hunks`'s docstring (fix
    #2 above).
    """
    index: int
    search: str
    replace: str


@dataclass
class HunkOutcome:
    """The result of trying to locate (and, later, apply) one hunk.

    `status` is one of:
        APPLIED            — found a unique match, `span` is set
        EMPTY_SEARCH        — SEARCH was empty/whitespace-only, rejected outright
        AMBIGUOUS            — matched more than once at some strictness level
        NO_MATCH             — no match at any strictness level (exact through
                                normalize_all) — the hunk is NEVER applied at
                                position 0 in this case
        DISJOINT_CONFLICT    — matched uniquely, but its span overlaps another
                                hunk's matched span in the same patch
    `span` is the (start, end) character offset in the ORIGINAL content —
    set only when status == "APPLIED" (before any disjointness check may
    still demote it to DISJOINT_CONFLICT, at which point span is cleared).
    `strictness` records which `normalize_for_match` level found the
    match ("exact" | "normalize_ws" | "normalize_all").
    """
    hunk: Hunk
    status: str
    reason: str
    span: Optional[Tuple[int, int]] = None
    strictness: str = "exact"


@dataclass
class PatchResult:
    """The result of applying a whole patch (one or more hunks).

    `success` is True when `new_content` is not None: with
    `allow_partial=False` (the default) that means every hunk applied;
    with `allow_partial=True` it means at least one hunk applied (the
    rest, if any, are in `rejected` with their own reasons).
    """
    success: bool
    new_content: Optional[str]
    applied: List[HunkOutcome] = field(default_factory=list)
    rejected: List[HunkOutcome] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def looks_like_search_replace(text: str) -> bool:
    """Cheap sniff — "does this text contain at least a SEARCH marker" —
    without doing a full parse. Used by callers that need to branch on
    intent (SEARCH/REPLACE vs full-file rewrite) before committing to
    apply_search_replace's stricter contract."""
    return bool(re.search(r"<{3,10}\s*SEARCH", text))


def parse_hunks(patch_text: str) -> List[Hunk]:
    """Extract every SEARCH/REPLACE block, in the order they appear in
    `patch_text` (LLM-output order). Does NOT validate SEARCH content —
    that happens in `apply_hunks`/`_locate_hunk`, so a caller inspecting
    hunks before applying them (e.g. for a UI preview) still sees every
    block the model emitted, including ones that will later be rejected."""
    return [Hunk(index=i, search=search, replace=replace)
            for i, (search, replace) in enumerate(HUNK_RE.findall(patch_text))]


# --------------------------------------------------------------------------
# matching / normalization
# --------------------------------------------------------------------------

def normalize_for_match(text: str, strictness: str = "exact") -> str:
    """Normalize `text` for fallback matching. See this module's docstring
    for the full fallback contract. `strictness`:

      "exact"              identity — no transformation.
      "normalize_ws"       unify line endings (CRLF/CR -> LF) and strip
                           trailing whitespace from each line.
      "normalize_all"      normalize_ws, plus strip leading/trailing
                           whitespace on each line and collapse internal
                           whitespace runs to a single space.
      "strip_line_numbers" normalize_all, plus strip a leading
                           `view_file`-shaped line-number prefix
                           (`_LINE_NUMBER_PREFIX_RE`) from each line —
                           the most permissive level, tried last.
    """
    if strictness not in _STRICTNESS_LEVELS:
        raise ValueError(f"unknown strictness level: {strictness!r} (expected one of {_STRICTNESS_LEVELS})")
    if strictness == "exact":
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if strictness == "normalize_ws":
        lines = [ln.rstrip() for ln in lines]
    elif strictness == "normalize_all":
        lines = [re.sub(r"\s+", " ", ln.strip()) for ln in lines]
    else:  # strip_line_numbers
        lines = [re.sub(r"\s+", " ", _LINE_NUMBER_PREFIX_RE.sub("", ln).strip()) for ln in lines]
    return "\n".join(lines)


def _match_exact(content: str, search: str) -> List[int]:
    """All character start-offsets where `search` occurs verbatim in
    `content` (a plain, non-overlapping substring scan)."""
    if not search:
        return []
    positions = []
    start = 0
    while True:
        idx = content.find(search, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _line_char_offsets(lines: List[str]) -> List[int]:
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1  # +1 for the '\n' joiner between lines
    return offsets


def _strip_trailing_cr(line: str) -> str:
    """Content is split on "\\n" only (see `_match_normalized_lines`), so a
    CRLF-terminated line still carries a trailing lone "\\r" here. Strip it
    before handing the line to `normalize_for_match` — that function's own
    CRLF unification assumes it receives a whole multi-line block and
    would otherwise turn one already-single line into two ("\\r" -> "\\n"
    -> a fresh split), corrupting the per-line comparison with a spurious
    extra empty line."""
    return line[:-1] if line.endswith("\r") else line


def _match_normalized_lines(content: str, search: str, strictness: str) -> List[Tuple[int, int]]:
    """Line-based match at a given normalize_for_match strictness level.
    Returns every (start, end) character span in the ORIGINAL `content`
    whose lines, once normalized, equal the normalized SEARCH lines."""
    content_lines = content.split("\n")
    search_lines = search.split("\n")
    norm_content = [normalize_for_match(_strip_trailing_cr(ln), strictness) for ln in content_lines]
    norm_search = [normalize_for_match(_strip_trailing_cr(ln), strictness) for ln in search_lines]
    n, m = len(norm_content), len(norm_search)
    if m == 0 or m > n:
        return []
    offsets = _line_char_offsets(content_lines)
    spans = []
    for i in range(n - m + 1):
        if norm_content[i:i + m] == norm_search:
            start = offsets[i]
            end = offsets[i + m - 1] + len(content_lines[i + m - 1])
            spans.append((start, end))
    return spans


def _locate_hunk(content: str, search: str) -> Tuple[Optional[Tuple[int, int]], str, str]:
    """Resolve one hunk's SEARCH text to a unique span in `content`, per
    the exact -> normalize_ws -> normalize_all fallback contract. Returns
    (span_or_None, status, strictness_used). Never returns a span at
    position 0 as a fallback for "nothing matched" — see this module's
    docstring, fix #4."""
    if not search.strip():
        return None, "EMPTY_SEARCH", "exact"

    exact_hits = _match_exact(content, search)
    if len(exact_hits) == 1:
        start = exact_hits[0]
        return (start, start + len(search)), "APPLIED", "exact"
    if len(exact_hits) > 1:
        return None, "AMBIGUOUS", "exact"

    for strictness in ("normalize_ws", "normalize_all", "strip_line_numbers"):
        spans = _match_normalized_lines(content, search, strictness)
        if len(spans) == 1:
            return spans[0], "APPLIED", strictness
        if len(spans) > 1:
            return None, "AMBIGUOUS", strictness

    return None, "NO_MATCH", "strip_line_numbers"


def _reason_for(hunk: Hunk, status: str, strictness: str) -> str:
    first_line = hunk.search.splitlines()[0][:80] if hunk.search.strip() else "(empty)"
    if status == "EMPTY_SEARCH":
        return ("SEARCH block is empty or whitespace-only — refusing to match (an empty "
                "SEARCH matches everywhere, including position 0, which is the exact bug "
                "class this parser exists to prevent)")
    if status == "AMBIGUOUS":
        return (f"SEARCH block matches more than once at strictness={strictness} "
                f"(first line: {first_line!r}) — must be unique; add more surrounding context")
    if status == "NO_MATCH":
        return (f"SEARCH block not found in file even after the {strictness} fallback "
                f"(first line: {first_line!r}) — re-view the file and match it exactly")
    if status == "DISJOINT_CONFLICT":
        return (f"SEARCH block's matched region overlaps another hunk's matched region in "
                f"this same patch (first line: {first_line!r}) — narrow the SEARCH context "
                "so hunks target disjoint regions")
    return ""


# --------------------------------------------------------------------------
# disjointness preflight
# --------------------------------------------------------------------------

def validate_hunk_disjointness(outcomes: List[HunkOutcome]) -> List[HunkOutcome]:
    """Given outcomes that have already been located (status == APPLIED,
    span set against the ORIGINAL content), return the subset whose span
    overlaps another's. Called as a preflight in `apply_hunks` BEFORE any
    text is rewritten — fails fast instead of letting one hunk's
    application silently corrupt what another hunk's application already
    wrote (or is about to write)."""
    located = [o for o in outcomes if o.span is not None]
    located.sort(key=lambda o: o.span[0])
    conflicting_ids: set[int] = set()
    for a, b in zip(located, located[1:]):
        if a.span[1] > b.span[0]:  # a's end is past b's start -> overlap
            conflicting_ids.add(id(a))
            conflicting_ids.add(id(b))
    return [o for o in outcomes if id(o) in conflicting_ids]


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

def apply_hunks(content: str, hunks: List[Hunk], *, allow_partial: bool = False) -> PatchResult:
    """Apply every hunk to `content`.

    Preflight order:
      1. Locate every hunk independently against the ORIGINAL `content`
         (exact -> normalize_ws -> normalize_all fallback per hunk).
      2. `validate_hunk_disjointness` over everything that resolved to a
         span — any overlap demotes both hunks involved to
         DISJOINT_CONFLICT before a single character is rewritten.
      3. If anything is left rejected (EMPTY_SEARCH / AMBIGUOUS / NO_MATCH
         / DISJOINT_CONFLICT):
           - `allow_partial=False` (default): reject the WHOLE patch —
             `new_content` stays None, nothing is applied.
           - `allow_partial=True`: apply whichever hunks DID resolve
             cleanly; the rejected ones are reported in `.rejected` with
             per-hunk reasons.
      4. Application itself happens against spans computed once in step 1
         (never re-derived from a mutating string), highest-offset-first,
         so file-position order is what actually lands regardless of the
         hunks' LLM-output order (fix #2 in this module's docstring).
    """
    if not hunks:
        return PatchResult(success=False, new_content=None, applied=[], rejected=[],
                            error="no SEARCH/REPLACE blocks found")

    outcomes: List[HunkOutcome] = []
    for h in hunks:
        span, status, strictness = _locate_hunk(content, h.search)
        outcomes.append(HunkOutcome(hunk=h, status=status,
                                    reason=_reason_for(h, status, strictness),
                                    span=span, strictness=strictness))

    for conflict in validate_hunk_disjointness(outcomes):
        conflict.status = "DISJOINT_CONFLICT"
        conflict.reason = _reason_for(conflict.hunk, "DISJOINT_CONFLICT", conflict.strictness)
        conflict.span = None

    applied = sorted((o for o in outcomes if o.span is not None), key=lambda o: o.span[0])
    rejected = [o for o in outcomes if o.span is None]

    if rejected and not allow_partial:
        return PatchResult(
            success=False, new_content=None, applied=[], rejected=outcomes,
            error="; ".join(f"hunk {o.hunk.index}: {o.reason}" for o in rejected),
        )

    if not applied:
        return PatchResult(
            success=False, new_content=None, applied=[], rejected=outcomes,
            error="; ".join(f"hunk {o.hunk.index}: {o.reason}" for o in rejected),
        )

    new_content = content
    for outcome in sorted(applied, key=lambda o: o.span[0], reverse=True):
        start, end = outcome.span
        new_content = new_content[:start] + outcome.hunk.replace + new_content[end:]

    return PatchResult(
        success=True, new_content=new_content, applied=applied, rejected=rejected,
        error="" if not rejected else "; ".join(f"hunk {o.hunk.index}: {o.reason}" for o in rejected),
    )


def apply_search_replace(content: str, llm_output: str, *, allow_partial: bool = False) -> Tuple[Optional[str], str]:
    """The convenience one-shot entry point: parse `llm_output` for
    SEARCH/REPLACE hunks and apply them to `content`.

    Returns `(new_content, error)` — matching the exact shape every
    retired implementation returned, so `repair.py`/`generate_agent.py`/
    `repair_agent.py` can become thin callers of this function with no
    signature changes at their own call sites. `new_content` is None on
    total failure; `error` is a human-readable combined message (empty
    string on a clean, complete success)."""
    hunks = parse_hunks(llm_output)
    if not hunks:
        return None, "no SEARCH/REPLACE blocks found"
    result = apply_hunks(content, hunks, allow_partial=allow_partial)
    return result.new_content, result.error
