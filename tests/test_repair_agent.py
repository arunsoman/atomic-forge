from pathlib import Path

from atomic_forge.repair_agent import (
    _blast_radius_violations, _diff_size, extract_signals, localize,
)
from atomic_forge.tools import LocalToolBackend
from atomic_forge.trajectory import Trajectory


def test_extract_signals_pytest_traceback():
    out = (
        "src/app/service.py:12: in run\n"
        "    raise ValueError('bad')\n"
        "ValueError: bad\n"
        "tests/test_service.py::test_run FAILED\n"
    )
    sig = extract_signals(out)
    assert "src/app/service.py" in sig.traceback_paths
    assert "tests/test_service.py::test_run" in sig.test_nodes
    assert "ValueError" in sig.exception_types


def test_extract_signals_import_error_symbol():
    out = "ImportError: cannot import name 'helper' from 'app.utils'"
    sig = extract_signals(out)
    assert "helper" in sig.symbol_names


def test_extract_signals_node_missing_module():
    out = "Cannot find module '../lib/util' from 'tests/app.test.js'"
    sig = extract_signals(out)
    assert ("../lib/util", "tests/app.test.js") in sig.missing_modules


def test_extract_signals_strips_ansi():
    out = "\x1b[31mValueError\x1b[0m: bad"
    sig = extract_signals(out)
    assert "ValueError" in sig.exception_types


def test_localize_ranks_traceback_hit_highest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def run():\n    raise ValueError()\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["src/service.py"])
    suspects = localize(sig, tools, traj, tmp_path)
    assert suspects
    assert suspects[0].file == "src/service.py"


def test_localize_excludes_test_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    tools = LocalToolBackend(tmp_path)
    traj = Trajectory(tmp_path)
    from atomic_forge.repair_agent import FailureSignals
    sig = FailureSignals(traceback_paths=["tests/test_x.py"])
    suspects = localize(sig, tools, traj, tmp_path)
    assert suspects == []


def test_diff_size_counts_changed_lines():
    old = "a\nb\nc\n"
    new = "a\nB\nc\n"
    assert _diff_size(old, new) == 2  # one removed, one added


def test_blast_radius_flags_removed_function_with_external_caller(tmp_path):
    (tmp_path / "lib.py").write_text("def shared():\n    return 1\n")
    (tmp_path / "app.py").write_text("from lib import shared\n\n\ndef run():\n    return shared()\n")
    tools = LocalToolBackend(tmp_path)
    old_content = "def shared():\n    return 1\n"
    new_content = "def renamed():\n    return 1\n"
    violations = _blast_radius_violations("lib.py", old_content, new_content, tools)
    assert violations
    assert "removed" in violations[0]


def test_blast_radius_allows_internal_only_change(tmp_path):
    tools = LocalToolBackend(tmp_path)
    old_content = "def helper():\n    return 1\n"
    new_content = "def helper():\n    return 2\n"  # same signature, no callers indexed
    violations = _blast_radius_violations("lib.py", old_content, new_content, tools)
    assert violations == []
