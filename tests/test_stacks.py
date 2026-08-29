from atomic_forge.stacks import detect_test_stack, is_test_file


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


def test_no_stack_still_none_with_only_readme(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n")
    assert detect_test_stack(tmp_path) is None
