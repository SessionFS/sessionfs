# Licensing + Org-Management Redesign — Synthesis & Review Routing

**Status:** Design complete (Atlas binding + Compass product). Pending review by Codex (architecture/correctness), Ledger (billing), Shield (compliance), Sentinel (authz). **No code until reviews clear + CEO signs the open decisions.**

Source docs:
- `licensing-org-redesign.md` — Atlas (data model, endpoints, migration, Sentinel checklist)
- `licensing-org-redesign-product.md` — Compass (commercial model, UX, rollout, risks)

---

## The problem (both docs, code-verified)

Entitlement lives in **three disconnected fields** — `User.tier`, `Organization.tier`, `HelmLicense.tier` — with no reconciliation. A `HelmLicense` **does not create or link to an `Organization`**, and self-service org creation (`POST /api/v1/org`) gates on `User.tier`, which an enterprise license holder never gets bumped to. Net effect, the headline bug: **an enterprise license holder ends up with no org → no `OrgMember` → the dashboard Organization surface (gated on `default_org_id`) never appears → they cannot administer their own enterprise.**

## Where the two docs AGREE (treat as settled, pending review)

1. **Headline fix = self-service license activation.** A customer redeems their license key (`/activate?key=…`) → an Organization is created → they become its **owner** — no SessionFS staff in the loop. Admin-assisted pre-provisioning kept as fallback for sales-led deals.
2. **Add an `owner` role** above `admin` (level 100/50/10). Exactly one owner; owner immutable by admins; ownership transfer is **two-step** (owner initiates, target admin accepts). Reuse/extend `perform_role_change` + `perform_member_removal` and their `SELECT FOR UPDATE` last-admin guards.
3. **Fix the dashboard access chain:** `/me` returns effective tier + `org_id`/`org_role`; sidebar gates on real `OrgMember` membership, not `default_org_id`.
4. **Additive migration**, old tier columns retained as denormalized caches, full backfill, reversible, no data loss. Backfill org creators → `owner`. Backfill existing `HelmLicense` rows → orgs (match by org_name + contact_email; unmatched → pending claim).
5. **License lifecycle:** 30-day expiry warning → grace period → degrade to free (data preserved, read-only), not destroyed.
6. **Same review flags:** Stripe specifics → Ledger; compliance/audit → Shield; activation trust boundary + privilege escalation → Sentinel.

## The ONE real divergence — needs a decision

**Where does the single source of truth live?**

| | **Atlas — new `entitlements` table** | **Compass — columns on `Organization`** |
|---|---|---|
| Shape | New polymorphic table (`owner_type` user\|org, `source`, `source_ref`, tier/seats/expiry/status) | Add `entitlement_source`/`entitlement_ref`/`entitlement_expires_at` to `Organization`; resolve through org |
| Solo paid users | First-class (`owner_type='user'`) | Stay on `User.tier` (org-only entitlement container) |
| Multi-org future | Supported by the model | Not modeled |
| Migration weight | Heavier (new table + denorm pointers) | Lighter (3 columns + backfill) |
| Stripe⊕license | Naturally separable rows | Compass adds explicit **mutual-exclusivity** rule (an org is Stripe-billed XOR license-entitled) |

**My recommendation:** Atlas's `entitlements` table. It is the cleaner single-source-of-truth, makes expiry/status first-class for *both* Stripe and Helm, and doesn't paint us into a corner on solo-paid or multi-org. Compass's lighter approach ships faster but re-couples entitlement to the org and leaves solo-paid on a separate path — i.e. it doesn't fully kill the fragmentation it's trying to fix. **Adopt the entitlements table; adopt Compass's mutual-exclusivity invariant as a constraint on it; adopt Compass's product/UX surface wholesale.** This is the call I want Codex to pressure-test.

## Open decisions for CEO sign-off (recommendation in **bold**)

- **OD-1 Owner role?** → **Yes** (both docs agree).
- **OD-2 Entitlements table vs Organization columns?** → **Entitlements table** (the divergence above; Codex to verify).
- **OD-3 Self-service activation vs admin-only?** → **Self-service** with admin fallback (both agree).
- **OD-4 SaaS/Helm unification point?** → **The entitlements table** (Ledger owns the Stripe→entitlement sync).
- **OD-5 Multi-org membership?** → **Defer** (keep one-user-one-org; model leaves the door open).
- **OD-6 Rename `HelmLicense`→`License`?** → **Defer** to implementation (FK churn; keep name + comment if it complicates migration).
- **Product cleanup Compass surfaced:** pricing is inconsistent across `docs/pricing.md` ($12/$20) vs dashboard ($14.99) vs `tiers.py` (no prices); a phantom **"business"** tier appears in README/docs but never in code. → **Dashboard prices canonical; drop "business"; add Starter to pricing page.** (Ledger confirms Stripe price IDs.)
- **Org deletion cascade** (Compass OQ-3): projects → owner's personal scope, sessions preserved, KB destroyed. → **Confirm with Shield.**

## Review routing (this is the "kick off the review" step)

| Reviewer | Scope | Lens |
|---|---|---|
| **Codex** | Both docs, esp. OD-2 (table vs columns), backfill edge cases, race-free activation, single-query resolution, owner-guard composition with last-admin guards | architecture/correctness |
| **Ledger** | Stripe↔entitlement sync contract, seat self-service bounds, price-ID reconciliation, past_due semantics | billing |
| **Shield** | Entitlement audit trail, activation PII/consent, org-deletion cascade, SIEM export, retention | compliance |
| **Sentinel** | License-key forgery/replay/theft, activation rate-limit, owner-transfer + privilege-escalation, cross-org entitlement isolation, deactivated-last-owner | authz (full checklist in Atlas §4) |

**Gate:** all four clear + CEO signs OD-1..OD-6 before any P1 code. Phasing (Atlas §3.3): P1 model+migration → P2 resolution switch+`/me`+sidebar → P3 activation → P4 owner enforcement → P5 self-service seats → P6 expiry+safety sweeps.
