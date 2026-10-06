# OSS Register — supply-chain and architectural governance

**Status:** DESIGN SPEC — read-only. Nothing here is implemented unless marked [EXISTS].
**Date:** 2026-10-06
**Reading order:** `IDENTITY_AND_AUTHORITY.md` → `CAPABILITY_LEASES.md` →
`CITIZEN_PROTOCOL.md` → `POLICY_ENGINE.md` → `MEMORY_ARCHITECTURE.md` →
`INTEROP_ARCHITECTURE.md` → **this document**

---

## 0. Why this exists

The biggest open-source risk to Emerovia is not someone stealing the idea.
It is accidentally turning Emerovia into a pile of external projects whose
licenses, maintainers, security boundaries, and architectural assumptions
control us.

This register is the governance layer for that risk. Rule:

> **No third-party code reaches production without a recorded license,
> exact version, rationale, integration owner, and replacement strategy.**

[EXISTS] The seed evaluations live in `../emerovia-architecture-analysis.md`
§8. [PROPOSED] This register becomes the maintained, canonical record;
the analysis doc stays as history.

## 1. Register entry schema

Every dependency — library, protocol SDK, runtime adapter target, or
reference codebase — gets one entry:

```yaml
project: "example-lib"
upstream_repo: "https://github.com/org/example-lib"
pinned_version: "1.30.0"          # exact version or commit SHA, never "latest"
license: "MIT"                    # SPDX identifier; see §2 posture table
files_used: ["src/client.py"]     # which parts we touch, if selective
transitive_deps: ["..."]          # audited, not assumed
role: "library"                   # library | protocol-sdk | runtime-adapter-target | reference-only
integration_owner: "mini"         # who keeps this entry current
upstream_health: "active, 3 maintainers, monthly releases"
cves_watched: true

decision: "depend"                 # study | integrate | depend | reuse | fork | reject — and why
decision_rationale: "..."

failure_mode:
  upstream_disappears: "existing Emerovia world unaffected because ..."
  api_breaks: "adapter disabled; citizens on other runtimes unaffected"
  security_incident: "connector revoked; Policy Engine path unchanged"

replacement:
  protocol: "MCP"                 # what the replacement speaks, if any
  alternatives: ["alt-a", "alt-b"]
  effort_class: "S"               # S/M/L — how bad is a forced swap
```

## 2. License posture (engineering guidance, not legal advice)

| License class | Emerovia posture |
|---|---|
| MIT / BSD / Apache-2.0 | Usually safe candidate for commercial reuse with required notices; still audit transitive dependencies and assets |
| MPL-2.0 | File-level copyleft: modified covered files stay MPL; a larger work can use different terms for other files. Selective use possible — keep clear file boundaries |
| GPL (v2/v3) | Prefer architecture study and clean-room reimplementation, or separately deployed components. **Never copy into the proprietary core** without legal review of the specific distribution model and component boundary |
| AGPL | Network use can trigger source-availability requirements. Legal review mandatory before any use |
| CC assets (BY / BY-SA) | Check each asset; BY requires attribution, BY-SA imposes share-alike on adaptations |
| "Source available" / noncommercial | Do not treat as open source or commercially reusable without reviewing exact terms |
| No license | **No permission to reuse code** until clarified |

Concrete rulings already made [EXISTS as decisions, PROPOSED as enforcement]:

- **Godot (MIT):** candidate for direct integration as a *client* (observer/visualization). Authoritative simulation stays server-side.
- **Unciv (MPL-2.0):** schemas/patterns adaptable; selective code reuse legally easier than GPL but probably not worth the Kotlin/LibGDX coupling.
- **Freeciv, OpenTTD (GPL-2.0):** study algorithms and architecture; reimplement Emerovia-native. Do not transplant into the core.
- **0 A.D. (GPL-2.0-or-later engine; CC BY-SA art/audio):** architecture laboratory only — territory, components, pathfinding patterns. Wholesale borrowing is unattractive.
- **Repository license ≠ every artifact's license.** Audit bundled third-party components and assets individually (Godot's own copyright inventory is the cautionary example).
- **"MIT" is not the end of license review.** Example: StarNet's code is MIT, but its name, logo, station art, and sprites are explicitly reserved — derivative products must rebrand. Study its capability metaphors; integrate its protocol surface; **never copy its brand or art into Emerovia** (see also `UI_FEEL_DIRECTIVE.md`).

