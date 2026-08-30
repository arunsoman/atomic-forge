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

## Addendum 2 — findings mined from ALL past failures (round-2 sweep + pilot)

### From the 55-attempt round-2 sweep (benchmarks/README.md)

| Finding | Evidence | Status |
|---|---|---|
| **Silent unchanged PRs** — 3 real PRs (psf/black#5214, psf/black#4420, Delgan/loguru#1502) died with opaque `No commits between X and Y` after the repair loop found the test green at round 0 and PR'd an unchanged branch | fix.py:443 comment | ✅ fixed in-code (rounds==0 gate) |
| **oracle_reject = 27/55** — the single largest bucket: CIE couldn't confirm the bug reproduces at HEAD within budget | benchmarks/README.md | ⚠ partially addressed by F1 (operator repro proves reproducibility independently); residual = testgen expressiveness limits |
| **repair_fail = 23/55** — reproduced but no green fix within round budget | same | open; scale `--max-rounds`/`--samples` per bug-class in campaign config |
| **2 bootstrap_fail** — base-image LLM trust + Makefile false-positive (gunicorn) | fixed w/ tests | ✅ fixed (previous session) |
| **Maintainer-closed PR (loguru) never re-raised** — closure respect norm | benchmarks/README.md:85 | ✅ protocol: no re-raise after maintainer closure unless invited |
| **Forks migrated to org (kannamma-labs)** — PR identity centralization | same | ✅ standing rule |

### From this session's pilot failures

| # | Finding | Evidence | Fix-ID |
|---|---|---|---|
| **F4** | **Testgen never writes**: sphinx run made 10 tool calls (99.6k prompt / 1.8k completion tok), zero write attempts; near-budget turns repeat the same 4201-char search result (t8≈t10). Render-order bug class has no fixture-first strategy available | logs/run-sphinx-13180.out | add `--test-file` (operator-authored test skips testgen); budget-aware nudge ("must attempt a write by turn N−2"); duplicate-tool-call short-circuit; tag render-order bugs in targets |
| **F5** | **Artifact wipe**: all /tmp/forge_fix workdirs (trajectories, checkpoints, learning.json) vanished mid-session — ephemeral work_root destroys forensic evidence | empty /tmp/forge_fix this session | campaign runner copies `.forge/{learning.json,exit_audit.jsonl,trajectory.jsonl}` + PR body into `benchmarks/real_issues/logs/<run>/` at every terminal event; expose `--work-root` CLI (done in this commit) and point campaigns at a durable dir |
| **F6** | **Gate verdict overstates health**: bootstrap "passed" with probe output opening `FFFFFFFFFFFFFFFF` (16 F's before 2%) — "suite runs" ≠ "suite green" | run-1 dask out: `exit=1, output began: ...FFFFFFFF` | gate persists probe tail + F-rate into `.forge/gate.json`; runs proceeding on a mass-failing probe get flagged into the run result |
| **F7** | **Fault localization returned zero suspects** on a 42k-node graph for a dtype-loss bug; repair round entered with no patch attempted (wasted round) | learning.json paths_tried | implement postmortem-proposed `MetaOriginTracker` CIE tool (dtype/meta dataflow provenance); treat "0 suspects" as a distinct repair-loop precondition event in the trajectory |
| **F8** | **Failure economics**: a failed LLM-run costs ~64–100k prompt tokens; staleness was 2/4. F1 removes the stale half; F4's early-write cap trims the testgen-exploration tail | usage lines in run logs | add per-run cost columns (llm_calls / prompt / completion) to campaign_log — data already printed by forge |

## Re-run queue (runtime-verified open)

`gh api` snapshot 2026-08-30: sphinx#13180 ✅ · sphinx#13841 ✅ · astroid#769 ✅ · dask#11447 ✅ · black#3294 ❌ dropped

## Addendum — run-3 bypass notes (2026-08-30)
- fresh-account GraphQL throttling → issue fetched via REST + `--issue-body-file` (works); **only fetch needs GraphQL**
- broken `git clone` (zero commits, "failed to encode 'wrongenc.inc'") undetected by forge → manual re-clone + `--project-dir` workaround, run continued