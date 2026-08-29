# R15 — Data privacy / no training on private code by default

**Requirement:** Guarantee data privacy / no training on private code by
default.

**Sourced from:** Google Jules.

**Status in atomic-forge:** Depends on the LLM endpoint used — forge is
BYO-endpoint (OpenAI-compatible or local Ollama), so this is a property of
the chosen backend, not forge itself.

**✅ IMPLEMENTED 2026-08-29:** the guarantee is now enforceable, not just
possible. `llm.py::default_llm(local_only=True)` (CLI: `atomic-forge
<phase> --local-only`) resolves the endpoint exactly as before, then
refuses to proceed (`LocalOnlyViolation`) unless the resolved `base_url`
is loopback (`localhost`/`127.0.0.1`/`::1`) or a private-range IP —
conservative by design: a base_url that can't be identified as local is
treated as NOT local, since a false "yes it's local" is the failure mode
that actually matters for a privacy guarantee. `FORGE_MOCK` is exempt (a
Python callable, never network traffic, can't violate this by
construction). Tests: `test_llm.py` (15 cases covering `_is_local_host`'s
loopback/private/public/missing-host matrix, hosted-endpoint rejection,
loopback-endpoint acceptance, the "OpenAI key with no explicit base_url
still defaults to a hosted endpoint" edge case, and that the flag is a
true no-op when unset). Full suite green.

## State of the art

No literature directly bears on this for forge specifically. The nearest
research area — training-data memorization/extraction from LLMs — is a
property of how a *model provider* trains and safeguards data, not
something a client tool like forge can independently guarantee. Not sourced
in depth here since it doesn't change forge's design.

## Implication for atomic-forge

No action item for forge itself: the strongest privacy posture forge can
offer is what it already has — BYO-endpoint, including fully local
inference via Ollama, which structurally sidesteps the question (no data
leaves the user's infrastructure at all when run that way). Worth stating
this explicitly in docs as the answer to "how does forge compare to Jules on
privacy," rather than treating it as an unmet requirement.

## What needs to be done (to beat the competition)

1. **Make the guarantee enforceable, not just possible.** Add a
   `--local-only` CLI flag that refuses to run if the configured endpoint
   isn't localhost/an explicitly allow-listed private host — turns "you
   *can* run fully local" into "the tool *verifies* you are," which no
   competitor surveyed here offers (they all guarantee privacy by their own
   platform policy, not by a client-side check).
2. **Document it as the competitive answer, not a gap.** A single README
   section: "unlike Devin/Jules/Copilot coding agent, forge never requires
   sending code to a hosted service — point it at Ollama and nothing leaves
   the machine" is a stronger, more verifiable claim than any competitor's
   training-policy promise.

## Implementation plan

**Phase 1 — `--local-only` flag (~1 day)**
- In the LLM-endpoint configuration path, add a flag/config option that checks the resolved endpoint host against `localhost`/`127.0.0.1`/an explicit allow-list, refusing to start the run otherwise with a clear error.

**Phase 2 — docs (~0.5 day)**
- Add the README section named above, cross-linking to this requirement's status as "met, and enforced" rather than merely possible.
