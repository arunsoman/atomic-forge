"""
A persisted, whole-project call graph — `symbols.SymbolIndex`'s exact
parsers (Python via `ast`, regex-heuristic for JS/TS/Java), but with two
things a pure in-memory index can't give you:

  1. **Persistence.** Symbols and call edges are written to
     `<project_dir>/.forge/codegraph.db` (SQLite). A second `CodeGraph`
     pointed at the same project_dir — a fresh process, a resumed run —
     loads straight from disk instead of re-walking and re-parsing every
     file. `build()` is incremental: each file's content hash is compared
     against what's stored, and only changed/new/deleted files are
     re-parsed; an unchanged tree costs one query per file, not N parses.
  2. **Precomputed edges, not regex-per-query.** `SymbolIndex.callers_of`/
     `callees_of` re-scan cached source text with a fresh regex on every
     call. Here, every symbol's call edges are computed ONCE at build
     time and stored in an indexed `edges` table, so `callers`/`callees`
     — and multi-hop `affected_by`/`path_between` — are graph lookups
     against an index, not O(project size) text scans repeated per call.

Same query surface as `SymbolIndex` plus depth-bounded transitive
traversal (`callers`/`callees` accept `depth`). `GraphToolBackend` below
wraps this behind the same `ToolBackend` protocol `LocalToolBackend`
implements — swap one line (`make_tools(..., backend="graph")`) to use
it; nothing else about the generate/repair pipeline changes, since both
backends speak the same protocol.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .symbols import SymbolIndex, Symbol, _CALL_RE_TEMPLATE, _EXTENSIONS, _SKIP_DIRS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE TABLE IF NOT EXISTS edges (
    caller_symbol TEXT NOT NULL,
    caller_file TEXT NOT NULL,
    callee_symbol TEXT NOT NULL,
    callee_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(caller_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON edges(callee_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_caller_file ON edges(caller_file);
CREATE INDEX IF NOT EXISTS idx_edges_callee_file ON edges(callee_file);

-- R11 statement-level def-use (ARISE, arXiv:2605.03117). ADDITIVE — the
-- function-level tables above stay authoritative for callers/callees;
-- these record per-statement binds/uses inside each function. Populated
-- during the same indexing pass, guarded by FORGE_STATEMENT_GRAPH (see
-- graph_statements.py).
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    engine TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statements_file_symbol ON statements(file, symbol);
CREATE INDEX IF NOT EXISTS idx_statements_line ON statements(file, line);
CREATE TABLE IF NOT EXISTS def_use (
    def_stmt INTEGER NOT NULL REFERENCES statements(id),
    use_stmt INTEGER NOT NULL REFERENCES statements(id),
    name TEXT NOT NULL,
    confidence TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_def_use_name ON def_use(name);
CREATE INDEX IF NOT EXISTS idx_def_use_stmts ON def_use(def_stmt, use_stmt);
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _statement_graph_enabled() -> bool:
    """R11 statement tables are built by default; FORGE_STATEMENT_GRAPH=0
    disables them (they meaningfully grow the index — see
    req-enterprise-scale-indexing's fallback-path note)."""
    return os.environ.get("FORGE_STATEMENT_GRAPH", "1") != "0"


@dataclass
class CodeGraph:
    """Owns one SQLite file. Not thread-safe for concurrent writers —
    callers already serialize writes behind the same write_lock the rest
    of generate_agent/repair_agent use for tool-backend mutation."""

    project_dir: Path
    db_path: Optional[Path] = None
    _conn: sqlite3.Connection = None  # type: ignore[assignment]
    #: Serializes every access to `_conn` — sqlite3 connections aren't
    #: thread-safe for concurrent use even for reads (Python's sqlite3
    #: module refuses cross-thread use of one connection object by
    #: default). Needed since `repair_agent.py` now runs K sampled repair
    #: attempts in parallel threads, each issuing read queries (callers/
    #: callees/search_symbol/affected_by/path_between) against the SAME
    #: CodeGraph/connection concurrently. Query volume here is light
    #: enough that serializing behind one lock (rather than a connection
    #: pool) is the simplest correct fix, not a throughput bottleneck.
    #: RLock, not Lock: `_compute_edges` (called from inside `build`/
    #: `reindex_file`, both already lock-held) calls back into
    #: `file_skeleton`, which itself acquires this lock — a plain Lock
    #: would deadlock a single thread against itself on that call chain.
    _query_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.db_path = self.db_path or (self.project_dir / ".forge" / "codegraph.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        #: file -> raw text, cached only for the current process (never
        #: persisted — re-read from disk on the next process, cheap
        #: compared to re-parsing).
        self._text: Dict[str, str] = {}

    # ------------------------------------------------------------ build ----

    def build(self) -> dict:
        """Incremental full-project scan. Returns {"parsed": N, "unchanged":
        N, "removed": N} so a caller can see how much work was actually
        done (e.g. assert a second build() on an untouched tree parses 0)."""
        with self._query_lock:
            return self._build_locked()

    def _build_locked(self) -> dict:
        on_disk: Dict[str, Path] = {}
        for path in self.project_dir.rglob("*"):
            if not path.is_file() or path.suffix not in _EXTENSIONS:
                continue
            if any(part in _SKIP_DIRS for part in path.relative_to(self.project_dir).parts):
                continue
            on_disk[str(path.relative_to(self.project_dir))] = path

        stored = dict(self._conn.execute("SELECT path, content_hash FROM files").fetchall())

        #: Two passes, not one: edge computation for file X needs every
        #: OTHER file's symbols already in the table too, including files
        #: not yet visited in this same build() call — a single-pass
        #: insert-then-link-immediately would silently miss edges into
        #: symbols defined by a file this loop hasn't reached yet.
        changed: list[tuple[str, str, str, float]] = []  # (rel, text, hash, mtime)
        parsed = unchanged = 0
        for rel, path in on_disk.items():
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            h = _hash(text)
            if stored.get(rel) == h:
                unchanged += 1
                continue
            changed.append((rel, text, h, path.stat().st_mtime))
            parsed += 1

        for rel, text, h, mtime in changed:
            self._insert_symbols(rel, text, h, mtime)

        for rel, text, _h, _mtime in changed:
            self._compute_edges(rel, text)

        removed = 0
        for rel in set(stored) - set(on_disk):
            self._remove_file(rel)
            removed += 1

        self._conn.commit()
        return {"parsed": parsed, "unchanged": unchanged, "removed": removed}

    def reindex_file(self, rel_path: str, content: str) -> None:
        """Incremental single-file update — the same operation
        write_file/edit_file/delete_file already trigger on
        `LocalToolBackend`, kept cheap here too (one file's worth of
        parsing, not a full-project rebuild). Safe to call standalone
        (outside build()) because every OTHER file's symbols are already
        persisted from a prior build/reindex — only build()'s multi-new-
        file case needs the two-pass split."""
        with self._query_lock:
            if content:
                self._insert_symbols(rel_path, content, _hash(content), time.time())
                self._compute_edges(rel_path, content)
            else:
                self._remove_file(rel_path)
            self._conn.commit()

    def _remove_file(self, rel: str) -> None:
        self._conn.execute("DELETE FROM files WHERE path = ?", (rel,))
        self._conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
        self._conn.execute("DELETE FROM edges WHERE caller_file = ? OR callee_file = ?", (rel, rel))
        self._conn.execute(
            "DELETE FROM def_use WHERE def_stmt IN (SELECT id FROM statements WHERE file = ?) "
            "OR use_stmt IN (SELECT id FROM statements WHERE file = ?)", (rel, rel))
        self._conn.execute("DELETE FROM statements WHERE file = ?", (rel,))
        self._text.pop(rel, None)

    def _insert_symbols(self, rel: str, text: str, content_hash: str, mtime: float) -> None:
        self._remove_file(rel)
        self._text[rel] = text
        self._conn.execute(
            "INSERT INTO files(path, content_hash, mtime) VALUES (?, ?, ?)", (rel, content_hash, mtime),
        )
        for s in _parse(rel, text):
            self._conn.execute(
                "INSERT INTO symbols(file, name, kind, line, end_line, signature) VALUES (?, ?, ?, ?, ?, ?)",
                (s.file, s.name, s.kind, s.line, s.end_line, s.signature),
            )
        self._insert_statements(rel, text)

    def _insert_statements(self, rel: str, text: str) -> None:
        """R11: statement rows + def_use edges for this file, extracted in
        the same locking/integration pass as symbols (a caller never
        invokes this directly — _insert_symbols leads)."""
        if not _statement_graph_enabled():
            return
        from .graph_statements import extract
        enclosing = None  # graph_statements pulls its own fallback source for non-py
        if not rel.endswith(".py"):
            enclosing = self.file_skeleton(rel)
        rows, edges = extract(rel, text, enclosing_symbols=enclosing)
        # row index -> sqlite id, edges reference positions in `rows`
        row_ids: list[int] = []
        for r in rows:
            cur = self._conn.execute(
                "INSERT INTO statements(file, symbol, kind, line, end_line, text, engine) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["file"], r["symbol"], r["kind"], r["line"], r["end_line"], r["text"], r["engine"]),
            )
            row_ids.append(cur.lastrowid)  # type: ignore[arg-type]
        # module rows exist before function rows per file (pass 1 before
        # pass 2), so every edge reference resolves within this insert.
        for def_i, use_i, name, confidence in edges:
            self._conn.execute(
                "INSERT INTO def_use(def_stmt, use_stmt, name, confidence) VALUES (?, ?, ?, ?)",
                (row_ids[def_i], row_ids[use_i], name, confidence),
            )

    def _compute_edges(self, rel: str, text: str) -> None:
        """Edges from THIS file's symbols outward: recomputed fresh
        against the CURRENT full symbol table (must be called only after
        every touched file's symbols are already inserted — see build()'s
        two-pass split). Edges INTO this file's symbols from other,
        untouched files are unaffected: this file's own line numbers
        changing doesn't change what other files' call sites match."""
        self._conn.execute("DELETE FROM edges WHERE caller_file = ?", (rel,))
        syms = self.file_skeleton(rel)
        if not syms:
            return
        all_names = {row[0] for row in self._conn.execute("SELECT DISTINCT name FROM symbols")}
        lines = text.split("\n")
        for s in syms:
            body = "\n".join(lines[s.line - 1: s.end_line])
            for other_name in all_names:
                if other_name == s.name:
                    continue
                if re.search(_CALL_RE_TEMPLATE.format(name=re.escape(other_name)), body):
                    for callee_file, in self._conn.execute(
                        "SELECT DISTINCT file FROM symbols WHERE name = ?", (other_name,)
                    ):
                        self._conn.execute(
                            "INSERT INTO edges(caller_symbol, caller_file, callee_symbol, callee_file) "
                            "VALUES (?, ?, ?, ?)",
                            (s.name, s.file, other_name, callee_file),
                        )

    # ----------------------------------------------------------- queries ----

    def counts(self) -> dict:
        with self._query_lock:
            files = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            symbols = self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            statements = self._conn.execute(
                "SELECT COUNT(*) FROM statements").fetchone()[0]
            def_use = self._conn.execute("SELECT COUNT(*) FROM def_use").fetchone()[0]
        return {"files": files, "symbols": symbols, "edges": edges,
                "statements": statements, "def_use": def_use}

    def find(self, name: str, kind: str = "") -> List[Symbol]:
        with self._query_lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT file, name, kind, line, end_line, signature FROM symbols WHERE name = ? AND kind = ?",
                    (name, kind),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT file, name, kind, line, end_line, signature FROM symbols WHERE name = ?", (name,),
                ).fetchall()
        return [Symbol(name=r[1], kind=r[2], file=r[0], line=r[3], end_line=r[4], signature=r[5]) for r in rows]

    def file_skeleton(self, rel_path: str) -> List[Symbol]:
        with self._query_lock:
            rows = self._conn.execute(
                "SELECT file, name, kind, line, end_line, signature FROM symbols WHERE file = ? ORDER BY line",
                (rel_path,),
            ).fetchall()
        return [Symbol(name=r[1], kind=r[2], file=r[0], line=r[3], end_line=r[4], signature=r[5]) for r in rows]

    def callers(self, name: str, depth: int = 1) -> List[dict]:
        """BFS over precomputed edges, up to `depth` hops. depth=1 matches
        SymbolIndex.callers_of's direct-only behavior; depth>1 is the
        persisted graph's actual advantage — a multi-hop traversal
        SymbolIndex would need a fresh regex scan per hop to answer."""
        with self._query_lock:
            return self._traverse(name, depth, "callee_symbol", "caller_symbol", "caller_file")

    def callees(self, name: str, depth: int = 1) -> List[dict]:
        with self._query_lock:
            return self._traverse(name, depth, "caller_symbol", "callee_symbol", "callee_file")

    def _traverse(self, start: str, depth: int, match_col: str, next_col: str, file_col: str) -> List[dict]:
        """Caller must already hold `_query_lock` — see callers()/callees()."""
        frontier = {start}
        seen = {start}
        out: List[dict] = []
        for _ in range(max(1, depth)):
            next_frontier: set = set()
            for name in frontier:
                rows = self._conn.execute(
                    f"SELECT DISTINCT {next_col}, {file_col} FROM edges WHERE {match_col} = ?", (name,),
                ).fetchall()
                for sym, file in rows:
                    key = "caller_file" if file_col == "caller_file" else "file"
                    out.append({key: file, "symbol": sym} if file_col == "caller_file"
                               else {"file": file, "symbol": sym})
                    if sym not in seen:
                        seen.add(sym)
                        next_frontier.add(sym)
            if not next_frontier:
                break
            frontier = next_frontier
        return out

    def path_between(self, a: str, b: str, max_depth: int = 6) -> Optional[List[str]]:
        if a == b:
            return [a]
        frontier = [[a]]
        seen = {a}
        for _ in range(max_depth):
            next_frontier = []
            for path in frontier:
                with self._query_lock:
                    rows = self._conn.execute(
                        "SELECT DISTINCT callee_symbol FROM edges WHERE caller_symbol = ?", (path[-1],),
                    ).fetchall()
                for (name,) in rows:
                    if name in seen:
                        continue
                    new_path = path + [name]
                    if name == b:
                        return new_path
                    seen.add(name)
                    next_frontier.append(new_path)
            if not next_frontier:
                break
            frontier = next_frontier
        return None

    def affected_by(self, file_path: str, max_depth: int = 3, direction: str = "incoming") -> List[dict]:
        """Files that (transitively, up to max_depth) call into
        `file_path`'s symbols (incoming — this file's blast radius) or
        that `file_path` itself calls into (outgoing)."""
        with self._query_lock:
            defined_here = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT name FROM symbols WHERE file = ?", (file_path,)
            ).fetchall()]
        out: Dict[str, dict] = {}
        frontier = set(defined_here)
        seen_symbols = set(defined_here)
        for _ in range(max(1, max_depth)):
            next_frontier: set = set()
            for name in frontier:
                with self._query_lock:
                    if direction == "incoming":
                        rows = self._conn.execute(
                            "SELECT DISTINCT caller_symbol, caller_file FROM edges WHERE callee_symbol = ?", (name,)
                        ).fetchall()
                    else:
                        rows = self._conn.execute(
                            "SELECT DISTINCT callee_symbol, callee_file FROM edges WHERE caller_symbol = ?", (name,)
                        ).fetchall()
                for sym, file in rows:
                    if file != file_path:
                        out[file] = {"file": file, "via": name}
                    if sym not in seen_symbols:
                        seen_symbols.add(sym)
                        next_frontier.add(sym)
            if not next_frontier:
                break
            frontier = next_frontier
        return list(out.values())

    def statements_near(self, file_path: str, line: int, radius: int = 5,
                        limit: int = 40) -> list[dict]:
        """R11: the statement-level context around a line — rows overlapping
        [line-radius, line+radius], each annotated with which statements it
        defines/uses (def_use resolved). This is what the repair loop's
        `statement_graph` tool returns (see tools.GraphToolBackend)."""
        with self._query_lock:
            rows = self._conn.execute(
                "SELECT id, symbol, kind, line, end_line, text, engine FROM statements "
                "WHERE file = ? AND line <= ? AND end_line >= ? ORDER BY line LIMIT ?",
                (file_path, line + radius, line - radius, limit),
            ).fetchall()
            out: list[dict] = []
            for sid, symbol, kind, lineno, end_line, text, engine in rows:
                defines = [r[0] for r in self._conn.execute(
                    "SELECT DISTINCT name FROM def_use WHERE def_stmt = ? ORDER BY name", (sid,))]
                uses = [r[0] for r in self._conn.execute(
                    "SELECT name FROM def_use WHERE use_stmt = ? ORDER BY name", (sid,))]
                out.append({"file": file_path, "symbol": symbol, "kind": kind,
                            "line": lineno, "end_line": end_line, "text": text,
                            "engine": engine, "defines": defines,
                            "reads_from": uses})
        return out

    def uses_of(self, name: str, file_path: str = "", limit: int = 40) -> list[dict]:
        """R11: statement-level usage sites for `name` — each result names
        BOTH the using statement (file/symbol/line) and the def it reads
        (per statement-level def-use, not just the call graph). Empty when
        the name is never bound/used at statement level (e.g. only
        mentioned in non-Python blocks, which have no def_use edges by
        design — see graph_statements.py)."""
        with self._query_lock:
            if file_path:
                rows = self._conn.execute(
                    "SELECT u.file, u.symbol, u.line, u.end_line, d.file, d.symbol, d.line, du.confidence "
                    "FROM def_use du JOIN statements u ON u.id = du.use_stmt "
                    "LEFT JOIN statements d ON d.id = du.def_stmt "
                    "WHERE du.name = ? AND u.file = ? ORDER BY u.file, u.line LIMIT ?",
                    (name, file_path, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT u.file, u.symbol, u.line, u.end_line, d.file, d.symbol, d.line, du.confidence "
                    "FROM def_use du JOIN statements u ON u.id = du.use_stmt "
                    "LEFT JOIN statements d ON d.id = du.def_stmt "
                    "WHERE du.name = ? ORDER BY u.file, u.line LIMIT ?",
                    (name, limit)).fetchall()
        return [
            {"file": r[0], "symbol": r[1], "line": r[2], "end_line": r[3],
             "def_file": r[4], "def_symbol": r[5], "def_line": r[6],
             "confidence": r[7]}
            for r in rows
        ]

    def source_span(self, sym: Symbol) -> str:
        text = self._text.get(sym.file)
        if text is None:
            f = self.project_dir / sym.file
            text = f.read_text(errors="replace") if f.is_file() else ""
            self._text[sym.file] = text
        lines = text.split("\n")
        return "\n".join(lines[sym.line - 1: sym.end_line])

    def close(self) -> None:
        self._conn.close()


def _parse(rel: str, text: str) -> List[Symbol]:
    """One-off parser reuse: SymbolIndex's per-language `_parse_*` methods
    only need `self` for nothing (pure functions of rel+text), so a
    throwaway index with an empty project_dir is a safe, cheap way to
    reuse them without duplicating the Python/JS/TS/Java parsing logic
    here."""
    idx = SymbolIndex(project_dir=Path("."))
    return idx._parse(rel, text)


def make_codegraph(project_dir) -> CodeGraph:
    graph = CodeGraph(project_dir=Path(project_dir))
    graph.build()
    return graph
