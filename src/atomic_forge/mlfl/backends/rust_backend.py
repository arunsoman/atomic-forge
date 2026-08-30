from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from base import CoverageBackend, TestInfo


class RustCoverageBackend(CoverageBackend):
    """
    Coverage backend for Rust projects.

    Uses:
      - `cargo test -- --list` for test discovery
      - `cargo test` for failure detection
      - `cargo llvm-cov` (preferred) or `cargo-tarpaulin` for coverage

    Coverage tools (tried in order):
      1. cargo-llvm-cov: fast, accurate, uses LLVM source-based coverage
      2. cargo-tarpaulin: works via ptrace/Docker, slower but widely available
    """

    language = "rust"

    def __init__(self, project_root: str):
        super().__init__(project_root)
        self._cov_tool = self._detect_coverage_tool()

    @classmethod
    def detect(cls, project_root: str) -> bool:
        """Detect Rust project by Cargo.toml."""
        return os.path.exists(os.path.join(project_root, "Cargo.toml"))

    def _detect_coverage_tool(self) -> str | None:
        """Detect available Rust coverage tool."""
        # Try llvm-cov first (faster, more accurate)
        try:
            r = subprocess.run(
                ["cargo", "llvm-cov", "--version"],
                capture_output=True, text=True, timeout=15,
                cwd=self.project_root,
            )
            if r.returncode == 0:
                return "llvm-cov"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Try tarpaulin
        try:
            r = subprocess.run(
                ["cargo", "tarpaulin", "--version"],
                capture_output=True, text=True, timeout=15,
                cwd=self.project_root,
            )
            if r.returncode == 0:
                return "tarpaulin"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return None

    def _cargo_cmd(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run a cargo command in project_root."""
        return subprocess.run(
            ["cargo", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=self.project_root,
        )

    # ── Test discovery ─────────────────────────────────────────────────

    def discover_tests(self) -> list[TestInfo]:
        """Discover tests via `cargo test -- --list`."""
        try:
            r = self._cargo_cmd("test", "--", "--list", timeout=120)
        except subprocess.TimeoutExpired:
            return []

        tests: list[TestInfo] = []
        current_bin = ""

        for line in (r.stdout + r.stderr).splitlines():
            line = line.strip()

            # Track binary name: "Running unittests src/lib.rs"
            if line.startswith("Running "):
                # Extract file path
                parts = line.split()
                if len(parts) >= 3:
                    current_bin = parts[-1]
                continue

            # Parse test listing: "    test_name ... ok"
            m = re.match(r"^(\w+(?:::\w+)*)\s+.*ok$", line)
            if m:
                test_name = m.group(1)
                # Convert Rust test path to test ID
                # Cargo runs: cargo test test_name
                test_id = f"{test_name}"
                tests.append(TestInfo(
                    test_id=test_id,
                    source_file=current_bin,
                ))

        return tests

    # ── Failure detection ──────────────────────────────────────────────

    def find_failing_tests(
        self, test_ids: list[str] | None = None, timeout: int = 600,
    ) -> list[str]:
        """Run `cargo test` and parse failing test names."""
        try:
            r = self._cargo_cmd("test", "--", "--format", "json", timeout=timeout)
        except subprocess.TimeoutExpired:
            return []

        if r.returncode == 0:
            return []  # All passed

        failed: list[str] = []
        output = r.stdout + r.stderr

        # Parse JSON test output
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
                event_type = event.get("type", "")
                if event_type == "test" and event.get("event") == "failed":
                    test_name = event.get("name", "")
                    if test_name:
                        failed.append(test_name)
            except json.JSONDecodeError:
                continue

        # Fallback: parse text output
        if not failed:
            for line in output.splitlines():
                m = re.match(r"^test (.+?) \.{3} FAILED", line.strip())
                if m:
                    failed.append(m.group(1))

        return sorted(set(failed))

    # ── Coverage collection ────────────────────────────────────────────

    def collect_per_test_coverage(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        *,
        timeout_per_test: int = 300,
        batch_by_file: bool = True,
        verbose: bool = False,
    ) -> dict[str, dict[str, set[int]]]:
        """
        Collect per-test coverage.

        Strategy: run each test individually with coverage.
        Rust compiles once (incremental), then runs each test binary.
        """
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        if self._cov_tool == "llvm-cov":
            return self._collect_llvm_cov(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        elif self._cov_tool == "tarpaulin":
            return self._collect_tarpaulin(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        else:
            if verbose:
                print("[rust] No coverage tool found. Install cargo-llvm-cov or cargo-tarpaulin.")
            return {}

    def _collect_llvm_cov(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect coverage using cargo-llvm-cov."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                report_dir = os.path.join(tmpdir, f"cov_fail_{ft.replace('::', '_')}")
                os.makedirs(report_dir, exist_ok=True)

                try:
                    r = subprocess.run(
                        ["cargo", "llvm-cov", "--", ft,
                         "--format", "json", "-Z", "unstable-options"],
                        capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_llvm_cov_json(r.stdout, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[rust] TIMEOUT: {ft}")
                    continue

            # ── Passing tests in batches ─────────────────────────
            # Group tests by source file/module for batching
            if batch_by_file:
                batches = self._group_tests_by_file(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                # Build the test filter
                test_filter = " ".join(batch)

                try:
                    r = subprocess.run(
                        ["cargo", "llvm-cov", "--", *batch,
                         "--format", "json", "-Z", "unstable-options"],
                        capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_llvm_cov_json(r.stdout, verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    def _collect_tarpaulin(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect coverage using cargo-tarpaulin."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                out_xml = os.path.join(tmpdir, f"cov_fail.xml")
                try:
                    r = subprocess.run(
                        ["cargo", "tarpaulin",
                         "--out", "xml",
                         "--output-dir", tmpdir,
                         "--", ft],
                        capture_output=True, text=True,
                        timeout=timeout_per_test * 2,  # tarpaulin is slow
                        cwd=self.project_root,
                    )
                    lines_map = self._read_tarpaulin_xml(out_xml, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[rust] TIMEOUT: {ft}")
                    continue

            # ── Passing tests in batches ─────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_file(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                out_xml = os.path.join(tmpdir, f"cov_pass_{batch_idx}.xml")
                try:
                    r = subprocess.run(
                        ["cargo", "tarpaulin",
                         "--out", "xml",
                         "--output-dir", os.path.join(tmpdir, f"pass_{batch_idx}"),
                         "--", *batch],
                        capture_output=True, text=True,
                        timeout=timeout_per_test * 2,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_tarpaulin_xml(out_xml, verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """Build (file, line) -> function_name for Rust via regex."""
        func_map: dict[tuple[str, int], str | None] = {}

        for dirpath, _, filenames in os.walk(self.project_root):
            if any(d in dirpath.split(os.sep) for d in ("target", ".git", "tests")):
                continue
            for fname in filenames:
                if not fname.endswith(".rs"):
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

                self._regex_rust_func_map(source, rel, func_map)

        return func_map

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """Filter: skip test files, target/, generated code."""
        if not super().is_source_file(rel_path, abs_path):
            return False
        if not rel_path.endswith(".rs"):
            return False
        # Skip build.rs and test files
        if rel_path == "build.rs":
            return False
        parts = rel_path.replace(os.sep, "/").split("/")
        if "target" in parts:
            return False
        return True

    # ── Helpers ────────────────────────────────────────────────────────

    def _read_llvm_cov_json(
        self, json_output: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Parse cargo-llvm-cov JSON output."""
        result: dict[str, set[int]] = {}

        try:
            data = json.loads(json_output)
        except (json.JSONDecodeError, ValueError):
            return {}

        # llvm-cov JSON: {"files": [{"filename": ..., "segments": [[line, col, count, ...], ...]}]}
        files = data.get("files", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        for file_info in files:
            filepath = file_info.get("filename", "")
            try:
                rel = os.path.relpath(filepath, self.project_root)
            except ValueError:
                continue

            if not self.is_source_file(rel, filepath):
                continue

            executed = set()
            for segment in file_info.get("segments", []):
                if len(segment) >= 3 and segment[2] > 0:  # count > 0
                    line = segment[0]
                    if line > 0:
                        executed.add(line)

            if executed:
                result[rel] = executed

        return result

    def _read_tarpaulin_xml(
        self, xml_path: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Parse tarpaulin's Cobertura XML output (minimal, no lxml dependency)."""
        result: dict[str, set[int]] = {}

        try:
            with open(xml_path) as f:
                content = f.read()
        except FileNotFoundError:
            return {}

        # Simple XML parsing without lxml
        # Look for <class> elements with filename and <line> children
        import re as re_mod

        # Find all class elements
        class_pattern = re_mod.compile(
            r'<class[^>]*filename="([^"]+)"[^>]*>(.*?)</class>',
            re_mod.DOTALL,
        )
        line_pattern = re_mod.compile(r'<line[^>]*number="(\d+)"[^>]*/>')

        for match in class_pattern.finditer(content):
            filepath = match.group(1)
            try:
                rel = os.path.relpath(filepath, self.project_root)
            except ValueError:
                continue

            if not self.is_source_file(rel, filepath):
                continue

            class_body = match.group(2)
            executed = set()
            for line_match in line_pattern.finditer(class_body):
                try:
                    executed.add(int(line_match.group(1)))
                except ValueError:
                    continue

            if executed:
                result[rel] = executed

        return result

    @staticmethod
    def _regex_rust_func_map(
        source: str, rel: str,
        func_map: dict[tuple[str, int], str | None],
    ) -> None:
        """Extract Rust function names via regex."""
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            # fn name(...) or pub fn name(...) or async fn name(...)
            m = re.match(
                r"^(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+[^{]*\s+)?fn\s+(\w+)",
                stripped,
            )
            if m:
                func_map[(rel, i)] = m.group(1)

    @staticmethod
    def _group_tests_by_file(test_ids: list[str]) -> list[list[str]]:
        """Group Rust test IDs by source file."""
        groups: dict[str, list[str]] = {}
        for tid in test_ids:
            # Rust test IDs are just names like "test_something"
            # We group by implicit module (all tests in same binary)
            groups.setdefault("_default_", []).append(tid)
        return list(groups.values())


def create_backend(project_root: str) -> RustCoverageBackend:
    """Factory function for the Rust backend."""
    return RustCoverageBackend(project_root)
