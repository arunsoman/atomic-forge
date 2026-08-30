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

from .tools import LocalToolBackend, _envelope

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
    agent starts. Returns the last line of CIE's stdout (a one-line summary).

    Raises RuntimeError on a non-zero exit — a failed index used to be
    silently swallowed here (the error text just got returned as if it were
    a normal summary), leaving the MCP bridge to start against a missing or
    partial `graph.db` a moment later. That's the ambient-caller path to CIE
    not actually being usable as the main tool backend even though nothing
    fails loudly at the point where it should — see `fix.py`'s "cie
    unavailable" fail-fast, which depends on this actually raising."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    r = subprocess.run([sys.executable, "-m", "cie.cli", "index", str(project_dir),
                        "--db", str(db_path)],
                       env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"cie index failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()[:500] or 'no output'}")
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
        #: `parallel_samples=True` (repair_agent.py's default) runs K
        #: repair samples concurrently in a ThreadPoolExecutor, and each
        #: calls tools on this SAME bridge from its own thread.
        #: `run_coroutine_threadsafe` only guarantees the coroutine gets
        #: scheduled onto `self.loop`; the event loop itself can still have
        #: more than one `call_tool` in flight at once (request A sent and
        #: awaiting its response when request B is scheduled), so the CIE
        #: server subprocess can receive genuinely concurrent tool calls.
        #: CIE's graph.db is a plain rollback-journal SQLite file (no WAL),
        #: which allows exactly one writer and blocks readers against it —
        #: confirmed live on astroid#769 (2026-08-30): 10x "OperationalError:
        #: database is locked" in one repair session, each one silently
        #: swapping a real graph query for CIE's own lower-quality
        #: "heuristic index" fallback instead of erroring. A model calling
        #: the same tool twice and getting a different-quality answer each
        #: time is a very plausible driver of the "stuck: identical action
        #: repeated 5 times" abort this project's own trajectories showed
        #: dominating that entire run. `GraphToolBackend`/`CodeGraph`
        #: already serialize their own SQLite connection behind a lock for
        #: exactly this reason (see codegraph.py, and parallel_samples'
        #: docstring in repair_agent.py) — this bridge never got the same
        #: treatment. Serializing whole round-trips here (request fully
        #: completes before the next one is even sent) removes any
        #: concurrent server-side access forge itself could cause. CIE
        #: tool calls are fast (graph lookups, not LLM calls), so this
        #: costs little — the expensive concurrent work (LLM turns) is
        #: untouched.
        self._call_lock = threading.Lock()
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
        # Serialized: see the `_call_lock` note in __init__ — one full
        # request/response round-trip to the CIE server at a time, across
        # every thread sharing this bridge.
        with self._call_lock:
            fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(tool_name, kwargs), self.loop)
            res = fut.result(timeout=120)
        blocks = res.content if hasattr(res, "content") else res
        text = "\n".join(getattr(b, "text", None) or str(b) for b in blocks)
        try:
            return json.loads(text)
        except Exception:
            return {"ok": False, "tool": tool_name, "results": [], "hint": text[:600]}

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
        #: lazy: LocalToolBackend.__init__ eagerly scans the whole tree to
        #: build its SymbolIndex, so only pay for it if statement_graph is
        #: actually called (see statement_graph() below).
        self._local: LocalToolBackend | None = None

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

    def entity_context(self, symbol):
        """One compact, pre-assembled neighborhood block for `symbol` —
        node info, callers, callees, tests, and class hierarchy when
        applicable — cheaper than chaining callers()+callees()+search_symbol()
        as three separate turns for the common "give me everything relevant
        to this one symbol" investigation step."""
        return self._c("entity_context", symbol=symbol)

    def class_hierarchy(self, class_name):
        """Ancestors/interfaces/descendants of a class, resolved from
        extends/implements edges — confirmed live against astroid#769
        (2026-08-30) to return real, useful inheritance chains (e.g.
        ClassDef's ancestors/descendants), unlike `actual_callers` (needs
        runtime OTel telemetry a cold clone never has — always empty) and
        `contracts` (errors: `ModuleNotFoundError: No module named 'core'`
        in this installed CIE version) — deliberately not wrapped here."""
        return self._c("class_hierarchy", class_name=class_name)

    def failing_context(self, test):
        return self._c("failing_context", test_identifier=test)

    def hybrid_search(self, query, top_k=10):
        """Lexical + dense-embedding + graph-degree ranked search — CIE's
        `ToolService.hybrid_search` (RQ-01). Unlike every other read here,
        this isn't call-graph-distance-from-the-failing-test; it finds
        files by relevance to the failure's own vocabulary (assertion
        text, exception type), which reaches code a static call graph
        never touches — e.g. astroid's `# type:` comment handling in
        `rebuilder.py`, invisible to `callers`/`search_symbol` because
        nothing in the failing test's call chain statically references it.
        Confirmed live against the astroid-769 campaign graph
        (2026-08-30): this was the only tool that surfaced the real fix
        file across two full repair rounds. Degrades gracefully with
        `dense_score: 0.0` on every hit when embeddings weren't computed
        for the index (e.g. no NVIDIA_API_KEY at `cie index` time) — still
        useful via its lexical+graph legs, never an error."""
        return self._c("hybrid_search", query=query, top_k=top_k)

    def statement_graph(self, file, line=None, radius=5):
        """Statement-level def-use context around `line` in `file` — CIE
        has no equivalent tool, so this delegates straight to forge's own
        `LocalToolBackend.statement_graph` against the on-disk checkout
        (lazily built on first call; a fresh SymbolIndex scan, not a CIE
        graph query). Without this method, `REPAIR_SYSTEM`'s own prompt
        ("DEEP LOCALIZATION IN LONG FUNCTIONS: ... TOOL statement_graph(...)")
        was a dead instruction on every CIE-backed run (`forge fix`'s only
        path — CIE is mandatory there, per fix.py): describe()'s manifest
        introspection silently omitted it since MCPToolBackend never
        defined it, so the model could never actually call the tool its
        own system prompt told it to use for exactly this situation."""
        if self._local is None:
            self._local = LocalToolBackend(self.project_dir)
        return self._local.statement_graph(file, line=line, radius=radius)

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