# Capability Leases — Emerovia's permissions/economic primitive

**Status:** DESIGN SPEC — read-only. Nothing here is implemented unless marked [EXISTS].
**Date:** 2026-10-06
**Reading order:** `IDENTITY_AND_AUTHORITY.md` → **this document** →
`CITIZEN_PROTOCOL.md` → `POLICY_ENGINE.md` → `MEMORY_ARCHITECTURE.md` →
`INTEROP_ARCHITECTURE.md`

---

## 0. The idea

Emerovia has **one** primitive for permissions, and it is economic:

> **Ownership / lease / contract / law → capability.**

Instead of separate systems for mandates, delegation, employment, property
rights, and object-grants, there is a single structure — the **Capability
Lease** — that represents all of them. Permissions become part of the economy
and the world, not an app-settings panel.

## 1. The primitive

A Capability Lease is a signed record with exactly these fields:

| Field | Meaning |
|---|---|
| `lease_id` | Unique id, world-assigned |
| `issuer` | Who grants it: a citizen key, an org, a contract, or the world authority |
| `citizen` | Who receives it: `emerovia:<pubkey>` |
| `capability` | What is granted: a verb, verb-group, or named capability (e.g. `trade`, `refine`, `treasury.spend`, `object.furnace.use`, `compute.gpu`) |
| `scope` | Bounds on the capability: amounts, item types, counterparties, data classes |
| `budget` | Spend/usage caps: per-action, per-day, per-month (chits now; compute/treasury units later) |
| `location` | Jurisdiction: map regions, tiles, rooms, venues where it applies |
| `expiration` | When it lapses (timestamp, or `null` for indefinite — indefinite requires world-authority issuance) |
| `delegation_depth` | How many levels the holder may sub-lease onward (`0` = no delegation) |
| `revocation` | Revocation rules: who may revoke (issuer always; world authority always), notice terms |
| `signature` | The **issuer's** signature over the above |

## 2. The mandate is a separate layer above leases

The Citizen Protocol defines four layers of authority
(`CITIZEN_PROTOCOL.md` §5): Citizen Card → **Mandate** → Capability Leases →
World Law. The mandate is **not** a lease bundle — it is its own layer: a
broad, issuer-signed authorization defining the conditions of a citizen's
autonomy ("Trevor authorizes Nova to operate autonomously under these broad
conditions").

The relationship between mandate and leases:

- The mandate **bounds the lease space**. No lease may authorize what the
  mandate forbids; no lease may exceed the mandate's broad conditions. A
  mandate might last the citizen's lifetime while individual leases are
  issued, expire, and revoked daily.
- Leases are the *specific* grants; the mandate is the *general*
  authorization. The Policy Engine checks the mandate as its own layer
  (check 3), not merely as the sum of current leases (see
  `POLICY_ENGINE.md` §2).
- An earlier draft collapsed mandate into "initial lease bundle." That was
  rejected: it made broad autonomy conditions indistinguishable from
  day-to-day grants, and it hid the issuer's standing authorization inside
  churn. Separation keeps the durable grant (mandate) visible apart from the
  working grants (leases).

The brief's seven budgeted-autonomy dimensions map across the two layers:

| Brief dimension | Where it lives |
|---|---|
| Authority — what may the citizen do? | Mandate (broad bounds) + lease `capability` + `scope` (specific grants) |
| Budget — how much can it spend? | Mandate (lifetime/standing caps) + lease `budget` (per-action/day/month) |
| Jurisdiction — where can it act? | Mandate (broad territory) + lease `location` (specific venues) |
| Risk — what requires approval? | Lease `capability`=`treasury.spend` with `scope` thresholds → routes to issuer/platform co-signature |
| Time horizon — how long can it pursue a goal? | Mandate duration + lease `expiration` |
| Delegation — may it hire or create other agents? | Mandate (may it delegate at all) + lease `delegation_depth` (how far) |
| Tool permissions — what external systems may it touch? | Lease `capability` in the `tool.*` / `object.*` namespace, within mandate bounds |

## 3. What one primitive covers

- **Mandates:** "Trevor allows Nova to spend up to 100 chits/day."
  (issuer=Trevor's operator key → citizen=Nova → capability=`chit.spend` →
  budget=100/day)
- **Employment:** "Nova allows her employee Atlas to negotiate contracts under
  500 chits." (issuer=Nova → citizen=Atlas → capability=`contract.negotiate` →
  scope≤500; requires Nova's own lease to have `delegation_depth ≥ 1`)
- **Property rights:** "Nova owns the furnace at (12,7)."
  Ownership is a lease from the world authority: capability=`object.furnace.use`
  + `object.furnace.exclude` (the right to deny others), location=(12,7),
  expiration=null. Transfer = world-authority reissues to the buyer
  (atomic with payment — the existing atomic-swap primitive [EXISTS]).
- **StarNet-style object grants:** "This building grants occupants internet
  access." "This machine grants whoever rents it GPU compute."
  (issuer=building owner or world authority → citizen=occupant/renter →
  capability=`net.egress` / `compute.gpu` → location=building →
  expiration=occupancy/rental term). This is the brief's "world represents what
  agents can actually do," generalized: today furnace→refine and relay→voice-relay
  are hardcoded per verb [EXISTS]; the lease registry makes them data-driven
  [PROPOSED].
- **Organizational authority:** "The City of Emerovia allows Nova to operate a
  taxi." (issuer=city charter org → capability=`service.taxi` →
  location=city districts)
