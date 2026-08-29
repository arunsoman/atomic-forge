"""CLI-level tests for `atomic-forge fix-comment` (R8's entry point)."""
from atomic_forge import cli, llm

from _helpers import ScriptedChatLLM


def test_fix_comment_requires_repo_and_file(monkeypatch):
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))
    rc = cli.main(["fix-comment", "--comment-body", "hi"])
    assert rc == 2


def test_fix_comment_requires_owner_slash_repo(monkeypatch):
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))
    rc = cli.main(["fix-comment", "--repo", "not-a-slug", "--file", "x.py", "--comment-body", "hi"])
    assert rc == 2


def test_fix_comment_requires_comment_body(monkeypatch):
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))
    rc = cli.main(["fix-comment", "--repo", "o/r", "--file", "x.py"])
    assert rc == 2


def test_fix_comment_reads_body_from_stdin(monkeypatch, capsys):
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("piped bug text"))

    captured = {}

    def _fake_run_fix_from_comment(owner, repo, comment_body, file_path, llm_obj, **kw):
        captured.update(owner=owner, repo=repo, comment_body=comment_body, file_path=file_path, kw=kw)
        return {"success": True}

    import atomic_forge.fix as F
    monkeypatch.setattr(F, "run_fix_from_comment", _fake_run_fix_from_comment)

    rc = cli.main(["fix-comment", "--repo", "o/r", "--file", "src/mod.py",
                   "--comment-body-file", "-", "--line", "12", "--dry-run"])
    assert rc == 0
    assert captured["owner"] == "o" and captured["repo"] == "r"
    assert captured["comment_body"] == "piped bug text"
    assert captured["file_path"] == "src/mod.py"
    assert captured["kw"]["line"] == 12
    assert captured["kw"]["dry_run"] is True
