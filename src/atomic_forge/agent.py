"""
The agentic session loop — the spine of both generation and repair.

Action grammar (one action per model turn; the model waits for OBSERVATION):

    TOOL <name> <json-args>        call one of the tools listed in this run's
                                   manifest (see below — NOT a hardcoded list)
    RUN <shell command>            execute in the project dir (bounded output)
    PATCH                          followed by SEARCH/REPLACE blocks or ONE fenced full file
    SUBMIT                         finish; the phase's submit-check validates and accepts
                                   or REJECTS with feedback and the loop continues

Guardrails (mini-SWE-agent/SWE-agent lessons):
  - every observation truncated before entering context
  - stuck detection: identical action 3x -> nudge; 5x -> abort
  - consecutive-error cap; hard turn budget; temperature per attempt (sampling)

Tool discovery: `AGENT_GRAMMAR` below deliberately does not enumerate
specific tool names — a model given both a hardcoded list AND "you could
call discover() instead" will always use the free list (no discovery call
needed), silently defeating runtime discovery. The actual tools available
for THIS run are looked up once, at run initialization
(`ToolBackend.describe()`, cacheable in your own checkpoint if you want
resume to reuse it), rendered via `render_tool_manifest()` below, and
passed into `run_agent(..., tool_manifest_text=...)` as read-only context.
The agent never calls `describe()` itself mid-loop — that would both
defeat the "no free list to prefer" fix and burn turns on tool
introspection instead of real work.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .llm import ChatLLM
from .sandbox import run as sandbox_run
from .sandbox import truncate
from .tools import ToolBackend
from .trajectory import Trajectory

OBS_LIMIT = 4000

#: Confirmed live (2026-08-29 benchmark run): a session whose SUBMIT
#: keeps getting rejected (syntax gate, "patch does not apply", failing
#: tests) but never aborts early runs to the full turn budget anyway —
#: one dead-end sample burned all 25 turns after its 2nd rejected SUBMIT
#: (both from a SEARCH block using tabs against a space-indented file),
#: dominating that run's token cost. A rejected SUBMIT is a completed,
#: unambiguous "didn't work" signal — no need for domain knowledge of
#: *why* (syntax gate vs. failing tests vs. anything else) to know that
#: 3 of them in one session means the remaining budget has low expected
#: value. Kept separate from `action_counts`' "identical action 5x" stuck
#: detector, which never fires here since each rejected attempt is a
#: different patch.
REJECTED_SUBMIT_LIMIT = 3

AGENT_GRAMMAR = """You operate ONE action per response. After each action you receive an OBSERVATION. Then act again.

You have NO fixed tool set. The tools available for THIS run are listed below, under
"AVAILABLE TOOLS" — established once at run start. Use only these; do not assume any
other tool exists, and do not try to discover more (there is nothing further to discover
mid-run).

ACTIONS (exact formats):
  TOOL <name> <json-args>                                          — call one of the tools
                                                                     listed under AVAILABLE TOOLS
      e.g. TOOL view_file {"path": "src/x.py", "start": 1, "end": 100}
  RUN python -m pytest -q tests/test_x.py                         — run shell in the project dir (bounded output)
  PATCH                                                            — then SEARCH/REPLACE block(s):
      <<<<<<< SEARCH
      <exact current lines, must match UNIQUELY>
      =======
      <replacement>
      >>>>>>> REPLACE
    or ONE fenced code block with the COMPLETE file (rewrite fallback).
  SUBMIT                                                           — finish; validated before acceptance

RULES:
- Exactly ONE action per response. No prose outside actions.
- Read before you patch: SEARCH blocks must match the CURRENT file exactly.
- Prefer a file-skeleton-shaped tool before a full-file-view-shaped one on files you
  haven't seen, if both are available — it is far cheaper.
- If an observation says "hint:", follow it.
"""


#: System-prompt addendum for the real function-calling path
#: (`run_agent(..., tool_manifest=...)` with a `ChatLLM` that implements
#: `chat_with_tools`). `patch`/`submit`/`run_shell` (defined below,
#: `_SPECIAL_FC_TOOLS`) are now REAL callable functions too, not a
#: separate free-text convention — confirmed live as a real, if smaller,
#: source of wasted turns: a model given real function-calling naturally reaches for a
#: function call for EVERY action, including PATCH/RUN/SUBMIT, and kept
#: trying to call them as tools even when told to write them as plain
#: text instead — "unknown tool 'PATCH'" errors that then needed a whole
#: extra turn to recover from. Offering them as real tools removes that
#: whole class of friction, on top of the (larger, first-confirmed) fix
#: of moving inspection/query tools off the old free-text "TOOL <name>
#: {json}" line onto the API's own structured tool-calling.
FUNCTION_CALLING_GRAMMAR = """You have NO fixed tool set beyond what's offered to you as callable functions this
turn. Investigate using the read-only tools (view a file, search a symbol, run a query,
...), then call `patch` with your fix, then call `submit` to finish. You may call more
than one function in a turn if that's useful (e.g. patch immediately followed by submit).

