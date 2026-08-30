"""
javascript_backend.py -- Coverage backend for JavaScript/TypeScript projects.

Supports multiple test frameworks and coverage tools:
  Test runners: Jest, Vitest, Mocha
  Coverage tools: c8, nyc, istanbul

Coverage output: reads V8/Istanbul JSON coverage reports and extracts
per-test line coverage.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from base import CoverageBackend, TestInfo


class JavaScriptCoverageBackend(CoverageBackend):
    """Coverage backend for JS/TS projects using Jest/Vitest/Mocha + c8/nyc."""

    language = "javascript"

    def __init__(self, project_root: str):
        super().__init__(project_root)
        self._test_runner = self._detect_test_runner()
        self._cov_tool = self._detect_coverage_tool()
        self._pkg_json = self._read_package_json()

    @classmethod
    def detect(cls, project_root: str) -> bool:
        """Detect JS/TS project by package.json."""
        return os.path.exists(os.path.join(project_root, "package.json"))

    def _read_package_json(self) -> dict:
        """Read and cache package.json."""
        pkg_path = os.path.join(self.project_root, "package.json")
        try:
            with open(pkg_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _detect_test_runner(self) -> str | None:
        """Detect which test runner is configured."""
        deps = set()
        for section in ("dependencies", "devDependencies"):
            deps.update(self._pkg_json.get(section, {}).keys())

        scripts = self._pkg_json.get("scripts", {})
        test_script = scripts.get("test", "")

        # Check deps first
        if "vitest" in deps or "vitest" in test_script:
            return "vitest"
        if "jest" in deps or "jest" in test_script:
            return "jest"
        if "mocha" in deps or "mocha" in test_script:
            return "mocha"

        # Check for config files
        config_files = {
            "jest.config.js": "jest",
            "jest.config.ts": "jest",
            "jest.config.mjs": "jest",
            "vitest.config.ts": "vitest",
            "vitest.config.js": "vitest",
            ".mocharc.js": "mocha",
            ".mocharc.yml": "mocha",
            ".mocharc.json": "mocha",
        }
        for fname, runner in config_files.items():
            if os.path.exists(os.path.join(self.project_root, fname)):
                return runner

        # Fallback: check if npx can find them
        return None

    def _detect_coverage_tool(self) -> str | None:
        """Detect which coverage tool is available."""
        deps = set()
        for section in ("dependencies", "devDependencies"):
            deps.update(self._pkg_json.get(section, {}).keys())

        # c8 is built into Node 18+ and Vitest has built-in coverage
        if "c8" in deps:
            return "c8"
        if "nyc" in deps:
            return "nyc"
        if self._test_runner == "vitest":
            return "vitest-built-in"

        # Check if c8 is available on PATH
        try:
            r = subprocess.run(["c8", "--version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return "c8"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        try:
            r = subprocess.run(["npx", "c8", "--version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return "c8"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return None

    # ── Test discovery ─────────────────────────────────────────────────

    def discover_tests(self) -> list[TestInfo]:
        """Discover tests via the detected test runner."""
        runner = self._test_runner
        if runner == "jest":
            return self._discover_jest()
        elif runner == "vitest":
            return self._discover_vitest()
        elif runner == "mocha":
            return self._discover_mocha()
        return []

    def _discover_jest(self) -> list[TestInfo]:
        """Discover Jest tests via --listTests and --findRelatedTests."""
        try:
            # List test files
            r = subprocess.run(
                ["npx", "jest", "--listTests", "--json"],
                capture_output=True, text=True, timeout=60,
                cwd=self.project_root,
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                test_files = data.get("tests", [])
                return [TestInfo(test_id=f, source_file=f) for f in test_files]
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

        # Fallback: glob for test files
        return self._glob_test_files()

    def _discover_vitest(self) -> list[TestInfo]:
        """Discover Vitest tests."""
        try:
            r = subprocess.run(
                ["npx", "vitest", "list", "--json"],
                capture_output=True, text=True, timeout=60,
                cwd=self.project_root,
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                test_files = []
                for item in data if isinstance(data, list) else []:
                    if isinstance(item, dict) and "file" in item:
                        test_files.append(item["file"])
                        for name in item.get("tests", []):
                            test_id = f"{item['file']}::{name}"
                            test_files.append(test_id)
                return [TestInfo(test_id=f, source_file=f.split("::")[0])
                        for f in test_files]
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

        return self._glob_test_files()

    def _discover_mocha(self) -> list[TestInfo]:
        """Discover Mocha tests — glob-based."""
        return self._glob_test_files()

    def _glob_test_files(self) -> list[TestInfo]:
        """Fallback: find test files by convention."""
        test_files = []
        test_patterns = [
            "**/*.test.js", "**/*.test.ts", "**/*.test.jsx", "**/*.test.tsx",
            "**/*.spec.js", "**/*.spec.ts", "**/*.spec.jsx", "**/*.spec.tsx",
            "__tests__/**/*.js", "__tests__/**/*.ts",
        ]
        for root, _, files in os.walk(self.project_root):
            # Skip node_modules
            if "node_modules" in root.split(os.sep):
                continue
            for fname in files:
                if self._is_test_file(fname):
                    full = os.path.join(root, fname)
                    try:
                        rel = os.path.relpath(full, self.project_root)
                    except ValueError:
                        continue
                    test_files.append(TestInfo(test_id=rel, source_file=rel))
        return test_files

    @staticmethod
    def _is_test_file(fname: str) -> bool:
        """Check if a filename looks like a test file."""
        base = fname.lower()
        return (".test." in base or ".spec." in base or
                base.startswith("test_") or base.startswith("spec_"))

    # ── Failure detection ──────────────────────────────────────────────

    def find_failing_tests(
        self, test_ids: list[str] | None = None, timeout: int = 300,
    ) -> list[str]:
        """Run tests and return failing test IDs."""
        runner = self._test_runner
        if runner == "jest":
            return self._find_failing_jest(timeout)
        elif runner == "vitest":
            return self._find_failing_vitest(timeout)
        elif runner == "mocha":
            return self._find_failing_mocha(timeout)
        return []

    def _find_failing_jest(self, timeout: int) -> list[str]:
        """Run Jest and parse failing tests from JSON output."""
        try:
            r = subprocess.run(
                ["npx", "jest", "--json", "--forceExit"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
            )
            data = json.loads(r.stdout)
            failed = []
            for name, result in data.get("testResults", {}).items():
                if result.get("status") == "failed":
                    failed.append(name)
            return sorted(failed)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return []

    def _find_failing_vitest(self, timeout: int) -> list[str]:
        """Run Vitest and parse failing tests."""
        try:
            r = subprocess.run(
                ["npx", "vitest", "run", "--reporter=json", "--forceExit"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
            )
            # Vitest JSON reporter outputs to stdout
            failed = []
            for line in r.stdout.strip().splitlines():
                try:
                    data = json.loads(line)
                    if data.get("type") == "test" and data.get("status") == "failed":
                        failed.append(data.get("name", ""))
                except json.JSONDecodeError:
                    continue
            return sorted(failed)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _find_failing_mocha(self, timeout: int) -> list[str]:
        """Run Mocha and parse failing tests from TAP output."""
        try:
            r = subprocess.run(
                ["npx", "mocha", "--reporter", "json"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
            )
            data = json.loads(r.stdout)
            failed = []
            for result in data.get("failures", []) or data.get("tests", []):
                full_title = result.get("fullTitle", "")
                if full_title:
                    failed.append(full_title)
            return sorted(failed)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
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
        """Collect per-test coverage using c8/nyc or Vitest built-in."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}
        cov_tool = self._cov_tool

        if cov_tool == "vitest-built-in":
            return self._collect_vitest_coverage(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        elif cov_tool in ("c8", "nyc"):
            return self._collect_c8_nyc_coverage(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        else:
            # No coverage tool found — try to install and use c8
            if verbose:
                print(f"[javascript] No coverage tool detected, attempting c8 fallback")
            return self._collect_c8_nyc_coverage(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )

    def _collect_c8_nyc_coverage(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect coverage using c8 or nyc."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}
        cov_tool = self._cov_tool or "c8"
        cov_cmd = ["npx", cov_tool]
        test_cmd = self._get_test_command()

        if not test_cmd:
            if verbose:
                print("[javascript] Cannot determine test command")
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                out_dir = os.path.join(tmpdir, f"cov_fail")
                os.makedirs(out_dir, exist_ok=True)
                try:
                    cmd = cov_cmd + [
                        "--reporter=json", "--reports-dir", out_dir,
                        "--all", "--src", self.project_root,
                    ] + test_cmd + self._test_filter_args(ft)
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                        env={**os.environ, "NODE_V8_COVERAGE": ""},
                    )
                    lines_map = self._read_c8_coverage(out_dir, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[javascript] TIMEOUT: failing test {ft}")
                    continue

            # ── Passing tests in batches ─────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_file(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                out_dir = os.path.join(tmpdir, f"cov_pass_{batch_idx}")
                os.makedirs(out_dir, exist_ok=True)
                try:
                    cmd = cov_cmd + [
                        "--reporter=json", "--reports-dir", out_dir,
                        "--all", "--src", self.project_root,
                    ] + test_cmd + self._test_filter_args(*batch)
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_c8_coverage(out_dir, verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    def _collect_vitest_coverage(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect coverage using Vitest's built-in coverage (c8-based)."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                out_file = os.path.join(tmpdir, f"cov_fail.json")
                try:
                    cmd = [
                        "npx", "vitest", "run",
                        "--coverage", "--coverage.reporter=json",
                        f"--coverage.reporter=json",  # Vitest uses this format
                        f"--coverage.reportsDirectory", tmpdir,
                        "--forceExit",
                    ] + self._test_filter_args(ft)
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_v8_coverage_dir(tmpdir, verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                except subprocess.TimeoutExpired:
                    continue

            # ── Passing tests in batches ─────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_file(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                out_dir = os.path.join(tmpdir, f"cov_pass_{batch_idx}")
                os.makedirs(out_dir, exist_ok=True)
                try:
                    cmd = [
                        "npx", "vitest", "run",
                        "--coverage",
                        f"--coverage.reportsDirectory", out_dir,
                        "--forceExit",
                    ] + self._test_filter_args(*batch)
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_v8_coverage_dir(out_dir, verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    # ── Coverage reading ───────────────────────────────────────────────

    def _read_c8_coverage(
        self, out_dir: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read c8/nyc JSON coverage from output directory."""
        result: dict[str, set[int]] = {}

        # c8 outputs coverage-final.json
        cov_file = os.path.join(out_dir, "coverage-final.json")
        if not os.path.exists(cov_file):
            # Try any JSON file
            for fname in os.listdir(out_dir):
                if fname.endswith(".json"):
                    cov_file = os.path.join(out_dir, fname)
                    break

        try:
            with open(cov_file) as f:
                cov = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

        for filepath, file_data in cov.items() if isinstance(cov, dict) else []:
            try:
                rel = os.path.relpath(filepath, self.project_root)
            except ValueError:
                continue

            if not self.is_source_file(rel, filepath):
                continue

            # c8 format: {filepath: {s: {start_line: count, ...}, ...}}
            stmt_map = file_data.get("s", {})
            # c8 uses 1-based line keys as strings
            executed = set()
            for line_key, count in stmt_map.items():
                if count > 0:
                    try:
                        executed.add(int(line_key))
                    except (ValueError, TypeError):
                        continue

            # Fallback: use statementMap for line numbers
            if not executed:
                stmt_map_info = file_data.get("statementMap", {})
                for key, loc in stmt_map_info.items():
                    start = loc.get("start", {})
                    if start:
                        executed.add(start.get("line", 0))

            if executed:
                result[rel] = executed

        return result

    def _read_v8_coverage_dir(
        self, dir_path: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read V8 JSON coverage format (used by Vitest/c8 under the hood)."""
        result: dict[str, set[int]] = {}

        for fname in os.listdir(dir_path):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dir_path, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            # V8 format is a list of {url, script, functions: [{ranges, ...}]}
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                url = entry.get("url", "")
                if not url or url.startswith("node:"):
                    continue

                # Convert file URL to path
                if url.startswith("file://"):
                    url = url[7:]

                try:
                    rel = os.path.relpath(url, self.project_root)
                except ValueError:
                    continue

                if not self.is_source_file(rel, url):
                    continue

                executed = set()
                for func in entry.get("functions", []):
                    for r in func.get("ranges", []):
                        if r.get("count", 0) > 0:
                            start_line = r.get("start", {}).get("line", 0)
                            end_line = r.get("end", {}).get("line", 0)
                            for ln in range(start_line, end_line + 1):
                                if ln > 0:
                                    executed.add(ln)

                if executed:
                    result[rel] = executed

        return result

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """Build (file, line) -> function_name for JS/TS via regex."""
        func_map: dict[tuple[str, int], str | None] = {}

        for dirpath, _, filenames in os.walk(self.project_root):
            if "node_modules" in dirpath.split(os.sep):
                continue
            for fname in filenames:
                if not (fname.endswith(('.js', '.jsx', '.ts', '.tsx', '.mjs'))):
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

                self._regex_js_function_map(source, rel, func_map)

        return func_map

    @staticmethod
    def _regex_js_function_map(
        source: str, rel: str,
        func_map: dict[tuple[str, int], str | None],
    ) -> None:
        """Extract function names from JS/TS via regex."""
        patterns = [
            # function name(...) {
            re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE),
            # const/let/var name = (...) =>  or  = function(...)  or  = function*...
            re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\()", re.MULTILINE),
            # method shorthand: name(...) {
            re.compile(r"(?:^|\n)\s*(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE),
            # class method: name(...) {
            re.compile(r"(?:^|\n)\s+(?:static\s+)?(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE),
        ]
        for i, line in enumerate(source.splitlines(), 1):
            for pat in patterns:
                m = pat.search(line)
                if m:
                    name = m.group(1)
                    # Skip keywords that aren't function names
                    if name in ("if", "for", "while", "switch", "catch", "class",
                                "return", "throw", "new", "typeof", "void"):
                        continue
                    func_map[(rel, i)] = name
                    break  # First match wins per line

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """Filter: skip test files, node_modules, etc."""
        if not super().is_source_file(rel_path, abs_path):
            return False
        valid_exts = ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs')
        if not any(rel_path.endswith(ext) for ext in valid_exts):
            return False
        return True

    # ── Helpers ────────────────────────────────────────────────────────

    def _get_test_command(self) -> list[str]:
        """Get the npm test command as a list of strings."""
        scripts = self._pkg_json.get("scripts", {})
        test_script = scripts.get("test", "")
        if not test_script:
            return []

        runner = self._test_runner
        if runner == "jest":
            return ["npx", "jest"]
        elif runner == "vitest":
            return ["npx", "vitest", "run"]
        elif runner == "mocha":
            # Parse mocha config from the test script
            parts = test_script.split()
            # Extract mocha args after 'mocha'
            mocha_args = []
            for i, p in enumerate(parts):
                if "mocha" in p and i + 1 < len(parts):
                    mocha_args = parts[i + 1:]
                    break
            return ["npx", "mocha"] + mocha_args
        return []

    def _test_filter_args(self, *test_ids: str) -> list[str]:
        """Get test runner args to run specific tests."""
        runner = self._test_runner
        if runner == "jest":
            # Jest: --testPathPattern or specific test path
            return list(test_ids)
        elif runner == "vitest":
            return list(test_ids)
        elif runner == "mocha":
            # Mocha: --grep or file paths
            return list(test_ids)
        return list(test_ids)

    @staticmethod
    def _group_tests_by_file(test_ids: list[str]) -> list[list[str]]:
        """Group test IDs by source file."""
        groups: dict[str, list[str]] = {}
        for tid in test_ids:
            file_part = tid.split("::")[0] if "::" in tid else tid
            if file_part not in groups:
                groups[file_part] = []
            groups[file_part].append(tid)
        return list(groups.values())


def create_backend(project_root: str) -> JavaScriptCoverageBackend:
    """Factory function for the JavaScript backend."""
    return JavaScriptCoverageBackend(project_root)
