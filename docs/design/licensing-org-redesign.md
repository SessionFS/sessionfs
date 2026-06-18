# Binding Design — Licensing + Entitlement + Org-Management Redesign

**Author:** Atlas (backend/data-model)
**Branch:** `design/licensing-atlas` (worktree off develop, NEVER push)
**Status:** Design proposal — pending CEO review + Codex + Ledger + Shield + Sentinel
**Companion doc:** Compass writes `docs/design/licensing-org-redesign-product.md` (product/UX/billing shape)

---

## §1 — Current-State Audit

Every claim is grounded in real code. File paths are relative to repo root.

### 1.1 Three disconnected entitlement fields — no single source of truth

| Field | Default | Where used | Code |
|-------|---------|-----------|------|
| `User.tier` | `"free"` | Solo user tier, `/me` response, `require_admin` guard, org creation gate, dashboard tier badge | `src/sessionfs/server/db/models.py:26` |
| `Organization.tier` | `"team"` | Effective tier for org members (via `get_effective_tier`), inherited from creator's User.tier at org create time | `src/sessionfs/server/db/models.py:692` |
| `HelmLicense.tier` | `"enterprise"` | Self-hosted license validation ONLY — never linked to an Organization or User | `src/sessionfs/server/db/models.py:875` |

**Resolution chain (solo users):**
`User.tier` → `get_effective_tier()` (at `tier_gate.py:42-67`) → features + storage + tier gating.

**Resolution chain (org members):**
`OrgMember.user_id` lookup → `Organization.tier` → `get_effective_tier()` → features + storage + tier gating. The member's own `User.tier` is *never consulted* for org members.

**Resolution chain (HelmLicense holders):**
`HelmLicense.tier` → license validation (self-hosted Helm chart) → *nothing else*. No connection to Organization, User, or the tier_gate path.

**The fragmentation is structural:** `User.tier`, `Organization.tier`, and `HelmLicense.tier` are three independent columns with no reconciliation logic, no cross-table constraint, and no single resolution point. There is no `entitlement` or `subscription` record that unifies them.

### 1.2 Headline bug: Enterprise license holders cannot see org management

The causal chain, validated against real code:

1. **A `HelmLicense` does NOT create or link to an `Organization`.**
   - `POST /api/v1/admin/licenses/` (`admin_licenses.py:93-121`) creates a `HelmLicense` row with `org_name`, `tier`, `seats_limit` — but NO `Organization` row is created, and NO `org_id` FK exists on `HelmLicense`.
   - `POST /api/v1/admin/orgs` (`admin.py:442-545`) creates an `Organization` with `owner_user_id` — this is a **separate** admin-only endpoint. The two surfaces are never bridged.

2. **Self-service org creation requires `User.tier` to be team/enterprise/admin.**
   - `POST /api/v1/org` (`org.py:86`) checks `user.tier not in ("team", "enterprise", "admin")` — it checks the **User** record, not any effective tier, and not any HelmLicense. An enterprise license holder whose `User.tier` is still `"free"` gets a 403.

3. **No org → no OrgMember → dashboard Organization surface is hidden.**
   - `GET /api/v1/org` (`org.py:173-200`) returns `org=None, members=[], current_user_role=None` when the user has no `OrgMember` row.
   - `OrgPage.tsx:89-100` renders "No Organization" when `data.org` is null.
   - `Layout.tsx:175` gates the sidebar Organization link on `hasOrg = !!me.data?.default_org_id` — a field that must be explicitly set by the user.

4. **`/me` returns `User.tier`, not effective tier.**
   - `GET /api/v1/auth/me` (`auth.py:49`) returns `"tier": user.tier` — the personal tier, not the org tier. The dashboard's tier badge (`Layout.tsx:251`) shows this value.

**Result:** Enterprise customer buys a license → receives a `HelmLicense` row created by a SessionFS platform admin → license validates on their self-hosted cluster → but `User.tier` stays `"free"` → can't self-create org → no Organization row exists → no OrgMember → dashboard shows "No Organization" → **enterprise customer cannot see org management**.

### 1.3 What DOES work: self-administration once an org+admin exists

