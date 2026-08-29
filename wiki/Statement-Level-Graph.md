*Fixing a bug inside a 1,600-line function needs to know which *statements*
read a variable and which wrote it — not just which files mention a symbol.
[[Environment-Bootstrap]] (R11)*

## What shipped

`graph_statements.py` extracts a **statement-level def-use graph**,
additively stored in the existing code-graph database alongside
`files`/`symbols`/`edges` (`.forge/codegraph.db`):

- `statements` — one row per assignment / augmented assignment /
  annotated assignment / `for` target / `with` binding / `def`/`class`,
  with file, line, column, enclosing symbol, kind, and
  `engine` ∈ {`exact`, `heuristic`}.
- `def_use` — directed edges: the statement at `(file, line)` *reads from*
  and *writes to* which definitions.

Confidence is honest, not cosmetic:

- **Python is `ast`-exact.** Scoping is real (globals, nonlocals, nested
  functions, comprehension scopes), and shadowing follows true semantics —
  because resolution happens *before* a scope's own definitions register,
  `x = x + 1` reads the **previous** `x`.
- **Non-Python files get `block` rows** with `engine="heuristic"` and *no*
  def_use edges — a visible shape for future work rather than silently
  wrong edges.

## Accessing it

```python
from atomic_forge.codegraph import CodeGraph

g = CodeGraph(project_dir).build()        # incremental; content-hashed
g.statements_near("app/handlers.py", 412, radius=15)
# -> statements defining / reading around line 412
g.uses_of("retry_budget", "app/config.py")
# -> every place that reads that name
```

And as an agent tool (auto-surfaced in the tool manifest, both backends):

```
statement_graph(file="app/handlers.py", line=412)
```

Enabled by default; disable with `FORGE_STATEMENT_GRAPH=0` for huge repos
where the extra pass isn't wanted.

## Why: the long-function localization problem

The repair loop's localization used to stop at function granularity —
useful, but in a long function it can't say *"line 412 reads
`retry_budget`, whose only writer is line 238"*. The REPAIR_SYSTEM prompt
teaches the agent to use `statement_graph` for exactly that
second-pass localization, and the evidence chain (traceback frame →
symbol → statements) is recorded in the trajectory.

## Measurement note (honest)

The design point is the ARISE-style improvement (statement-level
localization over multi-hop dependency views,
[arXiv:2605.03117](https://arxiv.org/abs/2605.03117)). The *delta on
forge's own benchmark cases with a live LLM* is measured in Phase 3 — the
graph itself, its semantics, and its tests
([`tests/test_graph_statements.py`](https://github.com/arunsoman/atomic-forge/blob/main/tests/test_graph_statements.py))
are shipped and green. See [[Benchmarks]] for what's measured today.