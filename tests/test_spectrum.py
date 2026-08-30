import math

import pytest

from atomic_forge import spectrum
from atomic_forge.sandbox import RunResult
from atomic_forge.spectrum import SpectrumHit


def test_spectrum_localize_skips_non_pytest_stacks(tmp_path, monkeypatch):
    """Only Python/pytest is supported for now — must not even attempt a
    subprocess for e.g. a Jest/Go test command."""
    called = []
    monkeypatch.setattr(spectrum, "_run_one_with_coverage",
                        lambda *a, **k: called.append(1) or (False, {}))
    result = spectrum.spectrum_localize(tmp_path, "npm test", None, "some.test.js")
    assert result == {}
    assert not called


def test_spectrum_localize_returns_empty_when_failing_test_actually_passes(tmp_path, monkeypatch):
    """State moved under us (another round already landed a fix) — no
    signal, not a crash."""
    monkeypatch.setattr(spectrum, "_run_one_with_coverage",
                        lambda *a, **k: (True, {"src/a.py": {1, 2}}))
    result = spectrum.spectrum_localize(tmp_path, "python -m pytest -q", None, "tests/test_x.py::test_y")
    assert result == {}


def test_spectrum_localize_returns_empty_when_coverage_unavailable(tmp_path, monkeypatch):
    """pytest-cov not installed / --cov produced nothing to parse ->
    empty dict on the failing run -> no signal, no crash."""
    monkeypatch.setattr(spectrum, "_run_one_with_coverage",
                        lambda *a, **k: (False, {}))
    result = spectrum.spectrum_localize(tmp_path, "python -m pytest -q", None, "tests/test_x.py::test_y")
    assert result == {}


def test_spectrum_localize_computes_line_level_ochiai_and_discards_flaky_samples(tmp_path, monkeypatch):
    """Core math: ef=1/nf=0 for every LINE the failing test touched, so
    susp(line) = 1/sqrt(1+ep(line)), rolled up to one SpectrumHit per file
    by max. A sampled test that turns out to NOT be passing right now
    must be discarded entirely, not counted toward ep either way."""
    monkeypatch.setattr(spectrum, "_collect_test_ids",
                        lambda *a, **k: ["tests/test_p1.py::test_a",
                                         "tests/test_p2.py::test_b",
                                         "tests/test_flaky.py::test_c"])

    def fake_run(project_dir, test_cmd, image, test_id, out_json, timeout):
        if test_id == "tests/test_fail.py::test_bug":
            return False, {"src/only_failing.py": {10}, "src/shared.py": {5}}
        if test_id == "tests/test_p1.py::test_a":
            return True, {"src/shared.py": {5}}
        if test_id == "tests/test_p2.py::test_b":
            return True, {"src/shared.py": {5}}
        if test_id == "tests/test_flaky.py::test_c":
            # not actually passing right now — must be discarded, not
            # counted as touching src/only_failing.py even though it does
            return False, {"src/only_failing.py": {10}}
        raise AssertionError(f"unexpected test_id {test_id}")

    monkeypatch.setattr(spectrum, "_run_one_with_coverage", fake_run)
    result = spectrum.spectrum_localize(tmp_path, "python -m pytest -q", None,
                                        "tests/test_fail.py::test_bug", max_passing_samples=3)

    # src/only_failing.py:10 -> ep=0 (the flaky sample's touch is discarded) -> susp=1.0
    assert result["src/only_failing.py"] == SpectrumHit(score=pytest.approx(1.0), line=10, ep=0)
    # src/shared.py:5 -> ep=2 (both real passing samples touch it) -> susp=1/sqrt(3)
    assert result["src/shared.py"] == SpectrumHit(score=pytest.approx(1.0 / math.sqrt(3)), line=5, ep=2)


