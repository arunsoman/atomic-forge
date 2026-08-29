# Contributing to atomic-forge

Thanks for your interest in improving atomic-forge! This is a small,
opinionated codebase, so a few conventions keep it coherent.

> All contributions are licensed under the project's [Business Source License 1.1](./LICENSE).

## Quick start

```bash
git clone https://github.com/kannamma-labs/atomic-forge.git
cd atomic-forge
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # pydantic, httpx, openai + pytest
python -m pytest -q              # 141 tests, should be green
```

Python **3.10+** is required (CI runs the 3.10 / 3.11 / 3.12 matrix).

> **Run the suite as `python -m pytest` from the repo root.** `pyproject.toml`
> scopes collection to `tests/`. Don't `pytest benchmarks/` directly — the
> `cases/*/seed/test_mod.py` fixtures intentionally share a basename across
> cases and collide under pytest's rootdir import. See
> [`benchmarks/README.md`](benchmarks/README.md).

## Project layout

| Path | What's there |
|---|---|
| `src/atomic_forge/` | The library (one module per concern — see the architecture table in [`README.md`](README.md)) |
| `tests/` | The real test suite (what CI runs) |
| `benchmarks/` | Reproducible benchmark harnesses + standalone seed cases |
| `docs/` | Design docs + benchmark reports |
| `action.yml` | The GitHub Action wrapper around the CLI |

## What to keep in mind

- **The `AtomicTask` contract is the spine.** Every unit of work is exactly
  one file with a required `test_triad` (positive / negative / recovery),
  enforced by pydantic at construction time. If you touch `generator.py`,
  `repair_agent.py`, or `planner.py`, preserve that contract — don't soften
  the validation to "hoped for in a prompt".
- **One canonical patch engine.** `patch.py` is the single SEARCH/REPLACE
  parser. Don't add a second patch path; extend that one.
- **Repair is a real loop, not a retry.** Selection is by *running the
  suite*, and the blast-radius gate rejects a "passing" patch that silently
  breaks a caller. Keep it that way — don't replace execution-select with
  model-judged-select.
- **Stdlib + the three declared deps only.** New modules should not pull in
  new third-party packages without a strong reason; raise it in a
  Discussion or issue first.

## Adding a benchmark case

Benchmarks are standalone so they reproduce without a full repo checkout.
The pattern (see `benchmarks/cie_forge_realbugs/cases/`):

```
cases/<id>/
  case.json        # provenance: source repo, license, fix commit, bug kind
  seed/mod.py      # the verbatim pre-fix function (buggy)
  seed/mod_fixed.py# the real fix (oracle reference)
  seed/test_mod.py # the regression test (the real PR's, or CIE-generated)
```

A harness runs forge's real loop against the seed and re-checks green
against `mod_fixed.py` — measured, not self-reported. Match that shape and
record raw JSON in `benchmarks/results/`.

## Pull request flow

1. Fork, then branch **off `master`** (never commit onto `master`):
   ```bash
   git checkout -b feat/my-change
   ```
2. Keep commits focused. This repo uses conventional-style prefixes
   (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
3. `python -m pytest -q` green locally before pushing.
4. Open a PR against `master`. CI (`tests` workflow) must pass on all three
   Python versions.
5. **`master` is protected**: force-push and deletion are blocked (and
   enforced for admins too), so land changes via PR, never a direct
   force-push. You can still push normal commits to `master` if you have
   write access, but a PR is the expected path.

## Reporting bugs & ideas

- **Bugs:** open an issue with a minimal repro (ideally a standalone
  `mod.py` + `test_mod.py` like the benchmark seeds, so it reproduces with
  just pytest).
- **Questions / design discussion:** use
  [GitHub Discussions](https://github.com/kannamma-labs/atomic-forge/discussions).

## House rules

- **Never commit `.cie/`** — it's a multi-hundred-MB code-graph DB and is
  gitignored. Same for `.venv/`, `__pycache__/`, and any secrets.
- **No secrets in prompts/trajectories.** Trajectory JSONL can contain
  LLM output; scrub anything sensitive before sharing it in an issue/PR.
- Keep diffs reviewable — small, well-named commits beat one mega-commit.

Happy forging! ⚒️