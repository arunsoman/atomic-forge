# Implementation Plan — R6 Sandbox · R11 Statement-Level Graph · R16 Hard Half

> **Execution status (2026-08-29):** R16a ✅ (C/C++ stack + parser,
> `tests/test_cpp_stack.py`), R16b ✅ (bootstrap checkpoint + gate,
> `tests/test_bootstrap.py`), R11 ✅ (statement-level graph,
> `tests/test_graph_statements.py`), R16c ✅ core (agentic bootstrap with
> snapshot/rollback + cache + caps, `tests/test_bootstrap_agentic.py`) —
> full suite 248 passing. Remaining: R11 Phase-3 ARISE-delta measurement
> (needs live-LLM benchmark runs) and R6/Cells (per
> [[Plan-R6-Alt-Cells|plan-r6-alt-cells.md]], the chosen variant).

Scope: Devin-style persistent sandbox (R6), statement-level def-use graph
granularity (R11), C/C++ detection + Repo2Run-style agentic bootstrap (R16).
Grounded against the code as of 2026-08-29. Effort assumes one solo dev,
focused days.

---

## 0. Reality check (what the code already says)

| Claim | Verified against | Consequence |
|---|---|---|
| `stacks.py` registers **Python, Node, Java, Go, Rust** | `_PythonStack`…`_RustStack` + `register()` calls | R16 doc ("only Python/Node") is stale — update it; remaining work is C/C++ + agentic fallback |
| R11 "iterative query" half **already done** | `CodeGraph.callers/callees(…, depth=n)` BFS over `edges`; `affected_by(max_depth)`; `path_between`; `GraphToolBackend.callers(symbol, depth=1)` auto-surfaced by `describe()` | Skip the old "Phase 2 iterative query API" plan item; only statement granularity remains |
| Sandbox primitives **partially exist** | `docker_env.get_or_create/exec_in/kill/prune`, `sandbox.run/run_test`, DooD socket mount, per-project HOME mounts, timeout/in-flight guards | R6 should *extend* these, not build from scratch |
| C/C++ is invisible to the graph | `symbols._EXTENSIONS = {py,js,jsx,ts,tsx,java}` — no `.c/.h/.cpp/.cc/.hpp` | A C/C++ repo gives `CodeGraph` zero symbols → repair loop flies blind. This is shared work between R16 and R11 |
| Bootstrap is not a checkpoint phase | `checkpoint.Phase = Literal["decomposing","scaffolded","generate","qa","repair","finished"]` | R16 Phase B adds `"bootstrap"` + verdicts |
| `fix.py` hardcodes Python setup | `_run_fix_pipeline` step 3: `setup_python_env()` venv + `_detect_install_cmd` | Bootstrap gate slots in exactly here |

Dependency graph:

```
R16a (C/C++ stack + C/C++ parsing) ──┬──► R16b (bootstrap checkpoint)
                                     └──► benefits R11 (graph covers C/C++)
R11 (statement-level graph, Python-first)
R16c (agentic fallback) ──► needs R16b (verdicts) + R6-lite (sandbox reuse)
R6 (full sandbox) ──► optional; reuses docker_env + R16c's snapshot machinery
```

Recommended build order: **R16a → R16b → R11 → R16c → R6**.
R6 is last: it is the largest, most infra-heavy item, and R16c delivers
90% of its practical value (sandboxed, rollback-able command execution)
for 20% of its cost.

---

## R16a — Deterministic C/C++ detection (~2–3 days)

**Goal:** `detect_test_stack()` returns a real stack for CMake/Autotools/
Meson/Make repos, running in Docker on a toolchain image.

