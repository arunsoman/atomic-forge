*Even excellent results die without easy adoption. Priority order, with the
copy that matters: someone should be able to drop forge into their repo in
under five minutes.*

## 0. Distribution status (what shipped 2026-08-29)

- ✅ **v0.2.0 released** with full notes (release feed + Market-place readiness)
- ✅ **PyPI publish workflow committed** (`publish-pypi.yml`) —
  `pip install atomic-forge` goes live the moment a token is set as the
  `PYPI_API_KEY` secret (or the trusted-publisher path is configured)
- ✅ Repo card: description = positioning line, homepage = wiki, 17 topics
- ✅ README, wiki, Discussions live — the receiving surfaces are ready; the
  remaining 0-distribution gap is publishing + announcing, not building

## 1. GitHub Action (highest leverage)

The `action.yml`/`Dockerfile` at the repo root already is the shipped
integration surface ([[GitHub-Action]] for inputs/outputs). The adoption
goal for it:

- `fix` working with **almost zero config** — API key secret, issue URL, done
- `--dry-run` for safe first contact, **fork-only PRs** as the default
  safety posture, secrets never past our boundary
- **Caching of bootstrap environments** — the `.forge/bootstrap/manifest.json`
  cache ([[Bootstrap-Gate]]) is keyed by HEAD commit; the Action should
  keep the baked sandbox between runs of the same commit
- Two ready-to-ship example workflows (below)

### Example: fix an issue from a comment

```yaml
name: atomic-forge fix
on:
  issue_comment:
    types: [created]

jobs:
  fix:
    if: contains(github.event.comment.body, '@forge fix')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kannamma-labs/atomic-forge@v0.1.0
        id: forge
        with:
          command: fix
          issue-url: ${{ github.event.issue.html_url }}
          api-key: ${{ secrets.FORGE_API_KEY }}
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - name: Fail the job if no PR was raised
        if: steps.forge.outputs.success != 'true'
        run: exit 1
```

### Scheduled backlog cleaning

```yaml
name: atomic-forge backlog
on:
  schedule:
    - cron: "0 3 * * 1"   # Mondays, 03:00 UTC
  workflow_dispatch: {}

jobs:
  backlog:
    runs-on: ubuntu-latest
    strategy:
      max-parallel: 2
      matrix:
        issue: ${{ fromJSON(needs.pick.outputs.issues) }}
    steps:
      - uses: actions/checkout@v4
      - uses: kannamma-labs/atomic-forge@v0.1.0
        id: forge
        with:
          command: fix
          issue-url: ${{ matrix.issue }}
          dry-run: false
          api-key: ${{ secrets.FORGE_API_KEY }}
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Pick the issue list yourself (label query like
`label:"forge:try" state:open`), then let the matrix fan out — forge works
one issue per invocation and never auto-triggers on anything by default
([[GitHub-Action]], "What this is not").

## 2. CLI + library

- The `AtomicTask` contract is already the differentiator — keep the Python
  API dead simple ([[Quickstart]]), advanced options via progressive disclosure
- **Strong local-model support stays first-class** (Ollama, LM Studio, any
  OpenAI-compatible local proxy) — still underserved by the category;
  `--local-only` makes it enforced, not aspirational
  ([[Installation-and-LLM-Setup]])

## 3. Developer-experience polish

- Excellent error messages (the preflight pattern in `fix` is the template:
  name exactly what to set)
- Progress visibility — fused with the checkpoint store
  ([[Checkpointing-and-Resumability]]): "round 2/5, 3 failures, resuming
  from hash ..."
- **"Explain why this patch was chosen" mode** — per-round selection
  evidence (which candidates ran, what passed, why the winner won, what the
  blast-radius gate rejected with what feedback) — it's all already in the
  trajectory; surface it
- Inspectable graph/localization decisions — the code graph already ships
  in-Docker/LM-consumable; add a human CLI view

## Later / optional

- Hosted playground ("paste a GitHub issue URL")
- VS Code / Cursor extension calling the repair engine
- **MCP server so other agents can use forge as a tool** — the CIE backend
  already proves this shape works forge-ward ([[CIE-Integration]]); expose
  forge the same way

## The positioning line

> **"The reliable, test-driven repair engine you can drop under any
> coding agent."**

Stronger than trying to compete as another full agent surface — see
[[How-Is-This-Different]].