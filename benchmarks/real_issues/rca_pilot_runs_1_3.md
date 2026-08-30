# RCA — Pilot campaign runs 1–3 (2026-08-29/30)

**Scope:** first three live `atomic-forge fix --raise-pr` runs of the campaign50 pilot.
**Outcome:** 0 PRs raised; 3 different failure classes. Root causes are ~80% campaign
process, ~20% forge (fixable in-repo).

## Timeline

| # | Target | Outcome | Phase of failure |
|---|---|---|---|
| 1 | dask/dask#11884 | `repair_exhausted` — no PR | test execution during repair |
| 2 | sympy/sympy#29382 | bootstrap abort (exit 4) + issue closed upstream | bootstrap gate / curation |
| 3 | sphinx-doc/sphinx#13180 | process killed at launch; never ran | ops (outside forge) |

## Run-by-run

### Run 1 — dask #11884
**Evidence:** `.forge/learning.json` exit_reason `repair_exhausted`; paths: "test suite failed (pytest
cov-config error)", "localization: no suspects", "repair round: no patch attempted". Post-run env probe:
`.forge_venv` had **no pandas** (`dask[dataframe]` extras never installed), yet gate passed via widget tests.

| Layer | Cause |
|---|---|
| Immediate | No patch ever attempted: patch-selection requires a runnable suite; suite unrunnable |
| Contributing | `pytest-cov` missing while repo pytest config requests cov → cov-config error |
| Contributing | Gate installs only `[project.dependencies]`, ignores issue-relevant **extras** (`.[dataframe]`) |
| **Root cause** | **Gate scope mismatch: "some test in the repo runs" ≠ "tests in the affected module run"** — probe passed on `dask/widgets`, dataframe module unbootstrappable |
| Aggravator | Issue was already fixed upstream (computed dtypes now `string[pyarrow]` on HEAD) → even a successful repair would PR into a stale issue |

### Run 2 — sympy #29382
**Evidence:** gate abort `failed_deterministic — test command exited 4` (pytest usage error; sympy suite
expects `bin/test`). Post-mortem repro: issue **closed 2026-03-31**; `show_linprog` moved to
`sympy/solvers/simplex.py`; original repro passes (fixed).

| Layer | Cause |
|---|---|
| Immediate | Deterministic probe crashed; agentic fallback (R16c) disabled by default |
| Contributing | Probe assumes pytest-first suites; sympy uses its own runner |
| **Root cause 1** | **Stale target: curation query for sympy omitted `is:open`** (operator error in Round-2 search) and no runtime re-verification existed |
| Root cause 2 | forge has **no pre-flight repro check** — it will burn LLM cycles on an issue the maintainer already fixed; nothing cheap asserts "bug still reproduces on HEAD" |

### Run 3 — sphinx #13180 (ops)
**Evidence:** no process, empty `.out`, **no workdir created**.

| Layer | Cause |
|---|---|
| **Root cause** | Background run launched with `nohup ... &` *inside* the polling tool call; when the caller aborted at timeout, the whole process group was SIGKILLed — `nohup` does not detach the process group |

### Run 3 — continued (same session, post-RCA)
- **R3a GraphQL quota zeroed:** `gh issue view` (forge's fetch path) died on `GraphQL: 0/0` — fresh-account throttling. Bypassed via REST + `--issue-body-file` (title+body file); confirms only fetch needs GraphQL, fork/push/`pr create` are REST
- **R3b Broken clone undetected:** `git clone` delivered zero commits (`core.autocrlf`/encoding conversion failure on `tests/roots/test-warnings/wrongenc.inc`); forge proceeded to `git checkout -b` and crashed `branch yet to be born` — **no post-clone `git rev-parse HEAD` integrity check**
- Bootstrap gate on sphinx: passed (probe completed) — pytest-first repos are fine
- Re-clone + `--project-dir` trusted path → CIE index running

## Cross-cutting root causes

1. **Stale-issue poisoning (50% of targets dead on arrival).** Labels snapshot ≠ current state.
   Curation searched without `is:open`; targets json lists `black#3294` (closed 2024-09) as pilot.
2. **Token-burn before ground truth.** The repair loop is execution-selected for *patches*, but
   *issue validity* is asserted, not measured — contradicting forge's own doctrine.
3. **Bootstrap gate scope blindness.** venv built from declared deps only.
4. **Ops fragility.** `nohup`-in-toolcall launches die with the caller; `--log-file` buffered/empty.

## What worked (keep)

Device-flow auth; issue fetch; clone + `.forge_venv`; R16 gate for pytest-first repos; CIE index
(42,004 nodes / 112,210 edges, ~7 min on dask); clean crash reports; `learning.json` postmortem
correctly identified dtype-loss root cause and fix direction on run 1; SQLite checkpoints present.

## Corrective actions

### P0 — campaign protocol (today)
- **C1 Runtime issue-state gate:** re-verify `state=open` via API immediately before every run
- **C2 Pre-flight repro contract:** hand-run issue repro on HEAD *before* invoking forge; skip task if it passes or needs infra the env lacks. Re-query sympy/black replacement targets with `is:open`
- **C3 Launch hygiene:** `setsid nohup … &` + heartbeat file; short polling, never poll-inside-launch-call

### P1 — forge (small code changes)
- **F1 Pre-flight repro gate (new):** `fix --repro <script>` — run once on HEAD; if it *passes*, abort `issue_already_fixed` before any LLM spend. Also reuse as ground-truth contract post-patch (repro must *fail*→*pass*)
- **F1b Clone integrity:** after `clone_repo`, assert `git rev-parse HEAD` succeeds; retry once; hard-fail otherwise (run-3 evidence: zero-commit checkout passed unverified)
- **F1c GraphQL decoupling:** `fetch_issue` should fall back to REST (unauth-capable for public repos) when GraphQL quota is zero — `--issue-body-file` already proves the seam; wire REST as default
- **F2 Gate: install issue-relevant extras** (map `dask_expr`/`str accessor` → `.[dataframe]`) + auto-add pytest-cov when repo config requests it; probe the module nearest the issue text, not first-match
- **F3 Runner adapters:** pytest exit-4 → retry with `-p no:cacheprovider -p no:cov` minimal flags; document `FORGE_ENABLE_AGENTIC_BOOTSTRAP=1` per-repo in targets json

### P2 — later
- MetaOriginTracker (proposed by run-1 postmortem) — good candidate, but P0/P1 land the reliability win first

## Re-run queue (runtime-verified open)

`gh api` snapshot 2026-08-30: sphinx#13180 ✅ · sphinx#13841 ✅ · astroid#769 ✅ · dask#11447 ✅ · black#3294 ❌ dropped

## Addendum — run-3 bypass notes (2026-08-30)
- fresh-account GraphQL throttling → issue fetched via REST + `--issue-body-file` (works); **only fetch needs GraphQL**
- broken `git clone` (zero commits, "failed to encode 'wrongenc.inc'") undetected by forge → manual re-clone + `--project-dir` workaround, run continued