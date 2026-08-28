"""
A second, real `ToolBackend` implementation — proves the "bring your own
richer backend" claim in tools.py isn't just a docstring.

`LocalToolBackend` builds an in-memory symbol index up front (`ast` for
Python, regex-heuristic for JS/TS/Java) and answers queries against that
index. `RipgrepToolBackend` makes the opposite trade-off: **no index, no
build step, no staleness to track** — every query shells out to `rg`
against the live working tree. That's a real, useful alternative when:

  - the repo is large enough that an upfront index build is itself slow,
    or the working tree changes out from under the run (another process,
    a build step) faster than `reindex()` calls would catch,
  - you want symbol lookup across a language `symbols.py` doesn't parse at
    all — `rg`'s declaration patterns here cover Python/JS/TS/Java/Go/Rust,
    anything the regex list below is extended to cover,
  - you already depend on ripgrep being present (most CI images do) and
    would rather not carry a second index implementation to keep in sync.

The trade-off this backend accepts in return: `callers`/`callees`/
`path_between`/`affected_by` are **textual heuristics** (regex-matched
call sites and import statements), not a real resolved call graph the way
`symbols.SymbolIndex` gives you for Python. That's stated in each method's
docstring below rather than glossed over — this file is a worked example
of the protocol, not a claim that live-grep is strictly better than an
index.

Usage — swap it in wherever `make_tools()` is called:

    from ripgrep_tool_backend import RipgrepToolBackend
    tools = RipgrepToolBackend(project_dir)

Requires the `rg` binary on PATH. No other dependency — deliberately, so
this stays a single-file reference rather than a new package to maintain.
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

VIEW_WINDOW = 100

#: name -> (kind, rg regex). One alternation per language family; broad on
#: purpose (a live-grep heuristic, not a parser) — false positives are
#: possible (e.g. a comment containing "def foo("), false negatives are
#: not silent (every query returns a `hint` when it comes back empty).
_DECL_PATTERNS = {
    "function": r"^\s*(def|function|func|fn)\s+{name}\b",
    "class": r"^\s*(class|struct|trait|interface)\s+{name}\b",
    "const": r"^\s*(const|let|var|val)\s+{name}\b",
}
_ANY_DECL = r"^\s*(def|class|function|func|fn|struct|trait|interface|const|let|var|val)\s+([A-Za-z_][A-Za-z0-9_]*)"


def _envelope(tool: str, results: list, hint: Optional[str] = None,
              truncated: bool = False, total: Optional[int] = None, ok: bool = True) -> dict:
    return {"ok": ok, "tool": tool, "results": results, "truncated": truncated,
            "total": total if total is not None else len(results), "hint": hint}


def _require_rg() -> None:
    if shutil.which("rg") is None:
        raise RuntimeError(
            "RipgrepToolBackend requires the `rg` (ripgrep) binary on PATH; "
            "install it (e.g. `apt install ripgrep` / `brew install ripgrep`) "
            "or use LocalToolBackend instead."
        )


def _rg(args: list, cwd: Path) -> list[str]:
    """Runs rg, returns stdout lines. rg exits 1 (not an error here) when
    nothing matches, and exits 2 for "no files were searched" (an empty
    or all-ignored directory — also not an error: it just means zero
    matches). Only a genuine failure (bad pattern, missing path) raises."""
    proc = subprocess.run(["rg", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode == 2 and "No files were searched" in proc.stderr:
        return []
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"rg failed ({proc.returncode}): {proc.stderr.strip()}")
    return [ln for ln in proc.stdout.splitlines() if ln]


class RipgrepToolBackend:
    """Live-filesystem `ToolBackend` backed by `rg` — no persisted index."""

    def __init__(self, project_dir):
        _require_rg()
        self.project_dir = Path(project_dir)

    # -- introspection --------------------------------------------------------
    def health(self) -> dict:
        return _envelope("health", [{"backend": "ripgrep-live", "project_dir": str(self.project_dir)}])

    def describe(self) -> dict:
        manifest = []
        excluded = {"describe", "health"}
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
                         hint="live-grep backend: every query re-scans the working tree, "
                              "there is nothing to go stale")

    def reindex(self) -> dict:
        return _envelope("reindex", [{"indexed": False}],
                         hint="no persisted index — every query already reads the live tree")

    def reindex_file(self, path: str) -> dict:
        return _envelope("reindex_file", [{"path": path, "indexed": False}],
                         hint="no-op: this backend has no index to refresh")

    def list_pending_tasks(self) -> dict:
        return _envelope("list_pending_tasks", [], hint="no task queue configured")

    # -- reading ----------------------------------------------------------------
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

    def file_skeleton(self, path: str) -> dict:
        """Declarations found in this one file, via a live `rg -n` over
        just that path — not a parsed skeleton."""
        f = self.project_dir / path
        if not f.is_file():
            return _envelope("file_skeleton", [], hint=f"no such file: {path}")
        results = []
        for ln in _rg(["-n", "-P", _ANY_DECL, path], self.project_dir):
            line_no, _, text = ln.partition(":")
            m = re.search(_ANY_DECL, text)
            if m:
                results.append({"name": m.group(2), "kind": m.group(1), "line": int(line_no),
                                 "signature": text.strip()})
        if not results:
            return _envelope("file_skeleton", [], hint="no declarations matched in this file")
        return _envelope("file_skeleton", results)

    def search_symbol(self, name: str, kind: str = "") -> dict:
        """Live `rg` for a declaration of `name` across the tree — a
        textual match, not a resolved symbol-table lookup."""
        patterns = [_DECL_PATTERNS[kind].format(name=re.escape(name))] if kind in _DECL_PATTERNS \
            else [p.format(name=re.escape(name)) for p in _DECL_PATTERNS.values()]
        results = []
        for pattern in patterns:
            for ln in _rg(["-n", "-P", pattern], self.project_dir):
                path, _, rest = ln.partition(":")
                line_no, _, text = rest.partition(":")
                results.append({"source_file": path, "name": name, "kind": kind or "unknown",
                                 "line": int(line_no), "signature": text.strip()})
        if not results:
            return _envelope("search_symbol", [], hint=f"no declaration of {name!r} found")
        return _envelope("search_symbol", results)

    def callers(self, symbol: str) -> dict:
        """Live `rg` for `symbol(` call sites, minus lines that look like
        the declaration itself — a textual call-site match, not a
        resolved call graph."""
        hits = []
        for ln in _rg(["-n", "-P", rf"\b{re.escape(symbol)}\s*\("], self.project_dir):
            path, _, rest = ln.partition(":")
            line_no, _, text = rest.partition(":")
            if re.match(_ANY_DECL.replace("{name}", re.escape(symbol)), text) or \
               re.search(rf"^\s*(def|function|func|fn)\s+{re.escape(symbol)}\b", text):
                continue
            hits.append({"file": path, "line": int(line_no), "text": text.strip()})
        if not hits:
            return _envelope("callers", [], hint=f"no call sites found for {symbol!r}")
        return _envelope("callers", hits)

    def callees(self, symbol: str) -> dict:
        """Identifiers called inside `symbol`'s own body window — found by
        locating the declaration then scanning forward to the next
        top-level declaration (or 200 lines, whichever first). Returns raw
        call-site identifiers, not confirmed-resolved symbols."""
        found = self.search_symbol(symbol)["results"]
        if not found:
            return _envelope("callees", [], hint=f"{symbol!r} not found; cannot inspect its body")
        target = found[0]
        f = self.project_dir / target["source_file"]
        lines = f.read_text(errors="replace").split("\n")
        start = target["line"]
        end = len(lines)
        for i in range(start, min(start + 200, len(lines))):
            if i > start and re.match(_ANY_DECL, lines[i]):
                end = i
                break
        body = "\n".join(lines[start:end])
        names = sorted(set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)))
        keywords = {"if", "for", "while", "with", "return", "print", "super", symbol}
        results = [{"symbol": n, "file": target["source_file"]} for n in names if n not in keywords]
        if not results:
            return _envelope("callees", [], hint=f"no call-site identifiers found inside {symbol!r}")
        return _envelope("callees", results)

    def path_between(self, a: str, b: str, _max_depth: int = 4) -> dict:
        """BFS over `callees()`, capped at `_max_depth` hops — cheap on a
        live-grep backend only because each hop is one more `rg` scan, not
        a graph traversal over a pre-built adjacency list."""
        frontier = [[a]]
        seen = {a}
        for _ in range(_max_depth):
            next_frontier = []
            for path in frontier:
                for callee in self.callees(path[-1])["results"]:
                    nxt = callee["symbol"]
                    if nxt == b:
                        return _envelope("path_between", [{"path": path + [nxt]}])
                    if nxt not in seen:
                        seen.add(nxt)
                        next_frontier.append(path + [nxt])
            frontier = next_frontier
            if not frontier:
                break
        return _envelope("path_between", [], hint=f"no call path found from {a!r} to {b!r} within {_max_depth} hops")

    def affected_by(self, file_path: str, max_depth: int = 3, direction: str = "incoming") -> dict:
        """incoming: files whose text mentions this file's module stem (a
        textual proxy for "imports it"). outgoing: this file's own import
        lines. Both are heuristic — no resolved dependency graph."""
        stem = Path(file_path).stem
        if direction == "outgoing":
            f = self.project_dir / file_path
            if not f.is_file():
                return _envelope("affected_by", [], hint=f"no such file: {file_path}")
            results = []
            for ln in f.read_text(errors="replace").splitlines():
                if re.match(r"^\s*(import|from|require\(|#include)", ln):
                    results.append({"file": file_path, "line": ln.strip()})
            return _envelope("affected_by", results,
                             hint=None if results else f"{file_path!r} has no recognizable import lines")
        results = []
        for ln in _rg(["-n", "-l", re.escape(stem)], self.project_dir):
            if ln != file_path:
                results.append({"file": ln, "depends_on": file_path})
        return _envelope("affected_by", results,
                         hint=None if results else f"nothing mentions {file_path!r}'s module name")

    def resolve_import(self, symbol: str, importing_file: str = "", language: str = "") -> dict:
        found = self.search_symbol(symbol)["results"]
        if not found:
            return _envelope("resolve_import", [], hint=f"{symbol!r} not found anywhere in the project")
        results = []
        for s in found:
            results.append({"symbol": symbol, "defined_in": s["source_file"],
                             "import_statement": _import_statement(s["source_file"], symbol, importing_file)})
        return _envelope("resolve_import", results)

    def failing_context(self, test: str) -> dict:
        """Given a failing test id, greps the test file's own source for
        identifiers that resolve to a real declaration elsewhere — a
        single-hop, textual version of `LocalToolBackend`'s distance-2
        ranking."""
        file_part = test.split("::")[0]
        f = self.project_dir / file_part
        if not f.is_file():
            return _envelope("failing_context", [], hint=f"no such test file: {file_part}")
        text = f.read_text(errors="replace")
        candidates = sorted(set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)))
        results = []
        for name in candidates:
            hits = self.search_symbol(name)["results"]
            for h in hits:
                if h["source_file"] != file_part:
                    results.append({"file": h["source_file"], "symbol": name, "distance": 1})
        if not results:
            return _envelope("failing_context", [], hint=f"no symbols resolved from {test!r}'s own source")
        return _envelope("failing_context", results[:20], truncated=len(results) > 20, total=len(results))

    # -- writing ------------------------------------------------------------------
    def write_file(self, path: str, content: str) -> dict:
        f = self.project_dir / path
        if f.is_dir():
            return _envelope("write_file", [], ok=False, hint=f"{path!r} is a directory, not a file")
        f.parent.mkdir(parents=True, exist_ok=True)
        existed = f.is_file()
        f.write_text(content)
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
        return _envelope("edit_file", [{"path": path, "replacements": count if replace_all else 1}])

    def delete_file(self, path: str) -> dict:
        f = self.project_dir / path
        if not f.is_file():
            return _envelope("delete_file", [], ok=False, hint=f"no such file: {path}")
        f.unlink()
        return _envelope("delete_file", [{"path": path, "deleted": True}])

    def start_watch(self, debounce: float = 0.5) -> dict:
        return _envelope("start_watch", [{"started": False}],
                         hint="no persisted index to watch; every query already reads the live tree")

    def stop_watch(self) -> dict:
        return _envelope("stop_watch", [{"stopped": False}])

    def __enter__(self) -> "RipgrepToolBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


def _import_statement(defined_in: str, symbol: str, importing_file: str) -> str:
    suffix = Path(defined_in).suffix
    if suffix == ".py":
        mod = defined_in[: -len(suffix)].replace("/", ".")
        return f"from {mod} import {symbol}"
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        importing_dir = Path(importing_file).parent if importing_file else Path(".")
        rel = os.path.relpath(defined_in[: -len(suffix)], str(importing_dir))
        if not rel.startswith("."):
            rel = f"./{rel}"
        return f'import {{ {symbol} }} from "{rel}";'
    return f"# import {symbol} from {defined_in}"