RULES:
- Read before you patch: `patch`'s SEARCH blocks must match the CURRENT file exactly,
  including every character on every line — do NOT include any line-number prefix a
  file-viewing tool's result shows you; that numbering is for your reference only, never
  part of the file's real content.
- If a tool result says "hint:", follow it.
"""

#: The three actions that used to be a separate free-text convention
#: (PATCH/RUN/SUBMIT) — now real function-calling tools alongside
#: whatever `manifest_to_openai_tools` offers, so the model has exactly
#: ONE mechanism (real tool_calls) for every action instead of two.
_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "patch",
        "description": (
            "Stage your fix (validated only when you call submit next). `content` must be "
            "either one or more SEARCH/REPLACE hunks:\n"
            "<<<<<<< SEARCH\n<exact current lines, must match UNIQUELY>\n=======\n"
            "<replacement>\n>>>>>>> REPLACE\n"
            "or ONE fenced code block with the file's COMPLETE content (rewrite fallback, "
            "e.g. when the file doesn't exist yet). SEARCH must match the real file "
            "character-for-character — never include a line-number prefix a file-viewing "
            "tool showed you, that's for your reference only.\n"
            "`path` is OPTIONAL: omit it to patch the file you were already pointed at. "
            "Set it only if your own investigation shows the real fix belongs in a "
            "DIFFERENT file — SEARCH is then matched against THAT file's real content, "
            "not the one you started from."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "path": {"type": "string", "description": (
                    "Optional. Repo-relative path to patch, if different from the file "
                    "you were originally pointed at.")},
            },
            "required": ["content"],
        },
    },
}
_SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": (
            "Finish this session. Validates the most recently staged `patch` call and "
            "either ends the session (accepted) or returns why it was rejected so you can "
            "`patch` again and retry."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}
_RUN_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a shell command in the project directory (bounded output), e.g. to run tests.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
}
_SPECIAL_FC_TOOLS = (_PATCH_TOOL, _SUBMIT_TOOL, _RUN_SHELL_TOOL)
_SPECIAL_FC_TOOL_NAMES = {t["function"]["name"] for t in _SPECIAL_FC_TOOLS}


def _annotation_type_name(node: Optional[ast.expr]) -> str:
    """One signature-string annotation node -> its bare type name.

    Every `describe()` signature comes from `str(inspect.signature(...))`
    under `from __future__ import annotations` (PEP 563) — so an
    annotation like `path: str` is captured as the STRING literal `'str'`
    in the signature text, which `ast.parse` then sees as a plain
    `Constant` string node (not a real `str` type reference). This just
    unwraps that constant; callers map the resulting name
    ("str"/"int"/"Optional[list]"/...) to a JSON-schema type."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - best-effort; falls back to "" (-> string type)
        return ""


_JSON_SCHEMA_TYPE_BY_NAME = {
    "str": "string", "int": "integer", "float": "number", "bool": "boolean",
    "dict": "object", "list": "array", "tuple": "array",
}


def _json_schema_type(type_name: str) -> str:
    """"str" -> "string", "Optional[int]"/"List[str]" -> unwrap the outer
    generic and recurse once, anything unrecognized -> "string" (a
    permissive fallback: the underlying tool call itself still validates
    real argument types, so a slightly-loose JSON schema here costs
    nothing but a weaker hint to the model — never a hard failure)."""
    type_name = type_name.strip()
    m = re.match(r"^(?:Optional|List|list|Dict|dict|Sequence)\[(.+)\]$", type_name)
    if m:
        inner = m.group(1).split(",")[0].strip()
        return _json_schema_type(inner) if "Optional" in type_name else (
            "array" if type_name.lower().startswith(("list", "sequence")) else
            "object" if type_name.lower().startswith("dict") else _json_schema_type(inner)
        )
    return _JSON_SCHEMA_TYPE_BY_NAME.get(type_name, "string")


