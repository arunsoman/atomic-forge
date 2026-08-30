from atomic_forge.sandbox import _purge_pycache, commit, ensure_repo, lint_gate, run, run_test, truncate


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


def test_commit_adds_dco_signoff_trailer(tmp_path):
    """Every commit must carry a `Signed-off-by:` trailer — required by DCO-
    gated upstreams (e.g. pandas, xarray, scikit-learn among the real-issue
    campaign's own targets); a PR missing it fails that repo's DCO check on
    arrival regardless of whether the fix itself is correct."""
    if not ensure_repo(tmp_path):
        return  # git not available in this environment — nothing more to check
    (tmp_path / "f.txt").write_text("x")
    assert commit(tmp_path, "test commit")
    log = run(["git", "log", "-1", "--pretty=%B"], cwd=tmp_path).full_output
    assert "Signed-off-by:" in log


def test_ensure_repo_applies_forge_git_identity_env_override(tmp_path, monkeypatch):
    """A cloned repo (has its own .git already) must pick up
    FORGE_GIT_USER_NAME/FORGE_GIT_USER_EMAIL if set — otherwise every commit
    forge makes against a real upstream is authored under whatever
    `git config --global user.*` happens to be on the host machine, which
    for a real-issue PR campaign means the operator's own personal identity
    lands permanently in a stranger's public repo history (confirmed live:
    python-babel/babel#1334's commit author was the operator's real name +
    personal email)."""
    if not ensure_repo(tmp_path):
        return  # git not available in this environment — nothing more to check
    monkeypatch.setenv("FORGE_GIT_USER_NAME", "atomic-forge bot")
    monkeypatch.setenv("FORGE_GIT_USER_EMAIL", "bot@example.invalid")
    assert ensure_repo(tmp_path)  # re-entering an existing .git still applies it
    assert run(["git", "config", "user.name"], cwd=tmp_path).full_output.strip() == "atomic-forge bot"
    assert run(["git", "config", "user.email"], cwd=tmp_path).full_output.strip() == "bot@example.invalid"


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


def test_purge_pycache_removes_all_nested_caches(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"stale")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "sub.cpython-311.pyc").write_bytes(b"stale")
    (tmp_path / "keep.py").write_text("x = 1\n")

    _purge_pycache(tmp_path)

    assert not (tmp_path / "__pycache__").exists()
    assert not (tmp_path / "pkg" / "__pycache__").exists()
    assert (tmp_path / "keep.py").exists()  # only __pycache__ dirs are touched


def test_purge_pycache_on_missing_dir_does_not_raise(tmp_path):
    _purge_pycache(tmp_path / "does-not-exist")  # must not raise


def test_run_test_purges_pycache_before_running(tmp_path):
    """Regression test for the write→retest→write→retest bytecode-cache
    staleness bug (confirmed live 2026-08-29, see _purge_pycache's own
    docstring): rewriting a module's content between two `run_test` calls
    against the SAME project_dir must always be reflected in the second
    run's result — a stale `__pycache__` entry from the first run must
    never be trusted for the second."""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    cmd = "python -m pytest -q --continue-on-collection-errors"

    r1 = run_test(cmd, None, tmp_path, timeout=60)
    assert not r1.ok  # buggy version: fails as expected, and populates __pycache__

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    r2 = run_test(cmd, None, tmp_path, timeout=60)
    assert r2.ok, r2.output  # fixed version must be seen fresh, not the stale cached bytecode
