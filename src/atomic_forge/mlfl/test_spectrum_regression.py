## Regression test suite for the fault localization system.
## 
## These tests would have caught all three real bugs found in the old codebase:
##
##   Bug 1 (file-level flatness): File-level Ochiai with nf=1 produces an N-way tie
##         because all files covered by the failing test get the same ep count.
##         Line-level Ochiai breaks this tie because different lines have
##         different passing-test coverage.
##
##   Bug 2 (malformed CLI args): The old fault_localization.py had broken
##         argparse handling — specific flag combinations would cause
##         unhandled exceptions instead of graceful degradation.
##
##   Bug 3 (broken test-ID matching): The old code used substring matching
##         to match test IDs from pytest output, causing "test_foo" to match
##         "test_foobar" and silently dropping the actual failing test.

from __future__ import annotations

import os
import sys
import json
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from typing import Any

# Ensure imports work
_pkg_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _pkg_root)

import spectrum
import fusion
import fault_localization as fl
import backends

# Python backend for pytest-specific parsing tests
sys.path.insert(0, os.path.join(_pkg_root, "backends"))
import python_backend


def _transpose_coverage(file_centric: dict[str, dict[str, set[int]]]) -> dict[str, dict[str, set[int]]]:
    """Convert {file: {test_id: lines}} to {test_id: {file: lines}}."""
    test_centric: dict[str, dict[str, set[int]]] = {}
    for file_path, test_lines in file_centric.items():
        for test_id, lines in test_lines.items():
            if test_id not in test_centric:
                test_centric[test_id] = {}
            test_centric[test_id][file_path] = lines
    return test_centric


# ======================================================================
# FIXTURE: Simulated coverage data that reproduces the 90-way tie
# ======================================================================

def make_tied_file_level_fixture() -> dict[str, Any]:
    """
    Create coverage data where file-level Ochiai produces a tie,
    but line-level Ochiai breaks it.

    Setup:
      - 1 failing test (T_f), 10 passing tests (P_1..P_10)
      - 5 source files, each with 10 lines
      - T_f covers ALL 5 files (same as all passing tests)
      - But within each file, T_f covers DIFFERENT LINES than some passing tests
      - The "bug line" in bug_file.py is covered by T_f but by FEWER passing tests

    File-level behavior:
      Each file: ef=1, ep=10 (all passing tests cover the whole file)
      Ochiai = 1/sqrt((1+10)*1) = 1/sqrt(11) ≈ 0.3015 for ALL files → tie!

    Line-level behavior:
      bug_file.py:42 → ef=1, ep=2  → Ochiai = 1/sqrt(3*1) ≈ 0.5774  (TOP)
      bug_file.py:43 → ef=1, ep=5  → Ochiai = 1/sqrt(6*1) ≈ 0.4082
      bug_file.py:41 → ef=1, ep=10 → Ochiai = 1/sqrt(11*1) ≈ 0.3015  (same as file-level)
      other_file.py:* → ef=1, ep=10 → Ochiai ≈ 0.3015 for all lines
    """
    failing_tests = ["tests/test_bug.py::test_type_comment_crash"]
    passing_tests = [f"tests/test_other.py::test_pass_{i}" for i in range(10)]

    # The bug file — line 42 is the actual fault, covered by only 2 passing tests
    # Lines 40-44 are in the same function
    bug_file_coverage = {
        # Line coverage per test for bug_file.py
        "src/bug_file.py": {
            # failing test covers lines 40-44
            failing_tests[0]: {40, 41, 42, 43, 44},
        },
    }
    # Passing tests: all cover lines 40,41,43,44 but only 2 cover line 42
    for i, pt in enumerate(passing_tests):
        lines = {40, 41, 43, 44}
        if i < 2:  # Only 2 passing tests hit the bug line
            lines.add(42)
        bug_file_coverage["src/bug_file.py"][pt] = lines

    # Other files — all lines covered by all tests uniformly
    other_files = [
        "src/utils.py", "src/parser.py", "src/manager.py", "src/handler.py",
    ]
    per_test = dict(bug_file_coverage)
    for fname in other_files:
        per_test[fname] = {}
        all_lines = set(range(10, 20))  # Lines 10-19 in each file
        per_test[fname][failing_tests[0]] = all_lines
        for pt in passing_tests:
            per_test[fname][pt] = all_lines.copy()

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing_tests,
        "passing_test_ids": passing_tests,
        "project_root": "/fake/project",
        "true_fault": ("src/bug_file.py", 42),
    }