def _signature_to_parameters_schema(signature: str) -> dict:
    """`str(inspect.signature(bound_method))` -> an OpenAI function-calling
    `parameters` JSON schema. Parses via `ast` (wrapping the signature
    text as a bare `def f(...): pass`) rather than regex — signatures
    carry arbitrary nested generic annotations (`Optional[dict]`,
    `List[str]`, ...) that a hand-rolled regex would only ever partially
    cover; a real parser handles anything syntactically valid Python
    accepts. `self` is never present (these signatures come from BOUND
    methods). Falls back to a schema-less `{"type": "object"}` (any
    object accepted) for anything unparseable, rather than raising —
    describe()'s own contract already promises "(...)" for a signature
    `inspect.signature` itself couldn't stringify."""
    try:
        tree = ast.parse(f"def f{signature}: pass")
    except SyntaxError:
        return {"type": "object", "properties": {}}
    fn_args = tree.body[0].args
    properties: dict[str, dict] = {}
    required: list[str] = []
    all_args = [(a, False) for a in fn_args.args] + [(a, True) for a in fn_args.kwonlyargs]
    all_defaults = (
        [None] * (len(fn_args.args) - len(fn_args.defaults)) + list(fn_args.defaults)
        + [None] * (len(fn_args.kwonlyargs) - len(fn_args.kw_defaults))
        + list(fn_args.kw_defaults)
    )
    for (arg, _is_kwonly), default in zip(all_args, all_defaults):
        if arg.arg == "self":
            continue
        type_name = _annotation_type_name(arg.annotation)
        properties[arg.arg] = {"type": _json_schema_type(type_name)}
        # `default` is the Python sentinel `None` only for the zip-padding
        # entries representing "no default supplied at all" (true
        # required params) — an explicit `= None` default in the source
        # (`x: 'Optional[dict]' = None`) parses to an `ast.Constant`
        # object, which is never `is None`, so it's correctly NOT marked
        # required here.
        if default is None:
            required.append(arg.arg)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _is_read_only_tool(name: str) -> bool:
    """Gate on what a repair/generation agent may call directly via
    function-calling — pattern-matched on name shape rather than an
    enumerated list of ~121 (and growing) tool names, mirroring
    `cie.tools.ToolService.describe()`'s own "deliberately NOT a
    hand-maintained list" philosophy one level up.

    File mutation only ever happens through PATCH + `submit_check`'s own
    lint/apply gate (see this module's top docstring) — a model calling
    `write_file`/`edit_file`/`delete_file` directly would silently bypass
    that gate entirely. Job-triggering `*_run`-suffixed tools
    (`community_detect_run`, `sync_promote`, ...) and admin/lifecycle
    tools (`start_watch`, `install_git_hook`, ...) are expensive,
    stateful side effects meant for periodic/explicit invocation
    elsewhere in the pipeline, not something this agent should ever
    decide to fire ad hoc mid-turn. `run`/`run_tests` are excluded only
    because the plain-text `RUN <cmd>` action (see `FUNCTION_CALLING_
    GRAMMAR`) already covers that need — offering both would just be two
    ways to do the same thing."""
    if name in {"run", "run_tests", "write_file", "edit_file", "delete_file"}:
        return False
    if name.endswith("_run"):
        return False
    if name.startswith(("start_", "stop_", "install_", "sync_", "record_",
                        "configure_", "promote_", "reindex", "inject_", "strip_")):
        return False
    return True


def manifest_to_openai_tools(manifest: list[dict]) -> list[dict]:
    """A `describe()`-shaped manifest -> the `tools=` list `chat_with_tools`
    passes straight to the OpenAI-compatible API. One real function
    definition per READ-ONLY tool (`_is_read_only_tool`) — `parameters`
    auto-derived from the same signature string `render_tool_manifest`
    renders as prose, so the two never drift out of sync with each other
    or with `describe()` itself."""
    out = []
    for entry in manifest:
        name = entry.get("name")
        if not name or not _is_read_only_tool(name):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": entry.get("doc", "") or f"Call the {name} tool.",
                "parameters": _signature_to_parameters_schema(entry.get("signature", "()")),
            },
        })
    return out


def _execute_manifest_tool(tools: ToolBackend, name: str, arguments_json: str) -> str:
    """Like `_execute_tool` below, but for the real function-calling path:
    `name`/`arguments_json` arrive already separated (the API/SDK parsed
    them, not forge's own regex), so there's no wrapper envelope to
    re-parse and no free-text tool-name typo class to guard against — an
    unknown name here means the model called something outside its own
    offered tool list, not a guess at a name it invented from scratch.

    SF25: `_is_read_only_tool` is enforced HERE too, not just inside
    `manifest_to_openai_tools`'s offer-time filter — that filter only
    controls what's OFFERED to the model; nothing previously stopped a
    tool_call naming an out-of-manifest write tool (model hallucination,
    prompt injection, or a provider that doesn't strictly enforce the
    offered list) from actually being EXECUTED. File mutation must only
    ever happen through PATCH + `submit_check`'s lint/apply gate. Checked
    only AFTER confirming the name resolves to a real ToolBackend method —
    a genuinely unknown name (typo, hallucinated shell-like name) must
    still hit the "ERROR: unknown tool" branch below so callers matching
    on that exact prefix (run_agent's trajectory logging) keep working."""
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
        fn = getattr(tools, name, None)
        if fn is None:
            return f"ERROR: unknown tool '{name}'."
        if not _is_read_only_tool(name):
            return (f"ERROR: '{name}' is not callable directly (write/lifecycle tools are "
                    "blocked here). Use PATCH to stage a file change; it will be validated "
                    "at SUBMIT.")
        return json.dumps(fn(**args), indent=1, default=str)
    except json.JSONDecodeError as e:
        return f"ERROR: malformed tool arguments ({e})."
    except TypeError as e:
        return f"ERROR: bad arguments ({e})"
    except Exception as e:  # noqa: BLE001 - surfaced to the model as an observation, not raised
        return f"ERROR: tool failed: {e}"


