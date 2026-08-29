"""R11 — statement-level def-use graph (ARISE, arXiv:2605.03117).

Covers: extraction correctness (shadowing, module fallback, compound
blocks, nested defs), the FORGE_STATEMENT_GRAPH=0 kill switch, additive
integration with the function-level tables, persistence/reindex, and the
statement_graph tool surface on BOTH backends.
"""
from __future__ import annotations

import pytest

from atomic_forge.codegraph import CodeGraph
from atomic_forge.graph_statements import extract
from atomic_forge.tools import GraphToolBackend, LocalToolBackend, make_tools


_SRC = (
    "RATE = 10\n"                     # module def, line 1
    "\n"
    "\n"
    "def price(qty):\n"               # line 4
    "    return qty * RATE\n"         # line 5
    "\n"
    "\n"
    "def total(qty):\n"               # line 8
    "    x = price(qty)\n"            # line 9
    "    x = x + 1\n"                 # line 10 — reads PREVIOUS x
    "    if x > 5:\n"                 # line 11
    "        return x\n"              # line 12 — inside compound block
    "    return 0\n"                  # line 13
)

# line -> (kind, defines, reads_from) for the statements near total()
_TOTAL_ROWS = {
    8: ("def", ["qty"], []),
    9: ("assign", ["x"], ["price", "qty"]),
    10: ("assign", ["x"], ["x"]),
    11: ("if", [], ["x"]),
    12: ("return", [], ["x"]),
}


def _graph(tmp_path):
    (tmp_path / "m.py").write_text(_SRC)
    g = CodeGraph(project_dir=tmp_path)
    g.build()
    return g


# ---------------------------------------------------------------- extractor ----

def test_extract_module_and_function_rows():
    rows, edges = extract("m.py", _SRC)
    syms = {r["symbol"] for r in rows}
    assert "<module>" in syms and "price" in syms and "total" in syms
    # module rows precede function rows (pass order) so edges can reference them
    first_fn_row = next(i for i, r in enumerate(rows) if r["symbol"] == "price")
    assert all(r["symbol"] == "<module>" for r in rows[:first_fn_row])


def test_extract_reads_previous_def_not_own():
    """`x = x + 1` reads the PREVIOUS x — the exact ARISE def-use shape."""
    rows, edges = extract("m.py", _SRC)
    name_by_row = lambda i: rows[i]
    # the row at line 10 (aug-assign) must read a row at line 9
    line10 = next(i for i, r in enumerate(rows) if r["line"] == 10)
    readers = [(rows[d]["line"], rows[d]["line"]) for d, u, n, c in edges if u == line10]
    assert (9, 9) in readers


def test_extract_confidence_exact_vs_heuristic():
    rows, edges = extract("m.py", _SRC)
    conf = {}
    for d, u, n, c in edges:
        conf[n] = c
    assert conf["x"] == "exact"          # local scope
    rate_edges = [c for d, u, n, c in edges if n == "RATE"]
    assert rate_edges == ["heuristic"]   # module constant read inside a function


def test_extract_shadowed_name_resolves_locally():
    src = "Y = 1\n\n\ndef f():\n    y = 2\n    return y\n"  # case differs
    rows, edges = extract("m.py", src)
    # `return y` reads the LOCAL y (exact), never a heuristic module edge
    assert all(c == "exact" for _d, _u, n, c in edges if n == "y")


def test_extract_compound_blocks_share_scope():
    rows, edges = extract("m.py", _SRC)
    line12 = next(i for i, r in enumerate(rows) if r["line"] == 12)
    # `return x` inside the `if` reads the assign at line 10
    assert (10, 12) in [(rows[d]["line"], rows[u]["line"]) for d, u, n, _c in edges]


def test_extract_nested_def_binds_and_gets_its_own_scope():
    src = (
        "def outer():\n"
        "    def inner(p):\n"
        "        return p * 2\n"
        "    return inner\n"
    )
    rows, _edges = extract("m.py", src)
    # outer gets a binding row for the nested def (text is indented)
    assert any(r["symbol"] == "outer" and r["kind"] == "def"
               and r["text"].strip().startswith("def inner") for r in rows)
    # ...and the nested body is tracked as its own function scope
    assert any(r["symbol"] == "inner" and r["text"].strip().startswith("return p * 2")
               for r in rows)


