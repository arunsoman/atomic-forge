## Install

```bash
pip install git+https://github.com/arunsoman/atomic-forge.git   # one line, no checkout needed
atomic-forge --help                                            # sanity check the CLI
```

Or from a checkout: `pip install -e ".[dev]"` (also installs `pytest` for the suite).
Requires Python ≥3.10. At runtime forge needs an **OpenAI-compatible LLM endpoint** — point it at OpenAI, or a local [Ollama](https://ollama.com) model that supports tool-calling.

## LLM configuration

`default_llm()` resolves, in order:

1. `FORGE_MOCK=1` — use your own zero-network mock (register one via
   `atomic_forge.llm.set_mock_factory(...)`), useful for demos and CI.
2. `FORGE_API_KEY` / `FORGE_BASE_URL` / `FORGE_MODEL` — any OpenAI-compatible
   endpoint: OpenAI itself, a local vLLM/llama.cpp/Ollama proxy, OpenRouter,
   a corporate gateway.
3. `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`/`OPENAI_MODEL`) — the
   common case where you already have this set.
4. Otherwise: raises with a message naming exactly what to set. Never
   silently falls back to a fake key against real `api.openai.com`.

```bash
# OpenAI
export FORGE_API_KEY=sk-... FORGE_BASE_URL=https://api.openai.com/v1 FORGE_MODEL=gpt-4o-mini

# Local Ollama (tool-calling model required)
export FORGE_MODEL=qwen3.5:cloud FORGE_BASE_URL=http://localhost:11434/v1 FORGE_API_KEY=ollama
```

## Privacy: nothing has to leave your machine

`--local-only` refuses to run against a non-loopback/private LLM endpoint —
it enforces the "nothing leaves this machine" claim instead of merely
permitting it. See [[req-data-privacy-no-training]]. Forge never trains on
your code; there is no telemetry.

## Optional companion: CIE

forge's repair loop can use **[CIE — the Code Insight Engine](https://github.com/arunsoman/cie)** as its code-graph backend, served as a real MCP server over stdio (the same surface Claude Code / Cursor consume):

```bash
pip install git+https://github.com/arunsoman/cie.git pytest
```

CIE is **required** for the `fix` pipeline ([[Issue-to-PR]]) and optional everywhere else ([[CIE-Integration]]).