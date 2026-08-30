"""
java_backend.py — Coverage backend for Java projects.

Uses:
  - Maven (mvn) or Gradle for test execution
  - JaCoCo for coverage collection
  - jacoco.exec or XML reports for per-test coverage

JaCoCo is the de-facto standard for Java coverage. It integrates with
both Maven and Gradle via plugins. Coverage data comes in two forms:
  - jacoco.exec: binary coverage data (needs jacoco CLI to read)
  - XML reports: richer, human-readable, preferred when available

Per-test coverage in Java is tricky because JaCoCo typically produces
aggregate reports. We approximate by running tests individually or
in small batches.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Any

from base import CoverageBackend, TestInfo


class JavaCoverageBackend(CoverageBackend):
    """
    Coverage backend for Java projects using JaCoCo.

    Supports both Maven and Gradle build systems.
    """

    language = "java"

    def __init__(self, project_root: str):
        super().__init__(project_root)
        self._build_tool = self._detect_build_tool()
        self._has_jacoco = self._check_jacoco()

    @classmethod
    def detect(cls, project_root: str) -> bool:
        """Detect Java project by pom.xml or build.gradle."""
        entries = set(os.listdir(project_root))
        return bool(entries & {"pom.xml", "build.gradle", "build.gradle.kts"})

    def _detect_build_tool(self) -> str | None:
        """Detect Maven or Gradle."""
        entries = set(os.listdir(self.project_root))
        if "pom.xml" in entries:
            return "maven"
        if "build.gradle" in entries or "build.gradle.kts" in entries:
            return "gradle"
        return None

    def _check_jacoco(self) -> bool:
        """Check if JaCoCo is configured in the build."""
        build_tool = self._build_tool

        if build_tool == "maven":
            pom_path = os.path.join(self.project_root, "pom.xml")
            if os.path.exists(pom_path):
                try:
                    with open(pom_path) as f:
                        content = f.read().lower()
                    return "jacoco" in content
                except OSError:
                    pass

        elif build_tool == "gradle":
            for gradle_file in ("build.gradle", "build.gradle.kts"):
                fpath = os.path.join(self.project_root, gradle_file)
                if os.path.exists(fpath):
                    try:
                        with open(fpath) as f:
                            content = f.read().lower()
                        return "jacoco" in content
                    except OSError:
                        pass

        return False

    # ── Test discovery ─────────────────────────────────────────────────

    def discover_tests(self) -> list[TestInfo]:
        """Discover tests via build tool."""
        if self._build_tool == "maven":
            return self._discover_maven()
        elif self._build_tool == "gradle":
            return self._discover_gradle()
        return []

    def _discover_maven(self) -> list[TestInfo]:
        """Discover tests via Maven Surefire plugin."""
        try:
            r = subprocess.run(
                ["mvn", "test", "-DskipTests", "surefire:list",
                 "-Dmaven.test.failure.ignore=true",
                 "-q"],
                capture_output=True, text=True, timeout=120,
                cwd=self.project_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        tests: list[TestInfo] = []
        output = r.stdout + r.stderr

        # Parse: com.example.MyTest
        for line in output.splitlines():
            line = line.strip()
            # Maven surefire:list outputs test class names
            if line and not line.startswith("[") and "." in line:
                # Looks like a Java class name
                if re.match(r'^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)*\.[A-Z]', line):
                    # Convert to file path: com.example.MyTest -> src/test/java/com/example/MyTest.java
                    parts = line.split(".")
                    test_file = os.path.join(
                        "src", "test", "java",
                        *parts[:-1],
                        parts[-1] + ".java",
                    )
                    tests.append(TestInfo(test_id=line, source_file=test_file))

        return tests

    def _discover_gradle(self) -> list[TestInfo]:
        """Discover tests via Gradle."""
        gradle_cmd = self._gradle_cmd()
        if not gradle_cmd:
            return []

        try:
            r = subprocess.run(
                gradle_cmd + ["test", "--dry-run"],
                capture_output=True, text=True, timeout=120,
                cwd=self.project_root,
            )
        except subprocess.TimeoutExpired:
            return []

        tests: list[TestInfo] = []
        output = r.stdout + r.stderr

        # Gradle dry-run outputs test task names
        for line in output.splitlines():
            m = re.search(r'([A-Z][\w$]+Test(?:\.[\w$]+)?)', line)
            if m:
                test_class = m.group(1)
                parts = test_class.split(".")
                test_file = os.path.join(
                    "src", "test", "java",
                    *parts[:-1],
                    parts[-1] + ".java",
                )
                tests.append(TestInfo(test_id=test_class, source_file=test_file))

        return tests

    # ── Failure detection ──────────────────────────────────────────────

    def find_failing_tests(
        self, test_ids: list[str] | None = None, timeout: int = 600,
    ) -> list[str]:
        """Run tests and return failing test class/method names."""
        if self._build_tool == "maven":
            return self._find_failing_maven(timeout)
        elif self._build_tool == "gradle":
            return self._find_failing_gradle(timeout)
        return []

    def _find_failing_maven(self, timeout: int) -> list[str]:
        """Run Maven tests and parse failures from surefire reports."""
        try:
            r = subprocess.run(
                ["mvn", "test", "-Dmaven.test.failure.ignore=true", "-q"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        # Parse surefire-reports TEST-*.xml
        failed: list[str] = []
        reports_dir = os.path.join(self.project_root, "target", "surefire-reports")
        if os.path.isdir(reports_dir):
            for fname in os.listdir(reports_dir):
                if not fname.startswith("TEST-") or not fname.endswith(".xml"):
                    continue
                fpath = os.path.join(reports_dir, fname)
                try:
                    tree = ET.parse(fpath)
                    root = tree.getroot()
                    for test_case in root.iter("testcase"):
                        # Has failure or error child?
                        if (list(test_case.iter("failure")) or
                                list(test_case.iter("error"))):
                            classname = test_case.get("classname", "")
                            name = test_case.get("name", "")
                            if classname and name:
                                failed.append(f"{classname}#{name}")
                            elif classname:
                                failed.append(classname)
                except ET.ParseError:
                    continue

        return sorted(set(failed))

    def _find_failing_gradle(self, timeout: int) -> list[str]:
        """Run Gradle tests and parse failures."""
        gradle_cmd = self._gradle_cmd()
        if not gradle_cmd:
            return []

        try:
            r = subprocess.run(
                gradle_cmd + ["test", "--continue"],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.project_root,
            )
        except subprocess.TimeoutExpired:
            return []

        # Parse Gradle test reports
        failed: list[str] = []
        reports_dir = os.path.join(self.project_root, "build", "test-results", "test")
        if os.path.isdir(reports_dir):
            for fname in os.listdir(reports_dir):
                if not fname.endswith(".xml"):
                    continue
                fpath = os.path.join(reports_dir, fname)
                try:
                    tree = ET.parse(fpath)
                    root = tree.getroot()
                    for test_case in root.iter("testcase"):
                        if (list(test_case.iter("failure")) or
                                list(test_case.iter("error"))):
                            classname = test_case.get("classname", "")
                            name = test_case.get("name", "")
                            if classname and name:
                                failed.append(f"{classname}#{name}")
                            elif classname:
                                failed.append(classname)
                except ET.ParseError:
                    continue

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
        """Collect per-test coverage using JaCoCo."""
        if not self._has_jacoco:
            if verbose:
                print("[java] JaCoCo not configured. Add JaCoCo plugin to your build.")
            return {}

        if self._build_tool == "maven":
            return self._collect_maven(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        elif self._build_tool == "gradle":
            return self._collect_gradle(
                failing_test_ids, passing_test_ids,
                timeout_per_test, batch_by_file, verbose,
            )
        return {}

    def _collect_maven(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect Maven + JaCoCo coverage."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                # Parse test_id: com.example.MyTest#testMethod
                if "#" in ft:
                    test_class, test_method = ft.split("#", 1)
                else:
                    test_class, test_method = ft, None

                try:
                    # Run single test class/method with JaCoCo
                    cmd = [
                        "mvn", "test",
                        f"-Dtest={test_class}"
                        + (f"#{test_method}" if test_method else ""),
                        "-Djacoco.destFile=" + os.path.join(tmpdir, f"jacoco_fail_{len(per_test_coverage)}.exec"),
                        "-Dmaven.test.failure.ignore=true",
                        "-q",
                    ]
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )

                    # Read JaCoCo report (XML preferred)
                    lines_map = self._read_jacoco_report_maven(verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                        if verbose:
                            print(f"[java] FAIL {ft}: {len(lines_map)} files")
                except subprocess.TimeoutExpired:
                    if verbose:
                        print(f"[java] TIMEOUT: {ft}")
                    continue

            # ── Passing tests in batches ─────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_class(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                # Extract test classes
                test_classes = set()
                for t in batch:
                    cls = t.split("#")[0] if "#" in t else t
                    test_classes.add(cls)

                try:
                    test_spec = ",".join(sorted(test_classes))
                    cmd = [
                        "mvn", "test",
                        f"-Dtest={test_spec}",
                        f"-Djacoco.destFile={os.path.join(tmpdir, f'jacoco_pass_{batch_idx}.exec')}",
                        "-Dmaven.test.failure.ignore=true",
                        "-q",
                    ]
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_jacoco_report_maven(verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    def _collect_gradle(
        self,
        failing_test_ids: list[str],
        passing_test_ids: list[str],
        timeout_per_test: int,
        batch_by_file: bool,
        verbose: bool,
    ) -> dict[str, dict[str, set[int]]]:
        """Collect Gradle + JaCoCo coverage."""
        per_test_coverage: dict[str, dict[str, set[int]]] = {}
        gradle_cmd = self._gradle_cmd()
        if not gradle_cmd:
            return per_test_coverage

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── Failing tests ────────────────────────────────────
            for ft in failing_test_ids:
                if "#" in ft:
                    test_class, test_method = ft.split("#", 1)
                else:
                    test_class, test_method = ft, None

                try:
                    cmd = gradle_cmd + [
                        "test", "--tests", test_class
                        + (f".{test_method}" if test_method else ""),
                        "--continue", "-q",
                    ]
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_jacoco_report_gradle(verbose)
                    if lines_map:
                        per_test_coverage[ft] = lines_map
                except subprocess.TimeoutExpired:
                    continue

            # ── Passing tests in batches ─────────────────────────
            if batch_by_file:
                batches = self._group_tests_by_class(passing_test_ids)
            else:
                batches = [[t] for t in passing_test_ids]

            for batch_idx, batch in enumerate(batches):
                batch_label = f"batch_{batch_idx}"
                test_classes = [t.split("#")[0] for t in batch]

                try:
                    test_specs = []
                    for cls in test_classes:
                        test_specs.extend(["--tests", cls])

                    cmd = gradle_cmd + ["test", "--continue", "-q"] + test_specs
                    subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_per_test,
                        cwd=self.project_root,
                    )
                    lines_map = self._read_jacoco_report_gradle(verbose)
                    if lines_map:
                        per_test_coverage[batch_label] = lines_map
                except subprocess.TimeoutExpired:
                    continue

        return per_test_coverage

    # ── Function name mapping ──────────────────────────────────────────

    def build_function_name_map(self) -> dict[tuple[str, int], str | None]:
        """Build (file, line) -> function_name for Java via regex."""
        func_map: dict[tuple[str, int], str | None] = {}

        # Walk src/main/java
        src_main = os.path.join(self.project_root, "src", "main", "java")
        if not os.path.isdir(src_main):
            src_main = self.project_root  # Fallback: search from root

        for dirpath, _, filenames in os.walk(src_main):
            if any(d in dirpath.split(os.sep) for d in ("target", "build", ".git")):
                continue
            for fname in filenames:
                if not fname.endswith(".java"):
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

                self._regex_java_func_map(source, rel, func_map)

        return func_map

    # ── Source file filtering ──────────────────────────────────────────

    def is_source_file(self, rel_path: str, abs_path: str) -> bool:
        """Filter: skip test files, build output, generated code."""
        if not super().is_source_file(rel_path, abs_path):
            return False
        if not rel_path.endswith(".java"):
            return False
        parts = rel_path.replace(os.sep, "/").split("/")
        if "target" in parts or "build" in parts or "generated" in parts:
            return False
        return True

    # ── Helpers ────────────────────────────────────────────────────────

    def _read_jacoco_report_maven(
        self, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read JaCoCo XML report from Maven's target/site/jacoco/."""
        report_dir = os.path.join(
            self.project_root, "target", "site", "jacoco",
        )
        return self._read_jacoco_xml_dir(report_dir, verbose)

    def _read_jacoco_report_gradle(
        self, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read JaCoCo XML report from Gradle's build/reports/jacoco/."""
        report_dir = os.path.join(
            self.project_root, "build", "reports", "jacoco",
        )
        if not os.path.isdir(report_dir):
            # Try test variant
            report_dir = os.path.join(
                self.project_root, "build", "reports", "jacoco", "test",
            )
        if not os.path.isdir(report_dir):
            # Try code coverage plugin
            report_dir = os.path.join(
                self.project_root, "build", "reports", "test",
            )
        return self._read_jacoco_xml_dir(report_dir, verbose)

    def _read_jacoco_xml_dir(
        self, report_dir: str, verbose: bool = False,
    ) -> dict[str, set[int]]:
        """Read JaCoCo XML reports from a directory."""
        result: dict[str, set[int]] = {}

        if not os.path.isdir(report_dir):
            return result

        for fname in os.listdir(report_dir):
            if not fname.endswith(".xml"):
                continue
            fpath = os.path.join(report_dir, fname)
            try:
                tree = ET.parse(fpath)
                root = tree.getroot()
            except ET.ParseError:
                continue

            for package in root.iter("package"):
                pkg_name = package.get("name", "").replace("/", ".")
                for source_file in package.iter("sourcefile"):
                    filename = source_file.get("name", "")
                    # Build relative path
                    rel = os.path.join(
                        "src", "main", "java",
                        pkg_name.replace(".", os.sep),
                        filename,
                    )

                    if not self.is_source_file(rel, ""):
                        continue

                    executed = set()
                    for line_elem in source_file.iter("line"):
                        try:
                            ln = int(line_elem.get("nr", 0))
                            ci = int(line_elem.get("ci", 0))  # covered instructions
                            if ci > 0:
                                executed.add(ln)
                        except ValueError:
                            continue

                    if executed:
                        result[rel] = executed

        return result

    @staticmethod
    def _regex_java_func_map(
        source: str, rel: str,
        func_map: dict[tuple[str, int], str | None],
    ) -> None:
        """Extract Java function names via regex."""
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            # public/private/protected/static/synchronized/native/abstract/strictfp
            m = re.match(
                r"^(?:(?:public|private|protected|static|synchronized|native|abstract|strictfp|final)\s+)*"
                r"(?:<[^>]+>\s+)?"
                r"(\w+)\s+",
                stripped,
            )
            if m:
                name = m.group(1)
                # Must be a valid method name (not class/interface/enum)
                if name in ("class", "interface", "enum", "record", "import", "package"):
                    continue
                # Check next non-empty line has "("
                rest = stripped[m.end():].strip()
                if "(" in rest:
                    func_map[(rel, i)] = name

    def _gradle_cmd(self) -> list[str] | None:
        """Get the Gradle command to use."""
        # Check for Gradle wrapper
        wrapper = os.path.join(self.project_root, "gradlew")
        if os.path.exists(wrapper):
            return ["./gradlew"]
        # Try gradle on PATH
        try:
            r = subprocess.run(
                ["gradle", "--version"],
                capture_output=True, text=True, timeout=10,
                cwd=self.project_root,
            )
            if r.returncode == 0:
                return ["gradle"]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    @staticmethod
    def _group_tests_by_class(test_ids: list[str]) -> list[list[str]]:
        """Group Java test IDs by class name."""
        groups: dict[str, list[str]] = {}
        for tid in test_ids:
            cls = tid.split("#")[0] if "#" in tid else tid
            groups.setdefault(cls, []).append(tid)
        return list(groups.values())


def create_backend(project_root: str) -> JavaCoverageBackend:
    """Factory function for the Java backend."""
    return JavaCoverageBackend(project_root)
