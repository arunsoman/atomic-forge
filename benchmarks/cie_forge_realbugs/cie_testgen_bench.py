#!/usr/bin/env python3
"""
CIE-generated regression tests + forge repair.

For each bug (a natural-language issue, the same 4 real open-source bugs):
  1. CIE indexes the buggy `mod.py` and serves graph tools over MCP.
  2. A tool-calling agent uses CIE graph tools (view_file / file_skeleton /
     search_symbol / callers / ...) to ground itself in the REAL function
     signature and behavior, then writes `test_mod.py` and self-checks it
     FAILS on the buggy code (a valid regression test must reproduce the bug).
  3. The harness validates the generated test as an oracle independently:
        - fails on the buggy mod.py   (reproduces the bug)
        - passes on the known-fixed mod.py (correct, not a false oracle)
  4. If the generated test is a valid oracle, forge's real repair loop
     (CIE+forge) fixes the bug against it; the harness ground-truth re-checks.

So this answers: can CIE generate VALID test cases given a bug? Validity is
measured, not asserted — a generated test only counts if it both fails on the
buggy code and passes on the real fix.

Portable: reuse the MCP bridge + config from forge_cie_bench.py (same dir).
    pip install git+https://github.com/kannamma-labs/atomic-forge.git \
                git+https://github.com/arunsoman/cie.git pytest openai
    python benchmarks/cie_forge_realbugs/cie_testgen_bench.py            # all 4
    python benchmarks/cie_forge_realbugs/cie_testgen_bench.py mi_sliced_negative  # one
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # import the sibling harness
from forge_cie_bench import (  # reuse the MCP bridge + portable config
    MCPBridge, MCPToolBackend, PY, CASES_DIR, MODEL, BASE_URL, API_KEY,
    repair_loop_agentic, OpenAICompatLLM, Trajectory, render_tool_manifest, _check_cie,
)

OUT = Path(os.environ.get("BENCH_OUT", str(Path(tempfile.gettempdir()) / "cie_testgen_results.json")))
TG_WORK = Path(os.environ.get("BENCH_WORK_DIR", str(Path(tempfile.gettempdir()) / "testgen_work")))

OAI = OpenAI(base_url=BASE_URL, api_key=API_KEY)

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
    "Run `pytest test_mod.py` in the project and return pass/fail + output tail. "
    "A VALID regression test FAILS on the current buggy code (assertion failure, "
    "not a collection/import error).", "parameters": {"type": "object",
    "properties": {}, "required": []}}}

TG_SYSTEM = """You write a focused pytest regression test for ONE reported bug in the \
`mod` module of the project at the project root. The source file is `mod.py`.

Method:
1. Use the graph tools (file_skeleton / search_symbol / view_file) to find the EXACT \
   function signature and read its real implementation, so your test imports the right \
   name and calls it correctly — do not guess the signature.
2. Optionally check callers/affected_by to ground the test in real usage.
3. Write `test_mod.py` (import from `mod`, use pytest + stdlib only). The test must \
   reproduce the reported bug.
4. Call run_tests. A VALID regression test FAILS on the current (buggy) code on an \
   ASSERTION (exit code 1 with an AssertionError), NOT a collection/import/syntax error. \
   If run_tests shows a collection error or 0 failures, fix the test and re-run.
5. Once run_tests shows the test collects and fails on an assertion, respond with a \
   short final summary (no tool call) to finish.