The org_members.py routes (`/api/v1/orgs/{org_id}/members/*`) have strong self-administration:

- **Roles:** `admin` / `member` (`roles.py` — `OrgRole` enum, scalar levels)
- **Promote/demote:** `PUT /members/{user_id}/role` → `perform_role_change()` (`org_members.py:575-669`) — self-role blocked, last-admin demotion blocked with `SELECT FOR UPDATE` on all admin rows
- **Invite:** `POST /members/invite` — seat-limit check, UPSERT-in-place for stale rows, best-effort email
- **Remove:** `DELETE /members/{user_id}` → `perform_member_removal()` (`org_members.py:732-925`) — 5 CEO invariants: projects auto-transfer to admin, default_org_id cleared, pending transfers cancelled (incoming + outgoing), last-admin removal blocked with `SELECT FOR UPDATE`
- **Resend invite:** `POST /invites/{invite_id}/resend` — re-fires email, refuses accepted/declined/expired

This surface is well-built and the redesign preserves and extends it, not rebuilds it.

### 1.4 Additional gaps

| Gap | Detail | Code evidence |
|-----|--------|--------------|
| One-user-one-org constraint | Both `POST /api/v1/org` (`org.py:97-101`) and `POST /api/v1/admin/orgs` (`admin.py:494-499`) check `OrgMember WHERE user_id = ...` and reject if found. A user can belong to exactly ONE org. | `org.py:97-101`, `admin.py:494-499` |
| No org-level tier/seat self-upgrade | `PUT /api/v1/admin/orgs/{org_id}/tier` (`admin.py:548+`) is admin-only. Org admins have no self-service path to change tier or seats. | `admin.py:548` (entire route under `require_admin`) |
| Orphaned-admin risk on user deletion | `User.is_active` can be set to false — but there's no sweep that checks if the deactivated user is the last admin of any org. The last-admin guard on remove/demote only fires on explicit removal, not on user deactivation. | `models.py:33` (`is_active`), no cross-check against `OrgMember` |
| `HelmLicense` has no SaaS connection | `HelmLicense` is self-hosted-only; SaaS orgs use `Organization.tier` + `Organization.stripe_subscription_id`. Two separate licensing "worlds" with no shared abstraction. | `models.py:868-886` (no `org_id` column) |
| `require_admin` is user.tier-based, not role-based | `require_admin` checks `user.tier == "admin"` — platform-global, not org-scoped. There's no concept of "org admin with admin-tier privileges over their own org." | `auth/dependencies.py:373-379` |
| Stripe subscription and HelmLicense are disconnected | A customer migrating from SaaS (Stripe → Organization.tier) to self-hosted (HelmLicense) has no path to convert or reconcile the two entitlement records. | N/A — entire flow is missing |

---

## §2 — Target Model

### 2.1 One source of truth: the `Entitlement` record

**Recommendation:** Introduce an `entitlements` table as the single source of truth for what a customer is entitled to. Every existing entitlement field (`User.tier`, `Organization.tier`, `HelmLicense`) resolves through it.

```
entitlements
├── id (PK)
├── owner_type: "user" | "org"
├── owner_id: users.id | organizations.id (nullable — license can be unbound)
├── source: "stripe" | "helm_license" | "manual" | "admin_provisioned"
├── source_ref: stripe_subscription_id | helm_licenses.id | NULL (admin manual)
├── tier: "free" | "starter" | "pro" | "team" | "enterprise"
├── seats_limit: int (nullable — NULL = unlimited/default)
├── storage_limit_bytes: int (NULL = tier default)
├── status: "active" | "past_due" | "canceled" | "expired" | "revoked"
├── current_period_start: datetime
├── current_period_end: datetime (NULL = never expires / perpetual)
├── created_at, updated_at
```

**Resolution rule** (replaces `get_effective_tier`):
1. If the user is an org member → resolve `entitlements WHERE owner_type='org' AND owner_id=org.id AND status='active'`.
2. Else → resolve `entitlements WHERE owner_type='user' AND owner_id=user.id AND status='active'`.
3. Fallback: `tier='free', seats=1` (pessimistic safe default).

