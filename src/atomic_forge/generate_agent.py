"""
Agentic generation: AtomicTask -> file, with the model driving tools.

Unlike single-shot generation, the agent can READ its dependencies on disk
(skeleton first, then windows) before writing — the "zero imagination"
contract enforced by ground truth rather than by hope. SUBMIT is gated on
the lint gate + contract checks (required signatures present).
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import patch as patch_mod
from .agent import run_agent
from .concurrency import AdaptiveConcurrencyLimiter
from .generator import build_gen_prompt, extract_code
from .llm import ChatLLM
from .models import AtomicTask, AtomicTaskBatch
from .planner import topo_layers
from .sandbox import commit, lint_gate
from .tools import ToolBackend
from .trajectory import Trajectory


@dataclass
class FailedTask:
    """One task whose own agentic generation attempt raised."""
    name: str
    file_path: str
    reason: str


@dataclass
class SkippedTask:
    """One task never attempted because a dependency it needs (directly or
    transitively) failed or was itself skipped — generating it would mean
    coding against content that never actually landed on disk, violating
    the "zero imagination" ground-truth contract."""
    name: str
    file_path: str
    reason: str


@dataclass
class BatchGenResult:
    """Outcome of `generate_batch_agentic`. One bad task must not discard
    everything else that already succeeded."""
    written: list[Path] = field(default_factory=list)
    failed: list[FailedTask] = field(default_factory=list)
    skipped: list[SkippedTask] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only if every task in the batch actually generated."""
        return not self.failed and not self.skipped


GEN_SYSTEM = """You are an autonomous code generator. You receive an AtomicTask: an exact contract for ONE file.
You may use TOOL file_skeleton / view_file / search_symbol to read your dependencies on disk — do this BEFORE writing (skeleton first; it is cheap).
For any symbol this file imports FROM ANOTHER FILE (a shared model, a db session helper, a schema class, etc.), never guess its import path — an earlier task may have put it somewhere other than where its name suggests. TOOL resolve_import with the symbol's name (and this file's own path as importing_file) returns the exact, currently-correct import statement for it.
Then output the COMPLETE file with PATCH (one fenced code block), then SUBMIT.
Rules: follow the contract exactly (imports, signatures, steps). No extra public functions. No prose outside actions."""


def _contract_check(task: AtomicTask, code: str) -> tuple[bool, str]:
    """Cheap mechanical contract check: every declared signature's def/class
    name must appear in the code."""
    missing = []
    for sig in task.function_signatures:
        m = re.search(r"(?:def|class|function|const)\s+(\w+)", sig)
        if m and not re.search(rf"\b{re.escape(m.group(1))}\b", code):
            missing.append(m.group(1))
    if missing:
        return False, f"contract violation: declared signature(s) missing from code: {missing}"
    return True, ""


def _check_required_permission(task: AtomicTask, code: str) -> list[str]:
    """Conservative presence check: a task that declares a required
    access-control token must have that literal token somewhere in the
    generated code (a guard/decorator/dependency referencing it) — this
    doesn't verify the guard is correctly WIRED, only that it wasn't
    silently dropped."""
    if not task.required_permission:
        return []
    if task.required_permission in code:
        return []
    return [f"required_permission {task.required_permission!r} does not appear anywhere in the generated file"]


def _looks_like_search_replace(patch: str) -> bool:
    return patch_mod.looks_like_search_replace(patch)


def _apply_search_replace(current: str, patch: str) -> tuple[Optional[str], str]:
    return patch_mod.apply_search_replace(current, patch)


