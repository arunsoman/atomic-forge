# R16 — Language/build-agnostic environment bootstrap ("any GitHub URL")

**Requirement:** `atomic-forge fix <github-url>` must work on an arbitrary,
previously unseen repository — regardless of language, build system, or test
framework — by first getting the repo into a runnable state (dependencies
installed, at least one test discoverable and executable) before any repair
logic runs.

**Status in atomic-forge:** **Shipped 2026-08-29 (deterministic + checkpoint
phases); agentic fallback open (R16c).**
- **Deterministic detection:** six registered stacks — Python (venv per
  project, `stacks.py`), Node, Java (Maven/Gradle), Go, Rust, and now
  **C/C++** (`_CppStack`: CMake with scannable `enable_testing`/`add_test`
  markers, Makefile with an explicit `test:`/`check:` target, GNU Autotools
  with checked-in `configure` or `autoreconf -fi`; image `gcc:14`).
  Meson-only repos deliberately detect as "nothing" until R16c lands (no
  mainstream toolchain image ships meson/ninja — documented in the
  _CppStack docstring).
- **Bootstrap checkpoint (R16b):** `bootstrap.py::run_bootstrap_gate` —
  "at least one test discoverable and executable" (probe: detected stack
  command completes with exit 0/1 and output evidence); verdicts recorded
  per-run via `checkpoint.py` (`"bootstrap"` phase + `BootstrapVerdict`);
  `fix.py::_run_fix_pipeline` aborts cleanly at `stage="bootstrap"` on any
  non-`bootstrapped` verdict. `--project-dir` checkouts are user-vouched and
  skip the gate; `--skip-bootstrap` / `--bootstrap-timeout` CLI flags exist
  for the cold-clone path.
- **Agentic fallback (R16c) — implemented 2026-08-29, opt-in:**
  `bootstrap.py::agentic_bootstrap` — a Repo2Run-style external LLM
  configurator (ONE setup command per step) inside a Docker sandbox that
  is the ONLY execution surface (host-side execution is never attempted;
  without Docker the verdict is a clean `unsupported_ecosystem`). Each
  successful step is snapshotted via `docker commit` (last-good image) and
  a failed step rolls the scratch container back to that snapshot by
  re-creating from its image — never replaying commands. Hard caps
  (`max_steps=12`, `wall_clock_s=1200`, `per_step_timeout=120`,
  `verify_timeout=600`) bound runaway spend; every step lands in
  `.forge/bootstrap/transcript.jsonl`; success writes
  `.forge/bootstrap/manifest.json` keyed by HEAD commit so a repeat run of
  the same commit skips the loop (bootstrap cache hit). The sandbox base
  image comes from one cheap, MENU-CONSTRAINED LLM call (python/node/java/
  go/rust/c++ → pinned tags; anything else → `ubuntu:24.04`) — no
  free-form image names a prompt could hallucinate. Enable per-run with
  `FORGE_ENABLE_AGENTIC_BOOTSTRAP=1`; `fix` passes its llm through with
  `allow_agentic=True`, so the fallback runs ONLY when that env var is set.
  Tests (fake LLM + scripted docker_env): `tests/test_bootstrap_agentic.py`
  covers success+manifest, cache hit, rollback-on-failure, cap exhaustion,
  menu constraints, gate opt-in/out, and the no-Docker safety claim.
- **Still open:** wiring the bootstrapped sandbox image into the repair
  loop's execution path (all current repair execution is deterministic-
  stack Docker or host — the cells-based variant in
  [[Plan-R6-Alt-Cells|plan-r6-alt-cells.md]] is the designed bridge:
  bake-then-cells), plus the R16 Phase-4 benchmark suite (bootstrap
  success rate as its own metric on un-curated repos).

## State of the art

