"""
decompose.py — an optional, LLM-assisted on-ramp into the `AtomicTask`
contract (models.py).

This does **not** weaken the contract: `AtomicTask`'s pydantic validator
(`_enforce_contract`) still runs, unchanged, on whatever this module
produces. What this module does is take the friction out of *authoring*
that contract by hand — you hand it a loose natural-language spec/issue,
it asks the model to draft `AtomicTask` JSON (including a proposed
`test_triad`), and every draft is validated the same way a hand-written
one would be. A draft that fails validation is reported as a rejected
draft with the exact reason, never silently coerced into something that
merely looks like an `AtomicTask`.

The output is explicitly a **draft** — see `DecomposeResult.tasks`
(valid, contract-enforced `AtomicTask`s) vs. `DecomposeResult.rejected`
(raw dicts + validation error, for a human to fix and re-run). Nothing
here writes straight into `generate`/`repair` — that hand-off is still a
human decision, made by running `atomic-forge run` against the reviewed
output file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from pydantic import ValidationError

from .llm import ChatLLM
from .models import AtomicTask, AtomicTaskBatch

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

SYSTEM_PROMPT = """You decompose a natural-language spec/issue into a JSON array of \
AtomicTask objects for the atomic-forge pipeline. Rules:

- Each task touches exactly ONE file (task_type "dev" tasks only — never \
propose "qa" tasks yourself, forge's own qa phase synthesizes tests from \
your test_triad).
- Prefer small, single-responsibility files over one large file.
- Every dev task MUST include a test_triad with three keys: "positive" \
(happy path), "negative" (a failure case), "negative_to_positive" \
(recovery: the failure case fixed, succeeding). All three are plain \
English assertions, not code.
- "action" is "create" unless the spec clearly describes modifying an \
existing file, then "modify".
- "dependencies" lists other file_paths (from this same batch) this task's \
file imports from or otherwise depends on — this drives generation order.
- Output ONLY a JSON array (optionally inside a ```json fence), no prose \
before or after. Each element has EXACTLY these keys:
  name, task_type, action, file_path, layer, description, \
exact_imports, function_signatures, step_by_step_implementation, \
dependencies, test_triad {positive, negative, negative_to_positive}

Example element:
{
  "name": "create slugify util",
  "task_type": "dev",
  "action": "create",
  "file_path": "string_utils.py",
  "layer": "Backend",
  "description": "A slugify(text) function that lowercases and hyphenates.",
  "exact_imports": [],
  "function_signatures": ["def slugify(text: str) -> str"],
  "step_by_step_implementation": ["Lowercase input", "Strip punctuation", "Join words with hyphens"],
  "dependencies": [],
  "test_triad": {
    "positive": "slugify('Hello, World!') == 'hello-world'",
    "negative": "slugify(None) raises TypeError",
    "negative_to_positive": "slugify('Hello') after the TypeError still returns 'hello'"
  }
}
"""


@dataclass
class RejectedDraft:
    raw: Dict[str, Any]
    error: str


@dataclass
class DecomposeResult:
    tasks: List[AtomicTask] = field(default_factory=list)
    rejected: List[RejectedDraft] = field(default_factory=list)
    raw_response: str = ""

    def batch(self) -> AtomicTaskBatch:
        """Only the tasks that passed the real AtomicTask contract."""
        return AtomicTaskBatch(tasks=self.tasks)

    def summary(self) -> str:
        return f"{len(self.tasks)} draft task(s) validated, {len(self.rejected)} rejected"


def _extract_json_array(llm_output: str) -> Any:
    m = FENCE_RE.search(llm_output)
    text = m.group(1) if m else llm_output
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"decompose: model output was not valid JSON ({e}); "
            f"first 200 chars: {text[:200]!r}"
        ) from e


def build_decompose_prompt(spec_text: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Spec:\n\n{spec_text}"},
    ]


def decompose_spec(spec_text: str, llm: ChatLLM, *, max_tokens: int = 8192) -> DecomposeResult:
    """Drafts an AtomicTaskBatch from a loose spec. Never raises on a bad
    individual draft — each element is validated independently, a failure
    is recorded in `.rejected` with the real pydantic error, and decompose
    keeps going. Only raises if the model's output isn't parseable JSON at
    all (nothing to salvage per-element in that case)."""
    messages = build_decompose_prompt(spec_text)
    raw_response = llm.chat(messages, temperature=0.2, max_tokens=max_tokens)
    drafts = _extract_json_array(raw_response)
    if not isinstance(drafts, list):
        raise ValueError(f"decompose: expected a JSON array of tasks, got {type(drafts).__name__}")

    result = DecomposeResult(raw_response=raw_response)
    for draft in drafts:
        if not isinstance(draft, dict):
            result.rejected.append(RejectedDraft(raw={"_value": draft}, error="element is not a JSON object"))
            continue
        try:
            result.tasks.append(AtomicTask.model_validate(draft))
        except ValidationError as e:
            result.rejected.append(RejectedDraft(raw=draft, error=str(e)))
    return result


def write_draft_json(result: DecomposeResult, out_path: str | Path) -> Path:
    """Writes the validated tasks as an AtomicTaskBatch-shaped JSON file,
    ready for human review and edits — NOT auto-fed into `generate`. If
    anything was rejected, a sibling `<out>.rejected.json` is written too
    so nothing silently disappears."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tasks": [json.loads(t.model_dump_json()) for t in result.tasks]}
    out.write_text(json.dumps(payload, indent=2) + "\n")
    if result.rejected:
        rejected_path = out.with_suffix(out.suffix + ".rejected.json")
        rejected_path.write_text(json.dumps(
            [{"error": r.error, "raw": r.raw} for r in result.rejected], indent=2) + "\n")
    return out
