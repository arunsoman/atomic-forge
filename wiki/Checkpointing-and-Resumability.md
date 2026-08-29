## The run record

Every forge run is a row in a SQLite store, plus one checkpoint row per
phase transition — written **before** the phase's work starts, so a crash
leaves the record pointing at what was *about to* happen, never at
half-applied state that claims to be done.

- `RunCheckpointer` — `mark_phase`, `mark_written(file_hashes)`,
  `mark_bootstrap`, `finish(verdict)`.
- `load_run(run_id)` — reload a run anywhere, later.
- 7-way verdict taxonomy on finish: `passed` / `failed` / `partial` /
  `timeout` / `lint_error` / `crashed` / `skipped`.

## Hash-diff resume

```python
from atomic_forge.checkpoint import load_run, diff_file_hashes

record = load_run(run_id)
diff = diff_file_hashes(project_dir, record.file_hashes)
# diff.unchanged -> trust these, skip regenerating
# diff.changed   -> regenerate only these
```

On resume, forge re-hashes every file on disk and regenerates only what
actually changed or went missing — never an all-or-nothing restart. A run
interrupted mid-repair resumes *at repair*; a run interrupted mid-file
regenerates that file.

## What's checkpointed

| Store | Content |
|---|---|
| `checkpoint.py` | Per-run state: phases, per-file hashes, verdicts, bootstrap verdict |
| `checkpoint_store.py` | Durable SQLite store shared across runs (`CHECKPOINT_DB_PATH` override) |
| `trajectory.py` | Append-only JSONL audit trail of every action the agents took |

Together these make a run *inspectable*: you can answer "what did the
agent do, in what order, and which attempts failed how" after the fact —
[[Architecture]] shows where each piece lives in code.