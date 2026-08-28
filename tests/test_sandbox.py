from atomic_forge.sandbox import commit, ensure_repo, lint_gate, run, truncate


def test_run_captures_output():
    res = run(["echo", "hello"], cwd=".")
    assert res.ok
    assert "hello" in res.output


def test_run_nonzero_exit():
    res = run(["python3", "-c", "import sys; sys.exit(3)"], cwd=".")
    assert res.exit_code == 3
    assert not res.ok


def test_run_command_not_found():
    res = run(["definitely-not-a-real-command-xyz"], cwd=".")
    assert res.exit_code == 127


def test_truncate_short_text_unchanged():
    assert truncate("short", limit=100) == "short"


def test_truncate_long_text_keeps_head_and_tail():
    text = "A" * 5000 + "B" * 5000
    out = truncate(text, limit=100)
    assert out.startswith("A" * 10)
    assert out.endswith("B" * 10)
    assert "truncated" in out


def test_lint_gate_python_syntax_error(tmp_path):
    ok, why = lint_gate(tmp_path, "a.py", "def f(:\n    pass\n")
    assert not ok
    assert "syntax error" in why


def test_lint_gate_python_valid():
    ok, why = lint_gate(".", "a.py", "def f():\n    return 1\n")
    assert ok


def test_lint_gate_skips_unknown_extension(tmp_path):
    ok, why = lint_gate(tmp_path, "a.txt", "hello")
    assert ok
    assert "skipped" in why


def test_ensure_repo_and_commit(tmp_path):
    ok = ensure_repo(tmp_path)
    if not ok:
        return  # git not available in this environment — nothing more to check
    (tmp_path / "f.txt").write_text("x")
    assert commit(tmp_path, "test commit")


def test_ensure_repo_refuses_nested_project_dir(tmp_path):
    """Regression test for a real bug found live: a project_dir with no
    .git of its own, nested inside an existing repo, must NOT be
    git-init'd or committed into — `git add -A`/`git commit` with no
    pathspec act repo-wide regardless of cwd, so doing so previously
    staged and committed the ENCLOSING repo's entire unrelated working
    tree under a misleading "forge: ..." message."""
    outer = tmp_path / "outer"
    outer.mkdir()
    if not ensure_repo(outer):
        return  # git not available in this environment — nothing more to check
    (outer / "unrelated.txt").write_text("pre-existing, untracked, must stay untouched")
    head_before = run(["git", "rev-parse", "HEAD"], cwd=outer).full_output.strip()

    nested = outer / "project_dir"
    nested.mkdir()
    (nested / "generated.py").write_text("x = 1\n")

    assert ensure_repo(nested) is False
    assert not (nested / ".git").exists()
    assert commit(nested, "forge: generate generated.py") is False

    # The outer repo's history and untracked file must be exactly as before.
    head_after = run(["git", "rev-parse", "HEAD"], cwd=outer).full_output.strip()
    assert head_after == head_before
    status = run(["git", "status", "--porcelain"], cwd=outer).full_output
    assert "unrelated.txt" in status  # still untracked, never staged/committed
    assert "generated.py" not in status  # nested/'s own file never touched the outer repo