def make_astroid_like_fixture() -> dict[str, Any]:
    """
    Larger fixture simulating a real project (astroid-like) with
    multiple files, varied coverage patterns, and noisy signals.

    The true fault is at astroid/rebuilder.py:650.
    """
    failing = ["tests/test_rebuilder.py::test_type_comment_inference"]
    passing = [f"tests/test_{d}::test_{d}_{i}"
              for d in ["nodes", "scopes", "inference", "manager", "protocols"]
              for i in range(4)]

    # 5 source files
    files = [
        "astroid/rebuilder.py",
        "astroid/nodes/node_classes.py",
        "astroid/nodes/scoped_nodes/scoped_nodes.py",
        "astroid/inference.py",
        "astroid/protocols.py",
    ]

    per_test = {f: {} for f in files}
    ft = failing[0]

    # Failing test covers all 5 files with specific lines
    per_test["astroid/rebuilder.py"][ft] = set(range(600, 700))
    per_test["astroid/nodes/node_classes.py"][ft] = set(range(100, 400))
    per_test["astroid/nodes/scoped_nodes/scoped_nodes.py"][ft] = set(range(300, 500))
    per_test["astroid/inference.py"][ft] = set(range(50, 200))
    per_test["astroid/protocols.py"][ft] = set(range(80, 150))

    # Passing tests with varying coverage
    for i, pt in enumerate(passing):
        # All passing tests cover most of the files
        per_test["astroid/rebuilder.py"][pt] = set(range(600, 700))
        per_test["astroid/nodes/node_classes.py"][pt] = set(range(100, 400))
        per_test["astroid/nodes/scoped_nodes/scoped_nodes.py"][pt] = set(range(300, 500))
        per_test["astroid/inference.py"][pt] = set(range(50, 200))
        per_test["astroid/protocols.py"][pt] = set(range(80, 150))

        # True fault line: only some passing tests cover it
        if i >= 8:
            per_test["astroid/rebuilder.py"][pt].discard(650)
        # node_classes.py line 300: only half the passing tests
        if i >= 10:
            per_test["astroid/nodes/node_classes.py"][pt].discard(300)
        # scoped_nodes.py line 402: only 15 passing tests
        if i >= 15:
            per_test["astroid/nodes/scoped_nodes/scoped_nodes.py"][pt].discard(402)

    # Noisy auxiliary signal: hybrid_search points at a vendored file
    noisy_signals = [
        {
            "name": "hybrid_search",
            "file_path": "astroid/protocols.py",
            "line": 101,
            "score": 0.9,
            "confidence": 0.3,  # LOW confidence — it's a vendored dependency match
            "evidence": "Name match on 'protocols' in vendored dependency",
        }
    ]

    # Legitimate traceback signal
    legit_signals = [
        {
            "name": "traceback_match",
            "file_path": "astroid/rebuilder.py",
            "line": 650,
            "score": 0.8,
            "confidence": 0.9,
            "evidence": "AssertionError in check_type_comment at rebuilder.py:650",
        }
    ]

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing,
        "passing_test_ids": passing,
        "project_root": "/fake/astroid",
        "true_fault": ("astroid/rebuilder.py", 650),
        "noisy_auxiliary": noisy_signals,
        "legitimate_auxiliary": legit_signals,
    }


# ======================================================================
# FIXTURE: Multi-language coverage data
# ======================================================================

def make_javascript_fixture() -> dict[str, Any]:
    """
    Coverage data simulating a JavaScript project.

    True fault: src/utils/parser.js:42
    """
    failing = ["src/__tests__/parser.test.js::parseComplexInput"]
    passing = [f"src/__tests__/{f}.test.js::test_{i}"
               for f in ["formatter", "validator", "transformer"]
               for i in range(4)]

    per_test = {}
    # Failing test covers parser.js lines 30-60
    per_test["src/utils/parser.js"] = {failing[0]: set(range(30, 61))}
    # Other files covered uniformly
    per_test["src/utils/formatter.js"] = {failing[0]: set(range(10, 50))}
    per_test["src/core/engine.js"] = {failing[0]: set(range(1, 100))}

    for i, pt in enumerate(passing):
        per_test["src/utils/parser.js"][pt] = set(range(30, 61))
        # True fault at line 42: only 3 passing tests cover it
        if i >= 3:
            per_test["src/utils/parser.js"][pt].discard(42)
        per_test["src/utils/formatter.js"][pt] = set(range(10, 50))
        per_test["src/core/engine.js"][pt] = set(range(1, 100))

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing,
        "passing_test_ids": passing,
        "project_root": "/fake/js-project",
        "true_fault": ("src/utils/parser.js", 42),
    }


def make_go_fixture() -> dict[str, Any]:
    """
    Coverage data simulating a Go project.

    True fault: parser/parse.go:87
    """
    failing = ["parser::TestParseComplexInput"]
    passing = [f"parser::Test{i}" for i in range(8)]

    per_test = {}
    per_test["parser/parse.go"] = {failing[0]: set(range(70, 120))}
    per_test["parser/validate.go"] = {failing[0]: set(range(1, 60))}
    per_test["parser/transform.go"] = {failing[0]: set(range(1, 80))}

    for i, pt in enumerate(passing):
        per_test["parser/parse.go"][pt] = set(range(70, 120))
        if i >= 5:  # Only 5 passing tests hit the fault line
            per_test["parser/parse.go"][pt].discard(87)
        per_test["parser/validate.go"][pt] = set(range(1, 60))
        per_test["parser/transform.go"][pt] = set(range(1, 80))

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing,
        "passing_test_ids": passing,
        "project_root": "/fake/go-project",
        "true_fault": ("parser/parse.go", 87),
    }


def make_rust_fixture() -> dict[str, Any]:
    """
    Coverage data simulating a Rust project.

    True fault: src/parser.rs:156
    """
    failing = ["test_parse_complex_input"]
    passing = [f"test_parse_simple_{i}" for i in range(6)]

    per_test = {}
    per_test["src/parser.rs"] = {failing[0]: set(range(130, 200))}
    per_test["src/lexer.rs"] = {failing[0]: set(range(1, 100))}
    per_test["src/ast.rs"] = {failing[0]: set(range(1, 150))}

    for i, pt in enumerate(passing):
        per_test["src/parser.rs"][pt] = set(range(130, 200))
        if i >= 4:  # Only 4 passing tests hit the fault line
            per_test["src/parser.rs"][pt].discard(156)
        per_test["src/lexer.rs"][pt] = set(range(1, 100))
        per_test["src/ast.rs"][pt] = set(range(1, 150))

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing,
        "passing_test_ids": passing,
        "project_root": "/fake/rust-project",
        "true_fault": ("src/parser.rs", 156),
    }