Rules: write ONLY test_mod.py. Do not modify mod.py. Keep it minimal — one or two \
assertions that pin the buggy behavior. Use `import pytest` and `from mod import ...`."""


# ----------------------------------------------------------------- run tests
def _run_tests(cwd: Path) -> tuple[bool, str]:
    p = subprocess.run(f"{PY} -m pytest test_mod.py -q --tb=short -p no:cacheprovider",
                       shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=120)
    return p.returncode == 0, p.stdout + p.stderr


def _run_tests_with(mod_content: str, cwd: Path) -> tuple[bool, str]:
    """Run the project's test_mod.py but with `mod.py` temporarily replaced by
    `mod_content` (used to check the generated test against the known fix)."""
    modp = cwd / "mod.py"
    bak = modp.read_text()
    modp.write_text(mod_content)
    try:
        return _run_tests(cwd)
    finally:
        modp.write_text(bak)


# -------------------------------------------------------- test-generation agent
def generate_test(bridge: MCPBridge, work: Path, bug: str, max_turns: int = 10) -> dict:
    messages = [{"role": "system", "content": TG_SYSTEM},
                {"role": "user", "content": f"Bug report:\n{bug}\n\nProject root: {work}\n"
                                            f"Write test_mod.py that reproduces this bug."}]
    tools = CIE_TOOLS + [WRITE_TOOL, RUN_TOOL]
    usage = dict(calls=0, prompt=0, completion=0)
    trace = []
    for turn in range(1, max_turns + 1):
        resp = OAI.chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                           tool_choice="auto", temperature=0.2, max_tokens=2048)
        usage["calls"] += 1
        u = getattr(resp, "usage", None)
        if u:
            usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
            usage["completion"] += getattr(u, "completion_tokens", 0) or 0
        msg = resp.choices[0].message
        assistant = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant["tool_calls"] = [{"id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                for tc in msg.tool_calls]
        messages.append(assistant)
        if not msg.tool_calls:
            trace.append({"turn": turn, "done": True})
            break
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "write_file":
                (work / args["path"]).write_text(args["content"])
                result = f"OK wrote {args['path']} ({len(args['content'])} bytes)"
            elif name == "run_tests":
                ok, out = _run_tests(work)
                result = f"PASSED={ok}\n" + "\n".join(out.splitlines()[-16:])
            elif name in {t["function"]["name"] for t in CIE_TOOLS}:
                result = json.dumps(bridge.call(name, **args))
            else:
                result = f"ERROR: unknown tool {name}"
            trace.append({"turn": turn, "tool": name, "result_len": len(result)})
            print(f"  [testgen] t{turn}: {name} -> {len(result)} chars", flush=True)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:6000]})
    return {"usage": usage, "trace": trace, "turns": turn}


# --------------------------------------------------------------- one case
def run_case(case_name: str, bug: str):
    seed = CASES_DIR / case_name / "seed"
    work = TG_WORK / case_name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy(seed / "mod.py", work / "mod.py")          # buggy source, no test yet
    fixed_src = (seed / "mod_fixed.py").read_text()           # the real fix (oracle ref)

    db = work / ".cie" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([PY, "-m", "cie.cli", "index", str(work), "--db", str(db)],
                   env=os.environ.copy(), capture_output=True, text=True, timeout=300)

    print(f"\n{'='*72}\nCASE: {case_name} — CIE generating the test\n  model: {MODEL} @ {BASE_URL}\n{'='*72}", flush=True)
    bridge = MCPBridge(work, db)
    t0 = time.time()
    try:
        gen = generate_test(bridge, work, bug)
    finally:
        bridge.stop()

    # ---- validate the generated test as an oracle (measured, not asserted) ----
    generated = (work / "test_mod.py").read_text() if (work / "test_mod.py").exists() else ""
    fails_on_buggy = None
    passes_on_fixed = None
    if generated.strip():
        ok_buggy, out_buggy = _run_tests(work)                       # buggy mod.py in place
        fails_on_buggy = (not ok_buggy) and ("AssertionError" in out_buggy or "assert" in out_buggy.lower() or "FAILED" in out_buggy)
        ok_fixed, out_fixed = _run_tests_with(fixed_src, work)        # swap in the real fix
        passes_on_fixed = ok_fixed
    valid_oracle = bool(fails_on_buggy and passes_on_fixed)

    # ---- if valid, run forge repair (CIE+forge) against the generated test ----
    repair = None
    if valid_oracle:
        bridge2 = MCPBridge(work, db)
        try:
            llm = OpenAICompatLLM(model=MODEL, base_url=BASE_URL, api_key=API_KEY)
            backend = MCPToolBackend(bridge2, work)
            manifest = backend.describe()["results"]
            traj = Trajectory(work)
            test_cmd = f"{PY} -m pytest test_mod.py -q --tb=short -p no:cacheprovider"
            repair = repair_loop_agentic(work, llm, backend, traj, test_cmd=test_cmd,
                max_rounds=2, samples=2, max_turns_per_attempt=10, per_issue_seconds=900,
                timeout=120, tool_manifest=manifest, tool_manifest_text=render_tool_manifest(manifest),
                tasks_by_file={"mod.py": case_name})
            rep_usage = llm.usage
        finally:
            bridge2.stop()
        # ground-truth: suite green with the generated test against the now-patched mod.py
        gt_ok, _ = _run_tests(work)
        repair = {"forge_success": repair.get("success"), "rounds": repair.get("rounds"),
                  "final_failures": repair.get("final_failures"),
                  "ground_truth_green": gt_ok,
                  "llm_calls": rep_usage.calls, "total_tokens": rep_usage.prompt_tokens + rep_usage.completion_tokens}
    return {
        "case": case_name,
        "test_generated": bool(generated.strip()),
        "fails_on_buggy": fails_on_buggy,
        "passes_on_fixed": passes_on_fixed,
        "valid_oracle": valid_oracle,
        "gen_llm_calls": gen["usage"]["calls"],
        "gen_tokens": gen["usage"]["prompt"] + gen["usage"]["completion"],
        "gen_turns": gen["turns"],
        "wall_seconds": round(time.time() - t0, 1),
        "repair": repair,
        "generated_test": generated,
    }


BUGS = {
    "boltons_bits_offbyone":
        "Bits(val, len_) in mod.py is meant to represent an integer `val` using exactly "
        "`len_` bits. The largest value that fits in `len_` bits is `2**len_ - 1`, so "
        "`2**len_` does NOT fit and must be rejected. But Bits(4, 2) is silently accepted "
        "(4 needs 3 bits) and produces a 3-bit value, breaking the length invariant. "
        "Reproduce: Bits(4, 2) should raise ValueError; Bits(1, 0) should also raise; "
        "Bits(3, 2) should round-trip to bin '11'.",
    "boltons_singularize_ss":
        "singularize(word) in mod.py converts an English plural to singular, preserving "
        "case. But words already singular that end in a double 's' (glass, boss, kiss, "
        "class, address, business) are corrupted: singularize('glass') returns 'glas'. "
        "Their plurals end in 'sses' and are handled, but the bare double-s words fall "
        "through to a branch that blindly strips the trailing 's'. Reproduce: "
        "singularize('glass') == 'glass', singularize('boss') == 'boss', "
        "singularize('BOSS') == 'BOSS'; and the real plural still works, "
        "singularize('glasses') == 'glass'.",
    "mi_sliced_negative":
        "sliced(seq, n, strict=False) in mod.py yields consecutive length-n slices of a "
        "sliceable sequence. A NEGATIVE slice size n is silently accepted and yields a "
        "wrong, truncated result instead of raising. Reproduce: list(sliced('ABCDEFG', -1)) "
        "should raise ValueError (and list(sliced('ABCDEFG', -1, strict=True)) too). "
        "Positive sizes must still work: list(sliced('ABCDEF', 3)) == ['ABC','DEF'].",
    "mi_running_min_stability":
        "running_min(iterable, *, maxlen) / running_max(..., *, maxlen) in mod.py compute "
        "the running minimum/maximum over a sliding window of size maxlen. They use a "
        "monotonically-increasing/decreasing subsequence with a STRICT comparison, so when "
        "an EQUAL value arrives the incumbent is dropped and the running min/max silently "
        "changes TYPE. But min(x, y) returns the LEFT operand when x == y (and max the "
        "left too) — the running window must keep the incumbent's type for stability. "
        "Reproduce with mixed numeric types: for data = [0, 0.0, Fraction(0)] (0, 0.0, "
        "Fraction(0) are all == but different types), "
        "list(map(type, running_min(data, maxlen=2))) must equal "
        "[type(min(data[0:1])), type(min(data[0:2])), type(min(data[1:3]))] — i.e. it must "
        "track what plain min() would return at each prefix. The same for running_max.",
}


def main():
    _check_cie()
    print(f"[bench] model={MODEL} base_url={BASE_URL} api_key={'***' if API_KEY and API_KEY!='ollama' else 'ollama'}")
    print(f"[bench] cases={CASES_DIR} work={TG_WORK}")
    only = sys.argv[1:]
    todo = only or list(BUGS)
    results = []
    for case in todo:
        try:
            results.append(run_case(case, BUGS[case]))
        except Exception:  # noqa: BLE001
            results.append({"case": case, "error": traceback.format_exc()[-1500:]})
    print("\n" + "=" * 72)
    print("CIE-generated regression tests + forge repair")
    print("=" * 72)
    for r in results:
        if "error" in r and "test_generated" not in r:
            print(f"\n{r['case']}: HARNESS ERROR\n{r['error'][-400:]}")
            continue
        print(f"\n{r['case']}:")
        print(f"  test generated     : {r['test_generated']}")
        print(f"  fails on buggy     : {r['fails_on_buggy']}   (reproduces the bug)")
        print(f"  passes on real fix : {r['passes_on_fixed']}   (correct oracle, not false)")
        print(f"  VALID oracle       : {r['valid_oracle']}")
        print(f"  gen LLM calls/tok  : {r['gen_llm_calls']} / {r['gen_tokens']:,}  ({r['gen_turns']} turns)")
        rp = r.get("repair")
        if rp:
            print(f"  forge repair       : success={rp['forge_success']} rounds={rp['rounds']} "
                  f"final_fail={rp['final_failures']} ground_truth_green={rp['ground_truth_green']}")
            print(f"                     repair LLM calls={rp['llm_calls']} tokens={rp['total_tokens']:,}")
        else:
            print(f"  forge repair       : skipped (oracle invalid)")
        print(f"  wall time (s)      : {r['wall_seconds']}")
    n_valid = sum(1 for r in results if r.get("valid_oracle"))
    n_repaired = sum(1 for r in results if r.get("repair", {}) and r["repair"].get("ground_truth_green"))
    print(f"\nSUMMARY: CIE generated a VALID test for {n_valid}/{len(results)} bugs; "
          f"forge+CIE fixed {n_repaired}/{len(results)} green.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str))
    print(f"results written to: {OUT}")


if __name__ == "__main__":
    main()