"""Load an AtomicTaskBatch from JSON — the simplest possible bridge from
"however you planned your tasks" into forge's pipeline."""
from __future__ import annotations

import json
from pathlib import Path

from .models import AtomicTaskBatch


def load_batch_json(path: str | Path) -> AtomicTaskBatch:
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        data = {"tasks": data}
    return AtomicTaskBatch.model_validate(data)
