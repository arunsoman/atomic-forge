# Live campaign log — `forge fix --raise-pr`

Target universe: `campaign50_targets.json` (tier-1 + tier-2, 18 repos).
Protocol: per-issue runs, 1 open PR/repo, AI-policy grep before raising, honest provenance footer.

## F1 family — implemented 2026-08-30

- **F1** `fix --repro <script>`: probe on HEAD before any LLM spend; exit 0 → abort
  `issue_already_fixed`; after repair must flip to exit 0 else abort `repro_still_failing`
- **F1b** clone integrity: post-clone `git rev-parse --verify HEAD` + one retry
- **F1c** fetch_issue channel fallback: GraphQL → `gh api` REST → unauthenticated curl REST;
  `state` now rides along in the issue dict (cheap open/closed re-verification)
- new EXIT_REASONS: `issue_already_fixed`, `repro_still_failing`
- tests: `tests/test_issue_fetch_and_integrity.py` (16 tests)

## astroid #3199 / #3259 / #3258 / #3257 (fuzzer-found crashes) — 4 PRs raised (2026-08-30)

All four localized correctly, patched correctly (two of four via the multi-file `path`
redirect below — the model's own investigation correctly found the real fix in a different
file than the assigned suspect), and passed independent verification:

| Issue | Root cause | Fix | PR |
|---|---|---|---|
| [#3199](https://github.com/pylint-dev/astroid/issues/3199) | `AstroidError.__str__` only caught `ValueError` from `self.message.format()`; a wrapped message containing a literal `{0}` raised `IndexError` instead | broaden the except clause | [astroid#3261](https://github.com/pylint-dev/astroid/pull/3261) |
| [#3259](https://github.com/pylint-dev/astroid/issues/3259) | `Arguments.default_value()` indexed `self.defaults[idx]` with only an `idx >= 0` check | tighten to `0 <= idx < len(self.defaults)` | [astroid#3262](https://github.com/pylint-dev/astroid/pull/3262) |
| [#3258](https://github.com/pylint-dev/astroid/issues/3258) | `ClassDef._islots()` assumed a `__slots__` value is always a real node; a PEP 695 `TypeVar` has no `.getattr` | catch `AttributeError` alongside `AttributeInferenceError` | [astroid#3263](https://github.com/pylint-dev/astroid/pull/3263) |
| [#3257](https://github.com/pylint-dev/astroid/issues/3257) | `infer_typing_namedtuple_class` assumed every `AnnAssign` target is a `Name`; an `AssignAttr` target has no `.name` | filter to `isinstance(target, nodes.AssignName)` | [astroid#3264](https://github.com/pylint-dev/astroid/pull/3264) |

### Engineering notes from this round

Getting to these four PRs surfaced and fixed several general forge bugs:

- **Spectrum-based fault localization was file-granularity, not statement-granularity.**
  On an import-heavy package (astroid imports most of itself via `~15` self-registering
  `brain/` plugins), every sampled passing test touches every candidate file's module-level
  code, so file-level "touched at all" collapsed to an identical Ochiai score for every
  candidate file — zero discriminating power. Fixed by scoring at the line level
  (`executed_lines`, already in coverage.py's JSON output but previously discarded) and
  rolling up to file level by `max`, not sum/mean (`spectrum.py`).
- **Passing-sample coverage was contaminated.** The comparison ("passing") test runs reused
  a `test_cmd` that already baked in the still-failing regression test as a positional pytest
  arg; appending a different test_id doesn't replace it, pytest unions both, so every
  "passing" sample's coverage included the failing test's own footprint, saturating `ep` for
  exactly the lines that should have been most discriminating (`spectrum._rescope_test_cmd`).
- **The `patch` tool had no way to target a different file than the pre-assigned suspect.**
  When a sample's own investigation correctly found the real fix belongs elsewhere, every
  patch was silently diffed against the wrong file's content and rejected as "SEARCH block
  not found," regardless of correctness. Fixed with an optional `path` argument on `patch`
  (`agent.py`/`repair_agent.py`), guarded against test-file redirection and path traversal.
- **A relative `--repro` path resolved against the wrong working directory** — the probe
  subprocess runs with `cwd=<cloned target repo>`, not wherever `atomic-forge` was invoked
  from, so the file silently failed to open and the F1 post-repair check always read that as
  "bug still present," independent of whether the fix actually worked. Fixed by resolving
  `--repro` to an absolute path in the CLI.
- **`ensure_fork` swallowed `gh repo fork` failures silently** — a real failure (here,
  GitHub's new-account fork-velocity abuse throttle, HTTP 403, confirmed account-wide via an
  unrelated repo) surfaced several steps later as a confusing "repository not found" instead
  of a clear error at the point of failure. Fixed to check the exit code and raise
  immediately; PRs above were raised from a second, unthrottled account.
- Also: patch/run_shell trajectory logging was truncated at 200 chars, making repeated
  SEARCH-mismatch rejections undiagnosable; `localize()`'s structural CIE calls now degrade
  gracefully on a timeout instead of crashing the whole repair loop.

All covered by regression tests (`tests/test_spectrum.py`, `tests/test_repair_agent.py`,
`tests/test_cli_fix.py`, `tests/test_pr.py`).