#: Allow up to one indent level (4 cols) so class-level defs/attrs count as
#: "symbols" too, while a method BODY's local variables (indented further)
#: still don't.
_DEF_RE = re.compile(
    r"^[ \t]{0,4}(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function)\s+(\w+)",
    re.MULTILINE,
)
_DECL_RE = re.compile(r"^[ \t]{0,4}(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=", re.MULTILINE)
_ASSIGN_RE = re.compile(r"^[ \t]{0,4}(\w+)\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)


def _top_level_symbols(code: str) -> set[str]:
    return set(_DEF_RE.findall(code)) | set(_DECL_RE.findall(code)) | set(_ASSIGN_RE.findall(code))


def _regression_check(original: str, new_code: str) -> tuple[bool, str]:
    """Guard against a full-file rewrite on a "modify" task silently
    dropping pre-existing top-level functions/classes the model didn't
    re-emit. Heuristic, not a real parser, but catches the class of bug
    that matters: "declared before, silently gone after," not stylistic
    drift."""
    missing = sorted(_top_level_symbols(original) - _top_level_symbols(new_code))
    if missing:
        return False, (
            f"regression: this rewrite dropped pre-existing top-level symbol(s) {missing} "
            "that were in the file before your change. If removing them is genuinely part of "
            "this task's contract, re-include a stub or explain via a comment why it's gone; "
            "otherwise prefer SEARCH/REPLACE so untouched code can't be silently lost, or "
            "re-include them in the rewrite."
        )
    return True, ""


def generate_file_agentic(project_dir, task: AtomicTask, llm: ChatLLM,
                          tools: ToolBackend, traj: Trajectory,
                          reporter=None, max_turns: int = 20,
                          tool_manifest_text: str = "",
                          tool_manifest: Optional[list] = None,
                          write_lock: Optional[threading.Lock] = None) -> Path:
    """write_lock: serializes the write-file/reindex/commit tail below —
    required when generate_batch_agentic runs several tasks concurrently
    (git commit races if two threads commit at once; a shared in-memory
    tool-backend index isn't thread-safe either). A caller driving this
    function directly for a single task doesn't need to pass one."""
    write_lock = write_lock or threading.Lock()
    project_dir = Path(project_dir)
    target = project_dir / task.file_path
    if task.action == "delete":
        if target.exists():
            target.unlink()
        traj.log("generate_agentic", file=task.file_path, action="delete")
        return target

    can_patch_existing = task.action == "modify" and target.exists()
    prompt = build_gen_prompt(project_dir, task) + (
        "\n\nYou have tools. Read dependencies first (TOOL file_skeleton / view_file), then PATCH, then SUBMIT.\n"
        + ("This is a MODIFY task against a file that already exists — prefer SEARCH/REPLACE hunks "
           "(view the exact current lines first) over a full-file rewrite, so you never have to "
           "perfectly reproduce content you haven't read. A full-file fenced block is still accepted "
           "if you've viewed the whole file."
           if can_patch_existing else
           "This creates a new file — PATCH must be ONE fenced code block with the complete file "
           "(there is no existing content to SEARCH/REPLACE against)."))

    # Blast-radius constraint for MODIFY tasks: a "modify" task can break a
    # downstream consumer's imports/signatures just as easily as a repair
    # patch can. Only meaningful when there's an existing file with real
    # consumers to break.
    if can_patch_existing:
        try:
            affected = tools.affected_by(task.file_path, max_depth=3, direction="incoming")
            affected_files = sorted({r["file"] for r in affected.get("results", []) if r.get("file")})
        except Exception:  # noqa: BLE001 — blast-radius context is optional, never fatal
            affected_files = []
        if affected_files:
            prompt += (
                f"\n\nThis file is imported by: {', '.join(affected_files)}. Your patch "
                "must preserve all existing exports and type signatures consumed by "
                "these dependents."
            )

    holder: dict = {}

    def check(patch: str | None, _path: str | None = None) -> tuple[bool, str]:
        # `_path`: testgen always targets the one known generated-test
        # file — the `patch` tool's optional multi-file `path` argument
        # (added for repair's own check(), see repair_agent.py) has no
        # meaning here and is intentionally ignored.
        if not patch:
            return False, "SUBMIT without PATCH. Output PATCH with the complete file in one fenced block, or SEARCH/REPLACE hunks if modifying an existing file."
        if _looks_like_search_replace(patch):
            if not can_patch_existing:
                return False, ("SEARCH/REPLACE requires an existing file to patch against, but this task "
                               "creates a new file. Output ONE fenced code block with the complete file instead.")
            original = target.read_text()
            code, why = _apply_search_replace(original, patch)
            if code is None:
                return False, why
            ok, why = _regression_check(original, code)
            if not ok:
                return False, why
        else:
            code = extract_code(patch)
            if can_patch_existing:
                ok, why = _regression_check(target.read_text(), code)
                if not ok:
                    return False, why
        ok, why = lint_gate(project_dir, task.file_path, code)
        if not ok:
            return False, f"syntax gate rejected the file: {why}"
        ok, why = _contract_check(task, code)
        if not ok:
            return False, why
        violations = _check_required_permission(task, code)
        if violations:
            return False, "; ".join(violations)
        holder["code"] = code
        return True, ""

    result = run_agent(llm, tools, project_dir, GEN_SYSTEM, prompt, traj,
                       submit_check=check, max_turns=max_turns, temperature=0.0,
                       tag="gen", tool_manifest_text=tool_manifest_text,
                       tool_manifest=tool_manifest)
    if not result.success or "code" not in holder:
        raise RuntimeError(f"{task.name}: agentic generation failed ({result.abort_reason})")

    with write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(holder["code"])
        tools.reindex_file(task.file_path)
        commit(project_dir, f"forge: generate {task.file_path} (task {task.name}, agentic)")
        if reporter is not None:
            reporter.artifact(task.name, task.file_path, "source")
            reporter.status(task.id, "generated", {"attempts": 1})
            if task.task_type == "qa":
                reporter.qa_status(task.name, "qa_generated")
        traj.log("generate_agentic", file=task.file_path, turns=result.turns, result="written")
    return target


#: The throughput/reliability knee for independent, dependency-free
#: single-file generation tasks packed into one direct completion instead
#: of one agentic tool-loop session each — see `_generate_batch_direct`.
OPTIMAL_BATCH_SIZE = 16

_BATCH_TAG_EXAMPLE = (
    'Given a header `=== TASK name="Create API Service for Model Removal" '
    'action=CREATE file=frontend/src/api/modelApi.ts ===`, the correct tag is '
    "exactly `<<<FILE Create API Service for Model Removal>>>` — NOT "
    "`<<<FILE Create API Service for Model Removal (CREATE) — frontend/src/api/modelApi.ts>>>` "
    "and not any other variation. task_name is ONLY the quoted string after `name=`: "
    "copy it verbatim (spaces included, quotes excluded), never the action or file path "
    "even though they appear right next to it in the header."
)

BATCH_GEN_SYSTEM = f"""You are an autonomous code generator. You will receive MULTIPLE independent file-generation tasks in one request — each is a complete, self-contained AtomicTask with no dependency on any other task in this batch.
For EACH task, output exactly one block in this exact format (no markdown code fences inside the block, no explanation, nothing outside the blocks):

<<<FILE task_name>>>
<the complete contents of that task's file>
<<<END>>>

{_BATCH_TAG_EXAMPLE}

Output one such block per task, in the order given, covering EVERY task listed. Follow each task's exact imports, function signatures, and step-by-step implementation exactly."""

_BATCH_BLOCK_RE = re.compile(r"<<<FILE\s+(.+?)>>>\n(.*?)<<<END>>>", re.DOTALL)
_TRAILING_SINGLE_FILE_INSTRUCTION = "\nWrite the complete file now. One fenced code block, nothing else."


def _extract_batch_code(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw.rstrip() + "\n"


def _resolve_blocks(raw_blocks: dict[str, str], task_names: list[str]) -> dict[str, str]:
    """Maps raw parsed `<<<FILE tag>>>` tag strings back to real task
    names, tolerant of a model echoing more than just the bare name into
    the tag. Three tiers, most exact first: (1) tag == task_name, (2) tag
    starts with task_name, (3) task_name appears anywhere in tag. Longest
    task_name checked first within each tier so a short name that happens
    to be a substring/prefix of a longer one can't steal the longer one's
    own match."""
    resolved: dict[str, str] = {}
    remaining = dict(raw_blocks)
    ordered_names = sorted(set(task_names), key=len, reverse=True)
    tiers = (
        lambda tag, name: tag == name,
        lambda tag, name: tag.startswith(name),
        lambda tag, name: name in tag,
    )
    for tier in tiers:
        for name in ordered_names:
            if name in resolved:
                continue
            for tag, code in list(remaining.items()):
                if tier(tag, name):
                    resolved[name] = code
                    del remaining[tag]
                    break
    return resolved


def _generate_batch_direct(project_dir, tasks: list[AtomicTask], llm: ChatLLM,
                           tools: ToolBackend, traj: Trajectory, reporter=None,
                           write_lock: Optional[threading.Lock] = None,
                           ) -> tuple[dict[str, Path], list[AtomicTask]]:
    """Fast path for a batch of independent tasks (fresh files, no
    dependency): ONE direct chat completion generates every file in the
    batch instead of one agentic tool-loop session per task.

    Every returned block still goes through the exact same lint_gate /
    _contract_check validation `generate_file_agentic` applies — a task
    whose block is missing, fails to parse, or fails validation is handed
    back for the caller to run through the normal per-task agentic path
    instead. This path can only ever be as safe as the per-task path,
    never less."""
    write_lock = write_lock or threading.Lock()
    project_dir = Path(project_dir)

    prompt_parts = []
    for t in tasks:
        task_prompt = build_gen_prompt(project_dir, t).replace(_TRAILING_SINGLE_FILE_INSTRUCTION, "")
        prompt_parts.append(f'=== TASK name="{t.name}" ===\n{task_prompt}')
    user_msg = "\n\n".join(prompt_parts) + (
        f"\n\n=== END OF {len(tasks)} TASKS — output one <<<FILE task_name>>> block per task "
        "above, covering all of them, in the <<<FILE ...>>>/<<<END>>> format only. ==="
    )

    try:
        raw = llm.chat(
            [{"role": "system", "content": BATCH_GEN_SYSTEM},
             {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=32768,
        )
    except Exception as exc:  # noqa: BLE001 — a transport failure falls the WHOLE batch back
        traj.log("generate_batch_direct", result="request_failed", reason=str(exc),
                 tasks=[t.name for t in tasks])
        return {}, list(tasks)

    raw_blocks = {tag: _extract_batch_code(code) for tag, code in _BATCH_BLOCK_RE.findall(raw)}
    blocks = _resolve_blocks(raw_blocks, [t.name for t in tasks])

    written: dict[str, Path] = {}
    fallback: list[AtomicTask] = []
    for t in tasks:
        code = blocks.get(t.name)
        if code is None:
            traj.log("generate_batch_direct", task=t.name, result="fallback", reason="no block returned")
            fallback.append(t)
            continue
        ok, why = lint_gate(project_dir, t.file_path, code)
        if ok:
            ok, why = _contract_check(t, code)
        if ok:
            violations = _check_required_permission(t, code)
            if violations:
                ok, why = False, "; ".join(violations)
        if not ok:
            traj.log("generate_batch_direct", task=t.name, result="fallback", reason=why)
            fallback.append(t)
            continue
        target = project_dir / t.file_path
        with write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code)
            tools.reindex_file(t.file_path)
            commit(project_dir, f"forge: generate {t.file_path} (task {t.name}, batched)")
            if reporter is not None:
                reporter.artifact(t.name, t.file_path, "source")
                reporter.status(t.id, "generated", {"attempts": 1})
                if t.task_type == "qa":
                    reporter.qa_status(t.name, "qa_generated")
            traj.log("generate_batch_direct", file=t.file_path, task=t.name, result="written")
        written[t.name] = target

    return written, fallback


def generate_batch_agentic(project_dir, batch: AtomicTaskBatch, llm: ChatLLM,
                           tools: ToolBackend, traj: Trajectory, reporter=None,
                           tool_manifest_text: str = "",
                           tool_manifest: Optional[list] = None,
                           max_concurrency: int = 8) -> BatchGenResult:
    """Generate every task, layer by layer in dependency order, running
    each layer's independent tasks concurrently — but never let one task's
    failure discard the rest of the batch.

    A task that raises is recorded in `.failed` and generation moves on.
    Any OTHER task in the batch that depends — directly, or transitively
    through a chain of failed/skipped tasks — on a failed task's file_path
    is recorded in `.skipped` rather than attempted.

    `topo_layers` groups tasks into waves where nothing in a layer depends
    on anything else in that same layer, so a layer's tasks can run
    concurrently and only layer-to-layer needs to happen in order. Within
    a layer, concurrency is bounded by an `AdaptiveConcurrencyLimiter`:
    starts at 2, ramps up by 1 per task that completes without hitting a
    rate limit (up to `max_concurrency`), and steps down by 2 (floor 1)
    the instant the shared `llm` reports a 429.
    """
    result = BatchGenResult()
    #: file_path -> (root-cause task name, root-cause reason). Populated
    #: by both failures and skips, so a chain of dependents-of-dependents
    #: all resolve back to the ORIGINAL failure.
    unavailable: dict[str, tuple[str, str]] = {}
    result_lock = threading.Lock()
    write_lock = threading.Lock()
    ceiling = max(1, max_concurrency)
    limiter = AdaptiveConcurrencyLimiter(start=min(2, ceiling), ceiling=ceiling)

    if hasattr(llm, "on_rate_limited"):
        llm.on_rate_limited = limiter.record_rate_limited

    def _run_one(task: AtomicTask):
        limiter.acquire()
        events_before = limiter.rate_limit_events
        try:
            path = generate_file_agentic(project_dir, task, llm, tools, traj, reporter=reporter,
                                         tool_manifest_text=tool_manifest_text,
                                         tool_manifest=tool_manifest, write_lock=write_lock)
            return task, path, None
        except Exception as exc:  # noqa: BLE001 — one task's failure must not kill the batch
            return task, None, exc
        finally:
            if limiter.rate_limit_events == events_before:
                limiter.record_success()
            limiter.release()

    def _run_batch_chunk(chunk: list[AtomicTask]):
        limiter.acquire()
        events_before = limiter.rate_limit_events
        try:
            written, fallback = _generate_batch_direct(project_dir, chunk, llm, tools, traj,
                                                        reporter=reporter, write_lock=write_lock)
            return written, fallback, None
        except Exception as exc:  # noqa: BLE001 — a batch-direct crash falls the WHOLE chunk back
            return {}, chunk, exc
        finally:
            if limiter.rate_limit_events == events_before:
                limiter.record_success()
            limiter.release()

    for layer in topo_layers(batch):
        runnable: list[AtomicTask] = []
        for task in layer:
            blocking_path = None
            if task.file_path in unavailable:
                blocking_path = task.file_path
            else:
                for dep in task.dependencies:
                    if dep in unavailable:
                        blocking_path = dep
                        break

            if blocking_path is not None:
                root_name, root_reason = unavailable[blocking_path]
                reason = (
                    f"skipped: depends on '{blocking_path}', which never generated "
                    f"(task '{root_name}' failed: {root_reason})"
                )
                result.skipped.append(SkippedTask(name=task.name, file_path=task.file_path, reason=reason))
                unavailable.setdefault(task.file_path, (root_name, root_reason))
                traj.log("generate_agentic", file=task.file_path, task=task.name,
                         result="skipped", reason=reason)
                continue
            runnable.append(task)

        if not runnable:
            continue

        batchable = [t for t in runnable if t.action == "create" and not t.dependencies]
        batchable_names = {t.name for t in batchable}
        individual = [t for t in runnable if t.name not in batchable_names]

        chunks = [batchable[i:i + OPTIMAL_BATCH_SIZE] for i in range(0, len(batchable), OPTIMAL_BATCH_SIZE)]
        if chunks:
            with ThreadPoolExecutor(max_workers=min(len(chunks), max(1, max_concurrency))) as pool:
                futures = [pool.submit(_run_batch_chunk, chunk) for chunk in chunks]
                for future in as_completed(futures):
                    written, fallback, exc = future.result()
                    with result_lock:
                        for path in written.values():
                            result.written.append(path)
                        if exc is not None:
                            traj.log("generate_agentic", result="batch_chunk_error", reason=str(exc))
                        individual.extend(fallback)

        if not individual:
            continue

        with ThreadPoolExecutor(max_workers=min(len(individual), max(1, max_concurrency))) as pool:
            futures = [pool.submit(_run_one, task) for task in individual]
            for future in as_completed(futures):
                task, path, exc = future.result()
                with result_lock:
                    if exc is not None:
                        reason = str(exc)
                        result.failed.append(FailedTask(name=task.name, file_path=task.file_path, reason=reason))
                        unavailable[task.file_path] = (task.name, reason)
                        if task.task_type == "qa" and reporter is not None:
                            reporter.qa_status(task.name, "qa_failed")
                        traj.log("generate_agentic", file=task.file_path, task=task.name,
                                 result="failed", reason=reason)
                    else:
                        result.written.append(path)

    return result
