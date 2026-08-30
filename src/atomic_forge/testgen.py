"""
CIE-grounded regression-test generation.

Given a natural-language bug description and a project whose code graph CIE
has indexed, a tool-calling agent uses CIE graph tools (file_skeleton /
view_file / search_symbol / callers / affected_by) to ground itself in the
REAL function signatures and behavior, then writes a focused pytest
regression test that reproduces the bug.

The orchestrator then validates the generated test as a true oracle
(`oracle_fails_on_buggy`): it must FAIL on the current (buggy) code on an
*assertion* — not blow up with a collection/import/syntax error, which would
mean the test itself is broken rather than reproducing the bug. This is the
"measured, not asserted" gate: a test that fails for the wrong reason is
rejected, not counted.

Uses forge's own `OpenAICompatLLM.chat_with_tools` (no second LLM client).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .llm import OpenAICompatLLM

# CIE graph tool schemas offered to the test-gen agent (the grounding surface).
CIE_TOOLS = [
    {"type": "function", "function": {"name": "view_file", "description":
        "Windowed, line-numbered view of a file (content straight off disk).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
        "start": {"type": "integer", "default": 1}, "end": {"type": "integer", "default": 100}},
        "required": ["path"]}}},
    {"type": "function", "function": {"name": "file_skeleton", "description":
        "Signatures + line ranges for every symbol in a file, no bodies.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_symbol", "description":
        "Locate symbol definitions by name.", "parameters": {"type": "object",
        "properties": {"name": {"type": "string"}, "kind": {"type": "string", "default": ""}},
        "required": ["name"]}}},
    {"type": "function", "function": {"name": "callers", "description":
        "Who calls a symbol (blast radius).", "parameters": {"type": "object",
        "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "callees", "description":
        "What a symbol calls.", "parameters": {"type": "object",
        "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "affected_by", "description":
        "What depends on a file (blast radius).", "parameters": {"type": "object",
        "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
]
WRITE_TOOL = {"type": "function", "function": {"name": "write_file", "description":
    "Write a file's complete contents under the project root (path relative to project).",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"},
    "content": {"type": "string"}}, "required": ["path", "content"]}}}
RUN_TOOL = {"type": "function", "function": {"name": "run_tests", "description":
    "Run the regression test file with pytest and return pass/fail + output tail. "
    "A VALID regression test FAILS on the current (buggy) code on an assertion "
    "(exit code 1 with a FAILED assert), NOT a collection/import/syntax error.",
    "parameters": {"type": "object", "properties": {}, "required": []}}}

TG_SYSTEM = """You write a focused pytest regression test for ONE reported bug in the \
project at the project root. Use the project's real modules — find the exact \
function/class names and signatures with the graph tools before writing anything.

Method:
1. Use file_skeleton / search_symbol / view_file to find the EXACT function \
   signature and read its real implementation, so your test imports the right \
   name and calls it correctly — do not guess the signature or import path. \
   If unsure where a symbol lives, use search_symbol; if unsure how it's used, \
   use callers/affected_by.
2. Write the regression test file at the path given in the task. Import from \
   the project's real modules, use pytest + the Python stdlib only.
3. Call run_tests. A VALID regression test FAILS on the current (buggy) code \
   on an ASSERTION (exit code 1, a FAILED assert), NOT a collection/import/ \
   syntax error. If run_tests shows a collection error or 0 failures, your \
   test is malformed — fix the import/signature and re-run.
4. Once run_tests shows the test collects and fails on an assertion, respond \
   with a short final summary (no tool call) to finish.