def _handle_patch_call(arguments_json: str) -> tuple[str, bool, Optional[str], Optional[str]]:
    """Dispatch for the `patch` synthetic tool (`_PATCH_TOOL`). Returns
    (result_text_for_the_model, is_error, new_last_patch_or_None,
    new_last_patch_path_or_None) — mirrors the old text-grammar path's
    "PATCH" handling (parse.py's own empty-payload guard) so behavior
    stays identical, just reached via a real tool_call's `content`
    argument instead of everything after a bare "PATCH" line.

    `path`: optional, confirmed live on astroid#3257 (2026-08-30) — a
    sample's own investigation can correctly determine the real fix
    belongs in a file OTHER than the one it was pointed at, and previously
    had no way to say so: the patch gate always validated against the
    originally-assigned suspect's content, so a genuinely correct patch
    for a different file was rejected every time as "SEARCH block not
    found" (it was being diffed against the wrong file entirely)."""
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"ERROR: malformed patch arguments ({e}).", True, None, None
    content = (args.get("content") or "").strip()
    if not content:
        return (
            "ERROR: patch content must not be empty — call patch again with either "
            "SEARCH/REPLACE block(s) or one fenced code block with the complete file.",
            True, None, None,
        )
    path = (args.get("path") or "").strip() or None
    return (
        "Patch recorded. It will be validated when you call submit. If you are "
        "confident, submit now; otherwise keep investigating first.",
        False, content, path,
    )


def _handle_run_shell_call(arguments_json: str, project_dir) -> tuple[str, bool]:
    """Dispatch for the `run_shell` synthetic tool (`_RUN_SHELL_TOOL`) —
    same `sandbox_run` call the old text-grammar "RUN <cmd>" action used."""
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"ERROR: malformed run_shell arguments ({e}).", True
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return "ERROR: run_shell needs a non-empty 'cmd'.", True
    res = sandbox_run(cmd, cwd=project_dir, timeout=120)
    return f"exit_code={res.exit_code}\n{res.output}", False


def render_tool_manifest(manifest: list[dict]) -> str:
    """Renders a `describe()`-shaped manifest (`[{"name", "signature",
    "doc"}, ...]`) into the "AVAILABLE TOOLS" context block appended to
    the agent's system prompt. Returns "" for an empty/missing manifest —
    a caller that hasn't wired discovery through yet (or a backend whose
    `describe()` returned nothing) gets a byte-identical prompt to before
    this workstream existed, just without the AVAILABLE TOOLS section.

    SF25: only advertises READ-ONLY tools (`_is_read_only_tool`), matching
    `manifest_to_openai_tools`'s own filter for the function-calling path
    — a model should never be told a write/lifecycle tool is callable via
    the free-text "TOOL <name> {json}" convention in the first place, on
    top of `_execute_tool` now rejecting one even if it tries anyway."""
    manifest = [entry for entry in manifest if _is_read_only_tool(entry.get("name", ""))]
    if not manifest:
        return ""
    lines = ["AVAILABLE TOOLS (this run only — established once at start):"]
    for entry in manifest:
        name = entry.get("name", "?")
        sig = entry.get("signature", "")
        doc = entry.get("doc", "")
        lines.append(f"  {name}{sig}" + (f"  — {doc}" if doc else ""))
    return "\n".join(lines)


@dataclass
class AgentResult:
    success: bool
    turns: int
    final_patch: Optional[str] = None     # last PATCH payload (S/R or fenced code)
    abort_reason: str = ""
    messages: list = field(default_factory=list)


def parse_action(output: str) -> tuple[str, str]:
    """Extract the single action from a model turn. Returns (kind, payload)."""
    for line in output.splitlines():
        s = line.strip()
        if s == "SUBMIT":
            return "submit", ""
        if s == "PATCH":
            return "patch", output[output.index("PATCH") + len("PATCH"):].strip()
        if s.startswith("TOOL "):
            rest = s[5:].strip()
            name, _, argstr = rest.partition(" ")
            return "tool", json.dumps({"name": name.strip(), "args": argstr.strip() or "{}"})
        if s.startswith("RUN "):
            return "run", s[4:].strip()
    m = re.search(r"```bash\n(.*?)```", output, re.DOTALL)
    if m:
        return "run", m.group(1).strip()
    return "unknown", output.strip()[:400]


def _execute_tool(tools: ToolBackend, payload: str) -> str:
    try:
        spec = json.loads(payload)
        name, args = spec["name"], spec.get("args", "{}")
        args = json.loads(args) if isinstance(args, str) else args
        fn = getattr(tools, name, None)
        if fn is None:
            hint = ""
            if name.strip().lower() in {"run", "bash", "shell", "exec", "execute", "cmd"}:
                hint = (" Note: shell execution is NOT a tool call — there is no tool "
                        f"named '{name}'. Use the standalone 'RUN <command>' action instead "
                        "(no TOOL wrapper, no JSON args), e.g. RUN npx tsc --noEmit src/x.ts")
            return (f"ERROR: unknown tool '{name}'. Use only a tool named in this run's "
                     "AVAILABLE TOOLS list above." + hint)
        # SF25: same write-safety allowlist the function-calling path
        # enforces (`_execute_manifest_tool`) — the text-grammar "TOOL
        # <name> {json}" dispatcher had no allowlist at all, so a model
        # could call write_file/edit_file/delete_file (or any other
        # ToolBackend method) directly, completely bypassing the
        # lint_gate/_contract_check/_regression_check validation PATCH+
        # SUBMIT's submit_check enforces. File mutation must only ever
        # happen through PATCH + submit_check. Checked only after
        # confirming `name` resolves to a real method, so a genuinely
        # unknown/hallucinated name still hits the branch above (whose
        # exact "ERROR: unknown tool" prefix run_agent's trajectory
        # logging matches on).
        if not _is_read_only_tool(name):
            return (f"ERROR: '{name}' is not callable directly (write/lifecycle tools are "
                    "blocked here). Use PATCH to stage a file change; it will be validated "
                    "at SUBMIT.")
        return json.dumps(fn(**args), indent=1, default=str)
    except json.JSONDecodeError as e:
        return f"ERROR: malformed tool args ({e}). Format: TOOL <name> {{\"arg\": \"value\"}}"
    except TypeError as e:
        return f"ERROR: bad args ({e})"
    except Exception as e:
        return f"ERROR: tool failed: {e}"