def test_spectrum_localize_line_granularity_breaks_a_file_level_tie(tmp_path, monkeypatch):
    """The exact astroid#769 failure mode in miniature: a file whose
    MODULE-LEVEL line every test touches (import-time execution, e.g. a
    decorator-registered plugin), plus one FUNCTION-BODY line only the
    failing test's specific runtime path reaches. File-level ('was this
    file touched at all') would tie this file against every other
    ubiquitously-imported file; line-level must not — the module-level
    line should score low (like everyone else) while the function-body
    line scores the maximum, and the file's rolled-up score must be
    driven by the high line, not diluted by the low one."""
    monkeypatch.setattr(spectrum, "_collect_test_ids",
                        lambda *a, **k: [f"tests/test_p{i}.py::test_{i}" for i in range(6)])

    MODULE_LINE = 1     # e.g. `class Foo:` / `@registry.register` — executed at import time
    FUNC_LINE = 500      # only this failing test's runtime path reaches it
    UBIQUITOUS_FILE = "astroid/brain/brain_other.py"  # a second file, module-line only

    def fake_run(project_dir, test_cmd, image, test_id, out_json, timeout):
        if test_id == "tests/test_fail.py::test_bug":
            return False, {"astroid/rebuilder.py": {MODULE_LINE, FUNC_LINE},
                           UBIQUITOUS_FILE: {MODULE_LINE}}
        # every one of the 6 "passing" tests imports both files (module-level
        # line only) but never exercises the failing test's specific runtime path
        return True, {"astroid/rebuilder.py": {MODULE_LINE}, UBIQUITOUS_FILE: {MODULE_LINE}}

    monkeypatch.setattr(spectrum, "_run_one_with_coverage", fake_run)
    result = spectrum.spectrum_localize(tmp_path, "python -m pytest -q", None,
                                        "tests/test_fail.py::test_bug", max_passing_samples=6)

    # The file containing the uniquely-executed function-body line must
    # win outright, not tie with the file that's only ever import-touched.
    assert result["astroid/rebuilder.py"].score == pytest.approx(1.0)
    assert result["astroid/rebuilder.py"].line == FUNC_LINE
    assert result["astroid/rebuilder.py"].ep == 0
    # The purely-ubiquitous file scores the low, everyone-touches-it floor.
    assert result[UBIQUITOUS_FILE].score == pytest.approx(1.0 / math.sqrt(7))
    # And the two must NOT tie — this is the entire point of the fix.
    assert result["astroid/rebuilder.py"].score > result[UBIQUITOUS_FILE].score


def test_collect_test_ids_broadens_a_scoped_test_cmd(tmp_path, monkeypatch):
    """fix.py's own test_cmd (make_test_cmd) already bakes in ONE specific
    test file as a positional arg, scoped that way deliberately so
    ordinary repair-round test runs stay fast. Collection must still see
    the WHOLE project — regression test for astroid#769 (2026-08-30):
    without the trailing '.', this returned only the one already-scoped
    file's own test, which gets excluded as the failing test itself,
    leaving zero candidates every single time."""
    captured = {}

    def fake_run_test(cmd, image, project_dir, timeout=60):
        captured["cmd"] = cmd
        return RunResult(exit_code=0, output="")

    monkeypatch.setattr(spectrum, "run_test", fake_run_test)
    scoped_cmd = ".forge_venv/bin/python -m pytest tests/test_forge_769.py -q --tb=short -p no:cacheprovider"
    spectrum._collect_test_ids(tmp_path, scoped_cmd, None, 60)
    assert captured["cmd"].endswith("--collect-only .")


def test_run_one_with_coverage_ignores_unrelated_test_baked_into_test_cmd(tmp_path, monkeypatch):
    """A sampled 'passing' test run under a scoped test_cmd unions with
    whatever test_cmd already specifies (fix.py bakes in the failing
    regression test). If THAT co-run test fails, the aggregate exit code
    is nonzero even though the sampled test_id itself passed clean —
    checking res.ok alone silently discards every real passing sample.
    Regression test for astroid#769 (2026-08-30): this made every score
    tie at the same value, since ep was 0 for everything."""
    contaminated_output = (
        "F.......\n"
        "FAILED tests/test_forge_769.py::test_constructor_inference - AssertionError\n"
        "1 failed, 7 passed in 0.4s\n"
    )
    monkeypatch.setattr(spectrum, "run_test",
                        lambda cmd, image, project_dir, timeout=60:
                            RunResult(exit_code=1, output=contaminated_output))
    out_json = tmp_path / "out.json"
    passed, file_lines = spectrum._run_one_with_coverage(
        tmp_path, "scoped test_cmd", None, "tests/test_raw_building.py::test_something", out_json, 60)
    assert passed is True  # our test_id itself never appears in a FAILED line
    assert file_lines == {}  # no coverage json written in this fake — separate concern


