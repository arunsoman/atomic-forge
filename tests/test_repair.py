from atomic_forge.repair import failure_count, failure_count_for
from atomic_forge.sandbox import RunResult


def test_pytest_summary():
    out = "===== 3 failed, 2 passed in 1.2s ====="
    assert failure_count(out) == 3


def test_pytest_with_errors():
    out = "===== 2 failed, 1 error in 1.2s ====="
    assert failure_count(out) == 3


def test_junit_summary():
    out = "Tests run: 10, Failures: 2, Errors: 1, Skipped: 0"
    assert failure_count(out) == 3


def test_karma_summary():
    out = "Executed 5 of 5 (2 FAILED) (0.123 secs / 0.1 secs)"
    assert failure_count(out) == 2


def test_traceback_fallback():
    out = "Traceback (most recent call last):\nFAILED\nERROR"
    assert failure_count(out) >= 1


def test_zero_on_clean_output():
    assert failure_count("5 passed in 0.4s") == 0


def test_failure_count_for_ok_result_is_zero():
    res = RunResult(exit_code=0, output="5 passed")
    assert failure_count_for(res) == 0


def test_failure_count_for_floors_at_one_when_not_ok_and_no_summary():
    # e.g. a timeout or "command not found" — no "N failed" line at all.
    res = RunResult(exit_code=124, output="[TIMEOUT after 300s]", timed_out=True)
    assert failure_count_for(res) == 1


def test_failure_count_for_uses_full_output_not_truncated():
    long_prefix = "x" * 10000
    full = long_prefix + "\n3 failed, 1 passed\n"
    res = RunResult(exit_code=1, output="[truncated]", full_output=full)
    assert failure_count_for(res) == 3