**Why this over reconciling existing fields:**
- A single table means a single query for tier/features/seats/expiry. Today's resolution is two queries + conditional logic (`tier_gate.py:42-67`).
- Entitlement expiry is first-class. Today, only `HelmLicense` has an `expires_at`; Stripe subscriptions don't flow through to the DB as entitlement records.
- Audit is trivial: every entitlement change is a row mutation with timestamps.
- It unifies SaaS (Stripe) and self-hosted (HelmLicense) into one model — they differ only in `source` and `source_ref`.

**⚠️ Ledger flag:** The `source: "stripe"` path, Stripe webhook → entitlement reconciliation, and billing-specific fields (invoice, payment method) are Ledger's domain. This design defines the data contract; Ledger owns the Stripe integration and billing logic.

### 2.2 License → Organization: self-service activation

**The problem:** An enterprise gets a license key but no Organization, and can't create one because `User.tier == "free"`.

**Design: License activation flow**

A new endpoint `POST /api/v1/org/activate` accepts a license key (the same `HelmLicense.id` / license key format) and:

1. **Validate the key:** Look up `HelmLicense WHERE id = :key AND status = 'active' AND (expires_at IS NULL OR expires_at > now())`.
2. **Create an Organization** from the license's `org_name`, with tier/seats from the license.
3. **Create the Entitlement** record (`source='helm_license'`, `source_ref=key`, `owner_type='org'`, `owner_id=<new org>`).
4. **Add the activating user as OrgMember with role='admin'**.
5. **Mark the license as bound** (new column `HelmLicense.org_id` — nullable FK to `organizations.id`).

This is **self-service** — no SessionFS platform admin in the loop. The license key is the provisioning credential. One license → one org (first-to-activate wins; subsequent attempts get 409 "license already bound").

**Existing admin-provisioned path preserved:** `POST /api/v1/admin/orgs` remains as the back-office fallback for pre-sales provisioning and the SessionFS company org.

**For SaaS (Stripe) customers:** `POST /api/v1/org` (the existing endpoint) already creates an org from a Stripe subscription. In the target model, it also creates an Entitlement row. The `User.tier` check at `org.py:86` is REPLACED by a check against the user's (or new org's) Entitlement — so a user whose entitlement comes from a redeemed license can create an org even if `User.tier == "free"`.

### 2.3 Org-management access fix

Three changes ensure an enterprise customer reliably sees the management surface:

1. **`GET /api/v1/auth/me` includes effective org info.** Add `effective_tier`, `org_id`, `org_name`, `org_role` fields to the `/me` response (or add a separate lightweight `/me/org` endpoint — Atlas preference: add to `/me` so the dashboard makes one call at boot). The dashboard `useMe()` hook then populates `hasOrg` from actual OrgMember membership, not from the nullable `default_org_id`.

2. **Dashboard sidebar filters on `org_id`, not `default_org_id`.** `Layout.tsx:175` changes from `hasOrg = !!me.data?.default_org_id` to `hasOrg = !!me.data?.org_id`. The `orgOnly` filter works correctly.

3. **`OrgPage` handles the no-org case with a clear CTA.** The existing "No Organization" page (`OrgPage.tsx:89-100`) already shows CLI instructions. In the target model, it also shows a "Redeem License" button when the user has a pending/unbound license (discoverable via a new `GET /api/v1/licenses/mine` or by returning pending licenses in `/me`).

### 2.4 Org-management redesign (data/contract side)

#### 2.4.1 Roles: introduce `owner`?

**Recommendation: YES — add `owner` as a distinct role above `admin`.**

Rationale:
- Today, the first admin IS the owner in practice (they created the org, they passed the tier gate, their Stripe subscription funded it). But there's no durable record of this — any admin can demote any other admin (except the last one).
- An `owner` role that is **immutable by other admins** prevents hostile takeovers. Only the owner can transfer ownership.
- Mirrors GitHub's org model (owner vs. member) and is expected by enterprise buyers.
- The `OrgMember.role` column already stores strings; adding `"owner"` is additive.

