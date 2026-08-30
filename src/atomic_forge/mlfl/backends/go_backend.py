from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from base import CoverageBackend, TestInfo


class GoCoverageBackend(CoverageBackend):
    """
    Coverage backend for Go projects.

    Uses:
      - `go test -list` for test discovery
      - `go test` for failure detection
      - `go test -coverprofile` for per-package coverage
      - `go test -run` to isolate individual tests/functions

    Note: Go's coverage is per-function, not per-line, but coverprofile
    does provide line-level granularity when using `-covermode=count`.

    Since Go doesn't have per-test coverage natively, we use per-test
    invocations with `-run TestName` to approximate it.
    """

    language = "go"

    @classmethod
    def detect(cls, project_root: str) -> bool:
        """Detect Go project by go.mod."""
        return os.path.exists(os.path.join(project_root, "go.mod"))

    def _go_cmd(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run a go command in project_root."""
        return subprocess.run(
            ["go", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=self.project_root,
        )

    def _list_packages(self) -> list[str]:
        """List all Go packages (including tests) in the project."""
        r = self._go_cmd("list", "-json", "./...", timeout=60)
        if r.returncode != 0:
            return []

        packages = []
        # Parse JSON array output
        decoder = json.JSONDecoder()
        text = r.stdout.strip()
        pos = 0
        while pos < len(text):
            try:
                obj, end = decoder.raw_decode(text, pos)
                pos = end
                pkg_path = obj.get("ImportPath", "")
                test_files = obj.get("TestGoFiles", []) + obj.get("XTestGoFiles", [])
                if test_files and pkg_path:
                    packages.append(pkg_path)
            except json.JSONDecodeError:
                break
        return packages

    # ── Test discovery ─────────────────────────────────────────────────

    def discover_tests(self) -> list[TestInfo]:
        """Discover tests via `go test -list`."""
        packages = self._list_packages()
        if not packages:
            return []

        tests: list[TestInfo] = []
        for pkg in packages:
            r = self._go_cmd("test", "-list", ".", "-json", pkg, timeout=60)
            if r.returncode != 0:
                continue

            # Parse JSON output
            decoder = json.JSONDecoder()
            text = r.stdout.strip()
            pos = 0
            while pos < len(text):
                try:
                    obj, end = decoder.raw_decode(text, pos)
                    pos = end
                    action = obj.get("Action", "")
                    if action == "run":
                        test_name = obj.get("Test", "")
                        if test_name:
                            test_id = f"{pkg}::{test_name}"
                            tests.append(TestInfo(
                                test_id=test_id,
                                source_file=pkg,
                            ))
                except json.JSONDecodeError:
                    break

        return tests

    # ── Failure detection ──────────────────────────────────────────────

    def find_failing_tests(
        self, test_ids: list[str] | None = None, timeout: int = 300,
    ) -> list[str]:
        """Run `go test` and parse failing test names."""
        packages = self._list_packages()
        if not packages:
            return []

        r = self._go_cmd("test", "-v", "-json", *packages, timeout=timeout)
        if r.returncode == 0:
            return []  # All passed

        failed: list[str] = []
        # Parse JSON test output
        decoder = json.JSONDecoder()
        text = (r.stdout + r.stderr).strip()
        pos = 0
        while pos < len(text):
            try:
                obj, end = decoder.raw_decode(text, pos)
                pos = end
                action = obj.get("Action", "")
                if action == "fail":
                    test_name = obj.get("Test", "")
                    pkg = obj.get("Package", "")
                    if test_name and pkg:
                        failed.append(f"{pkg}::{test_name}")
            except json.JSONDecodeError:
                break

        return sorted(set(failed))

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
        """
        Collect per-test coverage using `go test -coverprofile`.

        Go doesn't have true per-test coverage, so we run each test
        individually with `-run TestName`.
        """
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests: each one individually ───────────────
            for ft in failing_test_ids:
                pkg, test_name = ft.split("::", 1) if "::" in ft else (ft, "")
                if not test_name:
                    continue

                cov_file = os.path.join(tmpdir, f"cov_fail_{test_name}.out")
                try:
                    r = self._go_cmd(
                        "test", "-run", f"^{re.escape(test_name)}\$",
                        "-coverprofile", cov_file,
                        "-covermode", "count",
                        pkg,
                        timeout=timeout_per_test,
                    )
                    lines_map = self._read_coverprofile(cov_file, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                        if verbose:
                            print(f"[go] FAIL {ft}: {len(lines_map)} files")
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[go] TIMEOUT: {ft}")
                    continue

            # ── Passing tests in batches (by package) ──────────────
            if batch_by_file:
                batches = self._group_tests_by_package(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                cov_file = os.path.join(tmpdir, f"cov_pass_{batch_idx}.out")

                if len(batch) == 1:
                    pkg, test_name = batch[0].split("::", 1)
                    run_arg = f"^{re.escape(test_name)}\$"
                    r = self._go_cmd(
                        "test", "-run", run_arg,
                        "-coverprofile", cov_file,
                        "-covermode", "count",
                        pkg,
                        timeout=timeout_per_test,
                    )
                else:
                    # Group by package and run multiple test patterns
                    # Go's -run takes a regex
                    pkg_groups: dict[str, list[str]] = {}
                    for t in batch:
                        pkg, tname = t.split("::", 1) if "::" in t else (t, "")
                        pkg_groups.setdefault(pkg, []).append(tname)

                    # For simplicity, run the whole package
                    # (individual test isolation needs multiple runs)
                    pkg = list(pkg_groups.keys())[0] if pkg_groups else ""
                    if not pkg:
                        continue
                    try:
                        r = self._go_cmd(
                            "test", "-coverprofile", cov_file,
                            "-covermode", "count",
                            pkg,
                            timeout=timeout_per_test,
                        )
                    except subprocess.TimeoutExpired:
                        continue

                lines_map = self._read_coverprofile(cov_file, verbose)
                if lines_map:
                    per_test_coverage[batch_label] = lines_map

        return per_test_coverage

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """Build (file, line) -> function_name for Go via regex."""
        func_map: dict[tuple[str, int], str | None] = {}

        for dirpath, _, filenames in os.walk(self.project_root):
            if any(d in dirpath.split(os.sep) for d in ("vendor", ".git")):
                continue
            for fname in filenames:
                if not fname.endswith(".go"):
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
                except OSError:
                    continue

                self._regex_go_func_map(source, rel, func_map)

        return func_map

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """Filter: skip _test.go files, vendor, generated code."""
        if not super().is_source_file(rel_path, abs_path):
            return False
        if not rel_path.endswith(".go"):
            return False
        if rel_path.endswith("_test.go"):
            return False
        # Skip generated files
        parts = rel_path.replace(os.sep, "/").split("/")
        gen_dirs = {"vendor", "generated", "proto", "pb.go", ".git"}
        if any(d in gen_dirs or d.startswith("pb.") for d in parts):
            return False
        return True

    # ── Helpers ────────────────────────────────────────────────────────

    def _read_coverprofile(
        self, coverprofile_path: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """
        Parse a Go coverprofile file.

        Format: each line is: file:start_line.start_col,end_line.end_col count
        """
        result: dict[str, set[int]] = {}

        try:
            with open(coverprofile_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("mode:"):
                        continue

                    # Parse: file:start.end count
                    parts = line.rsplit(" ", 1)
                    if len(parts) != 2:
                        continue

                    try:
                        count = int(parts[1])
                    except ValueError:
                        continue

                    if count == 0:
                        continue

                    loc_part = parts[0]
                    # Split file from line range
                    colon_idx = loc_part.rfind(":")
                    if colon_idx < 0:
                        continue

                    filepath = loc_part[:colon_idx]
                    line_range = loc_part[colon_idx + 1:]

                    # Parse start_line.start_col,end_line.end_col
                    try:
                        range_parts = line_range.split(",")
                        start_info = range_parts[0].split(".")
                        end_info = range_parts[1].split(".") if len(range_parts) > 1 else start_info
                        start_line = int(start_info[0])
                        end_line = int(end_info[0]) if end_info else start_line
                    except (ValueError, IndexError):
                        continue

                    # Make path relative
                    try:
                        rel = os.path.relpath(filepath, self.project_root)
                    except ValueError:
                        continue

                    if not self.is_source_file(rel, filepath):
                        continue

                    if rel not in result:
                        result[rel] = set()
                    for ln in range(start_line, end_line + 1):
                        result[rel].add(ln)

        except FileNotFoundError:
            pass

        return result

    @staticmethod
    def _regex_go_func_map(
        source: str, rel: str,
        func_map: dict[tuple[str, int], str | None],
    ) -> None:
        """Extract Go function names via regex."""
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            # func (receiver) Name(...) ...
            m = re.match(r"^func\s+(?:\([^)]*\)\s+)?(\w+)", stripped)
            if m:
                name = m.group(1)
                # Skip unexported functions if desired (keep them for coverage)
                func_map[(rel, i)] = name

    @staticmethod
    def _group_tests_by_package(test_ids: list[str]) -> list[list[str]]:
        """Group Go test IDs by package."""
        groups: dict[str, list[str]] = {}
        for tid in test_ids:
            pkg = tid.split("::")[0] if "::" in tid else tid
            groups.setdefault(pkg, []).append(tid)
        return list(groups.values())


def create_backend(project_root: str) -> GoCoverageBackend:
    """Factory function for the Go backend."""
    return GoCoverageBackend(project_root)
