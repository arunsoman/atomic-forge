#!/usr/bin/env python3
"""Round-3 orchestrator: run the 50-issue category-diversified sweep
(sweep/candidates_round3_selected.jsonl, built by diversify_round3.py)
repo-by-repo in small batches, pruning Docker + the batch's own /tmp
scratch clones after each batch. Mirrors run_round2.py's disk discipline.

FORGE_FORK_ORG is intentionally left unset: every PR this round forks
under whichever `gh` account is currently active (ensure_fork() falls
back to gh_login()) rather than the kannamma-labs org account — for now,
that's the operator's own personal GitHub account. FORGE_GIT_USER_* is
still hard-set (not just defaulted), pinned to that same account's
GitHub-issued noreply address so commit authorship can't drift onto the
operator's real personal email across ~47 strangers' repos regardless of
what the calling shell's global git config happens to be.

    FORGE_MODEL=glm-5.2:cloud .venv/bin/python benchmarks/real_issues/run_round3.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sweep_lib import check_ai_policy, harvest_and_clean, parse_cost, prune_docker  # noqa: E402

CANDIDATES = HERE / "sweep" / "candidates_round3_selected.jsonl"
RESULTS = HERE / "sweep" / "results_round3.jsonl"
LOGS_DIR = HERE / "logs"
TMP_ROOT = Path("/tmp/forge_fix")
FORGE_BIN = str(Path(sys.executable).parent / "atomic-forge")

BATCH_SIZE = 2   # prune after every N issues from the same repo
TOTAL_CAP = 50   # the campaign's headline number for this round

# Reporting-only difficulty tag derived from diversify_round3.py's bug-shape
# category. No reordering: diversify_round3.py's own PRIORITY order already
# attempts crash/race/leak/perf (hard) ahead of api/other (medium) ahead of
# logic_edge (easy) — this just makes that spread visible in the ledger.
_DIFFICULTY = {
    "crash": "hard", "race": "hard", "leak": "hard", "perf": "hard",
    "api": "medium", "other": "medium",
    "logic_edge": "easy",
}

# Statuses that mean "this never really ran" — load_done() must not treat
# these as permanently attempted, so a transient infra hiccup self-heals on
# the next invocation instead of silently freezing a candidate out forever.
_RETRY_STATUSES = {"infra_fail"}


def load_done(path: Path) -> set[str]:
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in _RETRY_STATUSES:
                continue
            done.add(rec.get("issue", ""))
    return done


def is_still_open(repo: str, number: int) -> bool | None:
    """C1 (rca_pilot_runs_1_3.md): re-verify `state=open` immediately before
    spending any LLM budget on an issue. Curation's own `is:open` filter ran
    hours-to-a-day earlier and GitHub's search index can lag; the pilot's
    sympy#29382 (closed 5 months before it was run) is the concrete case
    this exists to catch. Returns None (not False!) on a lookup failure —
    an inconclusive check must never masquerade as "confirmed closed"."""
    r = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}", "--jq", ".state"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip() == "open"


def run_one(cand: dict, env: dict, timeout_s: int) -> dict:
    url, repo, number = cand["url"], cand["repo"], cand["number"]
    print(f"=== {repo}#{number} — [{cand.get('_cat', '?')}] {cand['title'][:60]}", file=sys.stderr)

    still_open = is_still_open(repo, number)
    if still_open is False:
        print(f"  skip: closed upstream since curation (stale)", file=sys.stderr)
        return {"issue": url, "repo": repo, "number": number, "title": cand["title"],
                "category": cand.get("_cat"), "difficulty": _DIFFICULTY.get(cand.get("_cat"), "medium"),
                "status": "stale_closed", "pr_url": None,
                "seconds": 0, "returncode": None, "model": env.get("FORGE_MODEL"),
                "at": time.strftime("%F %T"), "log_tail": None}

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [FORGE_BIN, "fix", url, "--raise-pr",
             "--max-rounds", "3", "--bootstrap-timeout", "900"],
            capture_output=True, text=True, timeout=timeout_s,
            env={**os.environ, **env})
        stdout, rc = proc.stdout + "\n" + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        rc = -9
    seconds = round(time.monotonic() - t0, 1)

    import re
    m = re.search(r"PR opened: (\S+)", stdout)
    if m:
        pr_url, status = m.group(1), "pr_raised"
    elif "upstream blocks PR creation" in stdout:
        pr_url, status = None, "pr_locked"
    elif "abort at bootstrap gate" in stdout:
        pr_url, status = None, "bootstrap_fail"
    elif "abort: no regression test" in stdout or "no regression test generated" in stdout:
        pr_url, status = None, "oracle_reject"
    elif re.search(r"rate limit|session usage limit|error code: 429|quota/rate-limit exhausted",
                   stdout, re.I):
        # Never a real repair attempt — died on a `gh` API quota (rate limit)
        # or an LLM-provider quota (session usage limit / HTTP 429). Core
        # atomic_forge now raises this as its own LLMQuotaError (llm.py),
        # caught in fix.py/bootstrap.py and printed as "LLM quota/rate-limit
        # exhausted" — a clean, honest abort rather than an uncaught
        # RuntimeError. Match that phrase directly (defense in depth: the
        # older provider-message substrings above still catch it too, since
        # the original error text is nested inside LLMQuotaError's own
        # message, but don't rely on that nesting alone). Tagged distinctly
        # so load_done() retries it rather than mistaking a quota outage for
        # a genuine repair failure.
        pr_url, status = None, "infra_fail"
    else:
        pr_url, status = None, "repair_fail"

    tail = "\n".join(stdout.strip().splitlines()[-25:])
    rec = {"issue": url, "repo": repo, "number": number, "title": cand["title"],
           "category": cand.get("_cat"), "difficulty": _DIFFICULTY.get(cand.get("_cat"), "medium"),
           "status": status, "pr_url": pr_url,
           "seconds": seconds, "returncode": rc, "model": env.get("FORGE_MODEL"),
           "at": time.strftime("%F %T"), "log_tail": tail if pr_url is None else None,
           **parse_cost(stdout)}
    print(f"  {status}{' -> ' + pr_url if pr_url else ''} ({seconds}s)", file=sys.stderr)
    return rec


def main() -> int:
    env = {k: v for k, v in os.environ.items() if k.startswith(("FORGE_", "GH_"))}
    env.setdefault("FORGE_MODEL", "glm-5.2:cloud")
    env.setdefault("FORGE_BASE_URL", "http://localhost:11434/v1")
    env.setdefault("FORGE_API_KEY", "ollama")
    env.setdefault("FORGE_ENABLE_AGENTIC_BOOTSTRAP", "1")
    # Deliberately NOT set: leaving FORGE_FORK_ORG unset makes ensure_fork()
    # (src/atomic_forge/pr.py) fall back to gh_login() — fork under whatever
    # `gh` account is active. `gh repo fork --org` targets an organization,
    # not a personal account, so pinning this to a personal username would
    # risk the fork call itself failing.
    # env["FORGE_FORK_ORG"] intentionally absent — see docstring.
    # Hard-set: every commit forge makes must carry this identity, not the
    # operator's personal git identity — see _apply_forge_identity() in
    # sandbox.py. arunsoman's GitHub-issued noreply address (real,
    # resolvable, no personal information) rather than the real personal
    # email in global git config.
    env["FORGE_GIT_USER_NAME"] = "arunsoman"
    env["FORGE_GIT_USER_EMAIL"] = "1702420+arunsoman@users.noreply.github.com"

    rows = [json.loads(l) for l in CANDIDATES.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("pr_ok", True)]
    rows.sort(key=lambda r: -r["score"])

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_repo[r["repo"]].append(r)

    done = load_done(RESULTS)
    total_attempted = total_ok = 0

    for repo, issues in by_repo.items():
        if total_attempted >= TOTAL_CAP:
            break
        issues = [c for c in issues if c["url"] not in done]
        if not issues:
            continue
        # Confirmed live 2026-08-31: Rapptz/discord.py closed our PR within
        # seconds citing its own AI-contributions ban; jazzband/pip-tools
        # and pytest-dev/pytest both have an equally explicit "no purely-
        # agentic / unreviewed PRs" policy that --raise-pr's no-human-review
        # design can't satisfy. Check ONCE per repo, before spending any
        # attempt on it — a repair effort that will be rejected on principle
        # wastes the same real budget as any other dead end.
        policy_reason = check_ai_policy(repo)
        if policy_reason:
            print(f"\n##### {repo}: skipped, {len(issues)} candidate(s) — {policy_reason}",
                  file=sys.stderr)
            for cand in issues:
                rec = {"issue": cand["url"], "repo": repo, "number": cand["number"],
                       "title": cand["title"], "category": cand.get("_cat"),
                       "difficulty": _DIFFICULTY.get(cand.get("_cat"), "medium"),
                       "status": "policy_excluded", "policy_note": policy_reason,
                       "pr_url": None, "seconds": 0, "returncode": None,
                       "model": env.get("FORGE_MODEL"), "at": time.strftime("%F %T"),
                       "log_tail": None}
                with RESULTS.open("a") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            continue
        print(f"\n##### {repo}: {len(issues)} candidate(s)", file=sys.stderr)
        for i in range(0, len(issues), BATCH_SIZE):
            if total_attempted >= TOTAL_CAP:
                break
            batch = issues[i:i + BATCH_SIZE]
            for cand in batch:
                if total_attempted >= TOTAL_CAP:
                    break
                rec = run_one(cand, env, timeout_s=2100)
                with RESULTS.open("a") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total_attempted += 1
                if rec["status"] == "pr_raised":
                    total_ok += 1
                harvest_and_clean(TMP_ROOT, LOGS_DIR, repo, cand["number"], result=rec)
            print(f"-- batch done for {repo} ({min(i + BATCH_SIZE, len(issues))}/{len(issues)}); "
                  f"pruning docker [{total_attempted}/{TOTAL_CAP} total attempted]",
                  file=sys.stderr)
            prune_docker()

    print(f"\nround3 sweep done: {total_ok} PR(s) raised of {total_attempted} attempted",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
