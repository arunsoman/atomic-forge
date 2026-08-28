from atomic_forge.tools import LocalToolBackend


def test_write_view_edit_delete_roundtrip(tmp_path):
    tools = LocalToolBackend(tmp_path)
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


def test_edit_ambiguous_match_rejected(tmp_path):
    tools = LocalToolBackend(tmp_path)
    tools.write_file("a.py", "x = 1\nx = 1\n")
    r = tools.edit_file("a.py", "x = 1", "x = 2")
    assert not r["ok"]
    assert "2 locations" in r["hint"]


def test_search_symbol_and_file_skeleton(tmp_path):
    tools = LocalToolBackend(tmp_path)
    tools.write_file("mod.py", "def alpha():\n    pass\n\n\ndef beta():\n    return alpha()\n")

    found = tools.search_symbol("alpha")
    assert found["ok"]
    assert found["results"][0]["source_file"] == "mod.py"

    skeleton = tools.file_skeleton("mod.py")
    names = {s["name"] for s in skeleton["results"]}
    assert names == {"alpha", "beta"}


def test_callers_and_callees(tmp_path):
    tools = LocalToolBackend(tmp_path)
    tools.write_file("mod.py", "def alpha():\n    pass\n\n\ndef beta():\n    return alpha()\n")

    callers = tools.callers("alpha")
    assert any(c["caller_file"] == "mod.py" for c in callers["results"])

    callees = tools.callees("beta")
    assert any(c["symbol"] == "alpha" for c in callees["results"])


def test_affected_by_incoming(tmp_path):
    tools = LocalToolBackend(tmp_path)
    tools.write_file("lib.py", "def shared():\n    pass\n")
    tools.write_file("app.py", "from lib import shared\n\n\ndef run():\n    shared()\n")

    affected = tools.affected_by("lib.py", direction="incoming")
    assert any(r["file"] == "app.py" for r in affected["results"])


def test_resolve_import_python(tmp_path):
    tools = LocalToolBackend(tmp_path)
    tools.write_file("pkg/mod.py", "def helper():\n    pass\n")
    result = tools.resolve_import("helper")
    assert result["ok"]
    assert "helper" in result["results"][0]["import_statement"]


def test_missing_file_gives_hint_not_silence(tmp_path):
    tools = LocalToolBackend(tmp_path)
    r = tools.view_file("nope.py")
    assert not r["ok"]
    assert r["hint"]


def test_describe_lists_callable_tools(tmp_path):
    tools = LocalToolBackend(tmp_path)
    manifest = tools.describe()
    names = {m["name"] for m in manifest["results"]}
    assert "view_file" in names
    assert "write_file" in names
    assert "describe" not in names  # excluded from its own manifest
