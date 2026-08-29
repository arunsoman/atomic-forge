#!/usr/bin/env python3
"""
Measure the token/time/success benefit of giving a tool-calling LLM agent
a CODE GRAPH (CIE, served as a real MCP server over stdio) while it fixes a
mathematically-subtle bug, versus giving it only plain filesystem tools.

This is a *cross-tool* benchmark (does a code-graph help an agent fix a
bug?), deliberately separate from `run_case.py` (which measures
atomic-forge's own repair loop). See `docs/cie-graph-bugfix-benchmark.md`
for the methodology, the bug, and the measured numbers.

Reproduce:

    # 1. plant the bug into a throwaway copy + its failing test
    python benchmarks/measure_cie_graph_benefit.py --setup

    # 2. index that copy with CIE (needs the `cie` package on PYTHONPATH)
    PYTHONPATH=/path/to/cie python -m cie.cli index "$BENCH_PROJECT" --db "$CIE_DB"

    # 3. run both cases
    python benchmarks/measure_cie_graph_benefit.py

All paths are overridable via env vars (see CONFIG below); defaults match
the run that produced benchmarks/results/cie_vs_no_cie.json.

Requires: `openai`, `mcp` SDK, the `cie` package importable (PYTHONPATH),
Ollama running locally with a tool-calling model, and a Python with
`atomic_forge` + pytest installed (VENV_PY).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from openai import OpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ---------------------------------------------------------------- config (env-overridable)
PROJECT    = Path(os.environ.get("BENCH_PROJECT", "/tmp/bench_project"))
CIE_DB     = Path(os.environ.get("CIE_DB", "/tmp/bench_graph.db"))
CIE_ROOT   = Path(os.environ.get("CIE_ROOT", "/tmp/cie"))          # cie package dir
CIE_REPO   = Path(os.environ.get("CIE_REPO", "/home/arun/Downloads/atomic-forge"))  # for the buggy file + venv
PATCH_PY   = PROJECT / "src/atomic_forge/patch.py"
TEST_FILE  = "tests/test_patch.py"
MODEL      = os.environ.get("BENCH_MODEL", "qwen3.5:cloud")
BASE_URL   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
MAX_TURNS  = int(os.environ.get("BENCH_MAX_TURNS", "22"))
VENV_PY    = os.environ.get("VENV_PY", "/home/arun/Downloads/atomic-forge/.venv/bin/python")

CIE_TOOL_NAMES = ["affected_by", "callers", "callees", "search_symbol",
                  "file_skeleton", "path_between", "view_file"]

client = OpenAI(base_url=BASE_URL, api_key="ollama")


def _buggy_patch_py() -> str:
    """The planted-bug version of validate_hunk_disjointness (pairwise
    ADJACENT overlap check — wrong for interval-containment + straddle)."""
    return (CIE_REPO / "benchmarks/cie_overlap_sweep_seed/patch.buggy.py").read_text()


def _patch_test_py() -> str:
    return (CIE_REPO / "tests/test_patch.py").read_text()


def setup() -> None:
    """Build a throwaway buggy copy of atomic_forge + the failing test."""
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    for sub in ["src/atomic_forge", "tests"]:
        (PROJECT / sub).mkdir(parents=True, exist_ok=True)
    shutil.copytree(CIE_REPO / "src/atomic_forge", PROJECT / "src/atomic_forge",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(CIE_REPO / "tests/test_patch.py", PROJECT / "tests/test_patch.py")
    # plant the bug
    (PROJECT / "src/atomic_forge/patch.py").write_text(_buggy_patch_py())
    shutil.copy2(CIE_REPO / "pyproject.toml", PROJECT / "pyproject.toml")
    print(f"setup: buggy project at {PROJECT}")
    print(f"setup: now index it -> PYTHONPATH={CIE_ROOT} python -m cie.cli index {PROJECT} --db {CIE_DB}")


def reset_buggy_file():
    (PROJECT / "src/atomic_forge/patch.py").write_text(_buggy_patch_py())


def run_tests_capture() -> tuple[bool, str]:
    proc = subprocess.run(
        [VENV_PY, "-m", "pytest", TEST_FILE, "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=str(PROJECT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(PROJECT / "src")})
    return proc.returncode == 0, proc.stdout + proc.stderr


def resolve_path(p: str) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else PROJECT / pp


# ---------------------------------------------------------------- local tools
def tool_read_file(path): 
    p = resolve_path(path)
    if not p.is_file(): return f"ERROR: not a file: {p}"
    lines = p.read_text(errors="replace").splitlines()
    return "\n".join(f"{i+1:>5}\t{ln}" for i, ln in enumerate(lines))

def tool_list_dir(path="."):
    p = resolve_path(path)
    if not p.is_dir(): return f"ERROR: not a dir: {p}"
    return "\n".join(("d " if q.is_dir() else "f ") + q.name for q in sorted(p.iterdir())) or "(empty)"

def tool_grep(pattern, path="src"):
    p = resolve_path(path)
    try: proc = subprocess.run(["rg","-n","--",pattern,str(p)], capture_output=True, text=True, timeout=30)
    except FileNotFoundError: proc = subprocess.run(["grep","-rn","--",pattern,str(p)], capture_output=True, text=True, timeout=30)
    return (proc.stdout+proc.stderr).strip() or f"(no matches for {pattern!r} under {p})"

def tool_edit_file(path, old_string, new_string):
    p = resolve_path(path)
    try: text = p.read_text()
    except OSError as e: return f"ERROR reading: {e}"
    if old_string not in text: return "ERROR: old_string not found. Re-read the file and match exactly."
    if text.count(old_string) != 1: return f"ERROR: old_string matches {text.count(old_string)} times — must be unique."
    p.write_text(text.replace(old_string, new_string, 1))
    return f"OK: replaced 1 occurrence in {p}"

def tool_run_tests():
    ok, out = run_tests_capture()
    return f"PASSED={ok}\n" + "\n".join(out.splitlines()[-18:])

LOCAL_TOOLS = {"read_file": tool_read_file, "list_dir": tool_list_dir,
               "grep": tool_grep, "edit_file": tool_edit_file, "run_tests": tool_run_tests}

LOCAL_TOOL_SCHEMAS = {
 "read_file":{"type":"function","function":{"name":"read_file","description":"Read a file's full contents with line numbers. path absolute or relative to project root.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
 "list_dir":{"type":"function","function":{"name":"list_dir","description":"List entries in a directory (relative to project root).","parameters":{"type":"object","properties":{"path":{"type":"string","default":"."}},"required":[]}}},
 "grep":{"type":"function","function":{"name":"grep","description":"ripgrep for a pattern under a path. Returns matching lines with line numbers.","parameters":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string","default":"src"}},"required":["pattern"]}}},
 "edit_file":{"type":"function","function":{"name":"edit_file","description":"Replace exactly one unique occurrence of old_string with new_string in a file. old_string MUST match exactly and uniquely.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_string":{"type":"string"},"new_string":{"type":"string"}},"required":["path","old_string","new_string"]}}},
 "run_tests":{"type":"function","function":{"name":"run_tests","description":"Run pytest tests/test_patch.py and return pass/fail + output tail.","parameters":{"type":"object","properties":{},"required":[]}}},
}

SYSTEM_PROMPT = """You are a precise debugging agent. You work in the Python project at the project root \
(the `atomic_forge` package, importable via PYTHONPATH=src).