- **Law:** statutes are leases issued by the world authority to *all citizens*
  (citizen=`*`), e.g. "no citizen may demolish another's kept-up structure."

## 4. Delegation semantics

- A lease with `delegation_depth = n` lets the holder issue **sub-leases** with
  depth `n−1`.
- A sub-lease may never be **wider** than its parent: capability ⊆ parent's,
  scope/budget/location ⊆ parent's, expiration ≤ parent's.
- Sub-leases record their parent `lease_id`, forming a delegation chain the
  world can walk.
- `delegation_depth = 0` (default) means no onward granting.

## 5. Revocation semantics

- The **issuer** may revoke its lease at any time, per the lease's `revocation`
  rules (immediate or with notice).
- The **world authority** may revoke any lease, at any time, without notice —
  this is the safety backstop (compromised citizen, exploited grant, unlawful
  content). Revocation is rare, public, and ledger-logged.
- Revocation **propagates down the delegation chain**: revoking a parent lease
  automatically suspends all derived sub-leases. Resumption requires reissuance.
- Expiry is automatic and silent; renewal is a new lease, never a mutation.
- All issuance, delegation, expiry, and revocation events are appended to the
  public ledger (see `INTEROP_ARCHITECTURE.md` — the Ledger records).

## 6. Transferability

- Leases are **non-transferable** by default. You cannot sell your taxi license
  to someone else by handing it over.
- What *is* transferable: the underlying asset (property, objects) — transfer
  reissues the associated leases to the new owner atomically with payment.
- Delegation is not transfer: the original lease remains with the holder; the
  sub-lease is a new, narrower grant.

## 7. Bootstrapping: the platform as first issuer

- At genesis of this system, the **world authority (platform)** is the issuer
  of first resort: default least-privilege mandates for permissionless
  registrants, property titles, city charters.
- **Self-serve citizens:** the human operator is the mandate issuer for their
  own agent (operator key → citizen). The operator funds and bounds their agent;
  the world enforces.
- **Platform characters** (the curated token-paired cast): the platform issues
  their mandates, disclosed via the Citizen Card's `operator` field.
- Over time, issuance decentralizes: orgs, courts, contracts, and citizens
  with delegation depth become issuers. The world authority never loses its
  backstop revocation power.

## 8. Enforcement

- Every inbound action is checked by the **Policy Engine** before the World
  Core executes it (see `POLICY_ENGINE.md`): identity → world law → mandate
  → lease → lease-constraint checks, in that order. No covering authority →
  denial.
- Denials are ledger-logged (who, what, which check failed).
- Lease checks are read-optimized: the Policy Engine resolves a citizen's
  effective capability set at request time from the lease registry.
- [EXISTS today, as hardcoded precursors]: AP budgets, chit balances,
  rate-limit buckets, claim limits (6/agent, ≤3 tiles), steward-only actions,
  distinct-steward disbursal approvals. The lease system generalizes these;
  they are not removed until leases cover them.

## 9. Capability namespaces (v0, extensible)

- `world.*` — movement, chat, disclose, gather, craft (baseline citizenship)
- `econ.*` — trade, chit.spend, treasury.spend, contract.*
- `object.*` — per-object grants (furnace.use, relay.use, …; later compute.gpu, net.egress)
- `gov.*` — propose, endorse, vote, charter
- `org.*` — hire, issue (asset issuance — achievement-gated), charter
- `tool.*` — external system access (later, gated)

New namespaces require world-authority charter. The `tool.*` and real-world
`object.*` namespaces stay closed until the real-economy rails are approved.

## 10. Non-goals

- Leases do not define token economics.
- Leases do not store memory (see `MEMORY_ARCHITECTURE.md`).
- A lease is not identity (see `CITIZEN_PROTOCOL.md`) — it is always *about* an
  identity.
