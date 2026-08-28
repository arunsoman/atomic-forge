from atomic_forge.codegraph import CodeGraph
from atomic_forge.tools import GraphToolBackend


def _write_project(tmp_path):
    (tmp_path / "a.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import main\n\n\ndef entrypoint():\n    return main()\n"
    )


def test_build_finds_symbols_and_edges(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    stats = graph.build()
    assert stats["parsed"] == 2
    assert stats["unchanged"] == 0
    counts = graph.counts()
    assert counts["files"] == 2
    assert counts["symbols"] == 3  # helper, main, entrypoint
    names = {s.name for s in graph.find("main")}
    assert "main" in names
    graph.close()


def test_direct_callers_and_callees(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    callers_of_helper = graph.callers("helper")
    assert any(c["symbol"] == "main" for c in callers_of_helper)
    callees_of_main = graph.callees("main")
    assert any(c["symbol"] == "helper" for c in callees_of_main)
    graph.close()


def test_transitive_callers_beyond_depth_one(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    # entrypoint -> main -> helper: depth=1 callers of helper is just main;
    # depth=2 should also surface entrypoint (the whole point of a
    # persisted, precomputed graph over a regex-per-call index).
    depth1 = {c["symbol"] for c in graph.callers("helper", depth=1)}
    depth2 = {c["symbol"] for c in graph.callers("helper", depth=2)}
    assert depth1 == {"main"}
    assert "entrypoint" in depth2
    graph.close()


def test_path_between(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    path = graph.path_between("entrypoint", "helper")
    assert path == ["entrypoint", "main", "helper"]
    graph.close()


def test_affected_by_incoming(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    affected = graph.affected_by("a.py", max_depth=2, direction="incoming")
    files = {a["file"] for a in affected}
    assert "b.py" in files
    graph.close()


def test_second_build_on_unchanged_tree_reparses_nothing(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    first = graph.build()
    assert first["parsed"] == 2
    second = graph.build()
    assert second["parsed"] == 0
    assert second["unchanged"] == 2
    graph.close()


def test_persists_across_separate_instances(tmp_path):
    """The whole point: a fresh CodeGraph pointed at the same project_dir
    loads from the SQLite file on disk instead of needing a rebuild."""
    _write_project(tmp_path)
    first = CodeGraph(project_dir=tmp_path)
    first.build()
    first.close()

    second = CodeGraph(project_dir=tmp_path)  # no build() called
    names = {s.name for s in second.find("main")}
    assert "main" in names
    counts = second.counts()
    assert counts["symbols"] == 3
    second.close()


def test_reindex_file_updates_only_that_file(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    graph.reindex_file("a.py", "def helper():\n    return 2\n\n\ndef renamed_main():\n    return helper()\n")
    assert graph.find("main") == []
    assert len(graph.find("renamed_main")) == 1
    # b.py untouched
    assert len(graph.find("entrypoint")) == 1
    graph.close()


def test_removing_file_purges_symbols_and_edges(tmp_path):
    _write_project(tmp_path)
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    (tmp_path / "b.py").unlink()
    stats = graph.build()
    assert stats["removed"] == 1
    assert graph.find("entrypoint") == []
    graph.close()


# ---------------------------------------------------------- GraphToolBackend ----

def test_graph_tool_backend_matches_local_backend_envelope_shape(tmp_path):
    _write_project(tmp_path)
    backend = GraphToolBackend(tmp_path)
    result = backend.search_symbol("main")
    assert result["ok"] is True
    assert result["tool"] == "search_symbol"
    assert result["results"][0]["source_file"] == "a.py"
    backend.graph.close()


def test_graph_tool_backend_write_file_updates_graph(tmp_path):
    _write_project(tmp_path)
    backend = GraphToolBackend(tmp_path)
    backend.write_file("c.py", "def new_fn():\n    return 3\n")
    hits = backend.search_symbol("new_fn")
    assert hits["results"][0]["source_file"] == "c.py"
    backend.graph.close()


def test_graph_tool_backend_delete_file_removes_from_graph(tmp_path):
    _write_project(tmp_path)
    backend = GraphToolBackend(tmp_path)
    backend.delete_file("b.py")
    assert backend.search_symbol("entrypoint")["results"] == []
    backend.graph.close()


def test_graph_tool_backend_health_reports_persisted_counts(tmp_path):
    _write_project(tmp_path)
    backend = GraphToolBackend(tmp_path)
    health = backend.health()["results"][0]
    assert health["backend"] == "persisted-graph"
    assert health["symbols"] == 3
    assert "db_path" in health
    backend.graph.close()


def test_make_tools_graph_backend_selection(tmp_path):
    from atomic_forge.tools import make_tools
    backend = make_tools(tmp_path, backend="graph")
    assert isinstance(backend, GraphToolBackend)
    backend.graph.close()
