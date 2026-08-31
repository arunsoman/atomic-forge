# SWE-bench Verified harness — scaffold

Started 2026-08-31 per `goal.md`'s revised priority order (no longer
gated on merged-PR outcomes from the real-issues campaign).

## Design (mirrors `benchmarks/run_case.py`'s pattern, not a rewrite)

`run_case.py` already does the right shape for a curated case: copy a
seed into a temp dir, run `atomic-forge repair --tasks task.json
--project-dir <tmp>` as a real subprocess, score pass/fail off the real
test suite. A SWE-bench instance is the same shape with different
inputs:

1. **Instance → `AtomicTask`**: each SWE-bench instance ships
   `repo`, `base_commit`, `problem_statement`, `FAIL_TO_PASS`/
   `PASS_TO_PASS` test ids, and a golden patch (held out for scoring, not
   given to forge). Map `problem_statement` → the same task-description
   field `fix.py` already builds from a GitHub issue body — no new
   ingestion path needed, since forge already turns issue text into a
   regression test via CIE; here the regression test targets are already
   known (`FAIL_TO_PASS`) instead of generated.
2. **Environment**: SWE-bench's own per-instance Docker images (from the
   `swebench` PyPI package) pin the exact base commit + dependency
   state — use those directly rather than forge's own bootstrap-gate
   detection, since SWE-bench's whole point is a fixed, comparable
   environment across all submissions.
3. **Scoring**: SWE-bench's own harness already defines PASS
   (`FAIL_TO_PASS` all pass, `PASS_TO_PASS` still pass) — reuse it
   verbatim rather than reimplementing, so results are comparable to
   published leaderboard numbers.
4. **Pilot size**: 10–15 instances first, per `Evaluation-Plan.md`'s own
   "start small, publish early" guidance — not the full 500-instance
   Verified set.

## Blocked on a resourcing decision, not a design decision

- `swebench` + `datasets` aren't installed yet (checked — not a network
  problem, just not present).
- **Local disk is at 86% used, 14G free.** SWE-bench's per-instance
  Docker images run 1–5G+ each; even a 10-instance pilot risks filling
  the disk on this machine before a single result comes back. Options:
  free up space first (there's a `forge-test-repo` gcc:14 container
  already running — worth checking if that and other cached images can
  be pruned), or run the pilot somewhere with more headroom (a cloud
  agent / CI runner) rather than this machine.

## Next concrete step (once the disk question is settled)

```bash
pip install swebench datasets
python -c "from datasets import load_dataset; ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); print(len(ds))"
# then hand-pick 10-15 instances (favor repos forge's bootstrap already
# knows: pytest-style Python packages, permissive licenses) and write
# the adapter script mirroring run_case.py.
```

Not run yet — this file is the scaffold + the one open question, not a
result.
