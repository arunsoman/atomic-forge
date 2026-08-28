#!/usr/bin/env python3
"""
Measures cold-run vs. resume time using the real checkpoint/resume
primitives (checkpoint.py) exactly as README's "Resumable runs" section
documents them — this script IS that documented pattern, run for real
against a live LLM, not a mocked demonstration.

Method: generate a batch of N files cold (timed). Then simulate a crash
that left one file's content stale/missing (delete it) while the rest are
untouched on disk. Resume: diff_file_hashes tells us which files are
still trusted, we regenerate ONLY the batch filtered down to the
changed/missing file(s), and time that. The speedup is
cold_time / resume_time_for_the_same_single_file, which is the honest
number this measures — not a full process-crash-and-restart test (this
script never actually kills a process), and not a claim that resume is
always this fast (it's proportional to how many of N files actually
changed, which for a real interrupted run could be anywhere from 1 to N).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atomic_forge.batch_io import load_batch_json
from atomic_forge.checkpoint import (
    RunCheckpointer, diff_file_hashes, hash_files, new_run_id,
)
from atomic_forge.generate_agent import generate_batch_agentic
from atomic_forge.llm import default_llm
from atomic_forge.tools import make_tools
from atomic_forge.trajectory import Trajectory


def main() -> int:
    tasks_path = Path(__file__).resolve().parents[1] / "examples" / "tasks.json"
    batch = load_batch_json(tasks_path)
    file_paths = [t.file_path for t in batch.tasks]

    project_dir = Path(__file__).resolve().parent / "results" / "resume_measurement" / "workdir"
    import shutil
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True)

    llm = default_llm()
    tools = make_tools(project_dir)
    traj = Trajectory(project_dir)
    ckpt = RunCheckpointer(run_id=new_run_id(), project="resume-bench", project_dir=str(project_dir))
    ckpt.mark_phase("generate")

    t0 = time.time()
    generate_batch_agentic(project_dir, batch, llm, tools, traj)
    cold_time = time.time() - t0
    ckpt.mark_written(hash_files(project_dir, file_paths))
    ckpt.finish("passed")

    # Simulate an interrupted run: one file is missing/stale on disk, the
    # rest are exactly as the checkpoint recorded them.
    stale_file = file_paths[0]
    (project_dir / stale_file).unlink()

    diff = diff_file_hashes(project_dir, ckpt.record.file_hashes)
    resume_batch = batch.__class__(tasks=[t for t in batch.tasks if t.file_path in diff.changed])
    assert [t.file_path for t in resume_batch.tasks] == [stale_file], \
        f"expected only {stale_file} to need regeneration, got {[t.file_path for t in resume_batch.tasks]}"

    t1 = time.time()
    generate_batch_agentic(project_dir, resume_batch, llm, tools, traj)
    resume_time = time.time() - t1

    result = {
        "total_files": len(file_paths),
        "cold_run_s": round(cold_time, 2),
        "resume_run_s_for_1_of_n_files": round(resume_time, 2),
        "unchanged_skipped": diff.unchanged,
        "regenerated": diff.changed,
        "speedup_x": round(cold_time / resume_time, 2) if resume_time > 0 else None,
        "note": ("Speedup here is proportional to 1 changed file out of "
                 f"{len(file_paths)} — not a fixed constant; a resume with "
                 "more files changed would show a smaller ratio."),
    }
    out = Path(__file__).resolve().parent / "results" / "resume_measurement.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
