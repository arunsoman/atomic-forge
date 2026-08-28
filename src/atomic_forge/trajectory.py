"""
Append-only trajectory log (JSONL).

Every LLM call, file write, test run and repair attempt is one event.
This is the audit trail for debugging runs — and later, the raw material
for fine-tuning data (flat, replayable trajectories).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict


class Trajectory:
    def __init__(self, project_dir):
        self.path = Path(project_dir) / ".forge" / "trajectory.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = time.time()

    def log(self, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {"t": round(time.time() - self._t0, 3), "event": event, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
