## The honest landscape

The generate → compile → test → repair loop itself isn't novel — **aider**,
**SWE-agent**, **OpenHands**, **Devin**, and GitHub's coding agent all do a
version of it. This page says plainly what's the same, what's different,
and where forge deliberately is *not* the right tool.

## At a glance

| | aider | SWE-agent | OpenHands | Devin | **atomic-forge** |
|---|---|---|---|---|---|
| Primary interface | terminal chat | CLI / batch | web UI / canvas | cloud web | **library + CLI + GitHub Action** |
| Task unit | a chat turn | a GitHub issue | a conversation | a cloud session | **`AtomicTask` with a machine-checked test triad** |
| Patch selection | model proposes, you review | agentic trajectory | agentic trajectory | proprietary | **execution-selected: K patches, real suite decides** |
| Static safety gate | — | — | — | — | **blast-radius gate on signatures** |
| Crash-safe resume | git commits | — | events | proprietary | **SQLite phase checkpoint + hash-diff resume** |
| Environment bootstrap | n/a (you're already in the repo) | Docker per instance | Docker sandbox | full VM | **deterministic probe + agentic fallback** ([[Bootstrap-Gate]]) |
| Runs where | your terminal | your machine | your machine / cloud | cloud only | **your machine or your CI** |
| Data leaves your machine? | only if you point it at a cloud LLM | yes (API) | optional cloud | yes | **only if you choose a cloud endpoint; `--local-only` enforces the rest** |

## What forge does that the others don't (as of this writing)

- **The contract, enforced.** A task without a positive/negative/recovery
  triad cannot be constructed, let alone run — the harness makes an
  untestable unit of work *unrepresentable* rather than relying on the
  model's good intentions.
- **Selection by execution, gated by reachability.** The winning patch is
  the one whose test run actually passed — and then it's *still* rejected
  if it silently breaks an external caller's signature.
- **A 7-way verdict taxonomy and a hash-diff resume** that resumes exactly
  where a crashed run stopped, regenerating only what changed on disk.
- **A bootstrap gate before honesty-dependent work** — including an
  opt-in, Docker-only, snapshot/rollback agentic fallback for repos in
  unregistered ecosystems ([[Bootstrap-Gate]]).

## Where you should use one of the others instead

- **Interactive pair programming in an editor** → aider or Cursor. forge
  is not a chat-first pair programmer; there is no TUI conversation.
- **Research on agent–computer interfaces / SWE-bench experimentation** →
  SWE-agent (or its successor mini-swe-agent): purpose-built for that,
  and superb at it.
- **A hosted, always-on team surface with chat and automations** →
  OpenHands Agent Canvas or Devin. forge has no hosted service — by
  design ([[Environment-Bootstrap]], [[Environment-Bootstrap]]).

## What forge deliberately does not try to be

- **Not a language server or embeddings engine.** The bundled
  `ToolBackend`s are exact-for-Python / heuristic-beyond; a richer
  backend (LSL, cross-repo analysis) plugs in behind the same protocol.
- **Not a production-infrastructure platform.** [[Watchdog]] implements
  the canary loop end-to-end with a real local reference implementation,
  but Kubernetes and real load balancers are yours to bring.
- **Not a persistent-VM agent desktop.** Devin-style persistence is
  answered with a different trade (ephemeral execution + git-native
  checkpointing); see [[Environment-Bootstrap]].