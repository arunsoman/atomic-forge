"""
Write-back of forge facts (task status, produced artifacts, repair
events) — one protocol, two implementations. Bring your own (a database,
a live event stream, your CI system) by implementing `Reporter`.

`record(task, path, content_ref, verdict)` is the "always report, persist
only on pass" primitive: called every time a task's output is confirmed
(pass or fail), so a caller building durable state off of this can tell
"this task was attempted and failed" apart from "this task was never
attempted."
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import List, Optional, Protocol


class Reporter(Protocol):
    def artifact(self, task: str, path: str, kind: str, commit_sha: str = "") -> None: ...
    def status(self, task: str, status: str, meta: Optional[dict] = None) -> None: ...
    def qa_status(self, task: str, qa_status: str) -> None: ...
    def events(self, task: str, events: list[dict]) -> None: ...
    def record(self, task: str, path: str, content_ref: str, verdict: str) -> None: ...
    def name(self) -> str: ...


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class NullReporter:
    def artifact(self, task, path, kind, commit_sha=""): pass
    def status(self, task, status, meta=None): pass
    def qa_status(self, task, qa_status): pass
    def events(self, task, events): pass
    def record(self, task, path, content_ref, verdict): pass
    def name(self): return "null"


class JSONLReporter:
    """Appends one JSON line per call to `<project_dir>/.forge/reports.jsonl`
    — a durable, dependency-free record of everything forge did, readable
    by any downstream tool without forge needing to know what that tool is."""

    def __init__(self, project_dir):
        self.path = Path(project_dir) / ".forge" / "reports.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, kind: str, **fields) -> None:
        try:
            with self.path.open("a") as f:
                f.write(json.dumps({"ts": _now(), "kind": kind, **fields}, default=str) + "\n")
        except OSError as e:
            print(f"[forge] reporter WARNING: write failed: {e}", file=sys.stderr)

    def name(self):
        return "jsonl"

    def artifact(self, task, path, kind, commit_sha=""):
        self._write("artifact", task=task, path=path, artifact_kind=kind, commit_sha=commit_sha)

    def status(self, task, status, meta=None):
        self._write("status", task=task, status=status, meta=meta or {})

    def qa_status(self, task, qa_status):
        self._write("qa_status", task=task, qa_status=qa_status)

    def events(self, task, events):
        self._write("events", task=task, events=events)

    def record(self, task, path, content_ref, verdict):
        self._write("record", task=task, path=path, content_ref=content_ref, verdict=str(verdict))


class CompositeReporter:
    """Fans every Reporter call out to N other reporters — failure-isolated
    per member, so one raising reporter can never stop the rest from being
    called."""

    def __init__(self, reporters: List[Reporter]):
        self._reporters = reporters

    def name(self):
        return "+".join(r.name() for r in self._reporters)

    def _fan_out(self, method: str, *args, **kwargs) -> None:
        for r in self._reporters:
            try:
                getattr(r, method)(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — one member's failure must not stop the rest
                print(f"[forge] reporter WARNING: member {r.name()!r} failed on {method}(): {e}", file=sys.stderr)

    def artifact(self, task, path, kind, commit_sha=""):
        self._fan_out("artifact", task, path, kind, commit_sha)

    def status(self, task, status, meta=None):
        self._fan_out("status", task, status, meta)

    def qa_status(self, task, qa_status):
        self._fan_out("qa_status", task, qa_status)

    def events(self, task, events):
        self._fan_out("events", task, events)

    def record(self, task, path, content_ref, verdict):
        self._fan_out("record", task, path, content_ref, verdict)


def make_reporter(preference: str = "none", project_dir: Optional[str] = None) -> Reporter:
    """--report none|jsonl. project_dir is required for "jsonl"."""
    if preference == "jsonl":
        if project_dir is None:
            raise ValueError("make_reporter('jsonl', ...) requires project_dir")
        return JSONLReporter(project_dir)
    return NullReporter()