def run_agent(llm: ChatLLM, tools: ToolBackend, project_dir, system: str,
              task_prompt: str, traj: Trajectory,
              submit_check: Optional[Callable[[Optional[str], Optional[str]], tuple[bool, str]]] = None,
              max_turns: int = 25, temperature: float = 0.0,
              tag: str = "agent", tool_manifest_text: str = "",
              tool_manifest: Optional[list] = None) -> AgentResult:
    """Drive one agentic session to completion (SUBMIT-accepted) or abort.

    submit_check(last_patch, last_patch_path) -> (accepted, feedback):
    called on every SUBMIT. `last_patch_path` is the repo-relative path
    the model's `patch` call named via its optional `path` argument, or
    None if it didn't (the common case — the caller decides what "None"
    means, usually "whatever file this session was originally pointed
    at"). A caller that only ever targets one known file (testgen) can
    ignore the second argument entirely. Rejection feedback is fed back
    and the session continues within budget.

    tool_manifest_text: rendered once via `render_tool_manifest()` from a
    manifest discovered ONCE at run initialization (docs/forge-rebuild-
    plan.md's Phase 2 / WS3) — appended to the system prompt as read-only
    context, not re-discovered here. Defaults to "" (no AVAILABLE TOOLS
    section), which is the byte-identical prompt every existing caller
    got before this workstream existed.

    tool_manifest: the SAME manifest `tool_manifest_text` was rendered
    from (`describe()`'s raw `[{"name","signature","doc"}, ...]`), used
    for real OpenAI-spec function-calling instead of the free-text "TOOL
    <name> {json}" convention — but ONLY when both this is non-empty AND
    `llm` implements `chat_with_tools` (checked via `hasattr`, not a
    required part of `ChatLLM` — see llm.py's `ToolCallingChatLLM`
    docstring). Every other combination (no manifest, or a `ChatLLM` that
    only implements plain `.chat()`, e.g. every test double in
    `tests/forge/conftest.py`) falls straight through to the original
    text-grammar path, byte-for-byte unchanged. See `FUNCTION_CALLING_
    GRAMMAR`'s docstring — PATCH/RUN/SUBMIT are now real function-calling
    tools too (`_SPECIAL_FC_TOOLS`), not a separate free-text convention,
    so the model has exactly ONE mechanism for every action in this path.
    """
    use_fc = bool(tool_manifest) and hasattr(llm, "chat_with_tools")
    openai_tools = manifest_to_openai_tools(tool_manifest) + list(_SPECIAL_FC_TOOLS) if use_fc else []
    if use_fc:
        grammar = FUNCTION_CALLING_GRAMMAR
    else:
        grammar = AGENT_GRAMMAR + (f"\n{tool_manifest_text}\n" if tool_manifest_text else "")
    messages = [{"role": "system", "content": system + "\n\n" + grammar},
                {"role": "user", "content": task_prompt}]
    last_patch: Optional[str] = None
    #: repo-relative path the LAST patch call targeted, if the model gave
    #: one — None means "the file this session was originally pointed
    #: at," preserving old behavior for every caller that never uses it
    #: (testgen; any future submit_check that ignores its 2nd argument).
    last_patch_path: Optional[str] = None
    action_counts: dict[str, int] = {}
    consecutive_errors = 0
    rejected_submits = 0
    # Turns since the last non-empty PATCH — NOT reset by a SUBMIT
    # rejection, so a model that goes back to pure TOOL/RUN exploration
    # after being rejected (instead of revising and re-submitting) keeps
    # accumulating here too. Confirmed live (2026-07-31, procrastination-
    # station): this was the dominant cause of "turn budget exhausted"
    # failures — sessions that reached SUBMIT once, got rejected, and then
    # spent the rest of the budget on view_file/search_symbol without ever
    # emitting another PATCH.
    turns_since_patch = 0

    for turn in range(1, max_turns + 1):
        if use_fc:
            # Confirmed same class of bug as testgen.py's write_file forcing
            # fix (round-3 RCA, sphinx#14656/14625, urllib3#5164): the soft
            # "NOTE: only N turn(s) left" nudges below are a request, not a
            # guarantee, and a model was confirmed live to read them and
            # keep calling investigation tools anyway, right through the
            # true final turn, producing no patch at all. On the actual
            # last turn, if nothing has been patched yet, force the API's
            # own tool_choice to `patch` specifically — the model is then
            # structurally unable to call anything else this turn.
            tool_choice: object = "auto"
            if turn == max_turns and last_patch is None:
                tool_choice = {"type": "function", "function": {"name": "patch"}}
            chat_turn = llm.chat_with_tools(messages, tools=openai_tools, temperature=temperature,
                                            tool_choice=tool_choice)
            if chat_turn.tool_calls:
                # Confirmed live on astroid#769 (2026-08-30): a name-only preview
                # ("run_shell, run_shell, ...") made a whole class of stuck-loop
                # aborts undiagnosable — the trajectory showed WHICH tool got
                # stuck but never WHAT ARGUMENTS it repeated, so "same shell
                # command re-run 5x" and "5 different shell commands" looked
                # identical in the log.
                #
                # A flat [:200] cap on the whole joined string then broke the
                # NEXT debugging session (astroid#3199, same day): a `patch`
                # call's SEARCH/REPLACE content got truncated mid-SEARCH-block,
                # so a repeatedly-rejected "SEARCH block not found" patch was
                # undiagnosable from the trajectory alone — hand-testing the
                # visible (truncated) SEARCH text against the real file showed
                # it actually matched fine, meaning the mismatch lived entirely
                # in the part that had been cut off. `patch` calls are the one
                # case where the full argument IS the debugging evidence (are
                # SEARCH and file content byte-identical or not?), so they get
                # a much larger allowance; everything else keeps the compact
                # per-call preview so a many-tool-call turn doesn't bloat the
                # trajectory for tools where "which one, roughly what args" is
                # enough.
                def _preview_one(tc):
                    if tc.name == "patch":
                        return f"{tc.name}({tc.arguments})"[:4000]
                    return f"{tc.name}({tc.arguments})"[:200]
                traj.log(f"{tag}_turn", turn=turn, action="tool_calls",
                         payload_preview=", ".join(_preview_one(tc) for tc in chat_turn.tool_calls))
                assistant_msg: dict[str, Any] = {"role": "assistant",
                                                 "content": chat_turn.content or None,
                                                 "tool_calls": [tc.as_message_tool_call()
                                                                for tc in chat_turn.tool_calls]}
                messages.append(assistant_msg)
                any_error = False
                patched_this_turn = False
                for tc in chat_turn.tool_calls:
                    if tc.name == "submit":
                        # Bypasses the shared error/stuck/turns_since_patch
                        # bookkeeping entirely, exactly like the text-
                        # grammar path's own dedicated "kind == submit"
                        # branch below — an accept/reject here was never
                        # counted as an "action" for those purposes.
                        if submit_check is None:
                            ok, feedback = True, ""
                        else:
                            ok, feedback = submit_check(last_patch, last_patch_path)
                        traj.log(f"{tag}_submit", turn=turn, accepted=ok, feedback=feedback[:300])
                        result = "Accepted." if ok else f"SUBMIT REJECTED.\n{feedback}\nFix and act again."
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": truncate(result, OBS_LIMIT)})
                        if ok:
                            return AgentResult(True, turn, last_patch, messages=messages)
                        rejected_submits += 1
                        if rejected_submits >= REJECTED_SUBMIT_LIMIT:
                            traj.log(f"{tag}_abort", reason="rejected submits", turn=turn,
                                     rejected_submits=rejected_submits)
                            return AgentResult(False, turn, last_patch,
                                               f"{rejected_submits} rejected SUBMITs — stopping early "
                                               f"instead of burning the rest of the turn budget", messages)
                        continue
                    if tc.name == "patch":
                        result, is_error, new_patch, new_patch_path = _handle_patch_call(tc.arguments)
                        if new_patch is not None:
                            last_patch = new_patch
                            last_patch_path = new_patch_path
                            patched_this_turn = True
                        any_error = any_error or is_error
                    elif tc.name == "run_shell":
                        result, is_error = _handle_run_shell_call(tc.arguments, project_dir)
                        any_error = any_error or is_error
                    else:
                        result = _execute_manifest_tool(tools, tc.name, tc.arguments)
                        if result.startswith("ERROR"):
                            any_error = True
                            # Only a genuinely unrecognized name is logged
                            # as "_unknown_tool" (matching the plain-text
                            # path's own event below) — a real tool called
                            # with bad/malformed arguments is a different
                            # failure mode (right tool, wrong args) and
                            # shouldn't be filed under the same event name.
                            if result.startswith("ERROR: unknown tool"):
                                traj.log(f"{tag}_unknown_tool", turn=turn, name=tc.name)
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": truncate(result, OBS_LIMIT)})
                turns_since_patch = 0 if patched_this_turn else turns_since_patch + 1
                consecutive_errors = consecutive_errors + 1 if any_error else 0
                if consecutive_errors >= 4:
                    traj.log(f"{tag}_abort", reason="consecutive errors", turn=turn)
                    return AgentResult(False, turn, last_patch, "4 consecutive malformed actions", messages)
                key = "tool_calls:" + ",".join(f"{tc.name}:{tc.arguments}" for tc in chat_turn.tool_calls)[:200]
                action_counts[key] = action_counts.get(key, 0) + 1
                if action_counts[key] >= 5:
                    traj.log(f"{tag}_abort", reason="stuck (same action 5x)", turn=turn)
                    return AgentResult(False, turn, last_patch,
                                       "stuck: identical action repeated 5 times", messages)
                if action_counts[key] == 3:
                    messages.append({"role": "user", "content":
                                     "NOTE: you have issued this exact tool call 3 times. Do something different."})
                elif turns_since_patch == 6:
                    messages.append({"role": "user", "content":
                                     "NOTE: you haven't called patch in 6 turns. Stop investigating and "
                                     "patch your best attempt now, then submit."})
                elif turns_since_patch == 12:
                    messages.append({"role": "user", "content":
                                     "NOTE: still no patch call after 12 turns of investigation. Call patch "
                                     "immediately — further reading will not help once the turn budget runs out."})
                remaining = max_turns - turn
                # Confirmed live (testgen's F4b fix, rca_pilot_runs_1_3.md,
                # and repeated on repair's own astroid-769 campaign run,
                # 2026-08-30): a soft "NOTE" folded in among tool
                # observations is not enough — a session that never once
                # called patch can sail straight through turns_since_patch
                # 6 and 12 on pure exploration and die at the turn cap with
                # nothing produced. Mirror testgen's forcing fix: with 2
                # turns left and NO patch ever recorded, issue a standalone,
                # unambiguous directive instead of another soft nudge.
                if remaining == 2 and last_patch is None:
                    messages.append({"role": "user", "content":
                                     "2 turn(s) remain and no patch has been recorded yet. Stop "
                                     "investigating — call patch NOW with your best fix, using "
                                     "what you've already learned. If patch is not called this "
                                     "turn, no fix will be produced and this entire attempt fails."})
                elif 0 < remaining <= 3:
                    messages.append({"role": "user",
                                     "content": f"NOTE: only {remaining} turn(s) left. patch and submit now."})
                continue
            output = chat_turn.content
        else:
            output = llm.chat(messages, temperature=temperature)
        kind, payload = parse_action(output)
        # Same reasoning as the function-calling path above: a `patch`
        # payload's full text IS the debugging evidence for a rejected
        # SEARCH/REPLACE, so it gets a much larger allowance than the
        # compact preview every other action kind uses.
        traj.log(f"{tag}_turn", turn=turn, action=kind,
                 payload_preview=payload[:4000] if kind == "patch" else payload[:200])

        if kind == "submit":
            if submit_check is None:
                return AgentResult(True, turn, last_patch, messages=messages)
            ok, feedback = submit_check(last_patch, last_patch_path)
            traj.log(f"{tag}_submit", turn=turn, accepted=ok, feedback=feedback[:300])
            if ok:
                return AgentResult(True, turn, last_patch, messages=messages)
            rejected_submits += 1
            if rejected_submits >= REJECTED_SUBMIT_LIMIT:
                traj.log(f"{tag}_abort", reason="rejected submits", turn=turn,
                         rejected_submits=rejected_submits)
                return AgentResult(False, turn, last_patch,
                                   f"{rejected_submits} rejected SUBMITs — stopping early instead of "
                                   f"burning the rest of the turn budget", messages)
            messages += [{"role": "assistant", "content": output},
                         {"role": "user", "content": f"SUBMIT REJECTED.\n{feedback}\nFix and act again."}]
            continue

        if kind == "patch":
            # Parse-time validation: an empty PATCH payload used to be
            # recorded as-is and only rejected once the model got as far
            # as SUBMIT (submit_check's "SUBMIT without PATCH" branch) —
            # burning a full extra turn round-trip to say something the
            # parser already knew the instant it parsed the action. Catch
            # it here instead, as soon as the action is parsed, and don't
            # let an empty payload overwrite a still-good `last_patch`
            # from an earlier turn.
            if not payload.strip():
                observation = ("ERROR: PATCH must be followed by content — either SEARCH/REPLACE "
                               "block(s) or one fenced code block with the complete file. "
                               "Emit PATCH again with content.")
            else:
                last_patch = payload
                turns_since_patch = 0
                observation = ("Patch recorded. It will be validated at SUBMIT. "
                               "If you are confident, SUBMIT; otherwise keep investigating (TOOL/RUN).")
        elif kind == "tool":
            observation = _execute_tool(tools, payload)
            if observation.startswith("ERROR: unknown tool"):
                try:
                    attempted_name = json.loads(payload).get("name", "?")
                except (json.JSONDecodeError, AttributeError):
                    attempted_name = "?"
                traj.log(f"{tag}_unknown_tool", turn=turn, name=attempted_name)
        elif kind == "run":
            res = sandbox_run(payload, cwd=project_dir, timeout=120)
            observation = f"exit_code={res.exit_code}\n{res.output}"
        else:
            observation = (
                "ERROR: no valid action found in your response. Call a tool — "
                "an inspection tool, patch, run_shell, or submit."
                if use_fc else
                "ERROR: no valid action found in your response. "
                "Emit exactly one of: TOOL <name> {...} / RUN <cmd> / PATCH / SUBMIT."
            )

        if not (kind == "patch" and payload.strip()):
            turns_since_patch += 1

        consecutive_errors = consecutive_errors + 1 if observation.startswith("ERROR") else 0
        if consecutive_errors >= 4:
            traj.log(f"{tag}_abort", reason="consecutive errors", turn=turn)
            return AgentResult(False, turn, last_patch, "4 consecutive malformed actions", messages)

        # stuck detection
        key = f"{kind}:{payload[:200]}"
        action_counts[key] = action_counts.get(key, 0) + 1
        if action_counts[key] >= 5:
            traj.log(f"{tag}_abort", reason="stuck (same action 5x)", turn=turn)
            return AgentResult(False, turn, last_patch, "stuck: identical action repeated 5 times", messages)
        if action_counts[key] == 3:
            observation += "\nNOTE: you have issued this exact action 3 times. Do something different."

        # Convergence nudges — stuck detection above only catches an
        # IDENTICAL action repeated; a model that explores productively but
        # never returns to PATCH (e.g. after a SUBMIT rejection) sails
        # right past it. Fire each threshold once, not every turn past it.
        if turns_since_patch == 6:
            observation += ("\nNOTE: you haven't emitted PATCH in 6 turns. Stop investigating and "
                             "PATCH your best attempt now, then SUBMIT.")
        elif turns_since_patch == 12:
            observation += ("\nNOTE: still no PATCH after 12 turns of investigation. PATCH "
                             "immediately — further reading will not help once the turn budget "
                             "runs out.")
        remaining = max_turns - turn
        if remaining == 2 and last_patch is None:
            observation += ("\nNOTE: 2 turns remain and no PATCH has been recorded yet. Stop "
                             "investigating — emit PATCH NOW with your best fix, using what "
                             "you've already learned. If PATCH is not emitted this turn, no fix "
                             "will be produced and this entire attempt fails.")
        elif 0 < remaining <= 3:
            observation += f"\nNOTE: only {remaining} turn(s) left in this session. PATCH and SUBMIT now."

        messages += [{"role": "assistant", "content": output},
                     {"role": "user", "content": f"OBSERVATION:\n{truncate(observation, OBS_LIMIT)}"}]

    # Round-3 RCA (celery/celery#10102, confirmed live 2026-08-31 against
    # Ollama Cloud's glm-5.2:cloud): forcing tool_choice is not honored by
    # every provider — a direct API probe showed the SAME forced-to-a-
    # specific-function tool_choice silently ignored (a different tool
    # called anyway), and tool_choice="required" with only one tool
    # offered returned ZERO tool calls, finish_reason "stop". So the
    # tool_choice force above is not a guarantee against every backend —
    # only reaching this point with `last_patch is None` proves it didn't
    # work here. Fall back to ONE plain chat() call (no tools at all,
    # `use_fc` path only — the non-fc path already relies on plain text
    # throughout, so it has no equivalent gap), asking for a patch in the
    # same PATCH/SEARCH-REPLACE text format `parse_action` already
    # understands. There is no tool call for the model to decline this
    # time — a live probe confirmed a plain-text ask reliably produces
    # exactly the requested content where a tool call did not.
    if use_fc and last_patch is None:
        fallback_prompt = (
            "Turn budget is exhausted and no patch was ever recorded. Reply with a "
            "line containing exactly PATCH, followed by one or more SEARCH/REPLACE "
            "blocks (<<<<<<< SEARCH / ======= / >>>>>>> REPLACE) or one fenced code "
            "block with the complete fixed file — using what you've already learned "
            "from this session. This is the last chance for this attempt to produce "
            "anything at all.")
        text = llm.chat(messages + [{"role": "user", "content": fallback_prompt}],
                        temperature=temperature)
        kind, payload = parse_action(text)
        traj.log(f"{tag}_fallback_plain_text", kind=kind, payload_preview=payload[:4000])
        if kind == "patch" and payload.strip():
            last_patch = payload
            last_patch_path = None

    # Deterministic fallback: forcing `patch` on the true final turn (above)
    # guarantees a patch ATTEMPT gets made, but that alone is not enough —
    # this function's contract is "success" only after submit_check
    # validates the patch, and a forced (or organically late) patch that
    # never reached an explicit SUBMIT/submit tool call before the budget
    # ran out was previously discarded outright and silently, even when it
    # was a genuinely good fix (`_attempt_patch` in repair_agent.py, and
    # `generate_file_agentic` in generate_agent.py, both require
    # `result.success` to keep anything). If anything was ever patched,
    # give it one last automatic submit_check pass before declaring the
    # whole attempt a loss — this is the same "structural guarantee, not a
    # request" principle as the tool_choice force above, just covering the
    # half of the last turn tool_choice forcing can't reach (the model
    # still has to be the one to decide the patch is done; nothing forces
    # a subsequent `submit`/SUBMIT call in the same turn).
    if last_patch is not None:
        if submit_check is None:
            ok, feedback = True, ""
        else:
            ok, feedback = submit_check(last_patch, last_patch_path)
        traj.log(f"{tag}_auto_submit", turn=max_turns, accepted=ok, feedback=feedback[:300])
        if ok:
            return AgentResult(True, max_turns, last_patch, messages=messages)

    traj.log(f"{tag}_abort", reason="turn budget exhausted")
    return AgentResult(False, max_turns, last_patch, f"turn budget ({max_turns}) exhausted", messages)
