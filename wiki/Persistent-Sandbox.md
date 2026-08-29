# R6 — Persistent sandbox with terminal, editor, and browser

**Requirement:** Run in a persistent sandbox environment with its own
terminal, editor, and browser, so multi-step research (docs, deps, running
code) happens autonomously.

**Status in atomic-forge:** Not a goal — forge explicitly scopes to
local/CI execution, not a full virtual desktop; browser access is out of
scope per the README's "What this doesn't try to be" section.

## State of the art

Thin/adjacent literature — what exists benchmarks sandboxes rather than
proposing a design forge should adopt:

- **Training Software Engineering Agents and Verifiers with SWE-Gym**
  ([arXiv:2412.21139](https://arxiv.org/abs/2412.21139)) — a training/eval
  environment built from real GitHub issues (repo + issue + executable
  tests); relevant as an evaluation harness reference, not a sandbox design
  forge needs to replicate.
- **AgentBench** (arXiv:2308.03688) — general LLM-as-agent evaluation across
  environments including OS/database/web tasks; establishes that
  broad-sandbox agents are evaluated on breadth of environment coverage,
  which is explicitly not forge's positioning.

## Implication for atomic-forge

No action recommended. This requirement pulls directly against the README's
stated non-goals ("Not a general production-infrastructure platform"). Including
it in the backlog is more a documentation exercise (know what Devin does and
why forge doesn't) than a real gap to close — flagged again in the Open
Questions section of the top-level `requirements.md` as likely scope creep.

## Implementation plan

**No build phases — this is a documentation/positioning task only.**
- Add a short "why not a sandbox" paragraph to the README's existing "What
  this doesn't try to be" section (mirroring the tone already used there for
  language servers and production infra), explicitly naming Devin/OpenHands
  as the comparison so readers evaluating forge against them get a direct
  answer instead of an omission.
- Revisit only if a specific, named user workflow surfaces that genuinely
  requires it — track such requests as GitHub issues rather than
  pre-building speculative sandbox support.