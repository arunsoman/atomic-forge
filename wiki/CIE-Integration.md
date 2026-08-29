## What CIE is

**[CIE — the Code Insight Engine](https://github.com/arunsoman/cie)** is a
separate code-graph engine by the same author. forge can consume it as its
code-graph backend, served as a real **MCP server over stdio** — the same
way Claude Code / Cursor consume tools. Division of labor:

| | Responsibility |
|---|---|
| **CIE** | Localization + blast radius: `callers`, `affected_by`, `failing_context`, `file_skeleton`, … |
| **forge** | draft tasks → generate → test → sample → select → gate → commit |

forge itself stays unchanged — CIE is another `ToolBackend`, exactly like
the bundled `local`/`graph` backends ([[Architecture]]).

## Install + one-line benchmark

```bash
pip install git+https://github.com/arunsoman/cie.git pytest
python benchmarks/cie_forge_realbugs/forge_cie_bench.py boltons_bits_offbyone
```

Needs a tool-calling model at `http://localhost:11434/v1` (Ollama default);
override with `FORGE_MODEL` / `FORGE_BASE_URL` / `FORGE_API_KEY`.

## What it changes

- **Localization & blast radius come from a real index** — multi-hop
  `callers`/`affected_by`, `failing_context`, `file_skeleton` — instead of
  the built-in per-process index. Measured effect: the same agent with CIE
  fixed a planted bug in ~63% fewer tokens; without the graph it broke the
  suite and did not converge ([[Benchmarks]]).
- **CIE generates regression tests** grounded in *real signatures* —
  `testgen.py` then validates the oracle: the test must fail on the buggy
  code (assertion, not import error) before repair starts
  ([[Issue-to-PR]]).

## Where it's required vs optional

- `fix` / `fix-comment` ([[Issue-to-PR]]): **required**.
- `run` / `repair` ([[Repair-Loop]]): optional; the built-in
  `LocalToolBackend`/`GraphToolBackend` work standalone.

Methodology and raw numbers: [[Benchmarks]]; harness in
[`benchmarks/cie_forge_realbugs/`](https://github.com/arunsoman/atomic-forge/tree/main/benchmarks/cie_forge_realbugs).