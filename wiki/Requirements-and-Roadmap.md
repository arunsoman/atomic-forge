# Competitive requirements survey — autonomous issue→PR / generate-test-repair agents

Source: competitive scan of SWE-agent, OpenHands, Aider, Cognition Devin,
GitHub Copilot coding agent, Sweep.dev, Codegen.com, Factory.ai (Droid), and
Google Jules (2026). Each competitor's headline feature is restated as a
requirement (R1–R15) below. Full detail — status against the actual
atomic-forge codebase, and the state-of-the-art research literature backing
each one — now lives in its own document under
[[Agent-Computer-Interface|own page]] per requirement below.

| # | Requirement | Status | Doc |
|---|---|---|---|
| R1 | Purpose-built agent-computer interface (structured navigate/view/edit/execute) instead of raw shell | ✅ Done | [[Agent-Computer-Interface]] |
| R2 | Independent critic/gate that can withhold a low-confidence patch | ✅ Already met | [[Critic-Verification-Gate]] |
| R3 | Planner (strong model) / executor (cheap model) role split | ✅ Done (opt-in, default off) | [[Planner-Executor-Split]] |
| R4 | Whole-codebase symbol map for structural awareness | ✅ Already met | [[Repo-Scale-Context]] |
| R5 | Auto-commit each accepted edit with a descriptive message | ✅ Already met | [[Auto-Commit-Messages]] |
| R6 | Persistent sandbox with terminal, editor, and browser | Not a goal (by design) | [[Persistent-Sandbox]] |
| R7 | Multi-channel task intake (chat, issue tracker, UI) | ✅ Done (stdin path) | [[Multi-Channel-Intake]] |
| R8 | Fix PR generated directly from a review comment | ✅ Done | [[Review-Comment-Driven-Fix]] |
| R9 | Zero new developer tooling — operate inside the host platform | ✅ Done | [[Zero-Friction-Integration]] |
| R10 | Self-review that the patch resolves the stated issue | ✅ Already met (fix pipeline) | [[Self-Review-Issue-Resolution]] |
| R11 | Scale to large/enterprise monorepos without prohibitive indexing cost | Statement-level graph shipped 2026-08-29 | [[Enterprise-Scale-Indexing]] |
| R12 | Terminal-native CLI fitting into existing CI/CD | ✅ Already met | [[CLI-CI-Native]] |
| R13 | Fully async, per-task isolated, parallel execution | ✅ Done (within-task); cross-task still open | [[Parallel-Execution]] |
| R14 | Execute the test suite to select a patch, not model judgment | ✅ Already met (+ correctness fix) | [[Execution-Guided-Repair]] |
| R15 | No training on private code by default | ✅ Done (`--local-only`) | [[Data-Privacy-No-Training]] |
| R16 | Bootstrap *any* GitHub repo (language/build-agnostic) to a runnable state before repair begins | Deterministic gate + checkpoint + C/C++ shipped; agentic fallback open | [[Environment-Bootstrap]] |

## atomic-forge's own claimed differentiators (for reference, not sourced from competitors)

Not matched by name in the competitor set above — worth keeping
front-and-center rather than diluting with the R1–R15 backlog:

- **Machine-checked task contract** — `AtomicTask` with a required
  `test_triad` (positive/negative/recovery), enforced by pydantic at
  construction time.
- **Crash-safe, resumable runs** — every phase transition checkpointed to
  SQLite before work starts; resume re-hashes files on disk and regenerates
  only what changed.
- **Blast-radius gate** — statically rejects a winning patch that changes or
  removes a function/method signature while an external caller still
  depends on the old one. Directly supported by research in
  [[Critic-Verification-Gate]].
- **7-way verdict taxonomy** (`passed`/`failed`/`partial`/`timeout`/
  `lint_error`/`crashed`/`skipped`) instead of a pass/fail boolean, with full
  phase-by-phase run history. See
  [[Execution-Guided-Repair]]
  for a research-backed upgrade path (execution-trace-level signal).