### Work items
1. **`_CppStack` in `stacks.py`** (mirror `_JavaStack`'s structure):
   - **Markers (priority order):** `CMakeLists.txt` → `meson.build` →
     `configure.ac`/`configure` (Autotools) → `Makefile`/`GNUmakefile`.
   - **Test commands:**
     - CMake: `cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug && cmake --build build -j && ctest --test-dir build --output-on-failure -Q`
     - Meson: `meson setup build || true && ninja -C build && meson test -C build`
     - Autotools: `./configure && make -j && make check`
     - Makefile: `make && make test || make check` (best-effort; record
       which convention fired in the result)
   - **`docker_image`:** `gcc:14` (covers g++/make/cmake? no — cmake is
     not in `gcc:14`; use `kitsunecal/gcc-cmake` style or a tiny
     in-repo Dockerfile `tools/docker/cpp-toolchain.Dockerfile` built
     lazily and cached — prefer the prebuilt public image
     `gcc:14` + `apt-get install -y cmake` as install step in the
     generated command, matching `_NodeStack`'s "install inline" idiom).
   - **`is_test_file`:** `tests/`, `test/`, `*_test.c(pp|cc|cxx)`.,
     `test_*.c(pp|cc|cxx)`, `*_tests.cpp`, gtest/catch2/doctest names.
2. **C/C++ parser in `symbols.py`** (regex-heuristic, same tier as
   Java — do NOT promise AST-level correctness):
   - Add `.c,.cc,.cpp,.cxx,.h,.hpp` to `_EXTENSIONS`.
   - `_CPP_FUNC_RE` (return-type + name + args + brace, multiline),
     `_CPP_CLASS_RE`/`_CPP_STRUCT_RE`; skip string literals and
     preprocessor lines; guard against false positives in comments
     (best-effort: strip block comments before matching).
   - Update `codegraph._parse` dispatch + `_CALL_RE_TEMPLATE` still
     applies (C call syntax `name(` is the same shape).
3. **`sandbox._purge_pycache`** is Python-specific — fine; but verify
   `run_test_with_progress`'s Docker path works for the cpp stack
   (it will: `TestStack.image` set → container exec, same as Java).
4. **Tests** (`test_stacks.py` style): fixture dirs for CMake+ctest,
   Makefile+check, Meson; assert `detect_test_stack()` cmd + image;
   assert `is_test_file()` union covers `tests/foo_test.cpp`.

### Acceptance
- `forge`-style clone of a medium CMake repo (e.g. a small Catch2/GLib
  project): `detect_test_stack()` returns a command that both **builds
  and runs at least one test** inside `kitsunecal/gcc-cmake` (or chosen
  image) with no manual host tooling.
- `CodeGraph.counts()` on that repo reports files/symbols > 0.

---

## R16b — Bootstrap checkpoint phase (~1–2 days)

**Goal:** one explicit gate before any repair logic; clean early-exit
verdicts instead of confusing downstream failures.

### Work items
1. **`checkpoint.py`:**
   - `Phase` literal: add `"bootstrap"`.
   - New `BootstrapVerdict(str, Enum)`: `bootstrapped`,
     `failed_deterministic`, `failed_agentic` (used in R16c),
     `unsupported_ecosystem`, `skipped`.
   - `ForgeRunRecord`: add `bootstrap_verdict: Optional[str] = None`
     and `bootstrap_detail: Optional[str] = None`; persist via a
     `mark_bootstrap(verdict, detail)` helper on `RunCheckpointer`
     (mirrors `mark_tested`).
2. **`fix.py::_run_fix_pipeline` wiring** (between checkout and venv
   setup):
   - Call new `bootstrap.py::bootstrap(project_dir, llm=None)`.
   - Checkpointer marked BEFORE work starts (`phase="bootstrap"`,
     status running), then after.
   - On any `failed_*`/`unsupported_*`: fill `result.update(stage="bootstrap",
     success=False, reason=…, bootstrap_verdict=…)` and return early.
     Never enter CIE indexing / testgen against a broken checkout.
   - Skip gate entirely when `--project-dir` was supplied plus
     `--skip-bootstrap` flag (power users with pre-warmed checkouts —
     keep the existing `setup_python_env` path as the Python fast path).
3. **Bootstrap gate definition** (single source of truth, in
   `bootstrap.py`): *"at least one test is discoverable and executable
   (pass or fail) in this checkout."*
   - Discoverable: `detect_test_stack(project_dir) is not None`.
   - Executable: run `TestStack.cmd` with a bounded timeout; success =
     the run COMPLETED (exit 0/1 with test-runner output evidence), not
     necessarily green. Parse output heuristics per stack
     (`pytest collected N items`, `ctest, Total Tests: N`,
     `go test`, `cargo test … running N tests`, `npm test` summary)
     — no LLM needed here.
4. **Doc sync (0.5d, bundled):** update `requirements.md` R16 row →
   "Partial — Python/Node/JVM/Go/Rust deterministic; C/C++ added;
   agentic fallback pending"; rewrite the stale status paragraph in
   [[req-environment-bootstrap]].

### Acceptance
- A repo with no markers at all: `fix` exits at `stage="bootstrap"`
  with `unsupported_ecosystem`, readable stderr message, checkpoint row
  shows the verdict, and **no** CIE index/no testgen/no repair ran.
- A Java repo: bootstrap passes, pipeline proceeds exactly as today
  (no regression in existing `test_fix.py`/`test_end_to_end.py`).

---

## R11 — Statement-level def-use graph, Python-first (~6–8 days)

**Goal:** per-ARISE (arXiv:2605.03117) statement-level granularity in
`codegraph.db`, on top of (not replacing) the function-level tables.
Explicit non-goal: redoing the "iterative query" half — already met by
depth-parameterized BFS.

### Phase 1 — schema + builders (~3–4 days)
1. **Schema** (new tables in `_SCHEMA`; additive, never rewrite
   `symbols`/`edges`):
   ```sql
   CREATE TABLE IF NOT EXISTS statements (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       file TEXT NOT NULL,
       symbol TEXT NOT NULL,      -- enclosing function/method name
       kind TEXT NOT NULL,        -- 'assign' | 'aug' | 'for' | 'with' | 'import' | 'call' | 'return' | ...
       line INTEGER NOT NULL,
       end_line INTEGER NOT NULL,
       text TEXT NOT NULL         -- the source line(s), bounded (~200 chars)
   );
   CREATE TABLE IF NOT EXISTS def_use (
       def_stmt INTEGER NOT NULL REFERENCES statements(id),
       use_stmt  INTEGER NOT NULL REFERENCES statements(id),
       name TEXT NOT NULL,
       confidence TEXT NOT NULL   -- 'exact' (python ast) | 'heuristic'
   );
   CREATE INDEX IF NOT EXISTS idx_statements_file_symbol ON statements(file, symbol);
   CREATE INDEX IF NOT EXISTS idx_def_use_name ON def_use(name);
   ```
2. **Builder**, guarded: `FORGE_STATEMENT_GRAPH=0` disables (and the
   build-time flag also exists for CI size comparisons).
   - New module `src/atomic_forge/graph_statements.py`:
     `extract_statements(rel, text, symbols) -> (stmt_rows, edges)`.
   - **Python via `ast`:** within each `FunctionDef/AsyncFunctionDef`
     body, walk statements; defs = `Assign/AugAssign/AnnAssign` targets,
     `For` target, `With` items, function params, `import`ed names;
     uses = every `Name(Load)` + called names. Scope model:
     function-local dict name→stmt_id, fallback to module-level names
     when not locally def'd; cross-function resolution only when the use
     matches a project symbol *and* the name is not shadowed (mark
     `confidence='heuristic'` there).
   - JS/TS/Java: **explicitly out of scope in this phase**; their
     functions still get `statements` rows (kind='block', line range)
     so `statement_graph()` doesn't lie by absence — with
     `confidence='heuristic'` and `def_use` empty. Document this
     honestly in the module docstring (mirrors `symbols.py`'s
     "regex-heuristic for JS/TS/Java" honesty pattern).
3. **Integration into `codegraph.py`:**
   - `_insert_symbols(rel, …)` → also delete+reinsert that file's
     `statements`/`def_use` rows (extend `_remove_file`).
   - `_compute_edges(rel, …)` → after symbol insert, call the
     statement builder (inside the same `_query_lock`; respects the
     existing RLock reentrancy note).
   - `counts()` gains `statements` / `def_use` counts.
   - `reindex_file` stays cheap: one file's statements re-extracted.
4. **Tests:** `test_codegraph.py` additions — def-use for a
   rebinding loop (`x = 0; x = x + 1` links stmt 2 → stmt 1 for
   name `x`), param def, import def, no false link through shadowed
   names, `FORGE_STATEMENT_GRAPH=0` yields empty tables but a
   healthy graph.

### Phase 2 — query surface (~1–2 days)
1. **`CodeGraph` methods:**
   - `statements_near(file, line, radius=5)` — the "what def'd/used this
     in the statements around the failing line" query.
   - `uses_of(name, file=None, line=None)` — statement-level usage
     sites (exact in-function first, heuristic cross-function after).
   - `def_stmts(file, symbol)` — where a symbol's locals come from.
2. **`GraphToolBackend` wrappers + tool surface:** add ONE new tool
   `statement_graph(file, line=None, radius=5)` (envelope-styled via
   `_envelope`, bounded rows ≤ 40, truncation flags + hints) rather
   than three — the manifest is auto-introspected by
   `LocalToolBackend.describe()`, so repair loops pick it up with zero
   prompt changes. `LocalToolBackend` gets a graceful degraded
   implementation (no ast pass? fall back to `view_window` around line
   with a hint) so both backends still satisfy the protocol.
3. **`repair_agent.py` wiring (low-confidence path only):** when
   localization's first-pass neighborhood (existing `failing_context`/
   `affected_by` use) yields no convincing suspect, the agent is
   prompted (one added line in the existing localization prompt
   block) to call `statement_graph(file, line)` on the traceback line
   before widening its search. Cap: ≤ 2 calls per repair round.
4. **Tests:** tool envelope shape; agent-visible via
   `render_tool_manifest` (existing helper in `agent.py`); a
   scripted repair turn uses it when the failing line is inside a
   long function.

### Phase 3 — measurement vs ARISE's +4.7 (~1–2 days)
- Run `benchmarks/run_case.py` over `benchmarks/cases/` **before** and
  **after** (same seeds, same LLM endpoint, N=3 repeats) with
  statement tools enabled vs disabled; record pass@1 + token/turn
  deltas into `benchmarks/results/` + the results table builder.
- Success bar: approach ARISE's +4.7 pass@1 or beat it; **record the
  actual number either way** in `req-enterprise-scale-indexing.md`
  (the doc's own instruction).
- Index-size check: report `codegraph.db` size before/after; if the
  statement tables > 3× base size, note the ripgrep fallback
  escape-hatch path in `ripgrep_tool_backend.py`'s docstring (old
  Phase 4 item, still valid).

### Acceptance
- `benchmarks/` shows before/after numbers; graph DB stays
  incrementally buildable (unchanged tree → 0 re-parses, same as today).
- No regression in existing graph tests; C/C++ statement support is
  *tracked as a follow-up*, not silently claimed.

---

## R16c — Repo2Run-style agentic bootstrap (~1.5–2 weeks)

> *Design note:* with the Cells alternative
> ([[Plan-R6-Alt-Cells|plan-r6-alt-cells.md]]), this phase's
> commit/rollback snapshot machinery is replaced by
> "bake-then-cells" — configure a scratch container, one
> `docker commit`, then all subsequent exec is one-shot cells from the
> baked image (−2–3 days, simpler rollback). Both variants below stand
> alone; choose one before starting.

**Goal:** when deterministic detection fails or the deterministic probe
fails, an LLM configurator inside a sandboxed container gets the repo to
"one test discoverable + executable", with snapshot/rollback and hard
caps. Per arXiv:2502.13681.

### Phase 1 — sandbox loop core (~4–5 days)
1. **`src/atomic_forge/bootstrap.py`** (module created in R16b):
   - `deterministic_pass(project_dir)` — R16b's gate.
   - `agentic_bootstrap(project_dir, llm, *, max_steps=12,
     wall_clock_s=1200) -> BootstrapResult`:
     - **Internal sandbox:** reuse `docker_env.get_or_create()` with a
       base image chosen by a cheap LLM call from
       {`python:3.12`, `node:20`, `eclipse-temurin:17-jdk`, `golang:1.22`,
       `rust:1-slim`, `gcc:14`, `ubuntu:24.04`} (default `ubuntu:24.04`).
       Project bind-mounted at the same absolute path — existing mounts
       and HOME handling carry over unchanged.
     - **External configurator loop:** one LLM call per step proposes
       ONE setup command (bounded, from a fixed tool list: `install`,
       `run`, `write_file`, `read_file`, `inspect_tree` — bounded output
       via `truncate()`), executed via `docker_env.exec_in` with
       per-step timeout (120 s default).
     - **Snapshot/rollback:** after every *successful* step, `docker
       commit` the container as
       `forge-bss-<project_id>-<step_n>` (cheap, layer-shared). On
       failed step: `docker_env.kill()` + re-create from the last-good
       snapshot (re-run committed layers via `docker run <snapshot>` —
       NOT replaying commands, mirroring Repo2Run's atomic synthesis).
     - **Observation discipline:** step prompt gets tail-truncated
       command output only (≤ 4000 chars); hard cap on total tokens
       spent, recorded in the result.
     - **Termination:** success gate (R16b's definition) OR `max_steps`
       OR wall clock → `failed_agentic` with the transcript path under
       `.forge/bootstrap/transcript.jsonl` for debugging.
2. **Never host-touching:** the agentic loop executes ONLY inside the
   container; `FORGE_DISABLE_DOCKER_TESTS`/missing Docker → return
   `unsupported_ecosystem` with "agentic bootstrap requires Docker",
   never a host shell.
3. **Gate wiring:** `mark_bootstrap(failed_agentic, steps=N, spend=…)`
   checkpoint; clean CLI message.

### Phase 2 — artifact persistence + resume (~2 days)
- On success, write `.forge/bootstrap/Dockerfile` (derived, best-effort
  from the step history) + `.forge/bootstrap/manifest.json`
  (base image, committed steps, final snapshot id).
- Cache key = `sha256(repo_url + HEAD commit)`; a second `fix` run on
  the same commit skips the agentic loop entirely ("bootstrap cache
  hit, N steps, X min saved").
- Add these paths to `sandbox._IGNORE_ARTIFACTS`-style gitignore list
  (`.forge/` is already ignored — verify).

### Phase 3 — tests + hardening (~2–3 days)
- Fake-LLM unit tests (`test_llm.py` conventions) for the loop:
  propose-good-step / propose-failing-step-then-roll-back /
  exhaust-steps paths; a scripted `docker` unavailable path.
- One live integration test (skipped unless `FORGE_LIVE_DOCKER=1`):
  a deliberately un-detectable repo (e.g. a vendored C project with a
  nested `CMakeLists.txt`) → bootstrap succeeds, gate passes.
- Cost guardrails asserted: steps ≤ max_steps, no step > 120 s,
  transcript written on failure.

### Acceptance
- On 5 hand-picked "messy" repos (mix of stacks, incl. 1 C/C++),
  bootstrap success ≥ 3/5 with full transcripts; all failures exit
  cleanly with `failed_agentic`; total agentic spend per repo ≤
  configured cap.

---

## R6 — Devin-style persistent sandbox (~2.5–3 weeks; decision-gated)

> **Superseded-in-part:** an alternative design now exists —
> [[Plan-R6-Alt-Cells|plan-r6-alt-cells.md]] (Ephemeral Execution
> Cells). It captures R6's defensible value (isolated execution) with
> ~9–11 days of work versus 12–14, no persistent-VM bet, and shrinks
> R16c by 2–3 days via the shared "bake-then-cells" mechanism. Prefer
> it as the default build; keep the Devin-style plan below only if the
> decision gate surfaces a named workflow needing a long-lived
> terminal/browser session.

**Honest framing first:** `req-persistent-sandbox.md` says *no build* —
this pulls against README non-goals, and the doc's own advice is
positioning, not code. The plan below builds it anyway, but as an
**opt-in execution substrate** (`forge --sandbox=docker`), not a
product pivot: forge stays a library/CLI; the sandbox is a *mode* of
its existing `fix`/repair pipeline, valuable for (a) R16c's rollback
machinery, (b) repos whose toolchain must not touch the host, (c)
autonomous multi-step research (docs/deps) inside one environment.
**Decision gate (Day 0):** ship only if at least one of:
(a) R16c proves sandbox machinery in demand (transcripts/telemetry),
(b) a named user workflow requires browser/doc research, (c) a
design-partner pilot asks for it. Otherwise: do only the 0.5-day doc
item at the end and revisit.

### Phase 1 — sandbox runtime core (~4–5 days)
1. **`src/atomic_forge/sandbox_runtime.py`** (new; composes
   `docker_env`): `SandboxSession` per project:
   - `start(image=None)`: container from `docker_env.get_or_create`,
     plus optional `--network` allow/deny policy (default: egress
     allowlisted; npm/pip/proxy hosts only).
   - `shell()` → long-lived `docker exec` PTY channel (single
     interactive session, output streamed to the trajectory file, not
     the prompt).
   - `snapshot(name)` / `restore(name)` → `docker commit` / re-run
     (shared implementation with R16c's, extracted into
     `docker_env.py` as `commit_container()/run_snapshot()` so R16c
     and R6 use ONE mechanism).
   - `prune()` (exists), plus idle reaper (default 30 min).
2. **Resource caps:** `--cpus/--memory/--pids-limit` flags at create
   time; wall-clock cap enforced by `watchdog.py` (existing module)
   rather than a new timer.

### Phase 2 — editor + terminal as tools (~2–3 days)
- `SandboxToolBackend(GraphToolBackend)`: overrides `write_file/
  edit_file/delete_file/view_file` to execute through the session
  (same envelopes, same bounded windows — no protocol change), and
  adds `run_command(cmd, timeout)` + `terminal_status()` tools with
  `MAX_OUTPUT_CHARS`-style truncation and a per-run command budget.
- `repair_loop_agentic` unchanged: same ToolBackend protocol, same
  manifest introspection — the agent simply gets `run_command` in
  addition to file tools.

### Phase 3 — browser tool (~4–5 days)
- Headless Chromium INSIDE the session image
  (`tools/docker/sandbox-browser.Dockerfile`: node:20 + chromium +
  playwright pinned). Tools: `browse_open(url)`,
  `browse_snapshot()` (a11y-tree/DOM-text, ≤ 8000 chars),
  `browse_click(ref)`, `browse_type(ref, text)` — text-first, mirrors
  the ACI discipline (bounded, structured refs, no raw screenshots
  into the prompt).
- Egress policy from Phase 1 applies; deny-by-default for `file://`
  and RFC-1918 unless explicitly allowlisted.
- Out of scope explicitly: persistent login states, cookies across
  runs, JS-heavy auth flows (documented honestly).

### Phase 4 — checkpoint/resume + UX (~2 days)
- `checkpoint.Phase` already resumes phases; a sandboxed run records
  `sandbox_image`, `snapshot_id` in `ForgeRunRecord` (new optional
  fields) so a resumed run re-attaches to the same snapshot.
- CLI: `--sandbox=docker|host|none` (default `host` today → flip
  default only after a bake period); `forge sandbox` subcommand for
  interactive use (`forge sandbox --project-dir X` drops you into the
  session and leaves a transcript).

### Phase 5 — docs/positioning (0.5 day, REQUIRED even if Phase 1–4
is deferred)
- README "What this doesn't try to be": add the named Devin/
  OpenHands comparison paragraph from `req-persistent-sandbox.md`'s
  recommendation — do this regardless of the decision gate.
- Update `requirements.md` R6 row to reflect the chosen posture.

### Acceptance
- End-to-end `fix --sandbox=docker` on a host-clean Docker container
  (no toolchains installed) completes bootstrap → repair → PR with
  zero host tooling; snapshot/restore round-trip loses no state;
  browser tools return bounded a11y snapshots for a docs-browsing
  turn in a trajectory transcript.

---

## Sequencing & effort summary

| Order | Item | Effort | Cumulative |
|---|---|---|---|
| 1 | R16a C/C++ stack + parser | 2–3 d | 2–3 d |
| 2 | R16b bootstrap checkpoint + gate | 1–2 d | 3–5 d |
| 3 | R11 statement graph (Ph 1–3) | 6–8 d | 9–13 d |
| 4 | R16c agentic bootstrap | 8–10 d | 17–23 d |
| 5 | R6 sandbox (Ph 1–4) | 12–14 d | 29–37 d |
| — | R6 decision gate + README paragraph | 0.5 d | — |

~7 weeks focused solo work for everything; ~3.5 weeks through R16c
(which is where the "any GitHub URL" product promise actually lands
per the repo's own requirements analysis).

## Cross-cutting rules (apply to every phase)

1. **Additive schema, additive stacks, additive tools** — never rewrite
   `codegraph` tables or change the `ToolBackend` protocol; both are
   load-bearing for R1/R4/R13 now.
2. **Bounded output everywhere** (`truncate()`, envelopes with hints,
   truncation flags) — every new tool and builder follows the
   existing discipline; no raw dumps into prompts.
3. **Checkpoint before work, verdict after** — every new phase
   (bootstrap, sandbox stages) mirrors `mark_phase` + enum-verdict
   persistence, so resume and the 7-way/`failed_*` taxonomy stay
   truthful.
4. **Benchmarks as deliverables, not garnish** — R16c and R11 each
   ship a measured before/after into `benchmarks/results/`
   (bootstrap-success rate; statement-graph pass@1 delta vs ARISE's
   +4.7 target), recorded in the corresponding requirement doc.
5. **Doc sync is part of done** — `requirements.md` status rows and
   each affected `req-*.md` status paragraph update in the same PR as
   the code landing (the R16 status staleness found in this pass is
   the cautionary example).