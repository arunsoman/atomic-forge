"""
Repo-stack detection: what language/framework is this project, how do I
run its tests, and what path pattern is its own test-file convention.

One `RepoStack` per language/framework. `detect_test_stack` loops the
registry and combines every stack that's actually present into one test
command; `is_test_file` (used by the repair loop to keep from scoring a
test file as a repair suspect) unions every registered stack's own rule.

Add your own by calling `register()` with anything satisfying the
`RepoStack` protocol — no need to edit this module.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class RepoStack(Protocol):
    name: str

    def detect(self, root: Path) -> bool: ...

    def test_command(self, root: Path) -> Optional[str]:
        """A real test-invocation shell command, or None if this stack
        isn't testable at `root` (not present, or present with nothing to
        test yet)."""
        ...

    def is_test_file(self, path: str) -> bool:
        """True if `path` (project_dir-relative) is one of THIS stack's
        own test files."""
        ...

    def docker_image(self, root: Path) -> Optional[str]:
        """Docker image `run_test` should execute the command inside, or
        None to run as a bare host subprocess."""
        ...


_registry: Dict[str, RepoStack] = {}


def register(stack: RepoStack) -> None:
    _registry[stack.name] = stack


def all_stacks() -> List[RepoStack]:
    return list(_registry.values())


# --------------------------------------------------------------- python ----

class _PythonStack:
    name = "python"

    _REQUIREMENTS = ("requirements.txt", "backend/requirements.txt")

    def _requirements_files(self, root: Path) -> List[Path]:
        return [root / name for name in self._REQUIREMENTS if (root / name).exists()]

    def detect(self, root: Path) -> bool:
        return bool(
            self._requirements_files(root)
            or (root / "pyproject.toml").exists()
            or (root / "backend" / "pyproject.toml").exists()
            or any(root.rglob("test_*.py"))
            or any(root.rglob("*_test.py"))
        )

    def test_command(self, root: Path) -> Optional[str]:
        if not self.detect(root):
            return None
        reqs = self._requirements_files(root)
        if not reqs:
            return "python -m pytest -q --continue-on-collection-errors"
        # Isolated per-project venv, installed from the project's own
        # requirements — a bare `python -m pytest` would run against
        # whatever happens to be on this process's own PATH, which
        # silently skips every one of the project's own declared deps.
        venv_python = ".forge_venv/bin/python"
        venv_pip = ".forge_venv/bin/pip"
        req_flags = " ".join(f"-r {r.relative_to(root)}" for r in reqs)
        setup = (
            f"test -x {venv_python} || "
            f"(python -m venv .forge_venv && {venv_pip} install -q --upgrade pip && "
            f"{venv_pip} install -q {req_flags} pytest pytest-asyncio)"
        )
        return f"{setup} && {venv_python} -m pytest -q --continue-on-collection-errors"

    def is_test_file(self, path: str) -> bool:
        return (
            path.startswith("tests/") or path.startswith("backend/tests/")
            or bool(re.search(r"(^|/)(test_\w+|\w+_test)\.py$", path))
        )

    def docker_image(self, root: Path) -> Optional[str]:
        return None


# ----------------------------------------------------------------- node ----

class _NodeStack:
    name = "node"

    def detect(self, root: Path) -> bool:
        return (root / "package.json").exists()

    def test_command(self, root: Path) -> Optional[str]:
        if not self.detect(root):
            return None
        pkg_json = root / "package.json"
        try:
            pkg = json.loads(pkg_json.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        scripts_test = (pkg.get("scripts") or {}).get("test") if isinstance(pkg, dict) else None
        base_cmd = None
        if isinstance(scripts_test, str) and scripts_test.strip() and "no test specified" not in scripts_test.lower():
            base_cmd = "npm test"
        else:
            has_vitest_config = any(
                (root / name).exists()
                for name in ("vitest.config.ts", "vitest.config.js", "vitest.config.mts", "vitest.config.mjs")
            )
            has_vitest_files = any(root.rglob("*.test.ts")) or any(root.rglob("*.test.tsx")) \
                or any(root.rglob("*.test.js")) or any(root.rglob("*.test.jsx"))
            if has_vitest_config or has_vitest_files:
                base_cmd = "npx vitest run"
        if base_cmd is None:
            return None
        install = "test -d node_modules || npm install"
        return f"({install}) && {base_cmd}"

    def is_test_file(self, path: str) -> bool:
        return (
            path.startswith("tests/") or path.startswith("frontend/tests/")
            or bool(re.search(r"\.(?:test|spec)\.(?:tsx|ts|jsx|js)$", path))
        )

    def docker_image(self, root: Path) -> Optional[str]:
        return "node:20" if self.detect(root) else None


register(_PythonStack())
register(_NodeStack())


# -------------------------------------------------------------- combine ----

from dataclasses import dataclass  # noqa: E402


@dataclass
class TestStack:
    #: Not a pytest test class despite the name.
    __test__ = False
    cmd: str
    image: Optional[str] = None


def detect_test_stack(project_dir: str | Path) -> Optional[TestStack]:
    """Pick a test command (and, for the single-stack case, the Docker
    image it needs) by inspecting what's actually in `project_dir`.
    Returns None when nothing testable is detected — the caller should
    treat that as "nothing to test yet," not run pytest anyway.

    More than one stack detected: run all of them and combine into one
    shell command whose exit code is non-zero if any failed (image is
    always None for the combined case — no multi-container orchestration)."""
    project_dir = Path(project_dir)
    candidates = [
        (cmd, stack) for stack in all_stacks()
        if (cmd := stack.test_command(project_dir))
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        cmd, stack = candidates[0]
        return TestStack(cmd=cmd, image=stack.docker_image(project_dir))

    cmds = [cmd for cmd, _stack in candidates]
    parts = [f"( {cmd} ); e{i}=$?;" for i, cmd in enumerate(cmds, start=1)]
    condition = " || ".join(f"[ $e{i} -ne 0 ]" for i in range(1, len(cmds) + 1))
    parts.append(f"if {condition}; then exit 1; else exit 0; fi")
    return TestStack(cmd=" ".join(parts), image=None)


def is_test_file(path: str) -> bool:
    """True if `path` matches ANY registered stack's own test-file
    convention — used to keep the repair loop from scoring a test file as
    a suspect to *patch*."""
    return any(stack.is_test_file(path) for stack in all_stacks())
