"""Post-mortem learning engine.

Runs once a repair attempt is DONE and failed to land a fix within its round
budget (`repair_exhausted`) — never mid-run, and never feeds back into the
same attempt. Its job is the analysis forge itself has no budget to do while
it's still trying: read the full round-by-round trajectory (every suspect
considered, every patch tried, every test result, every blast-radius
rejection) and answer three things a human studying the campaign afterward
would otherwise have to reconstruct by hand:

1. what distinct approaches were actually tried,
2. what concrete, trajectory-grounded approaches were NOT tried,
3. whether a new code-graph/MCP tool function would plausibly have changed
   the outcome — argued against a specific moment in the trajectory, not
   asserted in the abstract.

Persisted per-run to `<project_dir>/.forge/learning.json` (latest) and
appended to `<project_dir>/.forge/learning.jsonl` (history — the natural
place to point an aggregate study across a whole campaign, the way
`benchmarks/real_issues/sweep/results_round2.jsonl` aggregates outcomes).
"""
from __future__ import annotations

import json
from pathlib import Path

POSTMORTEM_SYSTEM = """You are forge's post-mortem analyst. forge just exhausted its repair-round budget on a real bug without landing a green fix. You are given the bug report and the full round-by-round trajectory: which files were suspected, which patches were tried, what each test run said, and why any patch was rejected by the blast-radius gate.

Answer three things, and ONLY these three, as a single JSON object:
1. "paths_tried": a short bullet list of the DISTINCT strategies/approaches actually attempted across rounds (not a transcript — collapse repeats of the same idea).
2. "untried_paths": concrete, specific approaches that were NOT tried and plausibly could have found the fix — grounded in what the trajectory actually shows about the bug and the code touched. Generic advice ("try harder", "write more tests", "think step by step") is not an acceptable answer; each entry must reference something specific from the trajectory (a file not looked at, a suspect dismissed too early, a class of fix never attempted).
3. "new_tool_would_help" (true/false) and "proposed_tool" (null if false, else {"name": str, "purpose": str, "why_it_would_have_changed_the_outcome": str}) — argue concretely: point to a SPECIFIC moment in the trajectory where the agent was blind to something a new code-graph/MCP tool function (something not already in its tool manifest) would have surfaced. Do not propose a tool "just in case" — if nothing in the trajectory suggests missing tooling was the blocker, say false.

Respond with ONLY the JSON object — no prose before or after it, no markdown code fence."""

#: Trajectory fields worth keeping in the digest fed to the postmortem LLM —
#: everything else (raw diffs, full tool-call payloads) is noise at this
#: level of analysis and would blow the context budget for no benefit.
_KEEP_FIELDS = ("event", "kind", "round", "file", "files", "suspect", "suspects",
                "ok", "failures", "verdict", "reason", "violation", "task_name",
                "tool", "skipped", "output_tail")


def _summarize_trajectory(traj_path: Path, max_chars: int = 12000) -> str:
    """Compact round-by-round digest of trajectory.jsonl. Keeps the TAIL of
    the filtered log (most recent rounds are the most informative — the
    early rounds' dead ends are still present, just more likely to be
    truncated first if the whole thing doesn't fit)."""
    if not traj_path.exists():
        return "(no trajectory recorded)"
    lines = []
    for line in traj_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        keep = {k: v for k, v in rec.items() if k in _KEEP_FIELDS}
        if keep:
            lines.append(json.dumps(keep, ensure_ascii=False))
    text = "\n".join(lines)
    return text[-max_chars:] if text else "(trajectory had no analyzable events)"


def _parse_analysis(raw: str) -> dict:
    raw = (raw or "").strip()
    # tolerate a stray ```json fence — cheap models sometimes add one despite
    # the system prompt's explicit "no markdown code fence" instruction.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    data = json.loads(raw)
    return {
        "paths_tried": data.get("paths_tried") or [],
        "untried_paths": data.get("untried_paths") or [],
        "new_tool_would_help": bool(data.get("new_tool_would_help")),
        "proposed_tool": data.get("proposed_tool"),
    }


def run_postmortem(llm, project_dir, traj_path, *, bug_description: str,
                   exit_reason: str = "repair_exhausted") -> dict:
    """Ask the LLM to analyze a failed run's trajectory and persist the
    result. Never raises: a postmortem that itself fails to parse still
    writes a record (flagged with `parse_error` + the raw text) rather than
    take down the outer run that's already reporting its own failure."""
    project_dir = Path(project_dir)
    traj_path = Path(traj_path)
    digest = _summarize_trajectory(traj_path)
    messages = [
        {"role": "system", "content": POSTMORTEM_SYSTEM},
        {"role": "user", "content": f"Bug report:\n{bug_description}\n\n"
                                    f"Trajectory (most recent last):\n{digest}"},
    ]
    raw = ""
    try:
        raw = llm.chat(messages, temperature=0.3, max_tokens=1500)
        analysis = _parse_analysis(raw)
    except Exception as e:
        # `new_tool_would_help: false` here used to read as a confident
        # conclusion ("the postmortem looked and found nothing") when it
        # actually meant the postmortem itself never ran — found live
        # (2026-08-31): both occurrences so far (simonw/sqlite-utils#841,
        # pypa/pip#14269) followed a 150+-call, 2M+-token repair session
        # in the same process, and llm.chat() returned successfully with
        # EMPTY content rather than raising — a 200 response with nothing
        # in it doesn't trip _is_quota_exhausted (there's no exception
        # text to match), so this may be the same session-quota pressure
        # wearing a different disguise. Not proven, so not chasing the
        # exact mechanism further here; instead making the record itself
        # honest either way — None (unknown), never False (a specific,
        # unearned claim), when there was no real analysis to base it on.
        analysis = {"paths_tried": [], "untried_paths": [],
                    "new_tool_would_help": None, "proposed_tool": None,
                    "postmortem_failed": True,
                    "parse_error": str(e), "raw": raw[:2000]}

    record = {"exit_reason": exit_reason, **analysis}
    out_dir = project_dir / ".forge"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "learning.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    with (out_dir / "learning.jsonl").open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
