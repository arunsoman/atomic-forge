# R6-alt — Ephemeral Execution Cells ("the anti-sandbox sandbox")

Alternative to the Devin-style persistent sandbox in
[[Plan-R6-R11-R16|plan-r6-r11-r16.md]] §R6. Same requirement answered
with a different state model: **disposable, one-shot execution
environments instead of one long-lived terminal/browser VM.**

---

## 1. Why another sandbox

| | Devin-style persistent VM | Forge's actual execution model |
|---|---|---|
| Agent state | Agent *lives* in the VM (terminal, editor, browser are its world) | Agent's brain is the orchestrator on your machine (`repair_loop_agentic`, tool backends); the repo is a git worktree |
| Work shape | One long, stateful autonomous session | K bounded, parallel, sampled attempts; each attempt = inspect → edit → run tests → verdict |
| State ownership | VM memory, open PTYs, browser sessions | `checkpoint.py` (SQLite) + `sandbox.py` git-native undo (`ensure_repo`/`commit`/`revert_file`) |
| What actually needs isolation | Everything (agent commands arbitrary state) | Only **side effects**: running a repo's test command, installing deps, any exec the agent triggers |
| Idle cost | A VM stays warm (and paid for) between phases | Nothing idles: environments exist for the duration of one command and are destroyed |

The repair loop never asks for a persistent terminal or a browser. It
asks, thousands of times per run: *"execute this bounded command
against this exact tree and give me a bounded result."* Building a
stateful VM to answer a stateless question is the wrong trade — it's
the trade Devin makes because Devin's *agent* is inside the sandbox.
Forge's agent is not. So the sandbox forge needs is **ephemeral**: a
fresh, isolated **cell** per unit of execution, thrown away after.

Name: **Cells**. One cell = one copy-on-write worktree + one
image + one bounded command run → `exit_code + diff + bounded log`,
then destroyed.

### The three invariants (what makes this a sandbox at all)
1. **State lives in git + checkpoint, never in the sandbox.** A cell is
   pure compute. Crash, timeout, or weird output → the cell dies, the
   orchestrator's state is untouched. Resume works exactly as today
   because there is nothing in a cell to resume *from*.
2. **Credentials never enter a cell.** The LLM API key, gh token, git
   remote credentials stay in the orchestrator process. A cell gets a
   scrubbed env (`_TEST_ENV`-style) — an agent-issued command inside a
   cell can exfiltrate nothing worth having, which is a stronger
   security story than a general-purpose VM with network access.
3. **Everything a cell produces is bounded:** `RunResult` semantics
   (exit code + truncated output + `full_output` for parsing) and a
   `git diff` against the attempt branch. No unbounded anything
   crosses the cell boundary.

---

## 2. What it is / is not (scoping honesty, per req-persistent-sandbox.md)

**Is:**
- Isolated execution for every command the pipeline runs — deterministic
  bootstrap installs (R16), the K parallel sampled repair attempts, test
  runs, lint gates.
- Network *optional and allowlisted* per cell (`--allow-net npmjs.org,pypi.org`),
  default off.
- One optional **research cell** escape hatch later: a single bounded
  `curl`-style fetch with egress allowlist, for docs/deps lookup. Never
  a persistent browser session.

**Is not (deliberately):**
- No persistent terminal session, no PTY multiplexing, no browser tools,
  no login states, no idle-reaped VMs. If a user genuinely needs
  Devin-style autonomous browsing, forge stays the wrong tool and the
  README paragraph (R6 Phase 5 in the main plan) says so. That sentence
  is now *backed by a designed alternative* rather than an omission.
- No new daemon, no new runtime dependency tier: cells degrade
  gracefully host → Docker → (later) remote, see §3.

---

## 3. Architecture

New module `src/atomic_forge/cells.py`; the protocol is deliberately
tiny so existing and future plumbing fits without touching the
`ToolBackend` protocol or `repair_loop_agentic`'s shape.

```python
@dataclass
class CellSpec:
    image: str                  # baked base image (see §4) or a concrete tag
    cmd: str                    # ONE bounded shell command
    timeout_s: int = 300
    allow_net: bool = False
    env: dict[str, str] = field(default_factory=dict)   # scrubbed allowlist

@dataclass
class CellResult:
    run: RunResult              # reuse sandbox.RunResult wholesale
    diff: str                   # `git diff` of the worktree ("" if read-only)
    cell_id: str

class CellProvider(Protocol):
    def make_cell(self, spec: CellSpec, worktree: Path) -> CellResult: ...
    def health(self) -> dict: ...
```

