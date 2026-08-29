| Module | What it does |
|---|---|
| `models.py` | `AtomicTask` / `AtomicTaskBatch` — the task contract |
| `decompose.py` | Optional LLM-assisted draft of `AtomicTask` JSON from a loose spec — same contract enforced, human review still required |
| `planner.py` | Dependency-ordered execution planning (Kahn topological sort) |
| `agent.py` | The agentic session loop (TOOL / RUN / PATCH / SUBMIT grammar, or real function-calling) |
| `llm.py` | `ChatLLM` protocol + `OpenAICompatLLM` + provider resolution |
| `tools.py` | `ToolBackend` protocol + `LocalToolBackend` + `GraphToolBackend` (+ `statement_graph`; bring your own richer backend) |
| `codegraph.py` | Persisted SQLite call graph — symbols, edges, and statement-level def-use tables, incrementally rebuilt |
| `graph_statements.py` | Statement-level def-use extraction ([[Statement-Level-Graph]]) |
| `symbols.py` | The dependency-free symbol index behind `LocalToolBackend` and `codegraph.py`'s parsing |
| `patch.py` | The one canonical SEARCH/REPLACE parser |
| `generator.py` / `generate_agent.py` | Prompt building + the agentic/batch generation pipeline (including the direct multi-file-in-one-completion fast path for independent, dependency-free tasks) |
| `qa.py` | Synthesizes a test file per `test_triad`, gap-filling coverage |
| `repair_agent.py` / `repair.py` | The repair loop: signals → localize → sample → select → gate ([[Repair-Loop]]) |
| `bootstrap.py` | R16 environment gate + Repo2Run-style agentic fallback ([[Bootstrap-Gate]]) |
| `watchdog.py` | Production loop: detect a live failure → repair → canary → promote/rollback ([[Watchdog]]) |
| `pr.py` | Raise a GitHub PR for a landed fix (`atomic-forge repair --raise-pr`, via `gh`) |
| `cie_backend.py` | Optional CIE-as-MCP-server `ToolBackend`; required by `fix` ([[CIE-Integration]]) |
| `testgen.py` | CIE-grounded regression-test generation + oracle validation (fails-on-buggy gate) |
| `issue.py` | Issue URL parsing, `gh` fetch, shallow clone, install setup for `fix` |
| `fix.py` | `atomic-forge fix <url>` — one-shot issue → regression test → repair → fork-only PR ([[Issue-to-PR]]) |
| `sandbox.py` / `docker_env.py` / `stacks.py` | Command execution, git, lint gate, test-stack detection (6 ecosystems), optional Docker sandboxing |
| `concurrency.py` | The adaptive rate-limit-aware worker pool |
| `checkpoint.py` / `checkpoint_store.py` | Crash-safe, resumable run state (SQLite) ([[Checkpointing-and-Resumability]]) |
| `trajectory.py` | Append-only JSONL audit trail of every action taken |
| `reporter.py` | Write-back protocol for task status/artifacts (bring your own backend) |

Protocol extension points (each has at least one real reference
implementation): `ToolBackend`, `FailureDetector`, `DeployTarget`,
`Reporter`, `RepoStack`.