Before shipping copied GPL/MPL code in a commercial architecture, or before
any token/wallet/mainnet work: **legal review.** This document is an
engineering assessment, not legal advice.

## 3. The three-alternatives rule [PROPOSED]

> **No major generic subsystem begins implementation until this register
> documents at least three alternatives — or explicitly states that no
> credible alternative exists.**

For each candidate, record the verdict:

```text
Study?      read it, learn the pattern, write our own
Integrate?  bring the code inside our boundary (license permitting)
Depend?     use it as an external library with a pinned version
Reuse?      take the pattern, not the code
Fork?       almost never — only if strategically essential and upstream is gone
Reject?     wrong fit, wrong license, wrong trust model — say why
```

This does not mean "always use open source." It means **never reinvent
blindly.** The standing preference order is:

```text
Integrate > Depend > Reuse patterns > Fork
```

kept operational by the recorded rationale, not aspirational.

## 4. Adapter failure-mode principle [PROPOSED]

A runtime adapter must never be existential:

```text
Adapter breaks
→ that runtime temporarily cannot enter Emerovia.

NOT

Adapter breaks
→ Emerovia breaks.
```

Every adapter entry's `failure_mode` block (§1) must show this holds.
Consequences:

- Adapters live outside the World Core and talk through the Gateway →
  Policy Engine path like every other client (`INTEROP_ARCHITECTURE.md` §0).
- No world-semantic dependency on any runtime: no external project decides
  what a citizen, property right, company, law, or transaction *means*.
- Revocation is unilateral: Emerovia can disable a connector without the
  runtime's cooperation.

## 5. Protocol churn rule [PROPOSED]

Protocols move (MCP's 2026-07-28 spec made breaking changes: stateless core,
per-request negotiation, authorization rework, deprecations).

- **Pin the protocol version.** Emerovia declares `MCP compatibility:
  2026-07-28` (see `INTEROP_ARCHITECTURE.md` §5), never "whatever the SDK
  currently does."
- Maintain a conformance suite; upgrades are deliberate, never HEAD-chasing.
- The same discipline applies to A2A when the adapter lands.

## 6. Seed entries [EXISTS as usage, PROPOSED as registered]

To be completed with exact SHAs and owners; current known surface:

| Project | Role | License | Notes |
|---|---|---|---|
| `mcp` (Python SDK) | protocol-sdk | Apache-2.0 | Pinned `<2` in `emerovia-mcp` (2.x broke wrappers, Jul 2026). Pin exact version per §5 |
| `PyNaCl` | library (ed25519) | Apache-2.0 | Identity primitive; narrow interface, replaceable |
| `fastapi` / `uvicorn` | library (world server HTTP) | MIT / BSD-3 | Server transport only; no world semantics |
| `httpx` | library (client HTTP) | BSD-3 | Resident/SDK transport |
| `pytest` | dev-only | MIT | Never ships to production |

Runtime adapter targets (reference/adapter only, never world-side
dependencies): StarNet (MIT code; brand/art reserved), Hermes (MIT),
Agent Zero (MIT), Letta Code (Apache-2.0), elizaOS (MIT — token-history
caveats noted in analysis §8), OpenHands SDK (MIT), LangGraph (MIT).

Game-system references (study/reuse-patterns only): Freeciv, Unciv, OpenTTD,
0 A.D., Godot — see §2 rulings.

## 7. What exists vs. what is proposed

| Item | Status |
|---|---|
| OSS evaluations in `emerovia-architecture-analysis.md` §8 | [EXISTS] — seed |
| This register as the maintained canonical record | [PROPOSED] |
| Three-alternatives rule enforced on new subsystems | [PROPOSED] |
| Pinned protocol-version declarations with conformance suites | [PROPOSED] |
| Per-dependency failure-mode blocks | [PROPOSED] |
