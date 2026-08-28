from atomic_forge import cli, llm
from atomic_forge.sandbox import ensure_repo

from _helpers import ScriptedChatLLM

_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "src/app.py", line 3, in bar\n'
    "    raise ValueError('boom')\n"
    "ValueError: boom\n"
)


def test_watch_requires_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))
    rc = cli.main(["watch", "--project-dir", str(tmp_path)])
    assert rc == 2


def test_watch_phase_patches_via_cli_entrypoint(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "src").mkdir()
    (project_dir / "src" / "app.py").write_text("def bar():\n    raise ValueError('boom')\n")
    ensure_repo(project_dir)

    log_file = tmp_path / "app.log"
    log_file.write_text(_TRACEBACK)

    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM([
        "PATCH\n```python\ndef bar():\n    return 2\n```",
        "SUBMIT",
    ]))

    rc = cli.main([
        "watch", "--project-dir", str(project_dir), "--log-file", str(log_file),
        "--max-cycles", "1", "--poll-interval", "0",
    ])
    assert rc == 0
    assert "return 2" in (project_dir / "src" / "app.py").read_text()
