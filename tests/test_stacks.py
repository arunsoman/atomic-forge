from atomic_forge.stacks import detect_test_stack, is_test_file, pyproject_extras


def test_detects_python_stack(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert True\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "pytest" in stack.cmd


def test_detects_node_stack(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "npm test" in stack.cmd


def test_no_stack_detected_returns_none(tmp_path):
    assert detect_test_stack(tmp_path) is None


def test_combines_multiple_stacks(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "test_x.py").write_text("def test_x():\n    pass\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.image is None  # combined multi-stack command never picks one image


def test_is_test_file_python():
    assert is_test_file("tests/test_foo.py")
    assert is_test_file("backend/tests/test_foo.py")
    assert not is_test_file("src/foo.py")


def test_is_test_file_js():
    assert is_test_file("frontend/tests/Foo.test.tsx")
    assert not is_test_file("src/Foo.tsx")


# ---------------------------------------------------------- java (R16) ----
def test_detects_maven_stack(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "mvn" in stack.cmd
    assert stack.image == "eclipse-temurin:17-jdk"


def test_detects_gradle_stack_prefers_wrapper(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "./gradlew" in stack.cmd
    assert "chmod +x" in stack.cmd


def test_detects_gradle_kts_without_wrapper(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "gradle -q test --console=plain"


def test_maven_takes_priority_over_gradle_when_both_present(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>")
    (tmp_path / "build.gradle").write_text("")
    stack = detect_test_stack(tmp_path)
    assert "mvn" in stack.cmd


def test_is_test_file_java():
    assert is_test_file("src/test/java/com/foo/BarTest.java")
    assert is_test_file("src/test/kotlin/com/foo/BarTest.kt")
    assert not is_test_file("src/main/java/com/foo/Bar.java")


# ------------------------------------------------------------ go (R16) ----
def test_detects_go_stack(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.22\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "go test ./..."
    assert stack.image == "golang:1.22"


def test_is_test_file_go():
    assert is_test_file("pkg/foo_test.go")
    assert not is_test_file("pkg/foo.go")


# ---------------------------------------------------------- rust (R16) ----
def test_detects_rust_stack(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"foo\"\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "cargo test"
    assert stack.image == "rust:1-slim"


def test_is_test_file_rust():
    assert is_test_file("tests/integration_test.rs")
    assert is_test_file("src/foo_test.rs")
    assert not is_test_file("src/foo.rs")


def test_rust_optional_feature_gets_all_features_probe(tmp_path):
    """A crate declaring a non-default feature (gating an optional
    integration the bug can live behind, same shape as urllib3#5107's
    `secure` extra) must not be tested under default-features-only —
    `cargo test` alone would never even compile the gated code."""
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"foo\"\n\n"
        "[features]\ndefault = []\nintegration = [\"dep:foo\"]\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "cargo test --all-features" in stack.cmd
    assert "cargo build --all-features --tests" in stack.cmd
    assert stack.cmd.strip().endswith("else cargo test; fi")


def test_rust_optional_dependency_without_features_table_also_detected(tmp_path):
    # `optional = true` on a dependency implicitly creates a same-named
    # feature even with no [features] table at all.
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"foo\"\n\n"
        "[dependencies]\nfoo = { version = \"1\", optional = true }\n")
    stack = detect_test_stack(tmp_path)
    assert "--all-features" in stack.cmd


def test_rust_no_optional_features_matches_old_command_exactly(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"foo\"\n")
    stack = detect_test_stack(tmp_path)
    assert stack.cmd == "cargo test"
    assert stack.image == "rust:1-slim"


def test_no_stack_still_none_with_only_readme(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n")
    assert detect_test_stack(tmp_path) is None


# ---------------------------------------------- python: pyproject-only ----
def test_pyproject_only_python_project_gets_isolated_install(tmp_path):
    """No requirements.txt, just pyproject.toml (poetry/hatch/setuptools) —
    a bare `python -m pytest` used to run against the ambient host env with
    the project itself never installed (ModuleNotFoundError in conftest —
    confirmed on python-poetry/cleo) and addopts plugins missing (pytest's
    own "unrecognized arguments" — confirmed on benoitc/gunicorn). Both
    surfaced identically as an opaque bootstrap_fail. Now: isolated venv,
    editable install, curated pytest plugins."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert ".forge_venv" in stack.cmd
    assert "pip install -q -e ." in stack.cmd
    assert "pytest-cov" in stack.cmd
    assert stack.cmd.rstrip().endswith(
        ".forge_venv/bin/python -m pytest -q --continue-on-collection-errors")


def test_pyproject_with_requirements_still_uses_requirements(tmp_path):
    # requirements.txt takes priority when both exist and pyproject.toml
    # declares no extras — unchanged behavior, no `-e .` install added.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")
    stack = detect_test_stack(tmp_path)
    assert "-r requirements.txt" in stack.cmd
    assert "pip install -q -e ." not in stack.cmd


def test_pyproject_extras_installed_alongside_requirements_with_fallback(tmp_path):
    """Real shape: requirements.txt pins CI deps, pyproject.toml ALSO
    declares extras for optional integrations requirements.txt never
    mentions. Those extras used to be silently never installed — the
    requirements.txt branch never even looked at pyproject.toml. Same RCA
    class as urllib3#5107 (a fixed install list blind to what the project
    itself declares optional), just reachable through the OTHER branch."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n\n"
        "[project.optional-dependencies]\n"
        "secure = [\"pyopenssl\"]\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")
    stack = detect_test_stack(tmp_path)
    assert "-r requirements.txt" in stack.cmd
    assert ".[secure]" in stack.cmd
    assert " || " in stack.cmd  # fallback to the plain requirements install
    assert "pip install -q -r requirements.txt pytest pytest-asyncio)" in stack.cmd


def test_bare_test_files_no_manifest_still_bare_pytest(tmp_path):
    # no pyproject.toml, no requirements.txt — nothing to install, keep the
    # old bare-pytest behavior (nothing regresses for this shape).
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert True\n")
    stack = detect_test_stack(tmp_path)
    assert stack.cmd == "python -m pytest -q --continue-on-collection-errors"


def test_pyproject_extras_get_installed_with_fallback(tmp_path):
    """Round-3 RCA (urllib3#5107): a regression test for a bug living in an
    optional integration (urllib3.contrib.pyopenssl) collection-crashed
    with ModuleNotFoundError('OpenSSL') because bootstrap only ever
    installed a fixed generic pytest-plugin list, never the project's own
    declared extras. Now: install every extra pyproject.toml declares,
    with a fallback to the plain install if that whole line fails."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n\n"
        "[project.optional-dependencies]\n"
        "secure = [\"pyopenssl\"]\n"
        "socks = [\"pysocks\"]\n")
    stack = detect_test_stack(tmp_path)
    assert ".[secure,socks]" in stack.cmd
    assert " || " in stack.cmd  # fallback to the plain install on failure
    assert "pip install -q -e . pytest" in stack.cmd  # the fallback half


def test_pyproject_no_extras_declared_matches_old_command_exactly(tmp_path):
    # No optional-dependencies/poetry extras table at all — must produce
    # byte-identical output to the pre-extras-detection command (the one
    # `||` from the pre-existing venv-setup wrapper only; no extras
    # fallback wrapper added, no behavior change for the common case).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
    stack = detect_test_stack(tmp_path)
    assert stack.cmd.count("||") == 1
    assert "[" not in stack.cmd.split("pip install -q -e .", 1)[1].split()[0]
    assert "pip install -q -e . pytest pytest-asyncio pytest-cov pytest-xdist pytest-mock pytest-timeout" in stack.cmd


def test_setup_py_extras_require_dict_detected(tmp_path):
    """Round-3 RCA (celery/kombu#2582): kombu declares its `redis` extra
    (needed by any test touching the redis transport) in a classic
    setup.py's `extras_require={...}` dict — no pyproject.toml optional-
    dependencies table at all. Read via AST, never executed. Matches
    kombu's actual shape: string keys, values built from a helper call
    (not a literal list) — only the keys matter."""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "def extras(name): return [name]\n"
        "setup(\n"
        "    name='kombu',\n"
        "    extras_require={\n"
        "        'redis': extras('redis.txt'),\n"
        "        'msgpack': extras('msgpack.txt'),\n"
        "    },\n"
        ")\n")
    assert pyproject_extras(tmp_path) == ["msgpack", "redis"]


def test_setup_cfg_extras_require_section_detected(tmp_path):
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n"
        "[options.extras_require]\n"
        "test = pytest\nyaml = pyyaml\n")
    assert pyproject_extras(tmp_path) == ["test", "yaml"]


def test_setup_py_with_no_extras_require_returns_empty(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='x')\n")
    assert pyproject_extras(tmp_path) == []


def test_malformed_setup_py_does_not_crash(tmp_path):
    (tmp_path / "setup.py").write_text("this is not valid python {{{")
    assert pyproject_extras(tmp_path) == []


def test_all_three_extras_sources_combine(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n\n[project.optional-dependencies]\nsecure = [\"a\"]\n")
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(extras_require={'redis': ['b']})\n")
    assert pyproject_extras(tmp_path) == ["redis", "secure"]


def test_requirements_txt_plus_setup_py_extras_installed(tmp_path):
    # kombu's real shape: requirements.txt (takes priority in
    # detect_test_stack) AND setup.py's extras_require — the requirements
    # branch must still pick up setup.py-declared extras, not just
    # pyproject.toml ones.
    (tmp_path / "requirements.txt").write_text("amqp\n")
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(extras_require={'redis': ['redis']})\n")
    stack = detect_test_stack(tmp_path)
    assert ".[redis]" in stack.cmd
