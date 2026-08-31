#!/usr/bin/env python3
"""Round-2 orchestrator: run the 50-issue sweep repo-by-repo, in batches of
2 issues, pruning Docker (and the batch's own /tmp scratch clones) after
each batch instead of one big upfront wipe — keeps disk bounded through a
long run without a bulk `docker system prune`.

Disk reality check (2026-08-29): the host's root filesystem is at 99% full
(94/97GB) for reasons unrelated to forge — Docker's own footprint here is
only ~3GB of shared base images. Per-batch pruning can't fix a machine-wide
disk problem; what it CAN do is stop each issue's clone+venv+container
layer from accumulating on top of that, which is the actual growth this
sweep would otherwise add. Each issue's /tmp/forge_fix/<project> clone is
removed right after its result is logged (successes are already pushed to
a fork branch on GitHub — nothing here is otherwise recoverable-only-here).

    FORGE_MODEL=glm-5.2:cloud .venv/bin/python benchmarks/real_issues/run_round2.py
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
from sweep_lib import harvest_and_clean, parse_cost, prune_docker  # noqa: E402

CANDIDATES = HERE / "sweep" / "candidates.jsonl"
RESULTS = HERE / "sweep" / "results_round2.jsonl"
LOGS_DIR = HERE / "logs"
TMP_ROOT = Path("/tmp/forge_fix")
PY = sys.executable
FORGE_BIN = str(Path(sys.executable).parent / "atomic-forge")

BATCH_SIZE = 2  # prune after every N issues from the same repo
TOTAL_CAP = 70  # stop once this many fresh issues have been attempted


def load_done(path: Path) -> set[str]:
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                done.add(json.loads(line).get("issue", ""))
            except json.JSONDecodeError:
                continue
    return done


def run_one(cand: dict, env: dict, timeout_s: int) -> dict:
    url, repo, number = cand["url"], cand["repo"], cand["number"]
    print(f"=== {repo}#{number} — {cand['title'][:70]}", file=sys.stderr)
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
    else:
        pr_url, status = None, "repair_fail"

    tail = "\n".join(stdout.strip().splitlines()[-25:])
    rec = {"issue": url, "repo": repo, "number": number, "title": cand["title"],
           "status": status, "pr_url": pr_url, "seconds": seconds,
           "returncode": rc, "model": env.get("FORGE_MODEL"),
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
    # Every PR this campaign raises forks into the org account, not whoever's
    # personal `gh` session happens to run the sweep — keeps all campaign
    # forks/PRs in one maintained place instead of scattering them.
    env.setdefault("FORGE_FORK_ORG", "kannamma-labs")
    # Every commit must carry this identity, not the operator's personal
    # git identity — see _apply_forge_identity() in sandbox.py.
    env.setdefault("FORGE_GIT_USER_NAME", "kannamalabs")
    env.setdefault("FORGE_GIT_USER_EMAIL", "322530453+kannamalabs@users.noreply.github.com")

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

    print(f"\nround2 sweep done: {total_ok} PR(s) raised of {total_attempted} attempted",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
