"""
python_backend.py — Coverage backend for Python projects using pytest + coverage.py.

This is the original backend that was inline in spectrum.py.
It uses:
  - pytest --collect-only for test discovery
  - pytest for failure detection
  - coverage run (Python coverage.py) for per-test line coverage
  - ast module for function name mapping
"""

from __future__ import annotations

import ast as ast_mod
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from typing import Any

from base import CoverageBackend, TestInfo


class PythonCoverageBackend(CoverageBackend):
    """Coverage backend for Python projects using pytest + coverage.py."""

    language = "python"

    @classmethod
    def detect(cls, project_root: str) -> bool:
        """Detect Python project by presence of pyproject.toml, setup.py, etc."""
        markers = {"pyproject.toml", "setup.py", "setup.cfg"}
        entries = set(os.listdir(project_root))
        if entries & markers:
            return True
        # Check for any package dir with __init__.py
        for entry in entries:
            full = os.path.join(project_root, entry)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
                return True
        return False

    # ── Test discovery ─────────────────────────────────────────────────

    def discover_tests(self) -> list[TestInfo]:
        """Discover tests via pytest --collect-only."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
                capture_output=True, text=True, timeout=120,
                cwd=self.project_root,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            test_ids = self._parse_test_ids(result.stdout + result.stderr)
            return [TestInfo(test_id=tid, source_file=tid.split("::")[0] if "::" in tid else tid)
                    for tid in test_ids]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def find_failing_tests(
        self, test_ids: list[str] | None = None, timeout: int = 300,
    ) -> list[str]:
        """Run pytest and return failing test IDs."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--no-header", "-q", "--tb=no"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return sorted(self._parse_failed_tests(result.stdout + result.stderr))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    # ── Coverage collection ────────────────────────────────────────────

    def collect_per_test_coverage(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        *,
        timeout_per_test: int = 120,
        batch_by_file: bool = True,
        verbose: bool = False,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect per-test line coverage using coverage.py."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests: each one individually ───────────────
            for ft in failing_test_ids:
                cov_json = os.path.join(tmpdir, f"cov_fail.json")
                cov_db = os.path.join(tmpdir, ".coverage_fail")
                try:
                    subprocess.run(
                        [sys.executable, "-m", "coverage", "run",
                         f"--data-file={cov_db}",
                         "-m", "pytest", "-x", "-q", "--no-header", "--tb=no",
                         ft],
                        capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                        env={**os.environ, "COVERAGE_FILE": cov_db,
                              "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                    subprocess.run(
                        [sys.executable, "-m", "coverage", "json",
                         f"--data-file={cov_db}", "-o", cov_json],
                        capture_output=True, text=True,
                        timeout=30, cwd=self.project_root,
                        env={**os.environ, "COVERAGE_FILE": cov_db},
                    )
                    lines_map = self._read_coverage_json(cov_json, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                        if verbose:
                            total_lines = sum(len(v) for v in lines_map.values())
                            print(f"[python] FAIL {ft}: {len(lines_map)} files, {total_lines} lines")
                    elif verbose:
                        print(f"[python] FAIL {ft}: no coverage data parsed")
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[python] TIMEOUT: failing test {ft}")
                    continue

            # ── Passing tests in batches ──────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_file(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            if verbose:
                print(f"[python] Running {len(batches)} passing batches...")

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                cov_json = os.path.join(tmpdir, f"cov_pass_{batch_idx}.json")
                cov_db = os.path.join(tmpdir, f".coverage_pass_{batch_idx}")

                try:
                    subprocess.run(
                        [sys.executable, "-m", "coverage", "run",
                         f"--data-file={cov_db}",
                         "-m", "pytest", "-q", "--no-header", "--tb=no"] +
                        batch,
                        capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                        env={**os.environ, "COVERAGE_FILE": cov_db,
                              "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                    subprocess.run(
                        [sys.executable, "-m", "coverage", "json",
                         f"--data-file={cov_db}", "-o", cov_json],
                        capture_output=True, text=True,
                        timeout=30, cwd=self.project_root,
                        env={**os.environ, "COVERAGE_FILE": cov_db},
                    )
                    lines_map = self._read_coverage_json(cov_json, verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[python] TIMEOUT: passing batch {batch_idx}")
                    continue

                if verbose and (batch_idx + 1) % 20 == 0:
                    print(f"[python]   {batch_idx + 1}/{len(batches)} batches done")

        return per_test_coverage

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """Build (file, line) -> function_name mapping via Python AST."""
        func_map: dict[tuple[str, int], str | None] = {}
        if not self.project_root or not os.path.isdir(self.project_root):
            return func_map

        for dirpath, _, filenames in os.walk(self.project_root):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    rel = os.path.relpath(fpath, self.project_root)
                except ValueError:
                    continue
                if not self.is_source_file(rel, fpath):
                    continue

                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        source = f.read()
                    tree = ast_mod.parse(source, filename=fpath)
                except (SyntaxError, ValueError, UnicodeDecodeError):
                    self._regex_function_map(source, rel, func_map)
                    continue

                for node in ast_mod.walk(tree):
                    if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef)):
                        for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                            func_map[(rel, ln)] = node.name

        return func_map

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """Filter: skip test files, site-packages, etc."""
        if not super().is_source_file(rel_path, abs_path):
            return False
        if not rel_path.endswith(".py"):
            return False
        if "site-packages" in abs_path or "dist-packages" in abs_path:
            return False
        return True

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_test_ids(output: str) -> list[str]:
        """Parse test IDs from pytest --collect-only output."""
        ids = []
        for line in output.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("=") or line.isdigit():
                continue
            if "error" in line.lower() and "collected" not in line.lower():
                continue
            if "::" in line:
                test_id = line.split("(")[0].strip()
                if test_id and not test_id.startswith("FAILED") and not test_id.startswith("PASSED"):
                    ids.append(test_id)
        return ids

    @staticmethod
    def _parse_failed_tests(output: str) -> list[str]:
        """Parse failed test IDs from pytest output (exact matching)."""
        failed = []
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("FAILED "):
                test_id = line[len("FAILED "):]
                if " - " in test_id:
                    test_id = test_id.split(" - ")[0].strip()
                test_id = test_id.rstrip()
                if test_id:
                    failed.append(test_id)
        return failed

    def _read_coverage_json(
        self, cov_json_path: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read a coverage JSON file and return {rel_file: set_of_lines}."""
        try:
            with open(cov_json_path) as f:
                cov = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

        result: dict[str, set[int]] = {}
        files = cov.get("files", {})

        for abs_path, file_data in files.items():
            try:
                rel = os.path.relpath(abs_path, self.project_root)
            except ValueError:
                continue

            if not self.is_source_file(rel, abs_path):
                continue

            executed = set()
            for ln in file_data.get("executed_lines", []):
                if isinstance(ln, int):
                    executed.add(ln)
                elif isinstance(ln, str):
                    try:
                        executed.add(int(ln))
                    except ValueError:
                        continue

            if executed:
                result[rel] = executed

        return result

    @staticmethod
    def _group_tests_by_file(test_ids: list[str]) -> list[list[str]]:
        """Group test IDs by their source file for batched coverage."""
        groups: dict[str, list[str]] = {}
        for tid in test_ids:
            file_part = tid.split("::")[0] if "::" in tid else tid
            if file_part not in groups:
                groups[file_part] = []
            groups[file_part].append(tid)
        return list(groups.values())

    @staticmethod
    def _regex_function_map(
        source: str, rel: str,
        func_map: dict[tuple[str, int], str | None],
    ) -> None:
        """Fallback function name mapping via regex for unparseable Python."""
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            m = re.match(r"^(async\s+)?def\s+(\w+)", stripped)
            if m:
                func_map[(rel, i)] = m.group(2)


def create_backend(project_root: str) -> PythonCoverageBackend:
    """Factory function for the Python backend."""
    return PythonCoverageBackend(project_root)