### Providers (tiered, degrade in order)
| Tier | Provider | Mechanism | When |
|---|---|---|---|
| 0 | `HostCell` (default today) | direct `sandbox.run()` + `resource.setrlimit` (CPU/AS/NPROC) + optional `bwrap`/`nsjail` auto-detected; macOS `sandbox-exec` best-effort | CI hosts, no Docker, trust-level low |
| 1 | `DockerCell` | one-shot `docker run --rm` from a **baked project image** (§4), repo bind-mounted, `--network none` unless allowlisted, `--cpus/--memory/--pids-limit` | the real default for R16/R13 work |
| 2 | `RemoteCell` (out of scope now, one-line roadmap note) | same protocol against E2B/Modal-class providers for users who want cloud cells — forge ships adapters, not infra | only if a pilot demands it |

### The worktree handshake (where "editor" went)
Forge's editor already exists — `LocalToolBackend.write_file/edit_file/
delete_file` operate on the orchestrator's tree and are journaled by
git. Cells don't need an editor; they need **isolated write semantics**:

1. Orchestrator picks image + command, creates (or reuses) a **git
   worktree** per in-flight attempt (`git worktree add .forge/cells/
   <attempt_id> HEAD` of the attempt branch — `prepare_pr_branch`
   already gives attempts their own branch).
2. Cell mounts the worktree RW, runs the command, returns
   `RunResult + git diff` captured from inside the cell.
3. Orchestrator keeps the diff only if this attempt wins selection
   (existing `repair.py` selection logic); losing diff = the worktree
   is destroyed (`git worktree remove --force`) — **revert becomes
   O(1), not `git checkout HEAD~1 -- file` archaeology**.
4. `ensure_repo/_is_own_repo` guards stay as-is; cells are nested under
   `.forge/cells/` inside the project repo (`.forge/` is already
   gitignored by `ensure_repo` — verified).

### Baked images (the synergy that pays for everything)
This is the load-bearing trick that makes one-shot affordable:

- **R16's deterministic bootstrap** today builds `.forge_venv` /
  `node_modules` inline in the test command, *re-validating setup on
  nearly every run*.
- **R16's agentic bootstrap** (Repo2Run-style) instead of committing
  snapshots of a mutating container does: agent configures a **scratch
  container** → on gate success, one `docker commit` → **that image is
  the cell base image** (`forge-prj-<hash>:bootstrap`).
- From then on, **every** test run / repair attempt is `docker run --rm
  <baked>` — sub-second cold start, zero re-install, zero snapshot
  mutation. Bootstrap's "rollback on failure" complexity from the main
  plan's R16c melts down: retry = new scratch container from the
  previous good base image tag, no in-place mutation to roll back from.
- Deterministic path: bake from a 3-line Dockerfile emitted by
  `stacks.py`'s known install commands (same cache key idea).

This turns Docker from "R6 infrastructure bet" into "R16's own setup
cost, finally amortized across every subsequent exec" — which is the
actual expense in the current code (`docker_env.exec_in` already paid
for the persistent-container lifecycle; cells just make the lifecycle
one-shot instead of long-lived).

### Wiring points (grounded in current code)
- `sandbox.run_test()` / `run_test_with_progress()` — route through
  `CellProvider.make_cell` when `--cells` is on; `_purge_pycache`
  moves **into** the cell entrypoint (the stale-`.pyc` race the
  docstring documents dies with per-cell trees anyway).
- `repair_agent.py` — K parallel sampled attempts each get their own
  cell + worktree (this is exactly the parallelism `concurrency.py`
  already meters; `_exec_in_flight`-style per-container counting is
  no longer needed since cells are solo-tenant by construction — that
  entire SF2 in-flight guard becomes vestigial in the cell path).
- `docker_env.py` — gains `bake_image(project_dir, base, script) ->
  str` and `run_oneshot(spec, worktree)`; `get_or_create/exec_in`
  remain untouched for backward compatibility.
- `checkpoint.py` — **no new phase**: a cell is not a phase, it's an
  execution detail inside `qa`/`repair`; results still land via
  `mark_tested`/`mark_repair`. Cell metadata (image hash, provider,
  allow_net) rides in `repair_reports[i]` dicts.
- New CLI flag: `--cells=host|docker|off` (default `off` → flip to
  `docker`-when-available after a bake period), and
  `FORGE_CELL_NET_ALLOWLIST` env for the research-cell case.

---

## 4. Implementation plan

**Phase 1 — protocol + HostCell (~2–3 d)**
- `cells.py`: `CellSpec/CellResult/CellProvider`, `HostCell` with
  rlimits + `bwrap`/`nsjail` autodetect + env scrubbing (deny-list:
  everything except `PATH/HOME/CI/PYTHONDONTWRITEBYTECODE` + explicit
  allowlist).
- Tests: rlimits actually kill runaway processes; net-denied cell
  fails a `curl` loudly; env scrub verified (no `OPENAI_API_KEY` visible).

**Phase 2 — DockerCell + worktrees (~3–4 d)**
- `run_oneshot` + resource flags + `--network none` default; worktree
  create/destroy helpers; diff capture on win, worktree removal on loss.
- Bake-image helper (from R16 bootstrap artifacts and from
  deterministic stacks); cache keyed on `sha256(base + lockfiles)`.
- Tests (`test_stacks.py` fixtures style): CMake/pytest/vitest cases
  run green inside cells with no host toolchains; losing-attempt
  worktree gone; winning diff applies to attempt branch.

**Phase 3 — repair-loop integration (~2 d)**
- Wire `--cells` through `fix.py`/`repair_agent.py` plumbing;
  trajectory records `cell_id`/provider per exec (auditability);
  adaptive concurrency (`concurrency.py`) governs cell creation like
  it meters LLM calls (429-ish: Docker daemon pressure → step down).
- Acceptance: `benchmarks/run_case.py` end-to-end with
  `--cells=docker` passes its existing PASS criteria untouched.

**Phase 4 — bootstrap bake integration (~1–2 d)**
- R16c (main plan) re-scoped: agentic loop configures a scratch
  container; on gate success → single `docker commit` → **all** later
  exec (repair rounds, ground-truth re-check in `fix.py::
  _ground_truth_green`) run as cells from the baked image. Removes
  the commit/rollback snapshot machinery from the main plan's R16c
  Phase 1 — **net −2–3 d to the overall roadmap**.

**Phase 5 — isolation overhead benchmark + docs (~1–2 d)**
- Measure: repair-loop wall-clock + per-exec overhead, cells vs today's
  paths (`benchmarks/`, record numbers in the results table; the
  honest expectation is +<5% on Docker cells, ~0 on HostCell).
- README: "Sandboxing" section — one paragraph: bounded one-shot cells
  by default, why not a persistent VM, when to integrate forge inside
  an OpenHands/Devin orchestrator instead.
- Update [[Persistent-Sandbox]] status: from
  "no action" to "met, differently — cells; persistent-VM explicitly
  not built, with reasons".

**Total: ~9–11 days** vs ~12–14 d for the Devin-style R6 — and it also
**deletes ~2–3 d** from R16c, so the alternative is net cheaper for the
whole roadmap.

---

## 5. What we intentionally lose (and the honest cost)

| Lost | Real impact | Mitigation hook |
|---|---|---|
| Persistent terminal sessions (long-running dev servers, REPLs) | Rare in issue→PR; background processes are an anti-pattern for repair loops anyway (they're what timeouts exist for) | A cell may run `nohup`-style setups **within** its own lifetime; anything needing to outlive a cell is a design smell we want surfaced |
| In-VM browser research mid-loop | The only genuine Devin capability we drop | `research cell` (bounded fetch, allowlisted egress) covers 80%; full browsing stays out — README says where to go instead |
| Warm-cache VM state across phases (editors open, tmux) | None for a stateless repair loop | Worktrees + baked images carry the *durable* state; caches live in project-scoped volumes (`forge_docker_homes` pattern already proven) |

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Per-cell cold-start cost dominates on huge dep trees | Deps live in the **baked image**, not in the cell; worktree bind-mount is O(repo bytes) on first use, layer-cached after |
| Two runtimes (cells + legacy `docker_env` exec) drift | Cells route through `sandbox.run_test`'s single choke point; legacy path kept behind a flag for one release, then removed |
| `npm install`-style steps still want a mutating, persistent FS mid-bootstrap | During R16c *only*, the scratch configurator container is the long-lived one (existing `get_or_create` semantics); once baked, everything is cells. Two modes, clearly documented, zero overlap in code paths |
| Windows hosts (no bwrap/reflink) | `HostCell` degrades to plain subprocess + rlimits where available; cells feature-gated to unix; documented |

---

## 7. Sequencing change vs [[Plan-R6-R11-R16|plan-r6-r11-r16.md]]

- **R6 Phases 1–4 → replaced** by this doc's Phases 1–5 (cells), with
  R6's Phase 5 (README positioning) unchanged and still mandatory.
- **R16c shrinks**: the commit/rollback design is replaced by
  "bake-then-cells"; scratch container keeps `get_or_create`, everything
  downstream of the bootstrap gate is one-shot.
- Order stays: **R16a → R16b → R11 → R16c(+cells Phase 1) → cells Phases
  2–5**. Cumulative through "everything": **~26–33 days** (down from
  29–37), with the sandbox story ending in a *smaller* attack surface
  instead of a VM to babysit.