def test_run_one_with_coverage_still_detects_its_own_failure(tmp_path, monkeypatch):
    """The target test_id genuinely failing (not contamination from a
    co-run test) must still be detected, ANSI color codes included —
    pytest's default colored FAILED line wraps escape codes around both
    the word FAILED and the node id itself."""
    colored_output = (
        "\x1b[31mFAILED\x1b[0m tests/test_forge_769.py::\x1b[1mtest_constructor_inference\x1b[0m - "
        "AssertionError: Uninferable\n1 failed in 0.4s\n"
    )
    monkeypatch.setattr(spectrum, "run_test",
                        lambda cmd, image, project_dir, timeout=60:
                            RunResult(exit_code=1, output=colored_output))
    out_json = tmp_path / "out.json"
    passed, _ = spectrum._run_one_with_coverage(
        tmp_path, "scoped test_cmd", None, "tests/test_forge_769.py::test_constructor_inference",
        out_json, 60)
    assert passed is False


def test_spectrum_localize_returns_empty_when_no_candidates_to_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(spectrum, "_run_one_with_coverage",
                        lambda *a, **k: (False, {"src/a.py": {1}}))
    monkeypatch.setattr(spectrum, "_collect_test_ids", lambda *a, **k: [])
    result = spectrum.spectrum_localize(tmp_path, "python -m pytest -q", None, "tests/test_x.py::test_y")
    assert result == {}


def test_rescope_test_cmd_replaces_a_baked_in_test_path():
    """Confirmed live on astroid#769 (2026-08-30): fix.py's make_test_cmd
    bakes the regression test's own path in as a positional arg. Naively
    appending a DIFFERENT test_id doesn't replace it — pytest unions
    multiple positional paths — so every sampled 'passing' test actually
    co-ran the still-failing regression test, and --cov measured both.
    That made ep saturate to the full sample count for exactly the lines
    that should have been most discriminating, silently reproducing the
    original flat-tie bug one level removed. _rescope_test_cmd must drop
    the baked-in path entirely and substitute test_id in its place."""
    test_cmd = "/venv/bin/python -m pytest tests/test_forge_769.py -q --tb=short -p no:cacheprovider"
    rescoped = spectrum._rescope_test_cmd(test_cmd, "tests/test_other.py::test_x")
    assert "tests/test_forge_769.py" not in rescoped
    assert "tests/test_other.py::test_x" in rescoped
    # flags and their values must survive untouched
    for token in ("-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"):
        assert token in rescoped


def test_rescope_test_cmd_is_a_noop_when_nothing_is_baked_in():
    """An unscoped test_cmd (no specific file baked in) has nothing to
    strip — the sampled test_id is just appended."""
    test_cmd = "python -m pytest -q --continue-on-collection-errors"
    rescoped = spectrum._rescope_test_cmd(test_cmd, "tests/test_x.py::test_y")
    assert rescoped == "python -m pytest -q --continue-on-collection-errors tests/test_x.py::test_y"


def test_run_one_with_coverage_drops_files_with_zero_executed_lines(tmp_path, monkeypatch):
    """A file coverage.py lists with an empty executed_lines set (e.g.
    measured but genuinely never touched by this particular test) must
    not appear as a 'touched' file — matching the old set[str] contract's
    implicit meaning, and avoiding a spurious ep=0/score=1.0 for a file
    that was never actually executed."""
    def fake_run_test(cmd, image, project_dir, timeout=60):
        # simulate pytest-cov writing the JSON report as a side effect of
        # the (mocked) test run, exactly like the real subprocess would —
        # _run_one_with_coverage unlinks out_json BEFORE calling this, so
        # the file must appear as part of the call, not before it.
        out_json.write_text('{"files": {"src/untouched.py": {"executed_lines": []}, '
                            '"src/touched.py": {"executed_lines": [1, 2]}}}')
        return RunResult(exit_code=0, output="1 passed")

    out_json = tmp_path / "out.json"
    monkeypatch.setattr(spectrum, "run_test", fake_run_test)
    passed, file_lines = spectrum._run_one_with_coverage(
        tmp_path, "scoped test_cmd", None, "tests/test_x.py::test_y", out_json, 60)
    assert "src/untouched.py" not in file_lines
    assert file_lines["src/touched.py"] == {1, 2}
