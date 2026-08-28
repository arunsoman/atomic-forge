"""Test-output failure counting, shared by the repair loop."""
from __future__ import annotations

import re

from .sandbox import RunResult

# --------------------------------------------------------------------------
# Test output analysis
# --------------------------------------------------------------------------

def failure_count(pytest_output: str) -> int:
    """Count failed + errored tests from a pytest/vitest/JUnit/Karma
    summary.

    Collection errors are counted too — with --continue-on-collection-errors
    they show up as 'N error' in the summary, which keeps the count comparable
    between rounds (a collection error must not mask other failures and then
    make a good repair look like a regression when it unmasks them).
    """
    total = sum(int(x) for x in re.findall(r"(\d+) failed", pytest_output))
    total += sum(int(x) for x in re.findall(r"(\d+) errors?\b", pytest_output))
    # JUnit (Maven Surefire / Gradle) summary line, e.g. "Tests run: 5,
    # Failures: 1, Errors: 0[, Skipped: 0]" — word-then-number order, so
    # this doesn't collide with the pytest/vitest patterns above (those
    # are number-then-word).
    for failures, errors in re.findall(
        r"Tests run:\s*\d+,\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", pytest_output,
    ):
        total += int(failures) + int(errors)
    # Karma/Jasmine summary line, e.g. "Executed 5 of 5 (1 FAILED)".
    total += sum(int(x) for x in re.findall(r"Executed \d+ of \d+ \((\d+) FAILED\)", pytest_output))
    if total == 0 and ("Traceback" in pytest_output or "Interrupted" in pytest_output):
        total = max(1, len(re.findall(r"FAILED|ERROR", pytest_output)))
    return total


def failure_count_for(res: RunResult) -> int:
    """failure_count() parses a pytest/vitest SUMMARY line ("N failed") out
    of the run's captured output — a run that failed for a reason with no
    such summary line at all (a timeout, a "command not found", a JS/TS
    build/lint crash before any test ran) parses to 0 even though `res.ok`
    is False, which would let a genuinely-failed repair loop report
    "initial_failures: 0, final_failures: 0, exhausted: true". `res.ok`
    (exit code == 0 and not timed out) is the ground truth for whether the
    run failed at all; a not-ok run must never be reported as zero
    failures, so this floors the text-parsed count at 1 whenever res.ok is
    False. A no-op for the common case (a real "N failed" summary already
    parses to N >= 1).

    Parses `res.full_output`, NOT `res.output` — the latter is
    head+tail-truncated for display, and a combined multi-stack test
    command's summary line can land in the truncated-away middle once a
    second stack's output is appended after it.
    """
    if res.ok:
        return 0
    return max(1, failure_count(res.full_output))