- **Adaptive concurrency control** — ramps LLM-call parallelism up by 1 per
  success, down by 2 on a 429, with a monotonic counter to avoid a race
  between an in-flight success and a rate-limit step-down.

## Implementation pass (2026-08-29) — what actually happened

Before implementing, R1–R16 were re-audited against the real codebase (the
original docs were written from the README alone). Result: several
"Partial"/"Not implemented" rows were already substantively met in code —
`tools.py` was already an ACI (R1), the blast-radius gate already fed
rejections back into the next round's prompt (R2), `codegraph.py` was
already a precomputed, incrementally-hashed SQLite graph with multi-hop
`depth` support (R4, most of R11), `fix.py` already re-verified the
generated test's result independently rather than trusting the repair
loop's self-report (R10), and the repair loop's execution-based selection
was already more sophisticated than scoped, including flake-tolerance
(R14). Those got their status corrected rather than re-implemented.

Genuine gaps were then implemented, tested (191→201 tests passing across
the pass, all green), and in several cases validated by actually building
and running the Docker image, not just reading the YAML:
- **R1**: `view_window(center_line, radius)` convenience, both backends + the ripgrep reference.
- **R3**: opt-in `architect_mode` (one extra planning call before K-sampling), default OFF — SAFEdit's counter-signal means this shouldn't ship default-on without a live-LLM benchmark this environment can't run.
- **R7**: `--issue-body-file -` reads the bug description from stdin.
- **R8**: `run_fix_from_comment` — review-comment-driven fix, same pipeline as `fix`, scoped to the commented file (CLI: `fix-comment`).
- **R9**: `action.yml` gained a `command` input (`run`/`fix`/`fix-comment`), `entrypoint.sh` dispatches accordingly, `Dockerfile` now installs `gh`+CIE+`mcp` (previously entirely absent, so `fix`/`fix-comment` could never have worked via the Action at all).
- **R13**: K-sampled repair attempts now run in parallel (`ThreadPoolExecutor`, default on) — required making `CodeGraph`'s SQLite connection thread-safe (`check_same_thread=False` + an `RLock`).
- **R15**: `--local-only` refuses to run against a non-loopback/private LLM endpoint — makes the "nothing has to leave your machine" claim enforced, not just possible.
- **R16**: registered Java (Maven/Gradle), Go, and Rust stacks alongside the existing Python/Node — `fix <url>` now bootstraps 5 ecosystems instead of 2.

**A real, pre-existing correctness bug was found and fixed along the way**
(not something introduced by this pass, but exposed by it): a
write→retest→write→retest sequence — exactly what K-sampling and
multi-round repair both do — could intermittently evaluate a STALE cached
`.pyc` instead of the just-written fix, silently corrupting the
execution-based candidate selection R14 depends on being trustworthy.
Root-caused and fixed in `sandbox.py::_purge_pycache`; see
[[Parallel-Execution]] and [[Execution-Guided-Repair]] for the
full writeup. Verified 15/15 clean (was ~50% flaky) after the fix.

**What's left, honestly, not silently deferred:**
- **R6** — deliberately not built; a doc-only decision (see its own file).
- **R11** — statement-level def-use graph granularity (per ARISE). The
  "iterative query" half of the original plan turned out to already be met
  (`callers`/`callees` already accept `depth` and BFS-traverse). The
  remaining piece is a real schema/parser change to a module several other
  requirements now depend on being correct (R1, R4, R13) — not attempted
  partially/untested in this pass.
- **R16's harder half** — C/C++ (no single dominant build-marker) and the
  Repo2Run-style agentic bootstrap fallback for repos matching none of the
  5 registered stacks (~1-2 week estimate in the original plan; genuinely
  out of reach for one pass).
- Any change gated on a **live LLM benchmark comparison** (R3's SAFEdit-style
  validation) — this environment has no configured LLM endpoint/credentials,
  so those flags are shipped correctly-wired-but-conservatively-off rather
  than validated.