**Role hierarchy:**
```
owner  (100) — create org, manage billing, transfer ownership, delete org, all admin powers
admin   (50) — invite/remove members, promote/demote members (not owner), manage projects, manage settings
member  (10) — view roster, access org projects, use org features
```

**`OrgRole` enum update** (`roles.py`):
```python
class OrgRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

ROLE_LEVEL = {
    OrgRole.MEMBER: 10,
    OrgRole.ADMIN: 50,
    OrgRole.OWNER: 100,
}
```

**Owner-specific rules:**
- Owner cannot be demoted or removed by an admin (only by themselves — self-demotion to admin allowed if another admin exists).
- Owner cannot be removed from the org (must transfer ownership first).
- Org deletion requires owner role.
- Billing/entitlement changes require owner role (or admin + owner approval — defer to Ledger).
- When an org is created via license activation, the activating user is `owner`. When created via `POST /api/v1/org` (Stripe), the creator is `owner`.

**⚠️ Compass coordination:** The `owner` question is also a product decision. If Compass's product doc opts against `owner`, Atlas can implement this without it — the `admin`-only model with last-admin guards still works. Atlas's recommendation is to add `owner` now because it's additive and hard to retrofit later.

#### 2.4.2 Endpoints

**New:**

| Method | Path | Authz | Description |
|--------|------|-------|-------------|
| `POST` | `/api/v1/org/activate` | Authenticated user | Redeem a license key → create org + entitlement + owner role |
| `GET` | `/api/v1/orgs/{org_id}/entitlement` | Org member | Current entitlement: tier, seats, storage, expiry, source |
| `PUT` | `/api/v1/orgs/{org_id}/entitlement/seats` | Org owner | Self-service seat change within entitlement bounds (Stripe: triggers invoice; Helm: bounded by license.seats_limit) |
| `POST` | `/api/v1/orgs/{org_id}/owner/transfer` | Org owner | Transfer ownership to another org admin |

**Modified:**

| Method | Path | Change |
|--------|------|--------|
| `POST` | `/api/v1/org` | Replace `User.tier` check with Entitlement check; create Entitlement row on org create |
| `GET` | `/api/v1/org` | Include entitlement snapshot + owner info |
| `GET` | `/api/v1/auth/me` | Add `org_id`, `org_name`, `org_role`, `effective_tier` |
| `PUT` | `/api/v1/orgs/{org_id}/members/{user_id}/role` | Add owner-role guards (admin cannot target owner) |
| `DELETE` | `/api/v1/orgs/{org_id}/members/{user_id}` | Add owner-removal guard |

**Preserved (unchanged contract):**
- `GET /api/v1/orgs/{org_id}/members` — list members (extends to include owner role)
- `POST /api/v1/orgs/{org_id}/members/invite` — invite (admin/owner only)
- `POST /api/v1/orgs/{org_id}/invites/{invite_id}/resend` — resend invite
- `GET/PUT /api/v1/orgs/{org_id}/settings` — org settings
- `GET /api/v1/org/invites/me` — my pending invites
- `POST /api/v1/org/invite/{invite_id}/accept` — accept invite
- `POST /api/v1/org/invite/{invite_id}/decline` — decline invite

#### 2.4.3 The `perform_role_change` and `perform_member_removal` services

These shared services (at `org_members.py:575-669` and `org_members.py:732-925`) are extended, not replaced:

- `perform_role_change` adds: cannot change owner's role unless actor IS the owner (self-demotion). Owner→admin self-demotion requires at least one other admin.
- `perform_member_removal` adds: cannot remove owner. Must transfer ownership first.
- The existing `SELECT FOR UPDATE` last-admin guards continue to work.

### 2.5 Orphaned-admin / lifecycle safety

