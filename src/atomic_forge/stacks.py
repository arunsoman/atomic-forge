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


def weak_matches(root: Path) -> set[str]:
    """Names of registered stacks whose `detect()` is true but only via a
    weak, generic signal (currently: `_CppStack.is_weak_match` — a bare
    Makefile with no CMake/Autotools markers). Callers disambiguating an
    ecosystem tie (e.g. bootstrap.py's base-image picker) can drop these
    before deciding "ambiguous" — a stack with no `is_weak_match` is never
    weak (RepoStack doesn't require the method; only stacks whose own
    detect() has a generic-file false-positive risk need to define it)."""
    root = Path(root)
    return {s.name for s in all_stacks()
            if s.detect(root) and getattr(s, "is_weak_match", lambda _r: False)(root)}


# --------------------------------------------------------------- python ----

def _pep621_or_poetry_extras(root: Path) -> set:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return set()
    try:
        import tomllib
        data = tomllib.loads(pyproject.read_text())
    except Exception:
        return set()
    extras = set((data.get("project") or {}).get("optional-dependencies") or {})
    extras |= set(((data.get("tool") or {}).get("poetry") or {}).get("extras") or {})
    return extras


def _setup_py_extras(root: Path) -> set:
    """Extra names from a classic `setup.py`'s `extras_require={...}`
    kwarg — read via AST, NEVER executed (arbitrary code in an untrusted
    checkout; only the dict literal's string KEYS are needed to build
    `.[name1,name2]`, regardless of how each extra's value list is
    constructed — kombu builds each list from a helper call, not a literal,
    which this only needs to skip over, not evaluate). Confirmed live
    (celery/kombu#2582 RCA): kombu declares `redis` (among others) exactly
    this way, with no `pyproject.toml` optional-dependencies table at
    all — `_pep621_or_poetry_extras` alone misses it completely."""
    setup_py = root / "setup.py"
    if not setup_py.exists():
        return set()
    try:
        import ast
        tree = ast.parse(setup_py.read_text())
    except (SyntaxError, ValueError):
        return set()
    extras: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "extras_require" \
                and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    extras.add(key.value)
    return extras


def _setup_cfg_extras(root: Path) -> set:
    """Extra names from a classic `setup.cfg`'s
    `[options.extras_require]` section — plain INI, no execution risk."""
    setup_cfg = root / "setup.cfg"
    if not setup_cfg.exists():
        return set()
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read_string(setup_cfg.read_text())
    except Exception:
        return set()
    if not cp.has_section("options.extras_require"):
        return set()
    return set(cp.options("options.extras_require"))


def pyproject_extras(root: Path) -> List[str]:
    """Every extra a project declares, across every manifest format it
    might use: PEP 621 `[project.optional-dependencies]` or legacy Poetry
    `[tool.poetry.extras]` (`pyproject.toml`), a classic `setup.py`'s
    `extras_require={...}` kwarg, or `setup.cfg`'s
    `[options.extras_require]`. Not filtered to "test"-sounding names on
    purpose: a generated regression test can need ANY optional integration
    the bug itself lives in (urllib3#5107 needed `secure` — pyOpenSSL —
    because the bug is in urllib3.contrib.pyopenssl, nothing about
    "testing" names that extra).

    Module-level (not just a `_PythonStack` method) so every other caller
    that bootstraps a Python checkout — `issue.py`'s separate `fix`-command
    venv setup included — reads the SAME source of truth instead of
    re-guessing its own fixed extra name. (Name kept as `pyproject_extras`
    for every existing caller/test even though it now reads more than
    `pyproject.toml` — renaming would touch call sites for no behavioral
    benefit.)"""
    extras = _pep621_or_poetry_extras(root) | _setup_py_extras(root) | _setup_cfg_extras(root)
    return sorted(extras)