Rules: write ONLY the regression test file. Do NOT modify any source file. \
Keep it minimal — one or two assertions that pin the buggy behavior. Use \
`import pytest` and import the project symbols by their real names."""


def _run_tests(project_dir: Path, test_rel: str, py: str = sys.executable,
               timeout: int = 120) -> tuple[bool, str]:
    cmd = f"{py} -m pytest {test_rel} -q --tb=short -p no:cacheprovider"
    p = subprocess.run(cmd, shell=True, cwd=str(project_dir),
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, p.stdout + p.stderr


def oracle_fails_on_buggy(project_dir: Path, test_rel: str,
                          py: str = sys.executable) -> tuple[bool, str]:
    """True iff the test reproduces the bug: it FAILS on an assertion (not a
    collection/import/syntax error). Returns (fails_with_assertion, full_output)."""
    ok, out = _run_tests(project_dir, test_rel, py)
    if ok:
        return False, out  # passes on buggy code -> does NOT reproduce the bug
    low = out.lower()
    collection_error = (
        "error collecting" in low or "no tests ran" in low
        or "no tests collected" in low or "errors during collection" in low)
    if collection_error:
        return False, out  # the test itself is broken, not a valid reproduction
    return True, out


def generate_regression_test(llm: OpenAICompatLLM, bridge, project_dir: Path,
                             test_rel: str, bug_description: str,
                             max_turns: int = 10) -> dict:
    """Drive the test-generation agent. `bridge` is a `cie_backend.MCPBridge`
    (graph tool calls are relayed to CIE over MCP). Returns
    ``{"generated": str, "turns": int, "calls": int, "tokens": int, "trace": list}``.
    """
    project_dir = Path(project_dir)
    cie_names = {t["function"]["name"] for t in CIE_TOOLS}
    messages = [
        {"role": "system", "content": TG_SYSTEM},
        {"role": "user", "content": f"Bug report:\n{bug_description}\n\n"
                                    f"Project root: {project_dir}\n"
                                    f"Write the regression test at: {test_rel}"},
    ]
    tools = CIE_TOOLS + [WRITE_TOOL, RUN_TOOL]
    calls = 0
    tokens = 0
    trace = []
    turns = 0
    wrote_file = False
    for turns in range(1, max_turns + 1):
        # Confirmed live (rca_pilot_runs_1_3.md F4, sphinx#13180): raising
        # max_tokens alone does NOT fix this — the model isn't token-starved,
        # it just keeps exploring (search_symbol/file_skeleton/view_file)
        # right up to the turn budget without ever attempting write_file. With
        # 2 turns left, force the issue: write now with what's already been
        # learned, or the attempt fails outright with nothing generated.
        if turns == max_turns - 1 and not wrote_file:
            messages.append({"role": "user", "content":
                f"{max_turns - turns + 1} turn(s) remain. Stop exploring — call "
                "write_file NOW with the regression test, using what you've already "
                "learned about the code. If write_file is not called this turn, no "
                "test will be generated and this entire attempt fails."})
        # max_tokens must clear a reasoning model's thinking head (qwen3.5 et
        # al. burn thousands of tokens on <think> before emitting content or
        # tool calls; 2048 starved every turn into an empty, tool-less reply
        # -> "no regression test generated"). 16384 matches the generation-loop
        # convention (generate_batch_agentic uses 32768 for the bigger batch).
        turn = llm.chat_with_tools(messages, tools, temperature=0.2, max_tokens=16384)
        calls += 1
        tokens += (llm.usage.prompt_tokens + llm.usage.completion_tokens) if False else 0  # tracked on llm.usage
        assistant = {"role": "assistant", "content": turn.content or ""}
        if turn.tool_calls:
            assistant["tool_calls"] = [tc.as_message_tool_call() for tc in turn.tool_calls]
        messages.append(assistant)
        if not turn.tool_calls:
            trace.append({"turn": turns, "done": True})
            break
        for tc in turn.tool_calls:
            name = tc.name
            try:
                args = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "write_file":
                target = project_dir / args["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args["content"])
                result = f"OK wrote {args['path']} ({len(args['content'])} bytes)"
                wrote_file = True
            elif name == "run_tests":
                ok, out = _run_tests(project_dir, test_rel)
                result = f"PASSED={ok}\n" + "\n".join(out.splitlines()[-16:])
            elif name in cie_names:
                result = json.dumps(bridge.call(name, **args))
            else:
                result = f"ERROR: unknown tool {name}"
            trace.append({"turn": turns, "tool": name, "result_len": len(result)})
            print(f"  [testgen] t{turns}: {name} -> {len(result)} chars", flush=True)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:6000]})
    generated = (project_dir / test_rel).read_text() if (project_dir / test_rel).exists() else ""
    usage = getattr(llm, "usage", None)
    return {"generated": generated, "turns": turns, "calls": calls,
            "tokens": (usage.prompt_tokens + usage.completion_tokens) if usage else 0,
            "trace": trace}