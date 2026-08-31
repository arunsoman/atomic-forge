#!/usr/bin/env python3
"""Round-3 selection: re-rank candidates_round3.jsonl so this round's bug
MIX is deliberately different from round 2's, not just a different repo
list. Round 2 (see benchmarks/README.md) was dominated by "silently wrong
value" logic/edge-case bugs (date math, string formatting, retry
conditionals) — that's the LOGIC_EDGE bucket below, and it's what actually
landed: loguru's timestamp truncation, babel's blank-date crash, tenacity's
retry-logging condition were all this shape.

This round instead front-loads categories round 2 barely touched:
  CRASH  - raises on valid/edge input (exceptions users hit directly)
  RACE   - concurrency/cancellation bugs
  LEAK   - resources (sockets, fds, connections) not released
  PERF   - O(n^2)/hangs/freezes, not just wrong-answer bugs
  API    - contract bugs: type hints, hashability, permission bits, config
           not respected
LOGIC_EDGE bugs are kept (not excluded — still real, still worth fixing)
but pushed to the back of the queue so the round's PRs, if any land, are
categorically different from round 2's.

Output: sweep/candidates_round3_selected.jsonl (<=50 rows, <=3/repo).
    .venv/bin/python benchmarks/real_issues/diversify_round3.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "sweep" / "candidates_round3.jsonl"
OUT = HERE / "sweep" / "candidates_round3_selected.jsonl"

TOTAL_TARGET = 50
MAX_PER_REPO = 3

FEATURE = re.compile(r"^\s*(feature request|rfc|proposal)\s*:", re.I)
CRASH = re.compile(r"\b(RecursionError|IndexError|KeyError|ValueError|TypeError|"
                   r"AttributeError)\b.*\b(raise|crash)|crash(es)?\b", re.I)
RACE = re.compile(r"\brace\b|\bcancell?ation\b|deadlock|\bthread\b|\basync\b", re.I)
LEAK = re.compile(r"not (always )?closed|\bleak\b|not release|stale fd|dangling", re.I)
PERF = re.compile(r"O\(n|hangs?\b|freeze|too (long|slow)|regression\]", re.I)
APICONTRACT = re.compile(r"type annotation|type hint|unhashable|__hash__|"
                         r"executable bit|not usable on|not respected|search_order", re.I)
LOGIC_EDGE = re.compile(r"silently|incorrect\b|\bwrong (time|result|total)\b|"
                        r"dayfirst|dehumaniz|truncat|misleading", re.I)

PRIORITY = ["crash", "race", "leak", "perf", "api", "other", "logic_edge"]


def categorize(title: str) -> str:
    if CRASH.search(title):
        return "crash"
    if RACE.search(title):
        return "race"
    if LEAK.search(title):
        return "leak"
    if PERF.search(title):
        return "perf"
    if APICONTRACT.search(title):
        return "api"
    if LOGIC_EDGE.search(title):
        return "logic_edge"
    return "other"


def main() -> int:
    rows = [json.loads(l) for l in SRC.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if not FEATURE.search(r["title"])]
    for r in rows:
        r["_cat"] = categorize(r["title"])
    rows.sort(key=lambda r: (PRIORITY.index(r["_cat"]), -r["score"]))

    selected: list[dict] = []
    per_repo: dict[str, int] = {}
    for r in rows:
        if len(selected) >= TOTAL_TARGET:
            break
        if per_repo.get(r["repo"], 0) >= MAX_PER_REPO:
            continue
        per_repo[r["repo"]] = per_repo.get(r["repo"], 0) + 1
        selected.append(r)

    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in selected) + "\n")
    by_cat: dict[str, int] = {}
    for r in selected:
        by_cat[r["_cat"]] = by_cat.get(r["_cat"], 0) + 1
    print(f"{len(selected)} issue(s) -> {OUT}", file=sys.stderr)
    for cat in PRIORITY:
        if by_cat.get(cat):
            print(f"  {cat}: {by_cat[cat]}", file=sys.stderr)
    print(f"  repos: {len(per_repo)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
