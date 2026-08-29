"""
Optional CIE (Code Insight Engine) integration for forge.

CIE is served as a real **MCP server** over stdio — the same surface Claude
Code / Cursor consume — and a thin `MCPToolBackend` here satisfies forge's
`ToolBackend` protocol by relaying each call to that subprocess. forge's
repair loop itself is unchanged; this module is purely additive and is used
only by `atomic-forge fix <issue-url>` (and reusable anywhere you want a
CIE-backed ToolBackend).

Everything is lazy: `import atomic_forge.cie_backend` does NOT require `cie`
or `mcp` to be installed. `require_cie()` raises a friendly install error;
`MCPBridge` imports `mcp` only when instantiated.

Install CIE:  pip install git+https://github.com/arunsoman/cie.git
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from .tools import _envelope

VIEW_WINDOW = 100


def require_cie() -> None:
    """Raise a friendly, actionable error if CIE / the MCP client aren't
    importable. Called at the start of any CIE-backed flow."""
    try:
        importlib.import_module("cie.mcp_server")
    except Exception as e:
        raise RuntimeError(
            "CIE is required for this command but isn't importable "
            f"({e}). Install it:\n"
            "  pip install git+https://github.com/arunsoman/cie.git"
        ) from e
    try:
        importlib.import_module("mcp")
    except Exception as e:
        raise RuntimeError(
            f"the MCP python client isn't importable ({e}). Install it:\n"
            "  pip install mcp"
        ) from e


def cie_index(project_dir: Path, db_path: Path, timeout: int = 600) -> str:
    """Index `project_dir` with CIE so the graph is fully built before the
    agent starts. Returns the last line of CIE's stdout (a one-line summary),
    or a short stderr slice on failure."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    r = subprocess.run([sys.executable, "-m", "cie.cli", "index", str(project_dir),
                        "--db", str(db_path)],
                       env=env, capture_output=True, text=True, timeout=timeout)
    return (r.stdout.strip().splitlines()[-1] if r.stdout.strip()
            else r.stderr[:200] or "cie index produced no output")


# ----------------------------------------------------------------- MCP bridge
class MCPBridge:
    """Run a cie-mcp ClientSession in a background event-loop thread and
    expose a synchronous `call(name, **kwargs) -> dict`. forge's repair
    loop is synchronous, so this is the sync->async bridge."""

    def __init__(self, project_root, db_path, ready_timeout: float = 45.0):
        from mcp import ClientSession, StdioServerParameters          # lazy
        from mcp.client.stdio import stdio_client                    # lazy
        self._StdioServerParameters = StdioServerParameters
        self._stdio_client = stdio_client
        self._ClientSession = ClientSession
        self.params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cie.mcp_server", str(project_root),
                  "--embedded", "--db", str(db_path)],
            env=os.environ.copy(),
        )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.session = None
        self._ready = threading.Event()
        self.thread.start()
        if not self._ready.wait(ready_timeout):
            raise RuntimeError("CIE MCP bridge did not initialize in time")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._stdio_cm = self._stdio_client(self.params)
        self._r, self._w = self.loop.run_until_complete(self._stdio_cm.__aenter__())
        self._ses_cm = self._ClientSession(self._r, self._w)
        self.session = self.loop.run_until_complete(self._ses_cm.__aenter__())
        self.loop.run_until_complete(self.session.initialize())
        self._ready.set()
        self.loop.run_forever()

    def call(self, tool_name, **kwargs):
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(tool_name, kwargs), self.loop)
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
    """forge's `ToolBackend` protocol, relayed to CIE over MCP. CIE returns
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

    def _c(self, tool, **kw):
        # first positional must not be called `name`: several MCP tools
        # legitimately take a `name=` kwarg (search_symbol etc.) and the
        # collision raised TypeError: got multiple values for 'name'
        return self._rel(self.bridge.call(tool, **kw))

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