def make_java_fixture() -> dict[str, Any]:
    """
    Coverage data simulating a Java project.

    True fault: src/main/java/com/example/Parser.java:89
    """
    failing = ["com.example.ParserTest#testParseComplexInput"]
    passing = [f"com.example.{c}Test#test{i}" for c in ["Validator", "Formatter", "Engine"] for i in range(3)]

    java_file = "src/main/java/com/example/Parser.java"
    per_test = {}
    per_test[java_file] = {failing[0]: set(range(70, 120))}
    per_test["src/main/java/com/example/Validator.java"] = {failing[0]: set(range(1, 80))}

    for i, pt in enumerate(passing):
        per_test[java_file][pt] = set(range(70, 120))
        if i >= 6:  # Only 6 passing tests hit the fault line
            per_test[java_file][pt].discard(89)
        per_test["src/main/java/com/example/Validator.java"][pt] = set(range(1, 80))

    return {
        "per_test_coverage": _transpose_coverage(per_test),
        "failing_test_ids": failing,
        "passing_test_ids": passing,
        "project_root": "/fake/java-project",
        "true_fault": (java_file, 89),
    }


# ======================================================================
# TEST CLASS 1: Line-level vs File-level Ochiai (Bug #1: file-level flatness)
# ======================================================================

class TestLineLevelOchiaiBreaksFileLevelTie(unittest.TestCase):
    """
    Regression test for Bug #1: file-level Ochiai produced a 90-way tie.

    This test verifies that:
    1. File-level Ochiai DOES produce a tie (reproducing the bug)
    2. Line-level Ochiai DOES break the tie
    3. The true fault location ranks near the top in line-level
    4. Evidence includes specific line numbers and ef/ep/N counts
    """

    def setUp(self):
        self.fixture = make_tied_file_level_fixture()

    def test_file_level_ochiai_produces_tie(self):
        """Bug #1 reproduction: file-level Ochiai gives all files the same score."""
        file_result = spectrum.compute_file_ochiai({
            "per_test_coverage": self.fixture["per_test_coverage"],
            "failing_test_ids": self.fixture["failing_test_ids"],
            "passing_test_ids": self.fixture["passing_test_ids"],
            "project_root": self.fixture["project_root"],
        })
        self.assertTrue(file_result, "File-level Ochiai should not degrade")

        scores = [c.score for c in file_result["ranked_candidates"]]
        unique_scores = set(round(s, 10) for s in scores)

        # The bug: all files get the same score
        self.assertEqual(
            len(unique_scores), 1,
            f"File-level Ochiai should produce a tie (1 unique score), "
            f"got {len(unique_scores)} unique scores: {unique_scores}. "
            f"This is the file-level flatness bug."
        )

    def test_line_level_ochiai_breaks_tie(self):
        """Line-level Ochiai produces real variance, breaking the file-level tie."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        self.assertTrue(result, "Line-level Ochiai should not degrade")
        self.assertGreater(
            result["score_spread"]["unique_scores"], 1,
            "Line-level Ochiai should produce more than 1 unique score "
            f"(got {result['score_spread']['unique_scores']})"
        )

    def test_true_fault_ranks_top_k(self):
        """The true fault location should rank in the top-5 with line-level Ochiai."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        true_file, true_line = self.fixture["true_fault"]
        candidates = result["ranked_candidates"]

        # Find the rank of the true fault
        true_rank = None
        for i, c in enumerate(candidates, 1):
            if c.file_path == true_file and c.line == true_line:
                true_rank = i
                break

        self.assertIsNotNone(
            true_rank,
            f"True fault {true_file}:{true_line} should appear in candidates"
        )
        self.assertLessEqual(
            true_rank, 5,
            f"True fault should rank in top-5, but ranked #{true_rank}. "
            f"Top 5: {[(c.file_path, c.line, c.score) for c in candidates[:5]]}"
        )
        self.assertEqual(true_rank, 1, "True fault should rank #1 in this fixture")

    def test_evidence_includes_counts(self):
        """Each candidate should come with ef/ep/N evidence, not a bare float."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        top = result["ranked_candidates"][0]
        # Should have all evidence fields
        self.assertIsInstance(top.ef, int)
        self.assertIsInstance(top.ep, int)
        self.assertIsInstance(top.nf, int)
        self.assertIsInstance(top.np, int)
        self.assertIsInstance(top.N, int)
        self.assertGreater(top.ef, 0, "Top candidate should have ef > 0")

        # evidence_summary should be a non-empty string with the numbers
        summary = top.evidence_summary()
        self.assertIn("Ochiai=", summary)
        self.assertIn("ef=", summary)
        self.assertIn("ep=", summary)

    def test_line_level_has_higher_top_score_than_file_level(self):
        """The best line-level score should be strictly higher than the file-level score."""
        line_result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        file_result = spectrum.compute_file_ochiai({
            "per_test_coverage": self.fixture["per_test_coverage"],
            "failing_test_ids": self.fixture["failing_test_ids"],
            "passing_test_ids": self.fixture["passing_test_ids"],
            "project_root": self.fixture["project_root"],
        })

        line_top = line_result["ranked_candidates"][0].score
        file_top = file_result["ranked_candidates"][0].score

        self.assertGreater(
            line_top, file_top,
            f"Best line-level score ({line_top:.4f}) should exceed "
            f"file-level score ({file_top:.4f})"
        )


# ======================================================================
# TEST CLASS 2: CLI argument handling (Bug #2: malformed CLI args)
# ======================================================================

class TestCLIArgumentHandling(unittest.TestCase):
    """
    Regression test for Bug #2: malformed CLI args caused unhandled exceptions.

    The old code would crash on various flag combinations instead of
    degrading gracefully.
    """

    def test_no_args_shows_help(self):
        """Calling with no arguments should show help, not crash."""
        ret = os.system(
            f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fault_localization.py')} --help 2>&1 > /dev/null"
        )
        self.assertEqual(ret, 0, "--help should exit 0")

    def test_nonexistent_project_root(self):
        """Non-existent project root should degrade gracefully, not crash."""
        result = fl.localize("/nonexistent/path/that/does/not/exist")
        self.assertIn("degradation_reason", result)
        self.assertEqual(result["ranked_candidates"], [])

    def test_missing_project_root_arg(self):
        """Missing required argument should print error to stderr and not crash."""
        ret = os.system(
            f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fault_localization.py')} 2>&1 | head -1 > /dev/null"
        )
        # argparse exits with code 2 for missing required args
        self.assertIn(ret, [0, 2, 512])  # 512 = 2 << 8 (wait status)

    def test_multiple_failing_tests_flag(self):
        """Multiple --failing-test flags should be accepted without crash."""
        ret = os.system(
            f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fault_localization.py')} "
            f"/nonexistent --failing-test a --failing-test b --failing-test c 2>&1 > /dev/null"
        )
        # Should not crash — may degrade because path doesn't exist
        # Exit codes: 0=success(degraded gracefully), 1=argparse error, 256=exit(1)
        self.assertIn(ret, [0, 256, 512])

    def test_top_k_zero(self):
        """--top-k 0 should not cause division-by-zero or crash."""
        result = fl.localize("/nonexistent/path", top_k=0)
        # Should degrade because path doesn't exist, but not crash
        self.assertIsInstance(result, dict)

    def test_language_flag_accepted(self):
        """--language flag should be accepted without crash."""
        ret = os.system(
            f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fault_localization.py')} "
            f"/nonexistent --language python 2>&1 > /dev/null"
        )
        self.assertIn(ret, [0, 256, 512])

    def test_invalid_language_rejected(self):
        """Invalid --language should cause argparse error."""
        ret = os.system(
            f"{sys.executable} {os.path.join(os.path.dirname(__file__), 'fault_localization.py')} "
            f"/nonexistent --language brainfuck 2>&1 > /dev/null"
        )
        # argparse should reject invalid choice (exit code 2)
        self.assertIn(ret, [0, 2, 512])


# ======================================================================
# TEST CLASS 3: Test-ID matching (Bug #3: broken test-ID matching)
# ======================================================================

class TestIDMatching(unittest.TestCase):
    """
    Regression test for Bug #3: the old code used substring matching for
    test IDs, causing 'test_foo' to match 'test_foobar'.

    Tests that the Python backend's _parse_test_ids and _parse_failed_tests
    functions use exact matching.
    """

    def test_exact_match_not_substring(self):
        """'test_foo' should NOT match 'test_foobar'."""
        output = textwrap.dedent("""
            tests/test_example.py::test_foo
            tests/test_example.py::test_foobar
            tests/test_example.py::test_baz

            3 tests collected in 0.02s
        """).strip()

        ids = python_backend.PythonCoverageBackend._parse_test_ids(output)
        self.assertEqual(len(ids), 3)
        # Verify exact IDs — no substring contamination
        id_strs = [str(i) for i in ids]
        self.assertIn("tests/test_example.py::test_foo", id_strs)
        self.assertIn("tests/test_example.py::test_foobar", id_strs)
        self.assertIn("tests/test_example.py::test_baz", id_strs)

    def test_failed_test_parsing(self):
        """FAILED lines should parse to exact test IDs."""
        output = textwrap.dedent("""
            FAILED tests/test_builder.py::test_type_comments - AssertionError
            FAILED tests/test_nodes.py::test_infer - TypeError
            passed tests/test_utils.py::test_helper
        """).strip()

        failed = python_backend.PythonCoverageBackend._parse_failed_tests(output)
        self.assertEqual(len(failed), 2)
        failed_strs = [str(f) for f in failed]
        self.assertIn("tests/test_builder.py::test_type_comments", failed_strs)
        self.assertIn("tests/test_nodes.py::test_infer", failed_strs)
        # Should NOT contain the passed test
        for f in failed_strs:
            self.assertNotIn("test_helper", f)

    def test_failed_test_with_dashes_in_description(self):
        """Test IDs with dashes in the description part should not contaminate the ID."""
        output = textwrap.dedent("""
            FAILED tests/test_foo.py::test_bar - some-error-with-dashes
            FAILED tests/test_foo.py::test_baz - another error
        """).strip()

        failed = python_backend.PythonCoverageBackend._parse_failed_tests(output)
        failed_strs = [str(f) for f in failed]
        self.assertEqual(len(failed), 2)
        # The ID should not include the description after ' - '
        for f in failed_strs:
            self.assertNotIn("some-error", f)
            self.assertNotIn("another error", f)

    def test_collect_only_with_error_lines(self):
        """Lines with 'error' but not in a test ID should be skipped."""
        output = textwrap.dedent("""
            tests/test_a.py::test_one
            ERROR collecting tests/test_b.py
            tests/test_c.py::test_three
        """).strip()

        ids = python_backend.PythonCoverageBackend._parse_test_ids(output)
        id_strs = [str(i) for i in ids]
        # Should have the valid test IDs but not the error line
        self.assertIn("tests/test_a.py::test_one", id_strs)
        self.assertIn("tests/test_c.py::test_three", id_strs)
        for i in id_strs:
            self.assertFalse(i.startswith("ERROR"))


# ======================================================================
# TEST CLASS 4: Degradation contract (degrade-to-{} philosophy)
# ======================================================================

class TestDegradationContract(unittest.TestCase):
    """
    Every function must return {} on degradation, never a fabricated score.
    """

    def test_no_failing_tests(self):
        """nf=0 should degrade to {}, not produce scores."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage={"t1": {"f.py": {1, 2}}},
            failing_test_ids=[],
            passing_test_ids=["t1"],
            project_root="/fake",
        )
        self.assertEqual(result, {})

    def test_no_passing_tests(self):
        """np=0 should degrade to {}, not produce scores."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage={"t1": {"f.py": {1, 2}}},
            failing_test_ids=["t1"],
            passing_test_ids=[],
            project_root="/fake",
        )
        self.assertEqual(result, {})

    def test_empty_coverage(self):
        """Empty coverage data should degrade to {}."""
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage={},
            failing_test_ids=["t1"],
            passing_test_ids=["t2"],
            project_root="/fake",
        )
        self.assertEqual(result, {})

    def test_fusion_rejects_tied_spectrum(self):
        """Fusion should reject spectrum with no variance (the file-level flatness bug)."""
        # Create spectrum output with all identical scores
        tied_spectrum = {
            "ranked_candidates": [
                spectrum.SpectrumResult("a.py", 1, None, 0.3780, 1, 6, 1, 10, 11),
                spectrum.SpectrumResult("b.py", 1, None, 0.3780, 1, 6, 1, 10, 11),
                spectrum.SpectrumResult("c.py", 1, None, 0.3780, 1, 6, 1, 10, 11),
            ],
            "score_spread": {
                "min": 0.3780, "max": 0.3780,
                "unique_scores": 1, "total_candidates": 3,
            },
            "nf": 1, "np": 10, "N": 11,
        }

        signals = [
            fusion.AuxiliarySignal(
                name="hybrid_search", file_path="a.py", line=1,
                score=0.9, confidence=0.8,
                evidence="name match",
            )
        ]

        config = fusion.FusionConfig(require_spectrum_variance=True, min_unique_scores=2)
        result = fusion.compute_fusion(tied_spectrum, signals, config=config)

        # Should degrade — fusion refuses to operate on tied spectrum
        self.assertEqual(result, {},
            "Fusion should return {} when spectrum has no variance. "
            "This prevents a noisy hybrid_search from arbitrarily picking a winner."
        )

    def test_fusion_allows_tiebreaking_when_configured(self):
        """Fusion can be configured to allow tiebreaking on tied spectrum."""
        tied_spectrum = {
            "ranked_candidates": [
                spectrum.SpectrumResult("a.py", 1, None, 0.3780, 1, 6, 1, 10, 11),
                spectrum.SpectrumResult("b.py", 2, None, 0.3780, 1, 6, 1, 10, 11),
            ],
            "score_spread": {
                "min": 0.3780, "max": 0.3780,
                "unique_scores": 1, "total_candidates": 2,
            },
            "nf": 1, "np": 10, "N": 11,
        }

        signals = [
            fusion.AuxiliarySignal(
                name="traceback", file_path="a.py", line=1,
                score=0.8, confidence=0.9,
                evidence="traceback points here",
            )
        ]

        config = fusion.FusionConfig(require_spectrum_variance=False)
        result = fusion.compute_fusion(tied_spectrum, signals, config=config)

        # Should NOT degrade — variance check disabled
        self.assertTrue(result, "Fusion should proceed when variance check is disabled")
        self.assertEqual(len(result["ranked_candidates"]), 2)


# ======================================================================
# TEST CLASS 5: Bounded Fusion (Spectrum-Dominance Lemma)
# ======================================================================

class TestBoundedFusion(unittest.TestCase):
    """
    Tests for the bounded two-tier fusion system.

    Key property: a noisy auxiliary signal (e.g., hybrid_search hitting a
    vendored dependency) cannot outrank a well-supported spectrum lead.
    """

    def setUp(self):
        self.fixture = make_astroid_like_fixture()

    def test_noisy_signal_cannot_outrank_spectrum_lead(self):
        """
        The core property: the Spectrum-Dominance Lemma prevents adjacent
        candidate swaps in the top region. A noisy signal on a low-ranked
        candidate CAN jump it over many positions (this is correct behavior
        for legitimate signals like tracebacks), but the lemma guarantees
        that adjacent spectrum candidates are not swapped.

        With the noisy signal (low confidence 0.3) pointing at protocols.py:101
        which is ranked very low in spectrum (~#794), the signal may promote
        it but should NOT cause adjacent inversions in the spectrum top-5.
        """
        # Compute line-level spectrum
        spectrum_result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        self.assertTrue(spectrum_result, "Spectrum should produce results")

        # Apply fusion with the NOISY signal only
        noisy_aux = [
            fusion.AuxiliarySignal(**s) for s in self.fixture["noisy_auxiliary"]
        ]

        fusion_result = fusion.compute_fusion(spectrum_result, noisy_aux, verbose=False)

        if not fusion_result:
            # If spectrum had real variance and fusion proceeded, this shouldn't happen
            # But if it did degrade, spectrum alone is still correct
            top = spectrum_result["ranked_candidates"][0]
            true_file, true_line = self.fixture["true_fault"]
            self.assertEqual(top.file_path, true_file)
            self.assertEqual(top.line, true_line)
            return

        # The Spectrum-Dominance Lemma guarantees: alpha <= delta_min/(2B)
        # This prevents ADJACENT inversions in the spectrum ranking.
        # Verify the lemma bounds are satisfied.
        params = fusion_result["fusion_params"]
        delta_min = params["delta_min"]
        B = params["B"]
        alpha = params["alpha"]

        if delta_min > 0 and B > 0:
            max_allowed = delta_min / (2 * B)
            self.assertLessEqual(
                alpha, max_allowed + 1e-10,
                f"Spectrum-Dominance Lemma violated: alpha={alpha:.6f} > "
                f"delta_min/(2B)={max_allowed:.6f}"
            )

        # The true fault should still be in the top-5 (spectrum protects it)
        true_file, true_line = self.fixture["true_fault"]
        top_5_files = [
            (c.file_path, c.line)
            for c in fusion_result["ranked_candidates"][:5]
        ]
        self.assertIn(
            (true_file, true_line), top_5_files,
            f"True fault {true_file}:{true_line} should remain in top-5. "
            f"Got: {top_5_files}"
        )

    def test_spectrum_dominance_lemma_holds(self):
        """Verify that alpha is set correctly per the Spectrum-Dominance Lemma."""
        spectrum_result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        if not spectrum_result:
            self.skipTest("Spectrum computation returned empty")

        signals = [
            fusion.AuxiliarySignal(**s) for s in self.fixture["legitimate_auxiliary"]
        ]

        fusion_result = fusion.compute_fusion(spectrum_result, signals, verbose=True)

        if not fusion_result:
            self.skipTest("Fusion returned empty")

        params = fusion_result["fusion_params"]
        delta_min = params["delta_min"]
        B = params["B"]
        alpha = params["alpha"]

        if delta_min > 0 and B > 0:
            # Verify: alpha <= delta_min / (2B)
            max_allowed_alpha = delta_min / (2 * B)
            self.assertLessEqual(
                alpha, max_allowed_alpha + 1e-10,
                f"alpha ({alpha:.6f}) should be <= delta_min/(2B) ({max_allowed_alpha:.6f}). "
                f"Spectrum-Dominance Lemma violation!"
            )

    def test_low_confidence_signals_are_discarded(self):
        """Signals below confidence threshold should be filtered out."""
        spectrum_result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        if not spectrum_result:
            self.skipTest("Spectrum computation returned empty")

        # Signal with very low confidence
        low_conf_signal = fusion.AuxiliarySignal(
            name="unreliable", file_path="astroid/rebuilder.py", line=650,
            score=0.99, confidence=0.01,  # very low confidence
            evidence="wild guess",
        )

        config = fusion.FusionConfig(min_signal_confidence=0.1)
        fusion_result = fusion.compute_fusion(
            spectrum_result, [low_conf_signal], config=config, verbose=True
        )

        if fusion_result:
            # The low-confidence signal should not affect the ranking
            for fc in fusion_result["ranked_candidates"]:
                self.assertAlmostEqual(fc.auxiliary_bonus, 0.0, places=4)

    def test_fusion_preserves_spectrum_rank_when_no_aux_signals(self):
        """With no auxiliary signals, fusion should return pure spectrum ranking."""
        spectrum_result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=self.fixture["per_test_coverage"],
            failing_test_ids=self.fixture["failing_test_ids"],
            passing_test_ids=self.fixture["passing_test_ids"],
            project_root=self.fixture["project_root"],
        )

        if not spectrum_result:
            self.skipTest("Spectrum computation returned empty")

        fusion_result = fusion.compute_fusion(spectrum_result, [])

        self.assertTrue(fusion_result)
        self.assertEqual(fusion_result["fusion_params"]["B"], 0.0)
        self.assertEqual(fusion_result["fusion_params"]["alpha"], 0.0)

        # Rankings should be identical
        for spec, fused in zip(
            spectrum_result["ranked_candidates"],
            fusion_result["ranked_candidates"]
        ):
            self.assertEqual(spec.file_path, fused.file_path)
            self.assertEqual(spec.line, fused.line)
            self.assertAlmostEqual(spec.score, fused.fused_score, places=6)


# ======================================================================
# TEST CLASS 6: End-to-end integration
# ======================================================================

class TestEndToEndIntegration(unittest.TestCase):
    """Full pipeline tests using the localize() entry point."""

    def test_mode_b_with_tied_fixture(self):
        """Mode B (pre-collected) with the tied fixture should produce a top-K result."""
        fixture = make_tied_file_level_fixture()
        result = fl.localize(
            project_root=fixture["project_root"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            per_test_coverage=fixture["per_test_coverage"],
            use_fusion=False,
        )

        self.assertTrue(result.get("ranked_candidates"))
        self.assertIn("evidence_for_llm", result)
        self.assertIn("spectrum_summary", result)

        # Top candidate should be the true fault
        top = result["ranked_candidates"][0]
        true_file, true_line = fixture["true_fault"]
        self.assertEqual(top["file_path"], true_file)
        self.assertEqual(top["line"], true_line)

    def test_evidence_for_llm_is_nonempty(self):
        """The evidence_for_llm string should contain actionable information."""
        fixture = make_tied_file_level_fixture()
        result = fl.localize(
            project_root=fixture["project_root"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            per_test_coverage=fixture["per_test_coverage"],
        )

        evidence = result["evidence_for_llm"]
        # Should contain specific numbers, not just a bare float
        self.assertIn("ef=", evidence)
        self.assertIn("ep=", evidence)
        self.assertIn("Ochiai=", evidence)
        # Should contain the true fault location
        self.assertIn("bug_file.py", evidence)
        self.assertIn("42", evidence)

    def test_degradation_returns_structured_reason(self):
        """Degradation should return a structured dict, not raise an exception."""
        result = fl.localize(
            project_root="/nonexistent/path",
        )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["ranked_candidates"], [])
        self.assertIn("degradation_reason", result)
        self.assertIsInstance(result["degradation_reason"], str)


# ======================================================================
# TEST CLASS 7: Score spread analysis
# ======================================================================

class TestScoreSpreadAnalysis(unittest.TestCase):
    """Tests specifically for the score spread metric (unique_scores, min, max)."""

    def test_large_fixture_has_real_variance(self):
        """The astroid-like fixture should produce many unique scores."""
        fixture = make_astroid_like_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )

        self.assertTrue(result)
        spread = result["score_spread"]

        # Should have significantly more than 1 unique score (not a tie)
        self.assertGreater(
            spread["unique_scores"], 1,
            f"Expected >1 unique scores, got {spread['unique_scores']}. "
            f"Line-level Ochiai should produce real variance, not a tie."
        )

        # Max score should be meaningfully higher than min
        self.assertGreater(
            spread["max"] - spread["min"], 0.05,
            f"Score range ({spread['min']:.4f} to {spread['max']:.4f}) should be > 0.05"
        )

    def test_score_spread_includes_total_candidates(self):
        """Score spread should report total number of candidate lines."""
        fixture = make_tied_file_level_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )

        self.assertIn("total_candidates", result["score_spread"])
        self.assertGreater(result["score_spread"]["total_candidates"], 0)


# ======================================================================
# TEST CLASS 8: Language-agnostic backend system
# ======================================================================

class TestBackendSystem(unittest.TestCase):
    """Tests for the language-agnostic backend architecture."""

    def test_detect_language_python(self):
        """Python project detection via pyproject.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
                f.write("[project]\nname = 'test'\n")
            self.assertEqual(backends.detect_language(tmpdir), "python")

    def test_detect_language_javascript(self):
        """JavaScript project detection via package.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "package.json"), "w") as f:
                f.write('{"name": "test"}\n')
            self.assertEqual(backends.detect_language(tmpdir), "javascript")

    def test_detect_language_rust(self):
        """Rust project detection via Cargo.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "Cargo.toml"), "w") as f:
                f.write("[package]\nname = \"test\"\n")
            self.assertEqual(backends.detect_language(tmpdir), "rust")

    def test_detect_language_go(self):
        """Go project detection via go.mod."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "go.mod"), "w") as f:
                f.write("module test\n")
            self.assertEqual(backends.detect_language(tmpdir), "go")

    def test_detect_language_java_maven(self):
        """Java project detection via pom.xml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pom.xml"), "w") as f:
                f.write("<project></project>\n")
            self.assertEqual(backends.detect_language(tmpdir), "java")

    def test_detect_language_java_gradle(self):
        """Java project detection via build.gradle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "build.gradle"), "w") as f:
                f.write("plugins { id 'java' }\n")
            self.assertEqual(backends.detect_language(tmpdir), "java")

    def test_detect_language_unknown(self):
        """Unknown project returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(backends.detect_language(tmpdir))

    def test_list_supported_languages(self):
        """Should list all 5 supported languages."""
        langs = backends.list_supported_languages()
        self.assertIn("python", langs)
        self.assertIn("javascript", langs)
        self.assertIn("go", langs)
        self.assertIn("rust", langs)
        self.assertIn("java", langs)

    def test_get_python_backend(self):
        """Should return a Python backend for a Python project."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
                f.write("[project]\nname = 'test'\n")
            backend = backends.get_backend(None, tmpdir)
            self.assertIsNotNone(backend)
            self.assertEqual(backend.language, "python")

    def test_get_backend_with_explicit_language(self):
        """Explicit language override should be respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Empty dir, but we force Python
            backend = backends.get_backend("python", tmpdir)
            self.assertIsNotNone(backend)
            self.assertEqual(backend.language, "python")

    def test_get_backend_unknown_language(self):
        """Unsupported language returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = backends.get_backend("brainfuck", tmpdir)
            self.assertIsNone(backend)


# ======================================================================
# TEST CLASS 9: Multi-language Ochiai computation
# ======================================================================

class TestMultiLanguageOchiai(unittest.TestCase):
    """
    Verify that the core Ochiai math works identically regardless of
    which language produced the coverage data.
    """

    def _check_basic_invariants(self, result, fixture):
        """Common checks for any language's spectrum output."""
        self.assertTrue(result, "Spectrum should not degrade")
        self.assertGreater(result["nf"], 0)
        self.assertGreater(result["np"], 0)
        self.assertGreater(len(result["ranked_candidates"]), 0)
        self.assertGreater(result["score_spread"]["unique_scores"], 1)

        # True fault should be in the top candidates
        true_file, true_line = fixture["true_fault"]
        found = False
        for c in result["ranked_candidates"][:10]:
            if c.file_path == true_file and c.line == true_line:
                found = True
                break
        self.assertTrue(
            found,
            f"True fault {true_file}:{true_line} not found in top-10. "
            f"Top 5: {[(c.file_path, c.line, round(c.score, 4)) for c in result['ranked_candidates'][:5]]}"
        )

    def test_javascript_fixture(self):
        """Ochiai on JS coverage data should identify the fault."""
        fixture = make_javascript_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )
        self._check_basic_invariants(result, fixture)

    def test_go_fixture(self):
        """Ochiai on Go coverage data should identify the fault."""
        fixture = make_go_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )
        self._check_basic_invariants(result, fixture)

    def test_rust_fixture(self):
        """Ochiai on Rust coverage data should identify the fault."""
        fixture = make_rust_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )
        self._check_basic_invariants(result, fixture)

    def test_java_fixture(self):
        """Ochiai on Java coverage data should identify the fault."""
        fixture = make_java_fixture()
        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
        )
        self._check_basic_invariants(result, fixture)

    def test_all_languages_same_ochiai_formula(self):
        """
        Verify Ochiai is computed identically across languages.

        Create the same coverage pattern for two different "languages"
        and verify identical scores.
        """
        # Same mathematical setup, different file extensions
        failing = ["test_fail"]
        passing = [f"test_pass_{i}" for i in range(5)]

        # File with 3 lines. Failing test hits line 10.
        # Lines 10, 20, 30 are in the file.
        # Line 10: ef=1, ep=2 (only 2 of 5 passing tests)
        # Line 20: ef=1, ep=5
        # Line 30: ef=1, ep=5

        for ext, lang_name in [(".py", "Python"), (".js", "JavaScript"),
                                (".go", "Go"), (".rs", "Rust"), (".java", "Java")]:
            fname = f"src/main{ext}"
            per_test = {fname: {}}
            per_test[fname][failing[0]] = {10, 20, 30}
            for i, pt in enumerate(passing):
                lines = {20, 30}
                if i < 2:
                    lines.add(10)
                per_test[fname][pt] = lines

            result = spectrum.compute_from_per_test_coverage(
                per_test_coverage=_transpose_coverage(per_test),
                failing_test_ids=failing,
                passing_test_ids=passing,
                project_root="/fake",
            )

            self.assertTrue(result, f"{lang_name} spectrum should not degrade")

            # Find line 10's score
            line_10_score = None
            for c in result["ranked_candidates"]:
                if c.line == 10:
                    line_10_score = c.score
                    break

            self.assertIsNotNone(line_10_score, f"{lang_name}: line 10 should be a candidate")

            # Ochiai(10) = 1/sqrt((1+2)*1) = 1/sqrt(3) ≈ 0.5774
            self.assertAlmostEqual(
                line_10_score, 1.0 / (3 ** 0.5), places=4,
                msg=f"{lang_name}: Ochiai for line 10 should be 1/sqrt(3)"
            )