- **Multi-SWE-bench** (ByteDance, [arXiv:2504.02605](https://arxiv.org/abs/2504.02605))
  — 2,132 issues across Java, TypeScript, JavaScript, Go, Rust, C, C++.
  Evaluating Agentless, SWE-agent, and OpenHands shows resolve rates on
  non-Python languages are markedly worse than Python SWE-bench numbers —
  current agentic techniques, including SWE-agent's ACI
  ([[Agent-Computer-Interface]]), don't transfer cleanly across
  languages.
- **SWE-PolyBench** ([arXiv:2504.08703](https://arxiv.org/abs/2504.08703))
  and **SWE-bench Multilingual** ([swebench.com/multilingual](https://www.swebench.com/multilingual.html))
  — confirm the same cross-language performance gap on Java/JS/TS and a
  9-language, 42-repo set respectively.
- **SWE-rebench V2: Language-Agnostic SWE Task Collection at Scale**
  ([arXiv:2602.23866](https://arxiv.org/abs/2602.23866)) — the field is
  still building infrastructure to even *collect* language-agnostic tasks at
  scale, evidence this is treated as unsolved rather than a settled problem.
- **EnvBench: A Benchmark for Automated Environment Setup**
  ([arXiv:2503.14443](https://arxiv.org/abs/2503.14443)) — 329 Python + 665
  JVM repos; the best automated setup method succeeded on only **29.5% of
  JVM repos and 6.7% of Python repos**. This is the load-bearing finding for
  this whole requirement: most of the failure happens *before* any code
  fixing begins.
- **SetupBench** ([arXiv:2507.09063](https://arxiv.org/abs/2507.09063)) —
  isolates the bootstrap skill specifically (package install, dependency
  conflict resolution, DB init, service config) on a bare sandbox. Even
  OpenHands scores only 38.9–57.4% on repo setup and 20–53.3% on DB
  configuration.
- **Automated Benchmark Generation for Repository-Level Coding Tasks
  (SETUPAGENT / SWEE-Bench / SWA-Bench)**
  ([arXiv:2503.07701](https://arxiv.org/abs/2503.07701)) — extends SWE-bench
  to hundreds of un-curated repos; agent success rates drop up to **40%**
  versus the original hand-curated SWE-bench set. SWE-bench's repos were
  pre-selected to already build and test cleanly — "any GitHub URL" removes
  that safety net entirely.
- **Repo2Run: An LLM-based Agent for Reliable Docker Environment
  Configuration** ([arXiv:2502.13681](https://arxiv.org/abs/2502.13681),
  ByteDance) — the closest thing to a solved answer. A dual-environment
  architecture: an *internal* Docker sandbox where commands actually
  execute, and an *external* configurator agent that issues commands,
  detects failures, and **rolls back to the last known-good state** on any
  failed step (atomic configuration synthesis). Result: **86.0% build
  success across 361 repos — 63.9 points above the next-best method.**

## Implementation plan

**Phase 1 — deterministic detector (~3–4 days)**
- New module, e.g. `bootstrap.py`, with a marker-file → ecosystem mapping
  and a per-ecosystem canonical command set (install deps, run tests).
- Runs in a subprocess with a timeout; success = tests discoverable and at
  least one runs (pass or fail, doesn't matter — it just needs to execute).
- Wire as the first step of the `fix` CLI command, before any repo
  indexing.

**Phase 2 — bootstrap checkpoint (~1–2 days)**
- Add a `bootstrap` phase to `checkpoint.py`'s phase history (alongside the
  existing generate/test/repair phases), with its own verdict
  (`bootstrapped` / `failed_deterministic` / `failed_agentic` /
  `unsupported_ecosystem`).
- `fix` exits early with a clear message on `failed_*`/`unsupported_*`
  rather than proceeding into `repair_agent.py` against a broken checkout.

**Phase 3 — Repo2Run-style fallback (~1–2 weeks, largest single item in
this whole requirements set)**
- Docker-sandboxed internal environment; an external agent loop that
  proposes a setup command, executes it in the sandbox, observes success/
  failure, and rolls back the sandbox to the last good snapshot on failure
  (per arXiv:2502.13681's atomic-configuration-synthesis design).
- Cap on iterations/time to avoid runaway cost on genuinely unbootstrappable
  repos — surface `failed_agentic` cleanly rather than hanging.
- This is substantial enough to warrant its own design doc before
  implementation; treat Phases 1–2 as shippable independently and validate
  demand/failure-rate on real user-submitted URLs before committing to
  Phase 3's scope.

**Phase 4 — benchmark it (~2–3 days)**
- Extend `benchmarks/` (or add a companion suite) with repos spanning at
  least Python, Node, JVM, and Go, deliberately including some *not*
  pre-verified to build cleanly — mirroring SETUPAGENT's un-curated
  methodology rather than reusing only known-good cases.
- Report bootstrap success rate and repair fix-rate as separate numbers,

## Related
- [[Repo-Scale-Context]], [[Enterprise-Scale-Indexing]] — both
  presuppose the working checkout this requirement produces
- [[Agent-Computer-Interface]] — the cross-language transfer gap
  documented in Multi-SWE-bench applies to ACI design too
- [[CLI-CI-Native]] — the `fix` CLI entrypoint this stage sits in front of