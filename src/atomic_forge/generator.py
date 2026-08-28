"""
Generation-prompt building — turns one AtomicTask into the prompt handed
to the agentic generator (see generate_agent.py).
"""
from __future__ import annotations

import re

from .models import AtomicTask
from .planner import read_dependency_context

FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


def extract_code(llm_output: str) -> str:
    m = FENCE_RE.search(llm_output)
    if m:
        return m.group(1).rstrip() + "\n"
    # model ignored fencing: accept raw output if it looks like code
    return llm_output.strip() + "\n"


def build_gen_prompt(project_dir, task: AtomicTask) -> str:
    spec_parts = [
        f"# AtomicTask: {task.name}",
        f"file_path: {task.file_path}",
        f"layer: {task.layer} | action: {task.action}",
        f"\n## Description\n{task.description}",
    ]
    if task.exact_imports:
        spec_parts.append("\n## Exact imports (use these, in this form)\n" +
                          "\n".join(f"- {i}" for i in task.exact_imports))
    if task.function_signatures:
        spec_parts.append("\n## Exact function signatures (implement these verbatim)\n" +
                          "\n".join(f"- {s}" for s in task.function_signatures))
    if task.step_by_step_implementation:
        spec_parts.append("\n## Step-by-step implementation (follow in order)\n" +
                          "\n".join(f"{i + 1}. {s}" for i, s in enumerate(task.step_by_step_implementation)))
    if task.api_spec:
        spec_parts.append(
            f"\n## API contract\nendpoint: {task.api_spec.endpoint}\n"
            f"request_schema:\n{task.api_spec.request_schema}\n"
            f"response_schema:\n{task.api_spec.response_schema}\n"
            f"error_codes: {', '.join(task.api_spec.error_codes) or '(none)'}")
    if task.required_permission:
        spec_parts.append(
            f"\n## Access control\nThis file's functionality is gated on the permission "
            f"token \"{task.required_permission}\" — enforce it (e.g. a dependency/decorator "
            f"check) rather than leaving the route/action open to any caller.")
    if task.preconditions or task.postconditions:
        lines = ["\n## Story-level state contract (must hold across this task's file)"]
        if task.preconditions:
            lines.append("Preconditions:\n" + "\n".join(f"- {p}" for p in task.preconditions))
        if task.postconditions:
            lines.append("Postconditions:\n" + "\n".join(f"- {p}" for p in task.postconditions))
        spec_parts.append("\n".join(lines))
    if task.dependencies:
        spec_parts.append(f"\n## Dependencies on disk\n{read_dependency_context(project_dir, task)}")
    # Recurring failure mode: a file that locates another file on disk
    # relative to its own location via a hardcoded `Path(__file__).resolve()
    # .parents[N]` is only correct for one exact directory depth, and a
    # repair loop can't fix it after the fact from a traceback alone.
    spec_parts.append(
        "\n## Path resolution warning\nIf this file locates another file on "
        "disk relative to its own location (e.g. dynamically importing a "
        "sibling module via importlib), NEVER hardcode a "
        "`Path(__file__).resolve().parents[N]` integer — it is only correct "
        "for one exact directory depth and there is no way to repair it "
        "later if wrong. Instead walk up from `Path(__file__).resolve()` "
        "until you reach a directory whose name matches this file's own "
        "leading path component, then join the target's file_path from "
        "there. This is depth-independent and correct regardless of how "
        "deeply nested this file itself is."
    )
    spec_parts.append("\nWrite the complete file now. One fenced code block, nothing else.")
    return "\n".join(spec_parts)