A test is FAILING: `tests/test_patch.py::test_nested_then_straddling_hunks_all_conflict`. \
Find the ROOT CAUSE and fix it so the FULL file `tests/test_patch.py` passes, without breaking \
any currently-passing test.

Method:
1. Explore to LOCATE the failing logic. The failing test exercises an overlap/conflict detector.
2. Understand the algorithm and WHY it is wrong — this is a mathematically subtle bug about \
interval overlap detection. Think about edge cases (containment, straddle).
3. Check the BLAST RADIUS: who calls / depends on the buggy function? Make sure your fix is \
compatible with every caller (don't break them).
4. Edit the file (edit_file with exact, unique old_string).
5. run_tests to verify the full file is green.
6. When green, respond with a short final summary (NO tool call) to finish.

Rules: only edit files under src/atomic_forge/. Prefer surgical tools over reading whole large \
files. Think step by step before editing.
"""


async def cie_call(session, name, args):
    res = await session.call_tool(name, args)
    blocks = res.content if hasattr(res, "content") else res
    return "\n".join(getattr(b, "text", None) or str(b) for b in blocks)


def build_cie_openai_tools(tool_objs):
    return [{"type":"function","function":{"name":t.name,
            "description":(t.description or t.name)[:400],"parameters":t.inputSchema}}
            for t in tool_objs]


async def run_case(with_cie: bool, label: str) -> dict:
    reset_buggy_file()
    print(f"\n{'='*70}\nCASE: {label}  (cie={with_cie})\n{'='*70}", flush=True)
    messages = [{"role":"system","content":SYSTEM_PROMPT},
                {"role":"user","content":"The failing test is test_nested_then_straddling_hunks_all_conflict "
                 "in tests/test_patch.py. Find and fix the root cause. Start by exploring."}]
    m = dict(turns=0, llm_calls=0, prompt_tokens=0, completion_tokens=0, tool_calls=0, tool_log=[])
    local_only = ["edit_file","run_tests"] + (["read_file","list_dir","grep"] if not with_cie else [])
    openai_tools = [LOCAL_TOOL_SCHEMAS[n] for n in local_only]
    cie_session = ses_ctx = stdio_ctx = None
    if with_cie:
        params = StdioServerParameters(command="python",
            args=["-m","cie.mcp_server",str(PROJECT),"--embedded","--db",str(CIE_DB)],
            env={**os.environ,"PYTHONPATH":str(CIE_ROOT)})
        stdio_ctx = stdio_client(params)
        r, w = await stdio_ctx.__aenter__()
        ses_ctx = ClientSession(r, w)
        cie_session = await ses_ctx.__aenter__()
        await cie_session.initialize()
        all_tools = await cie_session.list_tools()
        cie_tool_objs = [t for t in all_tools.tools if t.name in CIE_TOOL_NAMES]
        openai_tools += build_cie_openai_tools(cie_tool_objs)
        m["cie_tools_exposed"] = len(cie_tool_objs)
    m["tools_exposed"] = len(openai_tools)
    t0 = time.time()
    try:
        for turn in range(1, MAX_TURNS+1):
            m["turns"] = turn
            def _call(): return client.chat.completions.create(model=MODEL, messages=messages,
                tools=openai_tools, tool_choice="auto", temperature=0.2, max_tokens=2048)
            resp = await asyncio.to_thread(_call)
            m["llm_calls"] += 1
            u = getattr(resp,"usage",None)
            if u:
                m["prompt_tokens"] += getattr(u,"prompt_tokens",0) or 0
                m["completion_tokens"] += getattr(u,"completion_tokens",0) or 0
            msg = resp.choices[0].message
            assistant = {"role":"assistant","content":msg.content or ""}
            if msg.tool_calls:
                assistant["tool_calls"] = [{"id":tc.id,"type":"function",
                    "function":{"name":tc.function.name,"arguments":tc.function.arguments or "{}"}}
                    for tc in msg.tool_calls]
            messages.append(assistant)
            if not msg.tool_calls:
                print(f"[{label}] turn {turn}: finished (no tool call).", flush=True); break
            for tc in msg.tool_calls:
                name = tc.function.name
                try: args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError: args = {}
                m["tool_calls"] += 1
                if name in LOCAL_TOOLS: result = LOCAL_TOOLS[name](**args)
                elif with_cie and name in CIE_TOOL_NAMES and cie_session: result = await cie_call(cie_session,name,args)
                else: result = f"ERROR: unknown tool {name}"
                m["tool_log"].append({"turn":turn,"tool":name,"args":args,"result_len":len(result)})
                print(f"[{label}] turn {turn}: {name}({list(args.keys())}) -> {len(result)} chars | "
                      f"{result.replace(chr(10),' ')[:120]}", flush=True)
                messages.append({"role":"tool","tool_call_id":tc.id,"content":result[:6000]})
    finally:
        for ctx in (ses_ctx, stdio_ctx):
            if ctx:
                try: await ctx.__aexit__(None,None,None)
                except Exception: pass
    m["wall_seconds"] = round(time.time()-t0,1)
    ok, out = run_tests_capture()
    m["final_test_pass"] = ok
    m["final_test_tail"] = "\n".join(out.splitlines()[-6:])
    m["total_tokens"] = m["prompt_tokens"]+m["completion_tokens"]
    return m


async def main():
    if "--setup" in (os.sys.argv if hasattr(os,"sys.argv") else []) or "--setup" in os.environ.get("BENCH_ARG","").split():
        setup(); return
    import sys
    if "--setup" in sys.argv: setup(); return
    results = {}
    for with_cie, key, label in [(True,"with_cie","WITH_CIE"),(False,"no_cie","NO_CIE")]:
        try: results[key] = await run_case(with_cie, label)
        except Exception as e:
            results[key] = {"error":str(e),"tb":traceback.format_exc()[-1200:]}
            reset_buggy_file()
    print("\n"+"="*70+"\nBENCHMARK RESULTS\n"+"="*70)
    for case, mm in results.items():
        if "error" in mm: print(f"\n{case}: ERROR\n{mm['tb']}"); continue
        print(f"\n{case}:\n  tools exposed     : {mm.get('tools_exposed')} (cie graph tools: {mm.get('cie_tools_exposed',0)})"
              f"\n  test suite GREEN  : {mm['final_test_pass']}\n  LLM calls         : {mm['llm_calls']}"
              f"\n  agent turns       : {mm['turns']}\n  tool calls        : {mm['tool_calls']}"
              f"\n  prompt tokens     : {mm['prompt_tokens']:,}\n  completion tokens : {mm['completion_tokens']:,}"
              f"\n  TOTAL tokens      : {mm['total_tokens']:,}\n  wall time (s)     : {mm['wall_seconds']}")
        for tl in mm["tool_log"]:
            print(f"      t{tl['turn']}: {tl['tool']}({list(tl['args'].keys())}) -> {tl['result_len']} chars")
        print(f"  final test tail   : {mm['final_test_tail'][-200:]}")
    if all("error" not in results.get(k,{}) for k in ("with_cie","no_cie")):
        a,b = results["with_cie"],results["no_cie"]
        delta = b["total_tokens"]-a["total_tokens"]
        pct = 100.0*delta/b["total_tokens"] if b["total_tokens"] else 0
        print(f"\nTOKEN DELTA (no_cie - with_cie): {delta:+,}  ({pct:+.1f}% {'more' if delta>0 else 'fewer'} without CIE)")
    Path("benchmarks/results/cie_vs_no_cie.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nresults written to benchmarks/results/cie_vs_no_cie.json")


if __name__ == "__main__":
    asyncio.run(main())