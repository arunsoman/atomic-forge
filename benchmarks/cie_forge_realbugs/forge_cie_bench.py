#!/usr/bin/env python3
"""
Fix real bugs from open-source repos using CIE (as an MCP server)
+ forge's real SOTA repair loop (localize -> sample K -> execution-select ->
blast-radius gate -> commit). No modification to forge: forge's ToolBackend
protocol is satisfied by an MCPToolBackend that relays each call to a live
`cie-mcp --embedded` subprocess over stdio.

Each case = a standalone seed (mod.py [pre-fix] + test_mod.py [the real PR's
regression test]) from a permissively-licensed repo with many open bugs.
CIE indexes the seed; the agent gets CIE graph tools (callers/affected_by/
failing_context/...) over MCP; forge drives the repair loop.

Portable: cases live next to this script (./cases), the current Python
interpreter is used for subprocesses, and atomic_forge + cie are expected to
be pip-importable. Override the LLM with the standard forge env vars
(FORGE_MODEL / FORGE_BASE_URL / FORGE_API_KEY) or Ollama vars
(OLLAMA_BASE_URL / OLLAMA_MODEL).

    pip install git+https://github.com/arunsoman/atomic-forge.git \
                git+https://github.com/arunsoman/cie.git pytest
    python benchmarks/cie_forge_realbugs/forge_cie_bench.py            # all 4
    python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone  # one
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]                       # atomic-forge/
CASES_DIR = HERE / "cases"                   # seeds shipped with the repo
WORK_ROOT = Path(os.environ.get("BENCH_WORK_DIR", str(Path(tempfile.gettempdir()) / "forge_work")))
OUT = Path(os.environ.get("BENCH_OUT", str(Path(tempfile.gettempdir()) / "forge_cie_results.json")))

PY = sys.executable                          # use the interpreter running this script

# LLM config: prefer forge's standard env, fall back to Ollama defaults.
MODEL = (os.environ.get("FORGE_MODEL") or os.environ.get("BENCH_MODEL")
         or os.environ.get("OLLAMA_MODEL") or "qwen2.5:7b")
BASE_URL = os.environ.get("FORGE_BASE_URL") or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
API_KEY = os.environ.get("FORGE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "ollama"

# Make atomic_forge importable even when run from a source checkout (no install).
try:
    import atomic_forge  # noqa: F401
except ImportError:
    sys.path.insert(0, str(REPO / "src"))

from atomic_forge.llm import OpenAICompatLLM                       # noqa: E402
from atomic_forge.repair_agent import repair_loop_agentic          # noqa: E402
from atomic_forge.trajectory import Trajectory                     # noqa: E402
from atomic_forge.agent import render_tool_manifest                # noqa: E402
from atomic_forge.tools import _envelope                           # noqa: E402

from mcp import ClientSession, StdioServerParameters               # noqa: E402
from mcp.client.stdio import stdio_client                          # noqa: E402

VIEW_WINDOW = 100


def _check_cie():
    try:
        importlib.import_module("cie.mcp_server")
    except Exception as e:
        raise SystemExit(
            f"CIE is not importable ({e}). Install it:\n"
            f"  pip install git+https://github.com/arunsoman/cie.git") from e


# ----------------------------------------------------------------- MCP bridge
class MCPBridge:
    """Run a cie-mcp ClientSession in a background event-loop thread and
    expose a synchronous `call(name, **kwargs) -> dict`. forge's repair
    loop is synchronous, so this is the sync->async bridge."""

    def __init__(self, project_root, db_path):
        self.params = StdioServerParameters(
            command=PY,
            args=["-m", "cie.mcp_server", str(project_root),
                  "--embedded", "--db", str(db_path)],
            env=os.environ.copy(),
        )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.session = None
        self._ready = threading.Event()
        self.thread.start()
        if not self._ready.wait(45):
            raise RuntimeError("MCP bridge did not initialize in time")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._stdio_cm = stdio_client(self.params)
        self._r, self._w = self.loop.run_until_complete(self._stdio_cm.__aenter__())
        self._ses_cm = ClientSession(self._r, self._w)
        self.session = self.loop.run_until_complete(self._ses_cm.__aenter__())
        self.loop.run_until_complete(self.session.initialize())
        self._ready.set()
        self.loop.run_forever()

    def call(self, name, **kwargs):
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(name, kwargs), self.loop)
        res = fut.result(timeout=120)
        blocks = res.content if hasattr(res, "content") else res
        text = "\n".join(getattr(b, "text", None) or str(b) for b in blocks)
        try:
            return json.loads(text)
        except Exception:
            return {"ok": False, "tool": name, "results": [], "hint": text[:600]}

    def stop(self):
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


# ------------------------------------------------------- ToolBackend over MCP
class MCPToolBackend:
    """forge's ToolBackend protocol, relayed to CIE over MCP. CIE returns
    ABSOLUTE paths; forge keys suspect files / blast-radius self-exclusion
    on RELATIVE paths, so `_rel` rewrites them. describe()/health()/watch
    are local (CIE's describe lists ~121 tools — we expose only the forge
    ToolBackend surface)."""

    def __init__(self, bridge: MCPBridge, project_dir: Path):
        self.bridge = bridge
        self.project_dir = Path(project_dir)

    def _rel(self, env):
        results = env.get("results")
        if not isinstance(results, list):
            return env
        root = str(self.project_dir.resolve()) + os.sep
        for r in results:
            if not isinstance(r, dict):
                continue
            for k, v in list(r.items()):
                if isinstance(v, str) and v.startswith(root):
                    r[k] = v[len(root):]
        return env

    def _c(self, name, **kw):
        return self._rel(self.bridge.call(name, **kw))

    # -- graph-backed reads --
    def view_file(self, path, start=1, end=VIEW_WINDOW):
        return self.bridge.call("view_file", path=path, start=start, end=end)

    def file_skeleton(self, path):
        return self.bridge.call("file_skeleton", path=path)

    def search_symbol(self, name, kind=""):
        return self._c("search_symbol", name=name, kind=kind)

    def resolve_import(self, symbol, importing_file="", language=""):
        return self._c("resolve_import", symbol=symbol, importing_file=importing_file, language=language)

    def callers(self, symbol):
        return self._c("callers", symbol=symbol)

    def callees(self, symbol):
        return self._c("callees", symbol=symbol)

    def path_between(self, a, b):
        return self._c("path_between", source=a, target=b)

    def affected_by(self, file_path, max_depth=3, direction="incoming"):
        return self._c("affected_by", file_path=file_path, max_depth=max_depth, direction=direction)

    def failing_context(self, test):
        return self._c("failing_context", test_identifier=test)

    # -- writes (CIE syncs the graph in-call) --
    def write_file(self, path, content):
        return self.bridge.call("write_file", path=path, content=content)

    def edit_file(self, path, old_string, new_string, replace_all=False):
        return self.bridge.call("edit_file", path=path, old_string=old_string,
                                new_string=new_string, replace_all=replace_all)

    def delete_file(self, path):
        return self.bridge.call("delete_file", path=path)

    def reindex(self):
        return self.bridge.call("reindex")

    def reindex_file(self, path):
        return self.bridge.call("reindex_file", path=path)

    # -- local --
    def list_pending_tasks(self):
        return _envelope("list_pending_tasks", [], hint="no task queue configured")

    def health(self):
        return _envelope("health", [{"backend": "cie-mcp", "project_dir": str(self.project_dir)}])

    def describe(self):
        manifest = []
        excluded = {"reindex", "describe", "health"}
        for name in sorted(vars(type(self))):
            if name.startswith("_") or name in excluded:
                continue
            attr = vars(type(self))[name]
            if not callable(attr):
                continue
            m = getattr(self, name)
            doc = (m.__doc__ or "").strip().splitlines()[0] if (m.__doc__ or "").strip() else ""
            try:
                sig = str(inspect.signature(m))
            except (TypeError, ValueError):
                sig = "(...)"
            manifest.append({"name": name, "signature": sig, "doc": doc})
        return _envelope("describe", manifest,
                         hint="use only these tools for this run; the list is fixed at run start")

    def start_watch(self, debounce=0.5):
        return _envelope("start_watch", [{"started": False}])

    def stop_watch(self):
        return _envelope("stop_watch", [{"stopped": False}])


# --------------------------------------------------------------- run one case
def run_case(case_name, max_rounds=2, samples=2, max_turns=10):
    seed = CASES_DIR / case_name / "seed"
    if not seed.exists():
        raise FileNotFoundError(f"case {case_name!r} seed not found in {seed}")
    work = WORK_ROOT / case_name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy(seed / "mod.py", work / "mod.py")
    shutil.copy(seed / "test_mod.py", work / "test_mod.py")

    # 1. index with CIE (so the graph is "fully aware" before the agent starts)
    db = work / ".cie" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    idx = subprocess.run([PY, "-m", "cie.cli", "index", str(work), "--db", str(db)],
                        env=os.environ.copy(), capture_output=True, text=True, timeout=300)
    idx_summary = idx.stdout.strip().splitlines()[-1] if idx.stdout.strip() else idx.stderr[:200]

    # 2. spawn CIE as an MCP server (as before) + build the forge backend
    bridge = MCPBridge(work, db)
    backend = MCPToolBackend(bridge, work)
    manifest = backend.describe()["results"]
    manifest_text = render_tool_manifest(manifest)

    llm = OpenAICompatLLM(model=MODEL, base_url=BASE_URL, api_key=API_KEY)
    traj = Trajectory(work)
    test_cmd = f"{PY} -m pytest test_mod.py -q --tb=short -p no:cacheprovider"

    print(f"\n{'='*72}\nCASE: {case_name}\n  model: {MODEL} @ {BASE_URL}\n  cie index: {idx_summary}\n{'='*72}", flush=True)
    t0 = time.time()
    err = ""
    try:
        report = repair_loop_agentic(
            work, llm, backend, traj, test_cmd=test_cmd,
            max_rounds=max_rounds, samples=samples, max_turns_per_attempt=max_turns,
            per_issue_seconds=900, timeout=120,
            tool_manifest=manifest, tool_manifest_text=manifest_text,
            tasks_by_file={"mod.py": case_name})
    except Exception as e:  # noqa: BLE001
        report = {"success": False, "rounds": 0, "initial_failures": -1, "final_failures": -1}
        err = traceback.format_exc()[-1200:]
    finally:
        bridge.stop()

    # ground-truth re-check of the suite (don't trust a self-reported green)
    chk = subprocess.run(test_cmd, shell=True, cwd=str(work), capture_output=True, text=True, timeout=120)
    green = chk.returncode == 0

    usage = llm.usage
    return {
        "case": case_name,
        "forge_success": report.get("success"),
        "forge_rounds": report.get("rounds"),
        "initial_failures": report.get("initial_failures"),
        "final_failures": report.get("final_failures"),
        "repaired_files": report.get("repaired_files", []),
        "ground_truth_green": green,
        "ground_truth_tail": "\n".join((chk.stdout + chk.stderr).splitlines()[-6:]),
        "llm_calls": usage.calls,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        "wall_seconds": round(time.time() - t0, 1),
        "cie_index": idx_summary,
        "error": err,
    }


CASES = [
    "boltons_bits_offbyone",
    "boltons_singularize_ss",
    "mi_sliced_negative",
    "mi_running_min_stability",
]


def main():
    _check_cie()
    if not CASES_DIR.exists():
        raise SystemExit(f"cases dir not found: {CASES_DIR}")
    print(f"[bench] model={MODEL} base_url={BASE_URL} api_key={'***' if API_KEY and API_KEY!='ollama' else 'ollama'}")
    print(f"[bench] cases={CASES_DIR} work={WORK_ROOT}")
    only = sys.argv[1:]  # optional subset of case names
    todo = only or CASES
    results = []
    for case in todo:
        try:
            results.append(run_case(case))
        except Exception as e:  # noqa: BLE001
            results.append({"case": case, "error": traceback.format_exc()[-1500:]})
        print(json.dumps({k: v for k, v in results[-1].items()
                          if k not in ("ground_truth_tail",)}, indent=2), flush=True)

    print("\n" + "=" * 72)
    print("CIE(MCP) + forge repair — real open-source bug fixes")
    print("=" * 72)
    for r in results:
        if "error" in r and "forge_success" not in r:
            print(f"\n{r['case']}: HARNESS ERROR\n{r['error'][-400:]}")
            continue
        print(f"\n{r['case']}:")
        print(f"  ground-truth green : {r['ground_truth_green']}")
        print(f"  forge success      : {r['forge_success']}  rounds={r['forge_rounds']}  "
              f"failures {r['initial_failures']}->{r['final_failures']}")
        print(f"  repaired file(s)   : {r['repaired_files']}")
        print(f"  LLM calls / tokens : {r['llm_calls']}  total={r['total_tokens']:,} "
              f"(prompt {r['prompt_tokens']:,} + completion {r['completion_tokens']:,})")
        print(f"  wall time (s)      : {r['wall_seconds']}")
        if r.get("error"):
            print(f"  note               : {r['error'][-200:]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str))
    n_green = sum(1 for r in results if r.get("ground_truth_green"))
    print(f"\nSUMMARY: {n_green}/{len(results)} cases fixed green (CIE+forge)")
    print(f"results written to: {OUT}")


if __name__ == "__main__":
    main()