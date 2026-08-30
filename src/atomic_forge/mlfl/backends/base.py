from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TestInfo:
    """A discovered test."""
    test_id: str            # unique identifier (e.g. 'tests/test_foo.py::test_bar')
    source_file: str | None = None  # file the test lives in (for grouping)


@dataclass
class CoverageCollectionResult:
    """Result of a full coverage collection run."""
    per_test_coverage: dict[str, dict[str, set[int]]]
    failing_test_ids: list[str]
    passing_test_ids: list[str]
    project_root: str
    coverage_method: str = ""
    warnings: list[str] = field(default_factory=list)


class CoverageBackend(abc.ABC):
    """
    Abstract base class for language-specific coverage backends.

    Each language (Python, JS, Go, Rust, Java) provides a concrete
    implementation. The fault localization core (Ochiai math, fusion)
    is language-agnostic — it only needs the CoverageCollectionResult.
    """

    # Subclasses set this to the language name
    language: str = "unknown"

    def __init__(self, project_root: str):
        self.project_root = project_root

    # ── Lifecycle ──────────────────────────────────────────────────────

    @classmethod
    @abc.abstractmethod
    def detect(cls, project_root: str) -> bool:
        """Return True if this backend can handle the project at project_root."""

    # ── Test discovery ─────────────────────────────────────────────────

    @abc.abstractmethod
    def discover_tests(self) -> list[TestInfo]:
        """Return all discoverable tests in the project."""

    @abc.abstractmethod
    def find_failing_tests(
        self,
        test_ids: list[str] | None = None,
        timeout: int = 300,
    ) -> list[str]:
        """Run the test suite and return IDs of failing tests."""

    # ── Coverage collection ────────────────────────────────────────────

    @abc.abstractmethod
    def collect_per_test_coverage(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        *,
        timeout_per_test: int = 120,
        batch_by_file: bool = True,
        verbose: bool = False,
    ) -> dict[str, dict[str, set[int]]]:
        """
        Collect line-level coverage for each test/batch.

        Returns: {test_id_or_batch_id: {file_path: set_of_line_numbers}}

        The file paths should be relative to project_root.
        Return {} on total failure (degrade-to-{} contract).
        """

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """
        Build (file, line) -> function_name mapping.

        Default implementation returns empty dict. Subclasses should
        override to provide language-specific parsing (AST, regex, etc.).
        """
        return {}

    # ── Convenience: full pipeline ─────────────────────────────────────

    def collect(
        self,
        failing_test_ids: list[str] | None = None,
        *,
        timeout_per_test: int = 120,
        batch_by_file: bool = True,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """
        End-to-end: discover tests -> find failures -> collect coverage.

        Returns:
            Success: {per_test_coverage, failing_test_ids, passing_test_ids, project_root, coverage_method}
            Degradation: {}
        """
        if verbose:
            print(f"[{self.language}] Starting collection from {self.project_root}")

        # Discover tests
        if verbose:
            print(f"[{self.language}] Discovering tests...")
        try:
            all_tests = self.discover_tests()
        except Exception as exc:
            if verbose:
                print(f"[{self.language}] SKIP: test discovery failed: {exc}")
            return {}

        if not all_tests:
            if verbose:
                print(f"[{self.language}] SKIP: no tests discovered")
            return {}

        all_test_ids = [t.test_id for t in all_tests]
        if verbose:
            print(f"[{self.language}] Found {len(all_test_ids)} tests")

        # Find failing tests
        if failing_test_ids is None:
            if verbose:
                print(f"[{self.language}] Running suite to find failures...")
            try:
                failing_test_ids = self.find_failing_tests(
                    test_ids=all_test_ids,
                    timeout=timeout_per_test * max(len(all_test_ids) // 50, 1),
                )
            except Exception as exc:
                if verbose:
                    print(f"[{self.language}] SKIP: failure detection failed: {exc}")
                return {}

        if not failing_test_ids:
            if verbose:
                print(f"[{self.language}] SKIP: no failing tests found")
            return {}

        failing_set = set(failing_test_ids)
        passing_test_ids = sorted(set(all_test_ids) - failing_set)

        if not passing_test_ids:
            if verbose:
                print(f"[{self.language}] SKIP: all tests fail — no discrimination possible")
            return {}

        if verbose:
            print(f"[{self.language}] {len(failing_test_ids)} failing, {len(passing_test_ids)} passing")

        # Collect per-test coverage
        per_test_coverage = self.collect_per_test_coverage(
            failing_test_ids,
            passing_test_ids,
            timeout_per_test=timeout_per_test,
            batch_by_file=batch_by_file,
            verbose=verbose,
        )

        if not per_test_coverage:
            if verbose:
                print(f"[{self.language}] SKIP: no coverage data collected")
            return {}

        return {
            "per_test_coverage": per_test_coverage,
            "failing_test_ids": failing_test_ids,
            "passing_test_ids": passing_test_ids,
            "project_root": self.project_root,
            "coverage_method": f"{self.language}_" + ("batch_by_file" if batch_by_file else "per_test"),
        }

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """
        Return True if a file should be included in coverage analysis.

        Default: skip test directories and external packages.
        Subclasses may override for language-specific patterns.
        """
        parts = rel_path.replace(os.sep, "/").split("/")
        # Skip test directories
        test_dirs = {"tests", "test", "__tests__", "spec", "specs"}
        if any(p in test_dirs for p in parts):
            return False
        # Skip common non-source dirs
        skip_dirs = {"node_modules", "vendor", "third_party", "third-party",
                     "site-packages", "dist-packages", "build", "dist",
                     ".git", ".venv", "venv", "env"}
        if any(p in skip_dirs for p in parts):
            return False
        return True


import os  # noqa: E402 — needed for is_source_file
