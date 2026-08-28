"""
A small, dependency-free symbol index: enough for the agent's investigation
tools (view_file/file_skeleton/search_symbol/callers/callees/path_between/
affected_by/failing_context) to give real, useful answers without a
database — exact for Python (stdlib `ast`), regex-heuristic for JS/TS/JSX/
TSX/Java.

Not a substitute for a real language server. It answers "who defines X" /
"who calls X" / "what does X call" well enough to localize a failure and
ground a generation/repair prompt — that's the whole job it needs to do.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".forge_venv",
    ".pytest_cache", "dist", "build", ".next", ".angular", "target", ".gradle",
    ".forge",
}
_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java"}

_JS_DEF_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_JS_CLASS_RE = re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?class\s+(\w+)", re.MULTILINE)
_JS_CONST_FN_RE = re.compile(
    r"^[ \t]*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>", re.MULTILINE)
_JAVA_CLASS_RE = re.compile(r"^[ \t]*(?:public|private|protected)?\s*(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)
_JAVA_METHOD_RE = re.compile(
    r"^[ \t]*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+?\s(\w+)\s*\(([^)]*)\)\s*\{", re.MULTILINE)
_CALL_RE_TEMPLATE = r"\b{name}\s*\("


@dataclass
class Symbol:
    name: str
    kind: str          # "function" | "class" | "method"
    file: str           # project_dir-relative
    line: int
    end_line: int
    signature: str = ""


@dataclass
class SymbolIndex:
    project_dir: Path
    symbols: List[Symbol] = field(default_factory=list)
    #: file (rel) -> raw text, cached at index build time; refreshed by
    #: reindex_file for a single file without a full walk.
    _text: Dict[str, str] = field(default_factory=dict)

    def build(self) -> None:
        self.symbols = []
        self._text = {}
        for path in self._walk():
            rel = str(path.relative_to(self.project_dir))
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            self._text[rel] = text
            self.symbols.extend(self._parse(rel, text))

    def _walk(self):
        for path in self.project_dir.rglob("*"):
            if not path.is_file() or path.suffix not in _EXTENSIONS:
                continue
            if any(part in _SKIP_DIRS for part in path.relative_to(self.project_dir).parts):
                continue
            yield path

    def reindex_file(self, rel_path: str, content: str) -> None:
        self.symbols = [s for s in self.symbols if s.file != rel_path]
        if content:
            self._text[rel_path] = content
            self.symbols.extend(self._parse(rel_path, content))
        else:
            self._text.pop(rel_path, None)

    def _parse(self, rel: str, text: str) -> List[Symbol]:
        suffix = Path(rel).suffix
        if suffix == ".py":
            return self._parse_py(rel, text)
        if suffix == ".java":
            return self._parse_java(rel, text)
        return self._parse_ts(rel, text)

    def _parse_py(self, rel: str, text: str) -> List[Symbol]:
        out: List[Symbol] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out

        def sig_of(node) -> str:
            try:
                args = ast.unparse(node.args)
            except Exception:  # noqa: BLE001 - best-effort
                args = "..."
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            return f"{prefix} {node.name}({args})"

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(Symbol(
                    name=node.name, kind="function", file=rel,
                    line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                    signature=sig_of(node),
                ))
            elif isinstance(node, ast.ClassDef):
                out.append(Symbol(
                    name=node.name, kind="class", file=rel,
                    line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                    signature=f"class {node.name}",
                ))
        return out

    def _parse_ts(self, rel: str, text: str) -> List[Symbol]:
        out: List[Symbol] = []
        lines = text.count("\n") + 1
        matches: List[tuple[int, str, str, str]] = []  # (line, name, kind, sig)
        for m in _JS_DEF_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            matches.append((line, m.group(1), "function", f"function {m.group(1)}({m.group(2)})"))
        for m in _JS_CONST_FN_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            matches.append((line, m.group(1), "function", f"const {m.group(1)} = ({m.group(2)}) =>"))
        for m in _JS_CLASS_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            matches.append((line, m.group(1), "class", f"class {m.group(1)}"))
        matches.sort(key=lambda t: t[0])
        for i, (line, name, kind, sig) in enumerate(matches):
            end = matches[i + 1][0] - 1 if i + 1 < len(matches) else lines
            out.append(Symbol(name=name, kind=kind, file=rel, line=line, end_line=end, signature=sig))
        return out

    def _parse_java(self, rel: str, text: str) -> List[Symbol]:
        out: List[Symbol] = []
        lines = text.count("\n") + 1
        matches: List[tuple[int, str, str, str]] = []
        for m in _JAVA_CLASS_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            matches.append((line, m.group(1), "class", f"class {m.group(1)}"))
        for m in _JAVA_METHOD_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            matches.append((line, m.group(1), "method", f"{m.group(1)}({m.group(2)})"))
        matches.sort(key=lambda t: t[0])
        for i, (line, name, kind, sig) in enumerate(matches):
            end = matches[i + 1][0] - 1 if i + 1 < len(matches) else lines
            out.append(Symbol(name=name, kind=kind, file=rel, line=line, end_line=end, signature=sig))
        return out

    # ------------------------------------------------------------ queries ----

    def find(self, name: str, kind: str = "") -> List[Symbol]:
        return [s for s in self.symbols if s.name == name and (not kind or s.kind == kind)]

    def source_span(self, sym: Symbol) -> str:
        text = self._text.get(sym.file, "")
        if not text:
            return ""
        lines = text.split("\n")
        return "\n".join(lines[sym.line - 1: sym.end_line])

    def callers_of(self, name: str) -> List[dict]:
        """Every file with a call-site `name(` outside `name`'s own
        definition line(s)."""
        pattern = re.compile(_CALL_RE_TEMPLATE.format(name=re.escape(name)))
        own_files = {s.file for s in self.find(name)}
        hits: List[dict] = []
        for file, text in self._text.items():
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                if file in own_files and any(s.line <= line <= s.end_line for s in self.find(name) if s.file == file):
                    continue  # inside its own definition — not a caller
                hits.append({"caller_file": file, "line": line})
                break  # one hit per file is enough signal
        return hits

    def callees_of(self, name: str) -> List[dict]:
        """Other known symbols referenced (by name+paren) inside `name`'s
        own source span."""
        out: List[dict] = []
        for sym in self.find(name):
            body = self.source_span(sym)
            for other in self.symbols:
                if other.name == name:
                    continue
                if re.search(_CALL_RE_TEMPLATE.format(name=re.escape(other.name)), body):
                    out.append({"file": other.file, "symbol": other.name})
        return out

    def path_between(self, a: str, b: str, max_depth: int = 6) -> Optional[List[str]]:
        """BFS over the callee graph from `a` to `b`, symbol names only."""
        if a == b:
            return [a]
        frontier = [[a]]
        seen = {a}
        for _ in range(max_depth):
            next_frontier = []
            for path in frontier:
                for callee in self.callees_of(path[-1]):
                    name = callee["symbol"]
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
        """Files that import/call symbols defined in `file_path`
        (direction="incoming", i.e. this file's blast radius) or files
        `file_path` itself calls into (direction="outgoing")."""
        defined_here = [s.name for s in self.symbols if s.file == file_path]
        out: Dict[str, dict] = {}
        if direction == "incoming":
            for name in defined_here:
                for hit in self.callers_of(name):
                    if hit["caller_file"] != file_path:
                        out[hit["caller_file"]] = {"file": hit["caller_file"], "via": name}
        else:
            for sym in [s for s in self.symbols if s.file == file_path]:
                for callee in self.callees_of(sym.name):
                    if callee["file"] != file_path:
                        out[callee["file"]] = {"file": callee["file"], "via": callee["symbol"]}
        return list(out.values())

    def file_skeleton(self, rel_path: str) -> List[Symbol]:
        return sorted((s for s in self.symbols if s.file == rel_path), key=lambda s: s.line)
