*A repo that can't run its own tests is a repo no repair loop can be
honest in. The bootstrap gate closes that hole at the start of every
run. — [[Environment-Bootstrap]]*

## The contract

Before the CIE test-generation or repair logic does anything against a
freshly cloned repo, forge proves:

> *"at least one test in this repo is discoverable and executable"*

by executing the repo's own test runner and reading real evidence from its
output (`collected 23 items`, `test result: 4 passed`, `ok pkg/...`, ...).

Three gate verdicts are checkpointed into the run record ("`bootstrap`"
phase + `BootstrapVerdict`) — a failed gate is durable state, not a lost
print:

| Verdict | Meaning |
|---|---|
| `bootstrapped` | The probe ran to a real end (exit 0/1 with output) |
| `failed_deterministic` | A registered stack was detected but its runner crashed/timed out (pytest's 2/3/4, `make` errors, ...) |
| `unsupported_ecosystem` | No registered stack matched any marker |
| `failed_agentic` | The agentic fallback ran and hit its caps |

An exit code of 0 or 1 counts as a *completed* run — a failing **assertion**
is a working environment; a runner crash or `127` is not, and that is
precisely what the gate exists to catch.

## Deterministic tier: marker-file stack detection

`stacks.py` registers per-ecosystem stacks (detect / test command /
`is_test_file` / Docker image):

| Ecosystem | Detected via | Docker image |
|---|---|---|
| Python | `pytest.ini`/`pyproject.toml`/… | `python:3.12-slim` |
| Node | `package.json` test script | `node:20` |
| Java | Maven `pom.xml` / Gradle | `eclipse-temurin:17-jdk` |
| Go | `go.mod` | `golang:1.22` |
| Rust | `Cargo.toml` | `rust:1-slim` |
| C/C++ | CMake (with test markers) / Makefile `test:` target / Autotools | `gcc:14` |

C/C++ detection deliberately scans *inside* CMakeLists for
`enable_testing`/`add_test`/CTest usage before claiming it — a C++ repo
whose build works but whose tests don't run should fall through, honestly,
to the agentic path (or `unsupported_ecosystem`), not to a silently broken
probe. Meson-only repos are likewise not claimed.

In `fix`, the gate runs on cold clones only (`--project-dir` = a checkout
you've vouched for, skips), honors `--skip-bootstrap` /
`--bootstrap-timeout`, and aborts the run at `stage="bootstrap"` on any
non-`bootstrapped` verdict.

## Agentic fallback (R16c): Repo2Run-style, Docker-only, opt-in

When the deterministic tier finds nothing (or the runner crashes), an
external LLM configurator can try to bring the environment up —
[[Environment-Bootstrap]] and
[arXiv:2502.13681](https://arxiv.org/abs/2502.13681) for the approach.

```bash
export FORGE_ENABLE_AGENTIC_BOOTSTRAP=1   # required; it spends real tokens
atomic-forge fix <issue-url>
```

Properties (each a deliberate design decision):

- **Docker sandbox ONLY.** Without Docker the fallback is a clean
  `unsupported_ecosystem` verdict — the host is never a fallback target.
- **One command per step**, chosen from a fixed prompt contract with a JSON
  response; every step and its output tail is appended to
  `.forge/bootstrap/transcript.jsonl`.
- **Snapshot on success, rollback on failure.** Each successful step is
  `docker commit`ed (last-good image); a failed step rolls the scratch
  container back by *re-creating from the snapshot*, never replaying
  commands.
- **Menu-constrained base image.** One cheap LLM call picks from a fixed
  set (python/node/java/go/rust/c++/unknown → pinned tags). A hallucinated
  image name can never reach `docker pull`.
- **Hard caps.** `max_steps=12`, wall clock 1200 s, 120 s per step — the
  loop cannot run away.
- **Bootstrap cache.** Success writes `.forge/bootstrap/manifest.json`
  keyed by the HEAD commit; a repeat run of the same commit skips the loop
  entirely.
- **Verified success.** The loop only ends in `bootstrapped` when a test
  command actually completes with real output *inside the container* —
  "I think it's set up now" from the model is not accepted.

Tests: [`tests/test_bootstrap.py`](https://github.com/arunsoman/atomic-forge/blob/main/tests/test_bootstrap.py) and
[`tests/test_bootstrap_agentic.py`](https://github.com/arunsoman/atomic-forge/blob/main/tests/test_bootstrap_agentic.py) (fake LLM + scripted Docker boundary).

**Still open:** wiring the bootstrapped image into the repair loop's
execution path — the designed bridge is the "bake-then-cells" mechanism in
[[Plan-R6-Alt-Cells]].