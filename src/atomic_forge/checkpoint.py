"""
ForgeRunRecord (checkpoint schema) + RunCheckpointer — the crash-safe,
resumable state for one forge run.

Every phase transition is durably saved to SQLite (via checkpoint_store)
BEFORE the corresponding work starts, so a mid-run crash always leaves the
*last completed* phase durable on disk instead of losing track of how far
the run got.

Resume semantics: `diff_file_hashes` re-hashes every file the checkpoint
recorded and compares against disk right now. A match means that file's
last-recorded phase is trusted — the caller can skip straight past
regenerating it. A mismatch (content changed, or the file is now missing)
means regenerate ONLY that file, not the whole batch — this per-file
granularity is what makes resume not an all-or-nothing restart.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .checkpoint_store import load_checkpoint, load_checkpoints_for, save_checkpoint


class Verdict(str, Enum):
    """Per-task verdict. Collapsing every non-pass outcome into a bare
    "failed" loses information the repair loop's next-round prompt
    needs — a lint/syntax error calls for a different fix than a failed
    assertion, and a timeout/crash needs neither a prompt nor a retry, it
    needs the run to stop digging."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"        # some tests passed, some failed
    TIMEOUT = "timeout"        # tests hung
    LINT_ERROR = "lint_error"  # syntax/type error, tests never ran
    CRASHED = "crashed"        # runner crashed


#: The owner_kind every forge run's checkpoint rows are stored under in
#: checkpoint_store's shared table.
OWNER_KIND = "forge_run"

Phase = Literal[
    "decomposing", "scaffolded", "generate", "qa", "repair", "finished",
]
RunStatus = Literal["running", "passed", "failed", "crashed", "cancelled"]


class ForgeRunRecord(BaseModel):
    checkpoint_version: int = 1
    run_id: str
    project: str
    project_dir: str
    phase: Phase = "generate"
    status: RunStatus = "running"
    task_batch: Optional[dict] = None
    #: path (relative to project_dir) -> sha256, for resume validation.
    file_hashes: Dict[str, str] = Field(default_factory=dict)
    #: task name -> verdict string.
    tested_verdicts: Dict[str, str] = Field(default_factory=dict)
    persisted_verdicts: Dict[str, str] = Field(default_factory=dict)
    repair_reports: List[dict] = Field(default_factory=list)
    #: label (e.g. "created", "phase:generate", "finished") -> ISO8601 UTC.
    timestamps: Dict[str, str] = Field(default_factory=dict)
    triggered_by: Optional[str] = None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id() -> str:
    return uuid.uuid4().hex


def hash_file(path: Path) -> Optional[str]:
    """sha256 of a file's bytes, or None if it doesn't exist on disk (or
    can't be read) — resume treats a missing file the same as a hash
    mismatch (regenerate), never as a spurious "unchanged"."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def hash_files(project_dir: Path, rel_paths: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rel in rel_paths:
        h = hash_file(project_dir / rel)
        if h is not None:
            out[rel] = h
    return out


class HashDiff(BaseModel):
    unchanged: List[str] = Field(default_factory=list)
    changed: List[str] = Field(default_factory=list)


def diff_file_hashes(project_dir: Path, recorded: Dict[str, str]) -> HashDiff:
    unchanged: List[str] = []
    changed: List[str] = []
    for rel, recorded_hash in recorded.items():
        current = hash_file(project_dir / rel)
        if current is not None and current == recorded_hash:
            unchanged.append(rel)
        else:
            changed.append(rel)
    return HashDiff(unchanged=unchanged, changed=changed)


class RunCheckpointer:
    """Wraps one ForgeRunRecord and persists it via checkpoint_store on
    construction and on every mark_*/finish call — the record is written
    BEFORE any work starts (status="running") and again at every phase
    boundary.

    `db_path`: forwarded to every checkpoint_store call — a real optional
    override (not a monkeypatched module constant) so tests can isolate
    each run's SQLite state under `tmp_path`.
    """

    def __init__(
        self, run_id: str, project: str, project_dir: str,
        task_batch: Optional[dict] = None, record: Optional[ForgeRunRecord] = None,
        db_path: Optional[Path] = None, triggered_by: Optional[str] = None,
    ):
        self._db_path = db_path
        if record is not None:
            self.record = record
            # A resumed run still starts a fresh "running" leg — status is
            # reset even though phase/history carry forward, so a stale
            # "crashed"/"failed" status from the prior attempt doesn't linger.
            self.record.status = "running"
        else:
            self.record = ForgeRunRecord(
                run_id=run_id, project=project, project_dir=project_dir,
                task_batch=task_batch, triggered_by=triggered_by,
                timestamps={"created": _now_iso()},
            )
        self._save()

    def _save(self) -> None:
        self.record.timestamps["updated"] = _now_iso()
        save_checkpoint(OWNER_KIND, self.record.run_id, self.record.model_dump(), db_path=self._db_path)

    def mark_phase(self, phase: Phase, status: RunStatus = "running") -> None:
        self.record.phase = phase
        self.record.status = status
        self.record.timestamps[f"phase:{phase}"] = _now_iso()
        self._save()

    def mark_written(self, file_hashes: Dict[str, str]) -> None:
        """Merge a batch of freshly-hashed files into the record (not
        replace) — repeated calls across generate/repair rounds accumulate
        the full picture of what's on disk for this run."""
        self.record.file_hashes.update(file_hashes)
        self._save()

    def mark_tested(self, task_id: str, verdict: str) -> None:
        self.record.tested_verdicts[task_id] = verdict
        self._save()

    def mark_repair(self, report: dict) -> None:
        self.record.repair_reports.append(report)
        self._save()

    def mark_persisted(self, task_id: str, verdict: str) -> None:
        self.record.persisted_verdicts[task_id] = verdict
        self._save()

    def finish(self, status: RunStatus) -> None:
        self.record.phase = "finished"
        self.record.status = status
        self.record.timestamps["finished"] = _now_iso()
        self._save()


def load_run(run_id: str, *, db_path: Optional[Path] = None) -> Optional[ForgeRunRecord]:
    """Latest checkpoint state for a run_id, or None if it was never checkpointed."""
    rec = load_checkpoint(OWNER_KIND, run_id, db_path=db_path)
    if rec is None:
        return None
    return ForgeRunRecord.model_validate(rec.data)


def load_run_history(run_id: str, *, db_path: Optional[Path] = None) -> List[ForgeRunRecord]:
    """Every checkpoint snapshot ever saved for a run_id, oldest first."""
    return [ForgeRunRecord.model_validate(r.data) for r in load_checkpoints_for(OWNER_KIND, run_id, db_path=db_path)]