# ------------------------------------------------------- codegraph integration ----

def test_codegraph_builds_statement_tables_alongside_function_tables(tmp_path):
    g = _graph(tmp_path)
    counts = g.counts()
    assert counts["statements"] > 0 and counts["def_use"] > 0
    assert counts["symbols"] == 2   # price, total — function tables unchanged
    assert counts["edges"] == 1     # total -> price — function tables unchanged
    g.close()


def test_codegraph_statements_near_resolves_def_use(tmp_path):
    g = _graph(tmp_path)
    rows = {r["line"]: r for r in g.statements_near("m.py", 10)}
    for line, (kind, defines, reads) in _TOTAL_ROWS.items():
        assert rows[line]["kind"] == kind, f"line {line}"
        assert rows[line]["defines"] == defines, f"line {line} defines"
        assert rows[line]["reads_from"] == reads, f"line {line} reads_from"
    g.close()


def test_codegraph_uses_of_reports_def_site_across_functions(tmp_path):
    g = _graph(tmp_path)
    uses = g.uses_of("RATE")
    assert any(u["symbol"] == "price" and u["confidence"] == "heuristic" for u in uses)
    assert all(u["def_symbol"] == "<module>" for u in uses)
    g.close()


def test_statement_graph_flag_off_disables_but_keeps_function_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_STATEMENT_GRAPH", "0")
    g = _graph(tmp_path)
    counts = g.counts()
    assert counts["statements"] == 0 and counts["def_use"] == 0
    assert counts["symbols"] == 2 and counts["edges"] == 1  # function tables intact
    g.close()


def test_reindex_file_updates_statement_rows(tmp_path):
    g = _graph(tmp_path)
    before = g.counts()["statements"]
    g.reindex_file("m.py", _SRC + "\n\ndef added():\n    return 1\n")
    after = g.counts()["statements"]
    assert after > before
    g.close()


def test_remove_file_purges_statement_rows(tmp_path):
    g = _graph(tmp_path)
    (tmp_path / "m.py").unlink()
    g.build()
    c = g.counts()
    assert c["statements"] == 0 and c["def_use"] == 0
    g.close()


# ---------------------------------------------------------------- tool surface ----

def test_statement_graph_tool_graph_backend(tmp_path):
    g = _graph(tmp_path)
    b = GraphToolBackend(tmp_path)
    b.graph = g  # reuse this test's graph and its built statements
    r = b.statement_graph("m.py", line=10)
    assert r["ok"] is True and r["tool"] == "statement_graph"
    by_line = {row["line"]: row for row in r["results"]}
    assert by_line[9]["defines"] == ["x"]
    assert by_line[10]["reads_from"] == ["x"]
    b.graph.close()


def test_statement_graph_tool_local_backend_no_db(tmp_path):
    b = LocalToolBackend(tmp_path)
    (tmp_path / "m.py").write_text(_SRC)
    r = b.statement_graph("m.py", line=10)
    by_line = {row["line"]: row for row in r["results"]}
    assert by_line[10]["reads_from"] == ["x"]


def test_statement_graph_missing_file_returns_error_envelope(tmp_path):
    b = make_tools(tmp_path, backend="graph")
    r = b.statement_graph("nope.py", line=3)
    assert r["ok"] is False
    assert "no such file" in r["hint"]
    b.graph.close()


def test_statement_graph_auto_surfaces_in_manifest(tmp_path):
    b = make_tools(tmp_path, backend="graph")
    names = {t["name"] for t in b.describe()["results"]}
    assert "statement_graph" in names
    b.graph.close()


def test_non_python_file_returns_block_rows_without_def_use(tmp_path):
    (tmp_path / "util.c").write_text("int add(int a, int b) {\n    return helper(a) + b;\n}\n")
    b = make_tools(tmp_path, backend="graph")
    r = b.statement_graph("util.c")
    assert r["results"][0]["kind"] == "block"
    assert r["results"][0]["engine"] == "heuristic"
    assert r["results"][0]["defines"] == []  # honest: no def_use for C blocks
    b.graph.close()