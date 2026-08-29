"""
Statement-level def-use extraction for `codegraph.py` (R11), per ARISE
(arXiv:2605.03117). Python-first and stdlib-`ast`-exact.

Scope model, stated honestly:
  - `exact` edges: the def and the use resolve within the same function
    (or to that file's module-level assignments/imports) under Python's
    real shadowing rules — a local def always shadows the module one.
  - `heuristic` edges: a function-local use of a name with no local def,
    resolved to this file's module-level def (a module constant/global).
    Cross-module shadowing is NOT modeled — that's what the function-level
    `edges` table (callers/callees) is for; statement-level edges
    deliberately stop at this file's boundary.
  - Nested `def`/`class`/`lambda` statements inside a function become ONE
    row (the binding: outer name + params as defs); their internal
    statements are not separately tracked. Attribute access (`self.value`)
    is tracked only for its `self` base — not per-field.
  - Comprehension implicit scopes are modeled as the enclosing scope.

Everything here is per-file and deterministic; `codegraph.py` calls
`extract_for_file()` during the same indexing pass it already runs,
guarded by `FORGE_STATEMENT_GRAPH` so the bigger index can be turned off
per-run (huge repos that want the ripgrep fallback; before/after bench).
"""
from __future__ import annotations

import ast
import sys

#: Compound statements whose nested blocks share the enclosing function's
#: scope — `gen_stmts` descends into those bodies. Nested def/class/lambda
#: bodies are NOT descended into (see module docstring).
_CONTROL_STMTS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                 ast.AsyncWith, ast.Try) + ((ast.TryStar,)
                                            if sys.version_info >= (3, 11)
                                            else ())

_TEXT_CAP = 200


def _iter_scoped(stmt: ast.AST):
    """All nodes of ONE statement's own code. Nested scope nodes are
    yielded themselves (so a nested `def g(...)` binds `g` in the outer
    row) but their children are not descended into."""
    stack = list(ast.iter_child_nodes(stmt))
    while stack:
        n = stack.pop(0)
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _names_in(stmt: ast.stmt) -> tuple[set[str], set[str]]:
    """(defined_names, used_names) for one statement row."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # a nested def's binding: its name (outer scope) + its own params
        return ({stmt.name, *(a.arg for a in ast.walk(stmt.args)
                               if isinstance(a, ast.arg))}, set())
    if isinstance(stmt, ast.ClassDef):
        return {stmt.name}, set()
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return {a.asname or a.name.split(".")[0] for a in stmt.names}, set()
    defs: set[str] = set()
    uses: set[str] = set()
    for n in _iter_scoped(stmt):
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                uses.add(n.id)
            else:
                # Store (assign/for/walrus target) and Del both (re)bind
                defs.add(n.id)
    return defs, uses


def _gen_stmts(stmts: list[ast.stmt]):
    """Source-order statements of one scope, descending into compound
    control blocks (same function scope) but never into nested defs."""
    for st in stmts:
        yield st
        if isinstance(st, _CONTROL_STMTS):
            for field in ("body", "orelse", "finalbody"):
                for inner in getattr(st, field, None) or []:
                    yield from _gen_stmts([inner])
            for handler in getattr(st, "handlers", None) or []:
                yield from _gen_stmts(handler.body)


def extract(rel: str, text: str,
            enclosing_symbols=None) -> tuple[list[dict], list[tuple[int, int, str, str]]]:
    """Statement rows + def_use edges for ONE file.

    rows:  [{file, symbol, kind, line, end_line, text, engine}] — `symbol`
             is the enclosing function (or "<module>"); `engine` is "ast"
             for Python rows, "heuristic" for the non-Python fallback.
    edges: [(def_row_index, use_row_index, name, confidence)] — indexes are
             positions in THIS file's `rows` list. A use resolves to the
             most recent def under Python shadowing: local scope first,
             then the file's module-level defs (confidence "heuristic").

    `enclosing_symbols` (non-Python only): file_skeleton() Symbol rows, so
    `statement_graph` reports honest "block" rows instead of silence.
    """
    lines = text.split("\n")

    if not rel.endswith(".py"):
        rows: list[dict] = []
        for s in enclosing_symbols or []:
            rows.append({"file": rel, "symbol": s.name, "kind": "block",
                         "line": s.line, "end_line": s.end_line,
                         "text": (s.signature or s.name)[:_TEXT_CAP],
                         "engine": "heuristic"})
        return rows, []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []

    rows: list[dict] = []
    edges: list[tuple[int, int, str, str]] = []

    def emit(node: ast.AST, symbol: str, kind: str) -> int:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        rows.append({"file": rel, "symbol": symbol, "kind": kind,
                     "line": start, "end_line": end,
                     "text": "\n".join(lines[start - 1: end])[:_TEXT_CAP],
                     "engine": "ast"})
        return len(rows) - 1

    # ---- pass 1: module level — seed the module scope --------------------
    module_scope: dict[str, int] = {}
    for stmt in tree.body:
        kind = type(stmt).__name__.lower()
        kind = {"functiondef": "def", "asyncfunctiondef": "def",
                "classdef": "def"}.get(kind, kind)
        idx = emit(stmt, "<module>", kind)
        defs, uses = _names_in(stmt)
        for u in sorted(uses):
            src = module_scope.get(u)
            if src is not None:
                edges.append((src, idx, u, "exact"))
        for d in defs:
            module_scope[d] = idx

    # ---- pass 2: every function/method body (any nesting) ----------------
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local: dict[str, int] = {}

        # params bind at function entry — one synthetic row for the def itself
        param_idx = emit(fn, fn.name, "def")
        for p in (a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)):
            local[p] = param_idx

        for stmt in _gen_stmts(fn.body):
            kind = type(stmt).__name__.lower()
            kind = {"functiondef": "def", "asyncfunctiondef": "def",
                    "classdef": "def"}.get(kind, kind)
            idx = emit(stmt, fn.name, kind)
            defs, uses = _names_in(stmt)
            for u in sorted(uses):
                # uses resolve BEFORE this statement's own defs register —
                # `x = x + 1` reads the PREVIOUS x, exactly like Python.
                if u in local:
                    edges.append((local[u], idx, u, "exact"))
                elif u in module_scope:
                    edges.append((module_scope[u], idx, u, "heuristic"))
            for d in defs:
                local[d] = idx

    return rows, edges