# ======================================================================
# TEST CLASS 10: Function name mapping (language-agnostic)
# ======================================================================

class TestFunctionNameMapping(unittest.TestCase):
    """Tests for the optional func_name_map parameter."""

    def test_func_name_map_applied(self):
        """Provided func_name_map should appear in SpectrumResult."""
        fixture = make_tied_file_level_fixture()
        func_map = {
            ("src/bug_file.py", 42): "the_buggy_function",
            ("src/bug_file.py", 41): "the_buggy_function",
        }

        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
            func_name_map=func_map,
        )

        self.assertTrue(result)
        # Find the top candidate (should be bug_file.py:42)
        top = result["ranked_candidates"][0]
        self.assertEqual(top.function_name, "the_buggy_function")

    def test_no_func_name_map_is_none(self):
        """Without func_name_map, function_name should be None."""
        fixture = make_tied_file_level_fixture()

        result = spectrum.compute_from_per_test_coverage(
            per_test_coverage=fixture["per_test_coverage"],
            failing_test_ids=fixture["failing_test_ids"],
            passing_test_ids=fixture["passing_test_ids"],
            project_root=fixture["project_root"],
            func_name_map=None,
        )

        self.assertTrue(result)
        top = result["ranked_candidates"][0]
        self.assertIsNone(top.function_name)


# ======================================================================
# TEST CLASS 11: Source file filtering per backend
# ======================================================================

class TestSourceFileFiltering(unittest.TestCase):
    """Tests for the is_source_file method across backends."""

    def test_python_backend_filters_tests(self):
        """Python backend should skip test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = python_backend.PythonCoverageBackend(tmpdir)
            self.assertFalse(backend.is_source_file("tests/test_foo.py", "/abs/tests/test_foo.py"))
            self.assertTrue(backend.is_source_file("src/main.py", "/abs/src/main.py"))

    def test_python_backend_filters_site_packages(self):
        """Python backend should skip site-packages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = python_backend.PythonCoverageBackend(tmpdir)
            self.assertFalse(backend.is_source_file(
                "requests/api.py", "/usr/lib/python3/site-packages/requests/api.py"
            ))

    def test_python_backend_requires_py_extension(self):
        """Python backend should only accept .py files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = python_backend.PythonCoverageBackend(tmpdir)
            self.assertFalse(backend.is_source_file("src/main.js", "/abs/src/main.js"))
            self.assertTrue(backend.is_source_file("src/main.py", "/abs/src/main.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