class _PythonStack:
    name = "python"

    _REQUIREMENTS = ("requirements.txt", "backend/requirements.txt")

    def _requirements_files(self, root: Path) -> List[Path]:
        return [root / name for name in self._REQUIREMENTS if (root / name).exists()]

    def _declared_extras(self, root: Path) -> List[str]:
        return pyproject_extras(root)

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
        venv_python = ".forge_venv/bin/python"
        venv_pip = ".forge_venv/bin/pip"
        if reqs:
            # Isolated per-project venv, installed from the project's own
            # requirements — a bare `python -m pytest` would run against
            # whatever happens to be on this process's own PATH, which
            # silently skips every one of the project's own declared deps.
            req_flags = " ".join(f"-r {r.relative_to(root)}" for r in reqs)
            base_install = f"{venv_pip} install -q {req_flags} pytest pytest-asyncio"
            # requirements.txt pins CI deps but real projects routinely ALSO
            # keep a pyproject.toml with its own extras for optional
            # integrations requirements.txt never mentions (the same shape
            # urllib3#5107 hit: a fixed install list with no view of what
            # the project itself declares optional). Root-level only —
            # matches _declared_extras' own scope, same as the pyproject-only
            # branch below.
            extras = self._declared_extras(root)
            if extras:
                extras_spec = f".[{','.join(extras)}]"
                install = (f"({venv_pip} install -q {req_flags} -e '{extras_spec}' "
                           f"pytest pytest-asyncio || {base_install})")
            else:
                install = base_install
        elif (root / "pyproject.toml").exists() or (root / "backend" / "pyproject.toml").exists():
            # No requirements.txt: pyproject.toml (poetry/hatch/setuptools)
            # is the only declared dependency source. A bare `python -m
            # pytest` here used to bootstrap_fail on both halves of this:
            # the project itself never gets installed (ModuleNotFoundError
            # in conftest — confirmed on python-poetry/cleo), and a plugin
            # wired into `addopts` (e.g. `--cov=...`) is missing (pytest's
            # own "unrecognized arguments" usage error — confirmed on
            # benoitc/gunicorn). Both showed up identically as an opaque
            # "test command exited 4" at the bootstrap gate. Installing the
            # project editable + a curated set of the pytest plugins most
            # commonly wired into addopts covers both.
            pyroot = "." if (root / "pyproject.toml").exists() else "backend"
            generic_plugins = ("pytest pytest-asyncio pytest-cov pytest-xdist "
                                "pytest-mock pytest-timeout")
            # Round-3 RCA (urllib3#5107, ipython#15359/#15379): the curated
            # list above is a guess at what pytest itself needs — it says
            # nothing about what the PROJECT's own code needs to import. A
            # regression test that exercises an optional integration (e.g.
            # urllib3.contrib.pyopenssl) or the project's own conventional
            # test dependency (e.g. ipython's `testpath`) collection-crashes
            # with ModuleNotFoundError and gets misread as "doesn't
            # reproduce" — the test was never able to run at all. Only the
            # project's own pyproject.toml actually knows what it needs, so
            # install every extra it declares; if that fails (a heavy/broken
            # extra needing system libs we don't have), fall back to the
            # plain install rather than let bootstrap fail outright.
            extras = self._declared_extras(root if pyroot == "." else root / "backend")
            base_install = f"{venv_pip} install -q -e {pyroot} {generic_plugins}"
            if extras:
                extras_spec = f"{pyroot}[{','.join(extras)}]"
                install = (f"({venv_pip} install -q -e '{extras_spec}' {generic_plugins} "
                           f"|| {base_install})")
            else:
                install = base_install
        else:
            return "python -m pytest -q --continue-on-collection-errors"
        setup = (
            f"test -x {venv_python} || "
            f"(python -m venv .forge_venv && {venv_pip} install -q --upgrade pip && {install})"
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


# ----------------------------------------------------------------- java ----

class _JavaStack:
    """Maven or Gradle, whichever the repo actually has. Maven takes
    priority when both are present (a repo migrating build tools usually
    still has the old pom.xml lying around after the new build/gradle.kts
    file is added) — this matches the same "trust the more-committal
    marker" spirit as Python's requirements.txt vs. pyproject.toml
    handling, without needing to actually run either tool to decide."""
    name = "java"

    def _is_maven(self, root: Path) -> bool:
        return (root / "pom.xml").exists()

    def _is_gradle(self, root: Path) -> bool:
        return (root / "build.gradle").exists() or (root / "build.gradle.kts").exists()

    def detect(self, root: Path) -> bool:
        return self._is_maven(root) or self._is_gradle(root)

    def test_command(self, root: Path) -> Optional[str]:
        if self._is_maven(root):
            return "mvn -q -B test"
        if self._is_gradle(root):
            # Prefer the repo's own wrapper (self-contained, pins a
            # known-good Gradle version) over a bare `gradle` that would
            # depend on whatever happens to be on the image's PATH.
            wrapper = root / "gradlew"
            if wrapper.exists():
                return "chmod +x ./gradlew && ./gradlew -q test --console=plain"
            return "gradle -q test --console=plain"
        return None

    def is_test_file(self, path: str) -> bool:
        return (
            path.startswith("src/test/java/") or path.startswith("src/test/kotlin/")
            or bool(re.search(r"(^|/)\w+Test\.(java|kt)$", path))
        )

    def docker_image(self, root: Path) -> Optional[str]:
        # A JDK is required either way (mvn needs one; gradlew bootstraps
        # its own Gradle but still needs `java` on PATH) — one image
        # covers both build tools.
        return "eclipse-temurin:17-jdk" if self.detect(root) else None


# ------------------------------------------------------------------ go ----

class _GoStack:
    name = "go"

    def detect(self, root: Path) -> bool:
        return (root / "go.mod").exists()

    def test_command(self, root: Path) -> Optional[str]:
        if not self.detect(root):
            return None
        return "go test ./..."

    def is_test_file(self, path: str) -> bool:
        return path.endswith("_test.go")

    def docker_image(self, root: Path) -> Optional[str]:
        return "golang:1.22" if self.detect(root) else None


# ---------------------------------------------------------------- rust ----

class _RustStack:
    name = "rust"

    def _declares_optional_features(self, root: Path) -> bool:
        """True if this crate's own Cargo.toml declares ANY non-default
        feature or `optional = true` dependency. `cargo test` alone only
        builds the DEFAULT feature set — the same "fixed generic install,
        blind to what the project itself declares optional" gap urllib3#5107
        exposed for Python extras: a regression test exercising code gated
        behind a non-default feature (a common crate shape — e.g. an
        optional backend/integration wired through `dep:foo` in
        `[features]`, or a dependency marked `optional = true` directly)
        never even compiles under a bare `cargo test`. Malformed/missing
        Cargo.toml — or no tomllib (< 3.11) — means "nothing declared", not
        a crash: same fail-open discipline as `_PythonStack._declared_extras`."""
        cargo_toml = root / "Cargo.toml"
        if not cargo_toml.exists():
            return False
        try:
            import tomllib
            data = tomllib.loads(cargo_toml.read_text())
        except Exception:
            return False
        features = data.get("features")
        if isinstance(features, dict) and any(f != "default" for f in features):
            return True
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            deps = data.get(section)
            if not isinstance(deps, dict):
                continue
            for spec in deps.values():
                if isinstance(spec, dict) and spec.get("optional") is True:
                    return True
        return False

    def detect(self, root: Path) -> bool:
        return (root / "Cargo.toml").exists()

    def test_command(self, root: Path) -> Optional[str]:
        if not self.detect(root):
            return None
        if self._declares_optional_features(root):
            # Probe compilation with every feature enabled as its own step
            # (discarded output, no test execution) so a broken/heavy
            # feature (needs an unavailable system lib) can't ever mask a
            # REAL test failure: the eventual `cargo test` call — whichever
            # branch runs — executes exactly once, so its exit code (0 pass,
            # 101 a real failure) is never swallowed by a second run the way
            # a bare `A && B || C` would (B failing would silently re-trigger
            # C and report C's exit code instead of B's).
            return ("if cargo build --all-features --tests -q >/dev/null 2>&1; "
                    "then cargo test --all-features; else cargo test; fi")
        return "cargo test"

    def is_test_file(self, path: str) -> bool:
        # Rust's dominant convention is inline `#[cfg(test)] mod tests`
        # within the SAME file as the code under test, not a separate
        # file — so this only catches the secondary convention (top-level
        # `tests/` integration-test files), same structural limitation
        # every other stack here has for its own language's inline-test
        # idiom (if any). Still strictly better than treating nothing as
        # a test file for this stack.
        return path.startswith("tests/") or path.endswith("_test.rs")

    def docker_image(self, root: Path) -> Optional[str]:
        return "rust:1-slim" if self.detect(root) else None


# ---------------------------------------------------------------- C/C++ ----

class _CppStack:
    """CMake, GNU Autotools, or a plain Makefile that declares an explicit
    `test:`/`check:` target — in that priority order.

    Image: `gcc:14` (buildpack-deps based — ships gcc/g++, make, and
    cmake/ctest, and runs every command here without installing anything).
    The command is therefore allowed to *assume* the toolchain instead of
    apt-getting for it, which matters because `docker_env` runs everything
    as the invoking host user (non-root in-container).

    Deliberately NOT detected here: `meson.build`-only repos. No mainstream
    toolchain image ships meson/ninja, and installing them inline as a
    non-root container user is unreliable — a meson-only checkout
    deterministically detects as "nothing" and falls to the agentic
    bootstrap path (see bootstrap.py), which installs tooling inside its
    own sandbox. Same reasoning for cmake repos that vendor their tests
    behind targets nothing scannable reveals: the marker scan looks for
    `enable_testing`/`include(CTest)`/`add_test` in ANY CMakeLists.txt in
    the tree, and a repo genuinely without scannable test markers exits
    clean (`test_command` -> None) rather than guessing.

    Priority: CMake > Makefile > Autotools. A CMakeLists.txt wins over a
    Makefile because generated Makefiles often linger next to a real
    CMake build (same "trust the more-committal marker" rule as
    pom.xml > build.gradle in _JavaStack)."""
    name = "cpp"

    def _is_cmake(self, root: Path) -> bool:
        return (root / "CMakeLists.txt").exists()

    def _cmake_declares_tests(self, root: Path) -> bool:
        for cmakelists in root.rglob("CMakeLists.txt"):
            try:
                text = cmakelists.read_text(errors="replace")
            except OSError:
                continue
            if re.search(r"enable_testing\s*\(|include\s*\(\s*CTest|add_test\s*\(", text):
                return True
        return False

    def _makefile(self, root: Path) -> Optional[Path]:
        for name in ("Makefile", "GNUmakefile", "makefile"):
            f = root / name
            if f.exists():
                return f
        return None

    def _make_test_target(self, text: str) -> Optional[str]:
        """The Makefile's own explicit test/check target, if any — a repo
        with no such target deterministically declares itself untestable
        rather than failing later with `No rule to make target 'test'.`"""
        for target in ("test", "check"):
            if re.search(rf"^{target}\s*:", text, re.MULTILINE):
                return target
        return None

    def _is_autotools(self, root: Path) -> bool:
        return any(
            (root / name).exists()
            for name in ("configure", "configure.ac", "configure.in", "Makefile.am")
        )

    def detect(self, root: Path) -> bool:
        return self._is_cmake(root) or self._is_autotools(root) or self._makefile(root) is not None

    def test_command(self, root: Path) -> Optional[str]:
        if self._is_cmake(root) and self._cmake_declares_tests(root):
            # `_cmake_declares_tests` finds the marker text anywhere in the
            # tree, including inside an `if(BUILD_TESTING) ... endif()`
            # guard — the standard CTest-provided option (from
            # `include(CTest)`, defaults ON but some projects flip that
            # default) that must be explicitly ON for the marker to
            # actually take effect. Same "read what the project declares"
            # gap as the extras fixes below: passing it unconditionally is
            # a no-op for projects that don't use it (unused cache
            # variable) and closes the false-negative for ones that do.
            return (
                "cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON"
                " && cmake --build build -j"
                " && ctest --test-dir build --output-on-failure"
            )
        makefile = self._makefile(root)
        if makefile is not None:
            try:
                target = self._make_test_target(makefile.read_text(errors="replace"))
            except OSError:
                target = None
            if target is not None:
                return f"make -j {target}"
        if self._is_autotools(root):
            # configure.ac-only checkouts need autoreconf first; repos with
            # a checked-in `configure` skip that. Both converge on make check,
            # the standard autotools test entrypoint.
            bootstrap = "" if (root / "configure").exists() else "autoreconf -fi && "
            return f"{bootstrap}./configure && make -j && make check"
        return None

    def is_test_file(self, path: str) -> bool:
        posix = path.replace("\\", "/")
        if posix.startswith("tests/") or posix.startswith("test/"):
            return True
        # gtest/catch2/doctest naming: test_foo.cc, foo_test.cpp,
        # foo_tests.cxx, FooTest.hpp — headers count too (a repair suspect
        # for a FooTest.cc failure is often FooTest.hpp itself).
        return bool(re.search(
            r"(^|/)(?:test_\w+|[\w-]+_test\w*|\w+_tests|\w*Test)\.(?:c|cc|cpp|cxx|h|hpp)$",
            posix,
        ))

    def docker_image(self, root: Path) -> Optional[str]:
        return "gcc:14" if self.detect(root) else None

    def is_weak_match(self, root: Path) -> bool:
        """True when this stack's ONLY signal is a bare Makefile/GNUmakefile
        with no CMake or Autotools markers. A generic Makefile is not a
        strong ecosystem signal — `make test`/`make lint` dev-convenience
        Makefiles are routine in Python/Node/Go repos that have nothing to
        do with C/C++ (confirmed on benoitc/gunicorn, whose Makefile sits
        next to a pyproject.toml). Callers doing ecosystem disambiguation
        (bootstrap.py's base-image picker) should not let this manufacture
        a false tie against another stack's real manifest match."""
        return (self.detect(root) and not self._is_cmake(root)
                and not self._is_autotools(root))


register(_PythonStack())
register(_NodeStack())
register(_JavaStack())
register(_GoStack())
register(_RustStack())
register(_CppStack())


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
