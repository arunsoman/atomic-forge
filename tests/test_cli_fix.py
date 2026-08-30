"""CLI-level tests for `atomic-forge fix`'s argument handling."""
from pathlib import Path

from atomic_forge import cli, llm

from _helpers import ScriptedChatLLM


def test_fix_resolves_a_relative_repro_path_to_absolute(monkeypatch, tmp_path):
    """Confirmed live (astroid #3259/#3258/#3257, 2026-08-30, three separate
    runs) that a relative `--repro` path silently broke the F1 second-
    witness re-check: `run_repro_probe` launches the probe as a subprocess
    with `cwd=project_dir` (the CLONED TARGET repo), not wherever
    `atomic-forge` was invoked from — a relative path resolved against the
    wrong base directory, the probe failed with "can't open file", and
    ANY non-zero exit reads as "bug still present" — silently blocking
    every PR that night even though the actual patches were independently
    confirmed correct. `cli.py` must resolve `--repro` to an absolute
    path before `run_fix` ever sees it, regardless of the CLI's own cwd
    at invocation time."""
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))

    relative_repro = "some/relative/probe.py"
    captured = {}

    def _fake_run_fix(url, llm_obj, **kw):
        captured.update(kw)
        return {"success": True}

    import atomic_forge.fix as F
    monkeypatch.setattr(F, "run_fix", _fake_run_fix)
    monkeypatch.chdir(tmp_path)  # the CLI's own cwd must not matter to the result

    rc = cli.main(["fix", "https://github.com/o/r/issues/1", "--repro", relative_repro])

    assert rc == 0
    assert captured["repro"] == (tmp_path / relative_repro).resolve()
    assert captured["repro"].is_absolute()