| Scenario | Guard |
|----------|-------|
| Last admin removed | Existing `SELECT FOR UPDATE` guard in `perform_member_removal` (`org_members.py:792-804`) — blocks removal if `_count_admins(db, org_id) <= 1`. |
| Last admin demoted | Existing `SELECT FOR UPDATE` guard in `perform_role_change` (`org_members.py:636-648`) — blocks demotion if `_count_admins(db, org_id) <= 1`. |
| Owner is the only admin and self-demotes | New guard: owner→admin self-demotion requires at least one other admin. |
| User deactivated (`is_active=false`) | New sweep: when a user is deactivated, check if they're the last admin (or owner) of any org. If so, NOTIFY org contact email + platform admin. Do NOT auto-deactivate — this requires human intervention. |
| License expires | `entitlements.status` transitions to `"expired"` (via a periodic job or Stripe webhook). Org continues to exist but features drop to FREE tier. Members can still access the org roster but team features are disabled. After a grace period (30 days), the org enters `"expired"` status — data is preserved, access is read-only. |
| License revoked | `entitlements.status` → `"revoked"`. Same expiry behavior as above. |
| Org with no owner (defensive) | Nightly job: for each org with members but no `OrgMember.role='owner'`, promote the longest-tenured admin to owner and log an audit event. |

### 2.6 SaaS ↔ self-hosted unification

The `entitlements` table is the bridge:

