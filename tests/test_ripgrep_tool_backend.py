"""Protocol-conformance tests for the `RipgrepToolBackend` reference
implementation in examples/ — proves the "bring your own backend" seam
in tools.py actually works with a second, independent implementation.

Requires `rg` on PATH; skips (not fails) when it isn't, so the suite
stays green on a machine without ripgrep installed.
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="requires the rg (ripgrep) binary")


@pytest.fixture
def tools(tmp_path):
    from ripgrep_tool_backend import RipgrepToolBackend
    return RipgrepToolBackend(tmp_path)


def test_write_view_edit_delete_roundtrip(tools, tmp_path):
    r = tools.write_file("a.py", "def foo():\n    return 1\n")
    assert r["ok"]

    view = tools.view_file("a.py")
    assert view["ok"]
    assert "def foo" in view["results"][0]["content"]

    edit = tools.edit_file("a.py", "return 1", "return 2")
    assert edit["ok"]
    assert tmp_path.joinpath("a.py").read_text() == "def foo():\n    return 2\n"

    delete = tools.delete_file("a.py")
    assert delete["ok"]
    assert not tmp_path.joinpath("a.py").exists()


def test_edit_ambiguous_match_rejected(tools):
    tools.write_file("a.py", "x = 1\nx = 1\n")
    r = tools.edit_file("a.py", "x = 1", "x = 2")
    assert not r["ok"]
    assert "2 locations" in r["hint"]


def test_search_symbol_and_file_skeleton(tools):
    tools.write_file("mod.py", "def alpha():\n    pass\n\n\ndef beta():\n    return alpha()\n")

    found = tools.search_symbol("alpha")
    assert found["ok"]
    assert found["results"][0]["source_file"] == "mod.py"

    skeleton = tools.file_skeleton("mod.py")
    names = {s["name"] for s in skeleton["results"]}
    assert names == {"alpha", "beta"}


def test_callers_and_callees(tools):
    tools.write_file("mod.py", "def alpha():\n    pass\n\n\ndef beta():\n    return alpha()\n")

    callers = tools.callers("alpha")
    assert any(c["file"] == "mod.py" for c in callers["results"])

    callees = tools.callees("beta")
    assert any(c["symbol"] == "alpha" for c in callees["results"])


def test_search_symbol_missing_has_hint(tools):
    r = tools.search_symbol("nope")
    assert not r["results"]
    assert "nope" in r["hint"]


def test_resolve_import(tools):
    tools.write_file("pkg/mod.py", "def widget():\n    pass\n")
    r = tools.resolve_import("widget")
    assert r["ok"]
    assert r["results"][0]["import_statement"] == "from pkg.mod import widget"


def test_path_between(tools):
    tools.write_file(
        "mod.py",
        "def a():\n    return b()\n\n\ndef b():\n    return c()\n\n\ndef c():\n    return 1\n",
    )
    r = tools.path_between("a", "c")
    assert r["ok"]
    assert r["results"][0]["path"] == ["a", "b", "c"]


def test_describe_lists_full_protocol(tools):
    # Mirrors LocalToolBackend's own convention (test_tools_local.py):
    # describe/health/reindex are meta-tools, excluded from their own manifest.
    from atomic_forge.tools import ToolBackend
    manifest_names = {m["name"] for m in tools.describe()["results"]}
    protocol_names = {name for name in vars(ToolBackend) if not name.startswith("_")}
    missing = protocol_names - manifest_names - {"describe", "health", "reindex"}
    assert not missing, f"RipgrepToolBackend is missing protocol methods: {missing}"
