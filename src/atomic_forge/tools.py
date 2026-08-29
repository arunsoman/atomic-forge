"""
The tool layer (forge's agent-computer interface): one protocol, one
real backend.

Design rules enforced here (bounded, self-describing responses):
  - windowed file views with line numbers, ~100 lines/call
  - capped lists, explicit truncation flags + hints
  - empty results are never silent — always a `hint` explaining why

`LocalToolBackend` needs nothing but a directory on disk: its query
methods (view_file/search_symbol/file_skeleton/callers/callees/
path_between/affected_by/failing_context/resolve_import) are backed by
`symbols.SymbolIndex`, a small dependency-free index (exact for Python,
regex-heuristic for JS/TS/Java — see that module's docstring).

`GraphToolBackend` is the second bundled implementation: same protocol,
same file I/O (delegated straight to a `LocalToolBackend`), but its
query methods are backed by `codegraph.CodeGraph` — a persisted SQLite
call graph instead of a rebuilt-every-process in-memory index, with
precomputed edges so multi-hop `callers`/`callees`/`affected_by` are
indexed lookups, not repeated regex scans. Bring your own richer backend
still (a real language server, an embeddings index) by implementing the
same `ToolBackend` protocol.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Optional, Protocol

from . import graph_statements
from .codegraph import CodeGraph
from .symbols import SymbolIndex

VIEW_WINDOW = 100


class ToolBackend(Protocol):
    def view_file(self, path: str, start: int = 1, end: int = VIEW_WINDOW) -> dict: ...
    def view_window(self, path: str, center_line: int, radius: int = VIEW_WINDOW // 2) -> dict: ...
    def search_symbol(self, name: str, kind: str = "") -> dict: ...
    def file_skeleton(self, path: str) -> dict: ...
    def callers(self, symbol: str) -> dict: ...
    def callees(self, symbol: str) -> dict: ...
    def path_between(self, a: str, b: str) -> dict: ...
    def failing_context(self, test: str) -> dict: ...
    def affected_by(self, file_path: str, max_depth: int = 3, direction: str = "incoming") -> dict: ...
    def resolve_import(self, symbol: str, importing_file: str = "", language: str = "") -> dict: ...
    def list_pending_tasks(self) -> dict: ...
    def reindex(self) -> dict: ...
    def reindex_file(self, path: str) -> dict: ...
    def health(self) -> dict: ...
    def describe(self) -> dict: ...
    def write_file(self, path: str, content: str) -> dict: ...
    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict: ...
    def delete_file(self, path: str) -> dict: ...
    def start_watch(self, debounce: float = 0.5) -> dict: ...
    def stop_watch(self) -> dict: ...


def _envelope(tool: str, results: list, hint: Optional[str] = None,
              truncated: bool = False, total: Optional[int] = None, ok: bool = True) -> dict:
    return {"ok": ok, "tool": tool, "results": results, "truncated": truncated,
            "total": total if total is not None else len(results), "hint": hint}


class LocalToolBackend:
    """Filesystem + in-memory symbol index. No external services."""

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.index = SymbolIndex(self.project_dir)
        self.index.build()

    # -- index lifecycle ---------------------------------------------------
    def reindex(self) -> dict:
        self.index.build()
        return _envelope("reindex", [{"symbols": len(self.index.symbols)}],
                         hint="index rebuilt over current working tree")

    def reindex_file(self, path: str) -> dict:
        f = self.project_dir / path
        content = f.read_text(errors="replace") if f.is_file() else ""
        self.index.reindex_file(path, content)
        return _envelope("reindex_file", [{"path": path, "symbols": len(self.index.symbols)}])

    def health(self) -> dict:
        return _envelope("health", [{"backend": "local-disk", "symbols": len(self.index.symbols),
                                     "project_dir": str(self.project_dir)}])

    def describe(self) -> dict:
        """Introspects this backend's own public methods — no hand-
        maintained tool list to drift out of sync with what's actually
        callable."""
        manifest = []
        excluded = {"reindex", "describe", "health"}
        for name in sorted(vars(type(self))):
            if name.startswith("_") or name in excluded:
                continue
            attr = vars(type(self))[name]
            if not callable(attr):
                continue
            method = getattr(self, name)
            doc = method.__doc__.strip().splitlines()[0] if method.__doc__ else ""
            try:
                signature = str(inspect.signature(method))
            except (TypeError, ValueError):
                signature = "(...)"
            manifest.append({"name": name, "signature": signature, "doc": doc})
        return _envelope("describe", manifest,
                         hint="use only these tools for this run; the list is fixed at "
                              "run start, not re-discovered mid-loop")

    # -- reading -------------------------------------------------------------
    def view_file(self, path: str, start: int = 1, end: int = VIEW_WINDOW) -> dict:
        f = self.project_dir / path
        if not f.is_file():
            return _envelope("view_file", [], ok=False, hint=f"no such file: {path}")
        lines = f.read_text(errors="replace").split("\n")
        start = max(1, start)
        end = min(len(lines), end)
        window = [f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1)]
        return _envelope("view_file", [{"path": path, "start": start, "end": end,
                                        "total_lines": len(lines), "content": "\n".join(window)}],
                         truncated=end < len(lines),
                         hint=None if end >= len(lines) else f"{len(lines) - end} more lines; call again with a later start")

    def view_window(self, path: str, center_line: int, radius: int = VIEW_WINDOW // 2) -> dict:
        """`view_file`, but addressed by a center line + radius instead of
        an explicit start/end — the shape `repair_agent.py`'s localization
        evidence (a traceback line, a blast-radius suspect) naturally
        produces, so a caller doesn't have to hand-compute
        ``max(1, line - radius)`` at every call site."""
        start = max(1, center_line - radius)
        end = center_line + radius
        return self.view_file(path, start=start, end=end)

    def file_skeleton(self, path: str) -> dict:
        syms = self.index.file_skeleton(path)
        if not syms:
            f = self.project_dir / path
            hint = "no symbols found" if f.is_file() else f"no such file: {path}"
            return _envelope("file_skeleton", [], hint=hint)
        return _envelope("file_skeleton", [
            {"name": s.name, "kind": s.kind, "line": s.line, "signature": s.signature} for s in syms
        ])

    def search_symbol(self, name: str, kind: str = "") -> dict:
        hits = self.index.find(name, kind)
        if not hits:
            return _envelope("search_symbol", [], hint=f"no symbol named {name!r} found")
        return _envelope("search_symbol", [
            {"source_file": s.file, "name": s.name, "kind": s.kind, "line": s.line, "signature": s.signature}
            for s in hits
        ])

    def resolve_import(self, symbol: str, importing_file: str = "", language: str = "") -> dict:
        hits = self.index.find(symbol)
        if not hits:
            return _envelope("resolve_import", [], hint=f"{symbol!r} not found anywhere in the project")
        results = []
        for s in hits:
            stmt = _import_statement(s.file, s.name, importing_file)
            results.append({"symbol": s.name, "defined_in": s.file, "import_statement": stmt})
        return _envelope("resolve_import", results)

    def callers(self, symbol: str) -> dict:
        hits = self.index.callers_of(symbol)
        if not hits:
            return _envelope("callers", [], hint=f"no call sites found for {symbol!r}")
        return _envelope("callers", hits)

    def callees(self, symbol: str) -> dict:
        hits = self.index.callees_of(symbol)
        if not hits:
            return _envelope("callees", [], hint=f"no known-symbol calls found inside {symbol!r}")
        return _envelope("callees", hits)

    def path_between(self, a: str, b: str) -> dict:
        path = self.index.path_between(a, b)
        if path is None:
            return _envelope("path_between", [], hint=f"no call path found from {a!r} to {b!r}")
        return _envelope("path_between", [{"path": path}])

    def affected_by(self, file_path: str, max_depth: int = 3, direction: str = "incoming") -> dict:
        hits = self.index.affected_by(file_path, max_depth, direction)
        return _envelope("affected_by", hits,
                         hint=None if hits else f"nothing depends on {file_path!r} yet" if direction == "incoming"
                         else f"{file_path!r} calls nothing else known")

    def statement_graph(self, file: str, line: Optional[int] = None, radius: int = 5) -> dict:
        """Statement-level context around `line` in `file` (R11): each row
        names the statement, what it defines, and which earlier statement
        each read name comes from (def-use). Rows are annotated metadata,
        not source dumps — pair with view_file for the code itself."""
        f = self.project_dir / file
        if not f.is_file():
            return _envelope("statement_graph", [], ok=False, hint=f"no such file: {file}")
        try:
            text = f.read_text(errors="replace")
        except OSError:
            return _envelope("statement_graph", [], ok=False, hint=f"unreadable: {file}")
        rows, edges = graph_statements.extract(
            file, text, enclosing_symbols=self.index.file_skeleton(file))
        if not rows:
            return _envelope("statement_graph", [],
                             hint=f"no statements extracted from {file}")
        anchor = line if line is not None else rows[0]["line"]
        # resolve def_use in-memory: edge (d, u, name) = row u reads a name
        # bound at row d
        defines_at: dict[int, list[str]] = {}
        reads_at: dict[int, list[str]] = {}
        for d_i, u_i, name, _conf in edges:
            defines_at.setdefault(d_i, []).append(name)
            reads_at.setdefault(u_i, []).append(name)
        window = [r for r in rows
                  if r["line"] <= anchor + radius and r["end_line"] >= anchor - radius]
        results = []
        for r in window[:40]:
            i = rows.index(r)
            results.append({**r, "defines": sorted(defines_at.get(i, [])),
                            "reads_from": sorted(reads_at.get(i, []))})
        return _envelope("statement_graph", results,
                         truncated=len(window) > len(results),
                         total=len(rows),
                         hint=None if len(window) <= len(results)
                         else f"window clipped to 40 rows; pass a more specific line")

    def failing_context(self, test: str) -> dict:
        """Given a failing test id (`path/to/test_x.py` or
        `path/to/test_x.py::test_name`), rank suspect source files by
        distance: symbols the test calls directly (distance 1), then
        symbols those call (distance 2)."""
        file_part = test.split("::")[0]
        f = self.project_dir / file_part
        if not f.is_file():
            # Fall back to a symbol-name lookup — a bare test class/method
            # name with no real path (e.g. Gradle's terse console output).
            hits = self.index.find(Path(file_part).stem)
            if not hits:
                return _envelope("failing_context", [], hint=f"no such test file: {file_part}")
            file_part = hits[0].file
            f = self.project_dir / file_part
        text = f.read_text(errors="replace")
        results = []
        seen = set()
        for sym in self.index.symbols:
            if sym.file == file_part:
                continue
            import re as _re
            if _re.search(rf"\b{_re.escape(sym.name)}\s*\(", text):
                if sym.name not in seen:
                    results.append({"file": sym.file, "symbol": sym.name, "distance": 1})
                    seen.add(sym.name)
        for r in list(results):
            for callee in self.index.callees_of(r["symbol"]):
                if callee["symbol"] not in seen:
                    results.append({"file": callee["file"], "symbol": callee["symbol"], "distance": 2})
                    seen.add(callee["symbol"])
        if not results:
            return _envelope("failing_context", [], hint=f"no symbols resolved from {test!r}'s own source")
        return _envelope("failing_context", results[:20], truncated=len(results) > 20, total=len(results))

    def list_pending_tasks(self) -> dict:
        """No task queue in this standalone package — always empty. If
        your integration tracks pending tasks, implement this on your own
        ToolBackend to give the agent visibility into related in-flight work."""
        return _envelope("list_pending_tasks", [], hint="no task queue configured")

    # -- writing --------------------------------------------------------------
    def write_file(self, path: str, content: str) -> dict:
        f = self.project_dir / path
        if f.is_dir():
            return _envelope("write_file", [], ok=False, hint=f"{path!r} is a directory, not a file")
        f.parent.mkdir(parents=True, exist_ok=True)
        existed = f.is_file()
        f.write_text(content)
        self.index.reindex_file(path, content)
        return _envelope("write_file", [{"path": path, "bytes_written": len(content.encode()),
                                         "created": not existed}])

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
        f = self.project_dir / path
        if not f.is_file():
            return _envelope("edit_file", [], ok=False, hint=f"no such file: {path}")
        text = f.read_text(errors="replace")
        count = text.count(old_string)
        if count == 0:
            return _envelope("edit_file", [], ok=False, hint="old_string not found")
        if old_string == new_string:
            return _envelope("edit_file", [], ok=False,
                             hint="old_string and new_string are identical; nothing to change")
        if not replace_all and count > 1:
            return _envelope("edit_file", [], ok=False,
                             hint=f"old_string matches {count} locations; add context or use replace_all")
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        f.write_text(new_text)
        self.index.reindex_file(path, new_text)
        return _envelope("edit_file", [{"path": path, "replacements": count if replace_all else 1}])

    def delete_file(self, path: str) -> dict:
        f = self.project_dir / path
        if not f.is_file():
            return _envelope("delete_file", [], ok=False, hint=f"no such file: {path}")
        f.unlink()
        self.index.reindex_file(path, "")
        return _envelope("delete_file", [{"path": path, "deleted": True}])

    def start_watch(self, debounce: float = 0.5) -> dict:
        """No-op: this backend's index is refreshed on write_file/edit_file/
        delete_file/reindex_file, not by a filesystem watcher."""
        return _envelope("start_watch", [{"started": False}],
                         hint="no persisted index to watch; call reindex() to pick up out-of-band changes")

    def stop_watch(self) -> dict:
        return _envelope("stop_watch", [{"stopped": False}])

    def __enter__(self) -> "LocalToolBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


def _import_statement(defined_in: str, symbol: str, importing_file: str) -> str:
    """A best-effort import statement for `symbol` (defined in
    `defined_in`) to use from `importing_file`. Python: dotted module path
    relative to the importing file's own package root guess. JS/TS: a
    relative `./`/`../` specifier with the extension stripped."""
    suffix = Path(defined_in).suffix
    if suffix == ".py":
        mod = defined_in[:-len(suffix)].replace("/", ".")
        return f"from {mod} import {symbol}"
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        import os
        importing_dir = Path(importing_file).parent if importing_file else Path(".")
        rel = os.path.relpath(defined_in[: -len(suffix)], str(importing_dir))
        if not rel.startswith("."):
            rel = f"./{rel}"
        return f"import {{ {symbol} }} from \"{rel}\";"
    return f"# import {symbol} from {defined_in}"


class GraphToolBackend:
    """`LocalToolBackend`'s filesystem operations (view/write/edit/delete/
    resolve_import/failing_context — none of these benefit from a
    persisted graph) delegated straight through, composed with
    `codegraph.CodeGraph` for the query methods that do: search_symbol,
    file_skeleton, callers, callees, path_between, affected_by. See this
    module's docstring for why that split exists."""

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self._local = LocalToolBackend(project_dir)
        self.graph = CodeGraph(project_dir=self.project_dir)
        self.graph.build()

    # -- index lifecycle -----------------------------------------------------
    def reindex(self) -> dict:
        self._local.reindex()
        stats = self.graph.build()
        return _envelope("reindex", [{**stats, **self.graph.counts()}],
                         hint="persisted graph rebuilt incrementally (unchanged files were not re-parsed)")

    def reindex_file(self, path: str) -> dict:
        r = self._local.reindex_file(path)
        f = self.project_dir / path
        content = f.read_text(errors="replace") if f.is_file() else ""
        self.graph.reindex_file(path, content)
        return r

    def health(self) -> dict:
        counts = self.graph.counts()
        return _envelope("health", [{"backend": "persisted-graph", **counts,
                                     "db_path": str(self.graph.db_path)}])

    def describe(self) -> dict:
        return self._local.describe()

    # -- reading (delegated — no graph benefit) -------------------------------
    def view_file(self, path: str, start: int = 1, end: int = VIEW_WINDOW) -> dict:
        return self._local.view_file(path, start, end)

    def view_window(self, path: str, center_line: int, radius: int = VIEW_WINDOW // 2) -> dict:
        return self._local.view_window(path, center_line, radius)

    def resolve_import(self, symbol: str, importing_file: str = "", language: str = "") -> dict:
        hits = self.graph.find(symbol)
        if not hits:
            return _envelope("resolve_import", [], hint=f"{symbol!r} not found anywhere in the project")
        return _envelope("resolve_import", [
            {"symbol": s.name, "defined_in": s.file, "import_statement": _import_statement(s.file, s.name, importing_file)}
            for s in hits
        ])

    def statement_graph(self, file: str, line: Optional[int] = None, radius: int = 5) -> dict:
        """Graph-backed counterpart of LocalToolBackend.statement_graph:
        same shape, answered from the persisted statements/def_use tables
        (codegraph.statements_near) — survives process restarts and avoids
        re-parsing the file per call."""
        if not (self.project_dir / file).is_file():
            return _envelope("statement_graph", [], ok=False, hint=f"no such file: {file}")
        rows = self.graph.statements_near(file, line if line is not None else 1, radius=radius)
        if not rows:
            return _envelope("statement_graph", [],
                             hint=f"no statements recorded near line {line} in {file} "
                                  "(non-Python files record function blocks without "
                                  "def_use edges — see graph_statements.py)")
        results = rows[:40]
        return _envelope("statement_graph", results,
                         truncated=len(rows) > len(results), total=len(rows),
                         hint=None if len(rows) <= len(results)
                         else "window clipped to 40 rows; pass a more specific line")

    def failing_context(self, test: str) -> dict:
        return self._local.failing_context(test)

    def list_pending_tasks(self) -> dict:
        return self._local.list_pending_tasks()

    # -- reading (graph-backed) -----------------------------------------------
    def file_skeleton(self, path: str) -> dict:
        syms = self.graph.file_skeleton(path)
        if not syms:
            f = self.project_dir / path
            hint = "no symbols found" if f.is_file() else f"no such file: {path}"
            return _envelope("file_skeleton", [], hint=hint)
        return _envelope("file_skeleton", [
            {"name": s.name, "kind": s.kind, "line": s.line, "signature": s.signature} for s in syms
        ])

    def search_symbol(self, name: str, kind: str = "") -> dict:
        hits = self.graph.find(name, kind)
        if not hits:
            return _envelope("search_symbol", [], hint=f"no symbol named {name!r} found")
        return _envelope("search_symbol", [
            {"source_file": s.file, "name": s.name, "kind": s.kind, "line": s.line, "signature": s.signature}
            for s in hits
        ])

    def callers(self, symbol: str, depth: int = 1) -> dict:
        hits = self.graph.callers(symbol, depth=depth)
        if not hits:
            return _envelope("callers", [], hint=f"no call sites found for {symbol!r}")
        return _envelope("callers", hits)

    def callees(self, symbol: str, depth: int = 1) -> dict:
        hits = self.graph.callees(symbol, depth=depth)
        if not hits:
            return _envelope("callees", [], hint=f"no known-symbol calls found inside {symbol!r}")
        return _envelope("callees", hits)

    def path_between(self, a: str, b: str) -> dict:
        path = self.graph.path_between(a, b)
        if path is None:
            return _envelope("path_between", [], hint=f"no call path found from {a!r} to {b!r}")
        return _envelope("path_between", [{"path": path}])

    def affected_by(self, file_path: str, max_depth: int = 3, direction: str = "incoming") -> dict:
        hits = self.graph.affected_by(file_path, max_depth, direction)
        return _envelope("affected_by", hits,
                         hint=None if hits else f"nothing depends on {file_path!r} yet" if direction == "incoming"
                         else f"{file_path!r} calls nothing else known")

    # -- writing (delegated, then both indexes updated via reindex_file) -----
    def write_file(self, path: str, content: str) -> dict:
        r = self._local.write_file(path, content)
        self.graph.reindex_file(path, content)
        return r

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
        r = self._local.edit_file(path, old_string, new_string, replace_all)
        f = self.project_dir / path
        if f.is_file():
            self.graph.reindex_file(path, f.read_text(errors="replace"))
        return r

    def delete_file(self, path: str) -> dict:
        r = self._local.delete_file(path)
        self.graph.reindex_file(path, "")
        return r

    def start_watch(self, debounce: float = 0.5) -> dict:
        return self._local.start_watch(debounce)

    def stop_watch(self) -> dict:
        return self._local.stop_watch()

    def __enter__(self) -> "GraphToolBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.graph.close()


def make_tools(project_dir, project: str = "", backend: str = "local") -> ToolBackend:
    """Factory. `backend="local"` (default) returns `LocalToolBackend`
    (in-memory index, rebuilt fresh every process — no setup, no state on
    disk). `backend="graph"` returns `GraphToolBackend` (persisted SQLite
    call graph at `<project_dir>/.forge/codegraph.db` — pays off across
    multiple runs/resumes against the same project_dir, or when you want
    multi-hop callers/callees/affected_by). Kept as a function (not a bare
    constructor call) so a caller can swap backends without every call
    site changing."""
    if backend == "graph":
        return GraphToolBackend(project_dir)
    return LocalToolBackend(project_dir)
