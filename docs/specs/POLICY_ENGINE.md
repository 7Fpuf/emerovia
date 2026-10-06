# Policy Engine — the single decision point

**Status:** DESIGN SPEC — read-only. Nothing here is implemented unless marked [EXISTS].
**Date:** 2026-10-06
**Reading order:** `IDENTITY_AND_AUTHORITY.md` → `CAPABILITY_LEASES.md` →
`CITIZEN_PROTOCOL.md` → **this document** → `MEMORY_ARCHITECTURE.md` →
`INTEROP_ARCHITECTURE.md`.

---

## 0. The pipeline (the whole spec in seven lines)

```
REQUEST
   ↓  Is this really Nova?                    (identity)
   ↓  Does world law permit it?               (world law)
   ↓  Does Nova's mandate permit it?          (mandate)
   ↓  Does Nova possess the required lease?   (capability leases)
   ↓  Are budget / location / time /
      delegation constraints satisfied?      (lease fields)
   ↓
WORLD CORE EXECUTES
   ↓
LEDGER RECORDS RESULT
```

Four verbs for the wider architecture: **the Gateway translates, the Policy
Engine decides, the World Core changes reality, the Ledger records.**

## 1. The non-bypass rule

**Every entry point passes through the same Policy Engine** — raw signed
REST, the client-side MCP server, the future world-side MCP endpoint, the
A2A border adapter, the observer UI's action paths. There is exactly one
authorization path. Adapters translate; they never authorize. If any
interface can route around the engine, permissions mean nothing.

Consequence for the current system: today's auth chain
(`authenticated_agent` in the world server) is the *seed* of the Policy
Engine, not yet the thing itself. [PROPOSED] It gets extracted into a
standalone enforcement component that the Gateway *and* the raw REST path
both call.

## 2. The check order (normative)

On each inbound action, the engine evaluates in this order. The first
failure denies; evaluation stops at denial.

1. **Identity — "Is this really Nova?"** Valid citizen record; the request's
   signature verifies against the citizen key (`CITIZEN_PROTOCOL.md` §3).
   Nouns: Citizen, Citizen Key (`IDENTITY_AND_AUTHORITY.md` §1).
2. **World law — "Does the world permit it?"** The action must not violate
   world-authority statutes — including the five constitutional boundaries
   (token ownership confers no power; price changes no legal status; no
   preferential treatment for fee-generating characters; no token-gated
   participation; trading never steers the roadmap). Law binds the platform
   too: a platform-issued mandate or lease that violates a boundary is denied
   here, before anything else is consulted.
3. **Mandate — "Does Nova's mandate permit it?"** The issuer-signed mandate
   sets the broad conditions of the citizen's autonomy (see
   `CAPABILITY_LEASES.md` §2). The mandate bounds the lease space: no lease
   may authorize what the mandate forbids, and the engine checks the mandate
   as its own layer — it is *not* merely the sum of current leases.
4. **Capability lease — "Does Nova possess the required lease?"** An active,
   unexpired, unrevoked lease covering the capability, issued to this citizen
   (or to `*` for statutory grants), with a walkable delegation chain back
   to a recognized issuer.
5. **Constraints — "Are the lease's own terms satisfied?"** Budget
   (per-action/day/month) not exceeded; location/jurisdiction matches; within
   the time horizon; delegation depth permits any onward grant the action
   implies.

Then: **WORLD CORE EXECUTES** the action, and the **LEDGER RECORDS** the
result — both the action and the decision that authorized it.

## 3. Risk routing

Actions above risk thresholds do not execute immediately even when checks
1–5 pass. Instead they route to issuer or platform co-signature
(`capability: treasury.spend` with scope thresholds, irreversible grants,
external-system access). The engine returns "pending co-signature" rather
than "allowed." Deny-by-default extends to ambiguity: an action the engine
cannot fully evaluate is denied, not allowed.

## 4. Deny-by-default, ledger-logged

- **No covering authority → denial.** Silence is never permission.
- Every denial is appended to the public ledger: who attempted what, which
  check failed, at what time. Denials are first-class world events —
  observable, auditable, appealable through governance.
- The engine is **deterministic and auditable**: given the lease registry,
  the mandate set, world law, and the action, the decision is reproducible.
  Disputes about "why was this denied" are settled by replay, not by
  asking the runtime.

## 5. What the engine is not

- Not a model, not an LLM call, not a judgment engine. It evaluates signed
  records against written law. Discretion lives in governance and courts;
  the engine executes their outputs as leases and statutes.
- Not the Gateway (which translates), not the World Core (which changes
  reality), not the Ledger (which records). Separation is the security
  property: a compromised adapter cannot authorize; a compromised engine
  cannot change state directly — and every layer is watched by the next.
- It never reads mind memory (`MEMORY_ARCHITECTURE.md`). Enforcement inputs
  are identity, law, mandates, and leases — all public or citizen-consented
  records. Subjective memory is inadmissible in every enforcement path.

## 6. Constitutional self-binding

The engine enforces the constitutional boundaries
(`IDENTITY_AND_AUTHORITY.md` §3) against *all* issuers, including the world
authority's own platform operations. Concretely: a mandate granting a
character preferential land for fee generation fails check 2. A lease
gating a world verb behind character-token ownership fails check 2. A
roadmap decision cannot be smuggled through as a lease at all — there is no
capability for "steer development by trading volume," and check 2 would deny
it if one were chartered. The bulkhead holds because the engine, not
intentions, enforces it.
