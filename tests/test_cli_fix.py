"""CLI-level tests for `atomic-forge fix`'s argument handling."""
from pathlib import Path

from atomic_forge import cli, llm
from atomic_forge.llm import LLMQuotaError

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


def test_cli_top_level_catches_llm_quota_error_without_crashing(monkeypatch, tmp_path, capsys):
    """Safety net for every phase OTHER than fix/fix-comment (which
    already get a clean result dict from _run_fix_pipeline's own
    LLMQuotaError handling — see test_fix.py): `run`/`generate`/`qa`/
    `repair`/`decompose`/`watch` have no exit_audit of their own, but an
    LLM call exhausting its retries against a quota wall there must still
    print something recognizable and exit cleanly, not crash with a raw
    Python traceback (see llm.py's LLMQuotaError docstring)."""
    monkeypatch.setenv("FORGE_MOCK", "1")
    llm.set_mock_factory(lambda: ScriptedChatLLM(["SUBMIT"]))

    spec_file = tmp_path / "spec.md"
    spec_file.write_text("some spec text")

    import atomic_forge.cli as C

    def _boom(spec_text, llm_obj):
        raise LLMQuotaError("LLM call failed after 4 retries: Error code: 429 - "
                            "session usage limit reached")
    monkeypatch.setattr(C, "decompose_spec", _boom)

    rc = cli.main(["decompose", "--spec", str(spec_file)])

    assert rc == 3
    err = capsys.readouterr().err
    assert "quota" in err.lower() or "rate-limit" in err.lower()
