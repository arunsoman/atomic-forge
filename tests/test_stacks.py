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