- **SaaS:** Stripe webhook creates/updates an Entitlement row with `source='stripe'`. `Organization.stripe_subscription_id` and `Organization.stripe_customer_id` remain as billing-side foreign keys (Ledger's domain). The entitlement row is the *resolved* state.
- **Self-hosted:** License activation creates an Entitlement with `source='helm_license'`. `HelmLicense` continues to exist for cluster-side validation (periodic `/api/v1/validate` calls from Helm chart). The entitlement row is the *resolved* state.
- **Manual/admin:** Admin-provisioned orgs get `source='admin_provisioned'`. No external system to reconcile.

**Tier resolution becomes one query:**
```sql
SELECT * FROM entitlements
WHERE owner_type = :owner_type AND owner_id = :owner_id AND status = 'active'
ORDER BY current_period_end DESC NULLS FIRST
LIMIT 1
```

---

## §3 — Migration

### 3.1 Additive migration (migration 050)

1. **Create `entitlements` table** with all columns from §2.1.
2. **Add `HelmLicense.org_id`** — nullable FK to `organizations.id`, UNIQUE constraint (one org per license).
3. **Backfill entitlements from existing data:**
   - For each `User` with `tier != 'free'` and NO `OrgMember` row: INSERT entitlement (`owner_type='user'`, `owner_id=user.id`, `source='manual'`, `tier=user.tier`, `status='active'`).
   - For each `Organization`: INSERT entitlement (`owner_type='org'`, `owner_id=org.id`, `source='stripe'` if `stripe_subscription_id` else `'manual'`, `tier=org.tier`, `seats_limit=org.seats_limit`, `storage_limit_bytes=org.storage_limit_bytes`, `status='active'`).
   - For each `HelmLicense` with `status='active'`: INSERT entitlement (`owner_type='org'` if `org_id` set else `NULL`, `source='helm_license'`, `source_ref=license.id`, `tier=license.tier`, `seats_limit=license.seats_limit`, `current_period_end=license.expires_at`, `status='active'`).
4. **Backfill `OrgMember` roles:** Existing admins → set to `'owner'` if they were the org creator (join to `AdminAction` where `action='admin_create_org'` and `target_id=org_id`). This is best-effort — some orgs predate the AdminAction audit log. For those, set all existing admins to `'owner'` (safe: they ARE the owners in practice).
5. **Add `entitlement_id` nullable FK to `users` and `organizations`** — a denormalized pointer to the active entitlement for fast resolution. Nullable during migration, populated in step 6.
6. **Set `entitlement_id`** on each User/Organization to the backfilled entitlement row.

### 3.2 Rollback path

- `User.tier`, `Organization.tier`, `HelmLicense.tier` are NOT dropped — they become denormalized cache columns populated from the entitlement on write. Rollback = revert `get_effective_tier` to read the old columns.
- No data loss. Every backfilled entitlement has a `source_ref` that traces back to the original row.
- Migration is reversible: drop `entitlements` table, drop `HelmLicense.org_id`, drop `entitlement_id` FKs.

### 3.3 Phased rollout

| Phase | Scope | Dependencies |
|-------|-------|-------------|
| **P1: Data model + migration** | `entitlements` table, backfill, `HelmLicense.org_id`, `OrgMember.role='owner'` backfill | Migration 050 |
| **P2: Resolution switch** | `get_effective_tier` → `resolve_entitlement`, `/me` enrichment, dashboard sidebar fix | P1 |
| **P3: License activation** | `POST /api/v1/org/activate`, license binding, `GET /me` shows pending licenses | P1 + P2 |
| **P4: Owner role enforcement** | `owner` role guards in role-change/removal, owner transfer endpoint, owner-only billing endpoints | P1 |
| **P5: Self-service seat/tier** | `PUT /orgs/{id}/entitlement/seats`, self-service tier change within bounds | P1 + Ledger |
| **P6: Expiry + safety sweeps** | Entitlement expiry job, license expiry grace period, orphaned-admin sweep, user-deactivation cross-check | P1 + P4 |

---

## §4 — Sentinel Authz Checklist

**To be completed by Sentinel before ANY code is written.** Flag each item as CLEAR / NEEDS CLARIFICATION / BLOCKED.

### 4.1 License activation trust boundary

- [ ] **License-key forgery:** Is the license key format cryptographically unforgeable? `generate_license_key()` in `license_keys.py` — Sentinel must verify key entropy and that keys cannot be enumerated.
- [ ] **Replay attack:** Can an activation key be used twice? The `UNIQUE` constraint on `HelmLicense.org_id` + atomic rowcount-1 guard on the UPDATE must prevent double-activation. Sentinel to verify the race condition under concurrent POST.
- [ ] **Cross-org license theft:** Can User A activate a license key issued to Company B? The license key alone is the credential — if it leaks, anyone can claim the org. Consider requiring an email-verification step (key sent to `HelmLicense.contact_email`; activation link includes key + email).
- [ ] **Rate limiting:** Activation endpoint must be rate-limited (brute-force license key guessing).

### 4.2 Org ownership and privilege escalation

- [ ] **Self-service admin creation:** The activation flow grants `owner` role to the activating user. Is there any path where a user could activate a license for an org they shouldn't own?
- [ ] **Owner transfer:** Can an owner transfer to a non-member? (No — must be an existing admin.) Can a malicious admin trick the owner into transferring? (Require confirmation + re-auth.)
- [ ] **Admin→owner promotion:** Can an admin promote themselves to owner? (No — only owner can transfer ownership.)
- [ ] **Cross-org visibility:** Entitlement queries must be scoped to the caller's org. `resolve_entitlement` must never return another org's entitlement.

### 4.3 License lifecycle

- [ ] **Expired license → FREE downgrade:** Does the downgrade preserve data? (Yes — read-only, data preserved.) Is there a grace period before features are cut? (30 days.)
- [ ] **Revoked license:** Same as expiry path. Can a revoked license be reactivated? (Only by platform admin.)
- [ ] **License transfer between orgs:** Not supported in v1. If needed later, requires Sentinel review.

### 4.4 User deactivation and org safety

- [ ] **Deactivated user who is last org owner:** The sweep must detect this and NOT auto-delete the org. Sentinel to verify the notification path.
- [ ] **Deactivated user who is last org admin (non-owner):** Promote another member to admin, or notify owner.

### 4.5 Admin surface changes

- [ ] **`require_admin` remains platform-global.** No new admin endpoints in this design that bypass platform-admin auth.
- [ ] **Org owner is NOT a platform admin.** Owner powers are org-scoped only.

---

## §5 — Open Decisions

These are blocking decisions for the CEO + reviewers. Atlas provides a recommendation for each.

### OD-1: Introduce `owner` role?
**Recommendation:** YES (see §2.4.1). Additive, expected by enterprise buyers, prevents hostile admin-on-admin takeover. Mirrors GitHub.

### OD-2: One entitlement record vs. reconcile existing fields?
**Recommendation:** New `entitlements` table (§2.1). The current three-field model is too fragmented to reconcile in place without data loss. The table is additive, backfills from existing data, and leaves existing columns as denormalized caches.

### OD-3: Self-service activation vs. admin-assisted only?
**Recommendation:** Self-service via `POST /api/v1/org/activate` (§2.2) with admin-assisted as fallback. This is the entire point of the redesign — removing the SessionFS-staff bottleneck.

### OD-4: How do SaaS Stripe and self-hosted HelmLicense unify?
**Recommendation:** Unify at the `entitlements` table (§2.6). Both sources write to the same table; the resolution path is source-agnostic. Ledger owns the Stripe→entitlement sync; Atlas defines the contract.

### OD-5: Multi-org membership?
**Recommendation:** DEFER. The current one-user-one-org constraint stays. Relaxing it touches auth, session routing, dashboard UX, and project ownership — a separate design. The `entitlements` table is designed to support it later (`owner_type`/`owner_id` pattern already supports multiple orgs per user; the constraint is at the `OrgMember` level, not the data model).

### OD-6: `HelmLicense` rename?
**Recommendation:** YES — rename to `License` (or keep `HelmLicense` as-is for now). "HelmLicense" is a self-hosted-centric name. In the target model, a license can be SaaS or self-hosted. However, renaming a table with FK references is high-touch. Defer to implementation — if the rename complicates migration, keep the name and add a comment.

---

## §6 — Review Checklists

### Codex (architecture/correctness review)
- [ ] Entitlement resolution is single-query, no N+1
- [ ] Backfill migration handles edge cases: users with NULL tier, orgs with NULL stripe fields, HelmLicenses with NULL expires_at
- [ ] `entitlement_id` denormalized pointer stays consistent with the `entitlements` table
- [ ] License activation rowcount-1 guard is race-free (concurrent activation attempts)
- [ ] `/me` enrichment doesn't add N+1 queries (single join)
- [ ] Owner role guards compose with existing `SELECT FOR UPDATE` last-admin guards
- [ ] `perform_role_change` and `perform_member_removal` extensions don't regress existing behavior
- [ ] Migration 050 downgrade path verified
- [ ] All existing tests pass with the backfilled data

### Ledger (billing/entitlement)
- [ ] Stripe webhook → entitlement sync contract (fields, idempotency, conflict resolution)
- [ ] Seat change self-service: does it trigger Stripe invoice? What are the bounds?
- [ ] License expiry → Stripe subscription cancellation: reconciliation path?
- [ ] `current_period_start` / `current_period_end` semantics for Stripe vs. HelmLicense
- [ ] `status: "past_due"` — when and how does Stripe set this? What features are gated?
- [ ] Billing page in dashboard: what entitlement fields to surface?

### Shield (compliance)
- [ ] Entitlement data retention: how long are expired/revoked entitlements kept?
- [ ] PII in entitlements table? (No — only references to users/orgs/licenses, no email/name.)
- [ ] DLP implications: entitlement tier/status is not PII but is business-sensitive.
- [ ] Audit trail: every entitlement mutation must be logged (AdminAction or new EntitlementAudit table).
- [ ] License activation email verification: does it need GDPR consent flow?
- [ ] Data residency: entitlement data is in the same DB as the org. No new residency concern.

### Sentinel (authz — see §4 for full checklist)
- [ ] License activation trust boundary
- [ ] Org ownership and privilege escalation
- [ ] License lifecycle
- [ ] User deactivation and org safety
- [ ] Admin surface changes

---

## §7 — Summary

### The problem in one sentence
Enterprise customers hold a license but get no Organization and no org management surface because `HelmLicense`, `Organization`, and `User.tier` are three disconnected systems with no self-service bridge.

### The fix in three sentences
1. Introduce an `entitlements` table as the single source of truth for tier, seats, storage, and expiry — unifying Stripe subscriptions, Helm licenses, and admin-provisioned orgs.
2. Add `POST /api/v1/org/activate` so an enterprise customer redeems their license key to create an Organization and become its owner — no SessionFS staff required.
3. Fix the dashboard access chain so org membership (not `default_org_id`) drives the Organization surface, `/me` returns effective tier/org info, and the `owner` role protects against hostile admin takeover.

### The migration path
Additive migration 050 backfills entitlements from existing `User.tier`, `Organization.tier`, and `HelmLicense` rows. Old columns remain as denormalized caches. Rollback preserves all data.
