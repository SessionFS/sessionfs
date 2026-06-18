# Binding Design — Licensing + Entitlement + Org-Management Redesign

**Author:** Atlas (backend/data-model)
**Branch:** `design/licensing-atlas` (worktree off develop, NEVER push)
**Status:** Design proposal — R1 (Codex) + R2 (Ledger + Shield + Sentinel) amendments applied; pending CEO final sign-off
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

**Table constraints** (enforced at the DB level — these make the table a true single source of truth):

1. **At most one active entitlement per owner:**
   ```sql
   CREATE UNIQUE INDEX uq_entitlements_one_active_per_owner
   ON entitlements(owner_type, owner_id) WHERE status = 'active';
   ```
   This partial unique index guarantees that runtime resolution hits exactly one row. The `ORDER BY current_period_end DESC NULLS FIRST` tiebreak (used in the resolution query below) is for **historical rows only** — under normal operation the partial unique index ensures at most one active row, so no ordering-based disambiguation is needed. The ORDER BY is a defensive fallback if the index constraint is somehow violated (e.g., by a bug in a future migration).

2. **Unique external binding:**
   ```sql
   CREATE UNIQUE INDEX uq_entitlements_source_ref
   ON entitlements(source, source_ref) WHERE source_ref IS NOT NULL;
   ```
   Each Stripe subscription or Helm license maps to exactly one entitlement row.

3. **Stripe-XOR-license invariant:** An owner's active entitlement has exactly one `source`. Replacing one entitlement with another (e.g., migrating from Stripe to Helm license) transitions the prior entitlement to a terminal status (`canceled` or `expired`) in the **same transaction** as inserting the new active row. The status transition rule:
   - `active` → `canceled` (voluntary replacement: Stripe→Helm, Helm→Stripe, tier change via admin)
   - `active` → `expired` (natural expiry: `current_period_end` passed)
   - `active` → `revoked` (platform admin action)
   - Terminal statuses (`canceled`, `expired`, `revoked`) are never reactivated — a new row is inserted instead.
   - `past_due` is a Stripe-only transient state; it remains `active` for resolution purposes but gates premium features.

**Resolution rule** (replaces `get_effective_tier`):
1. If the user is an org member → resolve `entitlements WHERE owner_type='org' AND owner_id=org.id AND status='active'` (guaranteed ≤1 row by the partial unique index).
2. Else → resolve `entitlements WHERE owner_type='user' AND owner_id=user.id AND status='active'` (guaranteed ≤1 row by the partial unique index).
3. Fallback: `tier='free', seats=1` (pessimistic safe default).

**Why this over reconciling existing fields:**
- A single table means a single query for tier/features/seats/expiry. Today's resolution is two queries + conditional logic (`tier_gate.py:42-67`).
- Entitlement expiry is first-class. Today, only `HelmLicense` has an `expires_at`; Stripe subscriptions don't flow through to the DB as entitlement records.
- Audit is trivial: every entitlement change is a row mutation with timestamps.
- It unifies SaaS (Stripe) and self-hosted (HelmLicense) into one model — they differ only in `source` and `source_ref`.

**⚠️ Ledger flag:** The `source: "stripe"` path, Stripe webhook → entitlement reconciliation, and billing-specific fields (invoice, payment method) are Ledger's domain. This design defines the data contract; Ledger owns the Stripe integration and billing logic.

**Entitlement isolation (Sentinel MEDIUM-2):**

`GET /orgs/{org_id}/entitlement` must verify the caller is an active `OrgMember` of the target org BEFORE returning any entitlement data. `owner_id` / `owner_type` are derived server-side from the caller's `OrgMember` row — never from client-supplied input. The denormalized `entitlement_id` pointer on `users` and `organizations` is a write-time hint for fast resolution; it is NEVER read as an authz shortcut. The authoritative entitlement is ALWAYS resolved via `SELECT ... FROM entitlements WHERE owner_type = :type AND owner_id = :id AND status = 'active'`, scoped to the caller's proven membership.

**No platform-admin via entitlement cache (Sentinel MEDIUM-3):**

Since `User.tier` becomes a denormalized cache populated from the entitlement, an explicit guard prevents any entitlement tier from setting `User.tier = 'admin'`. The valid entitlement tier catalog is `free | starter | pro | team | enterprise` — `admin` is NOT an entitlement tier. Platform-admin status is grantable ONLY by the existing admin path (`require_admin` guard, manual DB intervention, or the admin API key mint endpoint). Activation and owner-promotion code paths must NEVER write `User.tier = 'admin'` — this is enforced by a CHECK constraint on the entitlement row (add in migration 050) AND an application-layer assertion in `resolve_entitlement`.

**License-key hygiene (Sentinel LOW-2/3):**

Never log, echo, or store full license keys. Only the key prefix (matching the `ApiKey.key_prefix` pattern) is stored in audit rows, logs, or API responses. The entitlements partial unique index `uq_entitlements_one_active_per_owner` (§2.1, constraint 1) is the DB-level safety net for the Stripe-XOR-license race — two entitlement sources cannot simultaneously produce two active rows for the same owner.

### 2.2 License → Organization: required email-token activation

**The problem:** An enterprise gets a license key but no Organization, and can't create one because `User.tier == "free"`.

**CEO decision:** Email verification on activation is REQUIRED. Activation must prove control of `HelmLicense.contact_email`. This is a hard requirement, not a soft warning.

**Design: Two-phase activation with email-token verification**

Phase 1 — **Redeem-info** (unauthenticated, non-oracular):

`GET /api/v1/org/redeem-info?key=<license_key>` returns ONLY `org_name` and `tier`. It does NOT return `contact_email`. If the key is invalid/expired/bound, the response is identical to a valid key (non-oracular — an attacker cannot enumerate valid license keys). This endpoint exists solely so the UI can show "You're about to activate the **Acme Corp** Enterprise org" before the user commits.

Phase 2 — **Redeem** (authenticated, email-verified):

`POST /api/v1/org/activate` accepts a license key. The flow inside a single database transaction:

1. **Resolve the license:** `SELECT * FROM helm_licenses WHERE id = :key AND status = 'active' AND (expires_at IS NULL OR expires_at > now())`. If no row → 404 (non-oracular: same response for invalid/expired/bound key).

2. **Bind-first — claim the license atomically BEFORE creating anything:**
   ```sql
   UPDATE helm_licenses SET org_id = :new_org_id
   WHERE id = :key AND org_id IS NULL AND status = 'active'
     AND (expires_at IS NULL OR expires_at > now());
   ```
   This UPDATE runs with `rowcount == 1` check. If rowcount = 0, the license was bound by a concurrent activation → **roll back the entire transaction** (return 409 "license already bound or no longer valid"). No org, entitlement, or OrgMember rows are created — race losers write zero orphans.

3. **Verify email control** (AFTER bind-first, still inside the same txn):
   - If the authenticated user's verified email **exact-match equals** `HelmLicense.contact_email` → `verification_method = 'matched_contact_email'`. Proceed.
   - Otherwise → generate a single-use short-TTL token (stored in a new `activation_tokens` column or lightweight table, TTL = 15 minutes), send it to `HelmLicense.contact_email` via the configured email provider. The endpoint returns `{ status: "email_verification_required", message: "A verification code has been sent to your license contact email." }`. The license bind from step 2 is ROLLED BACK (the txn aborts). The user must call `POST /api/v1/org/activate/verify` with the token to complete activation. On token match → `verification_method = 'email_token'`. On expiry → the license is unbound (org_id set back to NULL) and the user must restart.
   - **Admin-assisted path:** A platform admin (SessionFS staff) can call `POST /api/v1/admin/orgs` to pre-provision an org + license binding with `verification_method = 'admin_assisted'`. The staff member is the trust anchor; no email token is required. This path is AdminAction-logged and requires `require_admin`.

4. **Create the Organization** from the license's `org_name`.

5. **Create the Entitlement** record (`source='helm_license'`, `source_ref=key`, `owner_type='org'`, `owner_id=<new org>`, `tier=license.tier`, `seats_limit=license.seats_limit`, `current_period_end=license.expires_at`, `status='active'`).

6. **Add the activating user as OrgMember with `role='owner'`.**

7. **Emit `org_audit_events`** row: `event_type='org_created_via_license_activation'`, with `license_id`, `verification_method`, `tier`, `seats_limit`.

**Design invariants:**

- **Bind-first** prevents the "create org + entitlement, then discover license was already claimed" race (Sentinel MEDIUM-1). The atomic rowcount-1 UPDATE on `helm_licenses` is the linearization point — everything else happens AFTER the claim is won.
- **Email verification** proves the activator controls the license's registered contact email (Sentinel HIGH-1, CEO decision #1). The only bypass is the platform-admin pre-provision path (staff as trust anchor).
- **Single-use short-TTL token** prevents replay and limits the window for email interception. Token expiry forces a clean restart (license unbound).
- **Non-oracular redeem-info** prevents license enumeration. An invalid key gets the same response shape as a valid one.

**Existing admin-provisioned path preserved:** `POST /api/v1/admin/orgs` remains as the back-office fallback for pre-sales provisioning and the SessionFS company org.

**For SaaS (Stripe) customers:** `POST /api/v1/org` (the existing endpoint) already creates an org from a Stripe subscription. In the target model, it also creates an Entitlement row. The `User.tier` check at `org.py:86` is REPLACED by a check against the user's (or new org's) Entitlement — so a user whose entitlement comes from a redeemed license can create an org even if `User.tier == "free"`.

### 2.3 Org-management access fix

Three changes ensure an enterprise customer reliably sees the management surface:

1. **`GET /api/v1/auth/me` includes effective org info.** Add `effective_tier`, `org_id`, `org_name`, `org_role` fields to the `/me` response. The dashboard `useMe()` hook then populates `hasOrg` from actual OrgMember membership, not from the nullable `default_org_id`. **Implementation requirement:** The enrichment is ONE joined query (or a single `get_user_context`-style resolver) — not per-field lookups. The `/me` endpoint currently has no DB context beyond the `users` row; the resolver joins `org_members` + `organizations` + `entitlements` in one query, falling back to the user's own entitlement for solo users.

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

**DB-level enforcement — partial unique index:**
```sql
CREATE UNIQUE INDEX uq_org_members_one_owner_per_org
ON org_members(org_id) WHERE role = 'owner';
```
This guarantees at most one owner per org at the database level. Combined with the deterministic single-owner backfill (§3.1 step 4) and activation-creates-owner (§2.2), the "exactly one owner" invariant is structural, not convention-based.

**Owner in the admin-count guards:**

`_count_admins()` is extended to count both `owner` and `admin` roles for the purpose of the last-admin safety guards:
```sql
SELECT COUNT(*) FROM org_members
WHERE org_id = :org_id AND role IN ('owner', 'admin')
FOR UPDATE;
```
This means:
- An org with an owner + zero admins is a **valid, non-orphaned state** — the owner alone satisfies the last-admin guard.
- The last-admin guard blocks demoting/removing the owner only if they are the **sole** owner-or-admin in the org (i.e., no admins either).
- An admin cannot be removed if they are the last admin and there is no owner (standard last-admin guard — unchanged).

**⚠️ Compass coordination:** The `owner` question is also a product decision. If Compass's product doc opts against `owner`, Atlas can implement this without it — the `admin`-only model with last-admin guards still works. Atlas's recommendation is to add `owner` now because it's additive and hard to retrofit later.

#### 2.4.2 Endpoints

**New:**

| Method | Path | Authz | Description |
|--------|------|-------|-------------|
| `GET` | `/api/v1/org/redeem-info?key=<key>` | None (unauthenticated) | Non-oracular: returns only `org_name` + `tier`. Never returns `contact_email`. |
| `POST` | `/api/v1/org/activate` | Authenticated user | Phase 2 activation: bind-first claim → email verification → create org + entitlement + owner. Returns `email_verification_required` if email mismatch. |
| `POST` | `/api/v1/org/activate/verify` | Authenticated user | Present email token to complete activation. |
| `GET` | `/api/v1/orgs/{org_id}/entitlement` | Org member (server-verified) | Current entitlement: tier, seats, storage, expiry, source. Membership verified server-side BEFORE returning data. |
| `PUT` | `/api/v1/orgs/{org_id}/entitlement/seats` | Org owner | Self-service seat change within entitlement bounds (Stripe: triggers Subscription.modify; Helm: bounded by license.seats_limit; source='helm_license' → 403 for direct writes) |
| `POST` | `/api/v1/orgs/{org_id}/owner/transfer` | Org owner | Two-step transfer: owner initiates (re-auth/email-confirm required), target admin accepts. Atomic single-txn ACCEPT. |
| `POST` | `/api/v1/admin/orgs/{org_id}/force-transfer-owner` | Platform admin | Admin-assisted recovery: force-transfer ownership when owner is deactivated/compromised. `require_admin` + AdminAction-logged. |

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

These shared services (at `org_members.py:575-669` and `org_members.py:732-925`) are extended, not replaced.

**`perform_role_change` — owner-specific logic (in evaluation order):**

1. **Owner counts as administrative for access gating.** The caller must be `owner` or `admin` to invoke this function (unchanged from current admin-only gate, extended to include owner).
2. **Server-authoritative: reject any caller setting `new_role='owner'`.** Ownership changes ONLY via the dedicated `POST /orgs/{id}/owner/transfer` endpoint (§2.4.2). `perform_role_change` rejects `new_role='owner'` unconditionally with 403 — this prevents any path (admin promotion, self-promotion, CLI, MCP) from creating a second owner or bypassing the two-step transfer flow. The partial unique index `uq_org_members_one_owner_per_org` is the structural backstop.
3. **Owner cannot be targeted by an admin, and admin cannot demote/remove an owner-role target.** If `target.role == 'owner'` and `actor.role != 'owner'`: reject 403 "only the owner can change the owner role."
4. **Owner self-demotion (owner → admin):** If `actor == target` and `actor.role == 'owner'` and `new_role == 'admin'`: require at least one other admin to exist (the extended `_count_admins()` that includes both roles). If the owner is the sole owner-or-admin, reject 409 "cannot demote — you are the last administrator. Promote another member to admin first."
5. **Owner self-demotion to member (owner → member):** Prohibited. Owner must first transfer ownership, or self-demote to admin (if another admin exists), then an admin can demote them further.
6. **Last-admin guard (extended):** The existing `SELECT FOR UPDATE` + `_count_admins()` check now counts `role IN ('owner', 'admin')`. Demoting an admin to member is blocked if they are the last owner-or-admin.
7. **All other role changes:** Unchanged from current behavior.

**`perform_member_removal` — owner-specific logic (in evaluation order):**

1. **Owner counts as administrative for access gating.** The caller must be `owner` or `admin` (extended from admin-only).
2. **Owner cannot be removed.** If `target.role == 'owner'`: reject 409 "cannot remove the org owner. Transfer ownership first."
3. **Last-admin guard (extended):** The existing `SELECT FOR UPDATE` + `_count_admins()` check now counts `role IN ('owner', 'admin')`. Removing an admin is blocked if they are the last owner-or-admin.
4. **All other removals:** Unchanged from current behavior (projects auto-transfer, default_org_id cleared, pending transfers cancelled).

**Ownership transfer — two-step atomic ACCEPT (Sentinel HIGH-3):**

`POST /api/v1/orgs/{org_id}/owner/transfer` is a two-step flow:

1. **INITIATE** (owner only): The current owner calls the endpoint with `target_user_id` (must be an existing `admin`-role OrgMember). The server requires **re-authentication + email confirmation** — the owner must reconfirm their credentials (or present a fresh auth token) AND a confirmation email is sent to the owner's email. Only after both are satisfied is the transfer offer created (status `pending`). The existing owner is NOT demoted yet.

2. **ACCEPT** (target admin): The target admin calls `POST /orgs/{id}/owner/transfer/accept`. The ACCEPT runs as a **single atomic transaction**:
   ```sql
   BEGIN;
   -- Re-validate: initiator is STILL owner
   SELECT role FROM org_members WHERE org_id = :id AND user_id = :initiator FOR UPDATE;
   -- assert role == 'owner', else rollback + 409 "initiator is no longer owner"
   -- Re-validate: target is STILL admin
   SELECT role FROM org_members WHERE org_id = :id AND user_id = :target FOR UPDATE;
   -- assert role == 'admin', else rollback + 409 "target is no longer admin"
   -- Demote old owner → admin
   UPDATE org_members SET role = 'admin' WHERE org_id = :id AND user_id = :initiator;
   -- Promote target → owner
   UPDATE org_members SET role = 'owner' WHERE org_id = :id AND user_id = :target;
   -- Emit org_audit_event: event_type='owner_transferred'
   COMMIT;
   ```
   Both re-validations use `SELECT FOR UPDATE` to serialize concurrent attempts. If either assertion fails, the entire txn rolls back. The `uq_org_members_one_owner_per_org` partial unique index blocks any bug that would produce two owners — the second `UPDATE ... SET role='owner'` would violate it.

3. **Notifications:** On INITIATE, email the current owner ("ownership transfer initiated to <target email>"). On ACCEPT, email the old owner ("ownership transferred to <target email> — you are now an admin") and the new owner ("you are now the owner of <org name>").

**Owner immutability summary:**
- Owner cannot be demoted/removed by any admin (only self-demotion to admin, if another admin exists).
- Owner cannot be removed from the org (must transfer first).
- Only the owner can initiate a transfer. Only the target admin can accept.
- The transfer ACCEPT is atomic — both re-validations + demotion + promotion run in one txn under `SELECT FOR UPDATE`.
- The admin force-transfer endpoint (`POST /api/v1/admin/orgs/{org_id}/force-transfer-owner`, §2.4.2) is the recovery path for deactivated/compromised owners. It is `require_admin`-gated, AdminAction-logged, and runs in a single atomic txn.

**Summary of changed invariants:**

| Scenario | Old behavior | New behavior |
|----------|-------------|-------------|
| Admin demotes owner | N/A (no owner role) | Rejected 403 |
| Owner self-demotes to admin | N/A | Allowed if ≥1 other admin exists |
| Owner self-demotes to member | N/A | Rejected — transfer first |
| Admin removes owner | N/A (no owner role) | Rejected 409 |
| Admin demotes last admin | Rejected (last-admin guard) | Rejected (guard extended to count owner+admin) |
| Remove last admin | Rejected (last-admin guard) | Rejected (guard extended to count owner+admin) |
| Owner is sole admin, self-demotes | N/A | Rejected — promote another member first |
| Org has owner + zero admins | N/A | Valid state — owner satisfies admin-count guards |

### 2.5 Lifecycle safety + entitlement retention

**Deactivation-time last-owner check (Sentinel MEDIUM-4):**

When a user is deactivated (`is_active` → `false`), the deactivation transaction MUST check whether the user is the owner or last admin of any org. This check runs IN the deactivation transaction (not in a nightly sweep):

```sql
SELECT org_id, role FROM org_members WHERE user_id = :user_id FOR UPDATE;
```

For each org where the deactivated user is `role='owner'`:
- If another admin exists → promote the longest-tenured admin to owner (atomic: `UPDATE org_members SET role='owner' WHERE org_id = :id AND user_id = :new_owner`), emit `org_audit_event` with `event_type='owner_auto_promoted_on_deactivation'`.
- If NO other admin exists → **BLOCK the deactivation**. Return 409 with message: "Cannot deactivate — you are the sole owner-or-admin of org '<name>'. Transfer ownership or promote another member to admin first." The user must resolve this before deactivation succeeds.

For each org where the deactivated user is `role='admin'` (and NOT owner): allow deactivation to proceed; the user is simply removed from the org roster. If they were the last admin (and an owner exists), the owner can invite/promote a replacement.

**Admin-assisted force-transfer (Sentinel MEDIUM-4 recovery):**

`POST /api/v1/admin/orgs/{org_id}/force-transfer-owner` (`require_admin`-gated, AdminAction-logged):
- Accepts `target_user_id` (must be an existing org member with `role='admin'`).
- Runs in a single atomic txn: re-validate current state → demote old owner (if still owner) → promote target → emit `org_audit_event` with `event_type='owner_force_transferred'`.
- This is the recovery path when the owner is deactivated, compromised, or departed and no other admin can transfer ownership. The platform admin is the trust anchor.

**Entitlement retention (Shield M-1, M-2):**

- **Never hard-delete entitlement rows.** Expired, revoked, and canceled entitlements stay in the database with their terminal status. Only status transitions occur — no `DELETE FROM entitlements`.
- **Grace-period semantics:** During the grace period (30 days after `current_period_end`), ALL data remains **readable and exportable** (portability guarantee). Features are gated to FREE tier, but no data is deleted or locked. After grace, the org enters `"expired"` status — data is preserved, access is read-only. Expiry **never deletes** member data.
- **Retention window:** Expired/revoked entitlement rows are retained per the tier's audit window (`audit_retention_6yr` / `compliance_exports` flags in `tiers.py`). The retention policy is operator-configurable; the default is 6 years for Team+ tiers, 1 year for Free/Starter/Pro.

**`current_period_end` semantics (Ledger M2/M4):**

- For `source='stripe'`: `current_period_end` is the subscription RENEWAL date, NOT an expiry date. Stripe subscriptions auto-renew; the entitlement stays `status='active'` across the renewal boundary. Only the expiry sweep touches `helm_license` rows — it NEVER expires Stripe-sourced entitlements. Stripe entitlements only enter a terminal status via webhook events (`subscription.deleted` → `canceled`).
- For `source='helm_license'`: `current_period_end` IS the expiry date. The periodic expiry sweep transitions `status` to `expired` when `current_period_end < now()`.
- Plan panel / billing UI reads `entitlement.{tier, source, status}`, NOT any billing-specific `is_beta` or internal Stripe status field.

**Lifecycle guard summary:**

| Scenario | Guard |
|----------|-------|
| Last admin removed | Existing `SELECT FOR UPDATE` guard in `perform_member_removal` — blocks removal if `_count_admins(db, org_id) <= 1`. |
| Last admin demoted | Existing `SELECT FOR UPDATE` guard in `perform_role_change` — blocks demotion if `_count_admins(db, org_id) <= 1`. |
| Owner is the only admin and self-demotes | New guard: owner→admin self-demotion requires at least one other admin. |
| User deactivated (`is_active=false`) — last owner | **In-txn check**: promote longest-tenured admin if exists; BLOCK deactivation if no other admin exists. |
| User deactivated — owner is sole admin (no other admins) | Deactivation BLOCKED with 409. Must transfer or promote first. |
| Owner deactivated/compromised, no other admin | Admin force-transfer endpoint (§2.4.2) — `require_admin` + AdminAction-logged. |
| License expires | `entitlements.status` → `"expired"`. Data readable/exportable during 30-day grace. Post-grace: read-only, preserved. |
| License revoked | `entitlements.status` → `"revoked"`. Same grace + retention semantics as expiry. |
| Entitlement rows | Never hard-deleted. Retained per tier audit window (default 6yr Team+, 1yr Free/Starter/Pro). |
| Org with no owner (defensive) | Nightly job: for each org with members but no `OrgMember.role='owner'`, promote the longest-tenured admin to owner and log an `org_audit_event`. |

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

### 2.7 Org Audit Events (Shield C-1, C-2, H-1)

A new `org_audit_events` table, modeled on `ProjectMergeAudit` (`models.py:1747`), provides a durable append-only audit trail for every org-level mutation. This is a binding design requirement — not an optional follow-up.

**Table schema:**

```
org_audit_events
├── id (PK)
├── org_id: FK→organizations.id ON DELETE SET NULL (NOT cascade — deleted-org audit rows survive)
├── org_name_snapshot: TEXT NOT NULL (org name at event time)
├── event_type: TEXT NOT NULL (see catalog below)
├── actor_user_id: FK→users.id ON DELETE SET NULL (nullable — survives user deletion)
├── actor_email_snapshot: TEXT (actor's email at event time)
├── actor_role_at_time: TEXT (actor's OrgMember.role at event time, or 'platform_admin')
├── target_type: TEXT (e.g., 'user', 'license', 'entitlement', 'invite', 'settings', 'organization')
├── target_id: TEXT (nullable — stringified ID of the target entity)
├── target_email_snapshot: TEXT (nullable)
├── before: JSON Text (nullable — state BEFORE mutation)
├── after: JSON Text (nullable — state AFTER mutation)
├── entitlement_id: FK→entitlements.id ON DELETE SET NULL (nullable — for entitlement-scoped events)
├── created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Design constraints:**

- **Append-only + immutability (C-2):** No UPDATE or DELETE routes exist on `org_audit_events`. All writes are INSERTs via a single shared `emit_org_audit_event()` helper that runs INSIDE the mutating transaction — one row per mutation. A no-mutation contract test verifies that the table has zero UPDATE/DELETE paths. True immutability at the storage layer (GCS object-lock, Cloud Logging sink) is a Forge deployment follow-up. Customer-facing language says "immutable audit retention can be configured with GCP logging/storage controls," never "tamper-proof."
- **Cascade vs retention (C-3):** `org_id ON DELETE SET NULL` ensures org-deletion events survive org deletion. The deletion event itself is written in a fresh committed transaction that outlives the cascade (following the `ProjectMergeAudit` precedent — insert the deletion event row, commit, THEN execute the cascade). Deleted-org audit rows are retained per the tier's audit policy; SIEM export and 6-year retention are gated on the existing `tiers.py` `audit_retention_6yr` / `compliance_exports` flags.
- **Never reuse `AdminAction`:** `AdminAction` has `NOT NULL admin_id` and no `org_id` column — it is platform-admin-only. `org_audit_events` is org-scoped with nullable `actor_user_id`.

**MUST-log event set (H-1 — binding acceptance criteria):**

| Domain | Event types |
|--------|------------|
| Membership | `member_joined`, `member_removed`, `member_left`, `role_changed`, `owner_transferred`, `owner_auto_promoted_on_deactivation`, `owner_force_transferred` |
| Invites | `invite_sent`, `invite_accepted`, `invite_declined`, `invite_expired`, `invite_resend` |
| License/entitlement | `license_activated`, `license_bound`, `license_revoked`, `license_expired`, `entitlement_created`, `entitlement_tier_changed`, `entitlement_seats_changed`, `entitlement_status_changed`, `entitlement_source_flipped` |
| Org settings | `org_settings_changed` (before/after JSON diff) |
| Org lifecycle | `org_created` (via activation or admin), `org_created_via_license_activation`, `org_deleted` (with member_count, project_count, kb_entry_count snapshotted in `after`) |
| Billing | `billing_stripe_event` (source='stripe' events only — Ledger-owned) |

**License-key hygiene (Sentinel LOW-2/3, applies here):** Only the license key prefix (matching `ApiKey.key_prefix` pattern) is stored in `org_audit_events.target_id` or `before`/`after` JSON. Full license keys are NEVER written to audit rows, logs, or API responses.

### 2.8 Stripe → Entitlement Sync Contract (Ledger-owned)

This subsection defines the data contract between the SessionFS backend and the Stripe webhook handler. Ledger owns the Stripe integration code and billing logic; Atlas defines the entitlement-side contract.

**H1 — Stripe event → entitlement state machine:**

| Stripe event | Entitlement action | Notes |
|-------------|-------------------|-------|
| `checkout.session.completed` | UPSERT entitlement (`status='active'`, tier/seats from checkout metadata) | Idempotent by `source='stripe'` + `source_ref=sub_id` |
| `subscription.updated` (active) | UPDATE tier, seats, `current_period_end` | Only when values differ from current row |
| `subscription.updated` (past_due) | SET `status='past_due'` ONLY | Do NOT change tier, seats, or `current_period_end` |
| `subscription.deleted` | SET `status='canceled'`, preserve `source_ref` | Do NOT delete the row or change tier/seats |

**Atomicity requirement:** The entitlement write + `StripeEvent` insert (idempotency record) MUST be a single database transaction — no two-phase, no external queue hop.

**H2 — Stripe→license source-flip ordering:**

When a customer migrates from Stripe to a Helm license:
1. **Single admin transaction:** Write the new license entitlement (`status='active'`) → THEN cancel the Stripe entitlement (`status='canceled'`). The license entitlement is active BEFORE the Stripe row is terminated — no gap in `status='active'` coverage.
2. **Stripe webhook defense:** When `subscription.deleted` fires after a source-flip, the handler resolves the active entitlement for that `source_ref`. If the resolved active entitlement has `source != 'stripe'` OR `source_ref != <this subscription ID>`, the event is a **no-op** (idempotent — the entitlement was already transitioned by the admin txn in step 1).

**H3 — Seats endpoint contract:**

`PUT /api/v1/orgs/{org_id}/entitlement/seats`:
- `source='stripe'` → call `stripe.Subscription.modify(proration_behavior='always_invoice')`. The entitlement row is updated ONLY by the `subscription.updated` webhook callback, not by the seats endpoint directly. The seats endpoint triggers the Stripe call and returns the pending state.
- `source='helm_license'` → 403 "seat changes for license-based orgs must be made via license renewal."
- **Bounds:** Team tier: 3–50 seats (reject if below currently-occupied seats). Enterprise Cloud: minimum 20 seats. Enterprise self-hosted: contract-only, no self-service seat change.

**M2/M4 — current_period_end: renewal vs expiry:**

Already specified in §2.5 (`current_period_end` semantics). Stripe entitlements auto-renew across `current_period_end`; the expiry sweep only targets `source='helm_license'` rows. The plan panel / billing UI reads `entitlement.{tier, source, status}` — never a billing-internal field.

### 2.9 Tier Ladder (canonical pricing)

CEO decision: dashboard prices are canonical. The tier ladder is:

| Tier | Price | Seats | Key capability |
|------|-------|-------|---------------|
| **Free** | $0 | 1 | Individual sessions, 50 MB storage, local-only |
| **Starter** | $4.99/mo | 1 | Cloud sync, 300 MB storage, session search |
| **Pro** | $14.99/mo | 1 | Knowledge Base, rules, 2 GB storage, MCP tools |
| **Team** | $14.99/user/mo | 3–50 | Org management, agent runs, team handoff, 5 GB/user |
| **Enterprise** | Custom | 20+ (cloud) / contract (self-hosted) | Self-hosted, SSO, audit retention, DLP, priority support |

**Notes:**
- There is NO "business" tier. References to a business tier in any doc, code, or UI must be removed (Scribe follow-up).
- `docs/pricing.md` must be rewritten to match this canonical ladder (omit no Starter, drop "business"). That rewrite is a Scribe follow-up — this design only records the canonical source.
- The `tiers.py` catalog and tier-gate constants must match these prices exactly.

### 2.10 Org Deletion Cascade

CEO decision: on org deletion, **each member's own sessions revert to THAT member's personal scope** (never auto-transferred to the owner). Only owner-authored sessions follow the owner. Org-shared KB is destroyed (audit-logged with counts).

**Deletion authorization:** Org owner only. Platform admin can force-delete via `POST /api/v1/admin/orgs/{org_id}` (existing path, `require_admin`-gated).

**Cascade steps (in order, single transaction where possible):**

1. **Pre-flight checks:** Verify caller is owner (or platform admin). `SELECT FOR UPDATE` on the Organization row.
2. **Write deletion audit event FIRST** in a fresh committed txn (survives cascade — §2.7 C-3): `org_audit_events` with `event_type='org_deleted'`, `after` JSON containing `{member_count, project_count, kb_entry_count, kb_page_count}`.
3. **Member sessions revert:** For each `OrgMember`, update all sessions authored by that member and currently scoped to the org → set `project_id = NULL` (personal scope). This is per-member: each member's sessions go to that member's personal scope.
4. **Owner sessions follow owner:** Owner-authored sessions retain org scope → set `project_id = NULL` (the owner's personal scope; same mechanical outcome as other members, but the owner's own sessions stay with them).
5. **Org KB destroyed:** DELETE all KnowledgeEntry rows where `project_id` references org-owned projects, DELETE all WikiPage rows, DELETE compiled `project.context_document`. Log counts in the `org_audit_event` `after` JSON (from step 2).
6. **Org projects:** DELETE all projects owned by the org. The `ProjectMergeAudit` precedent (SET NULL FKs, audit surviving rows) applies.
7. **OrgMember rows:** DELETE all (cascade — no members in a deleted org).
8. **Entitlement:** Transition to `status='canceled'` (never hard-deleted — §2.5).
9. **Organization row:** DELETE.

**⚠️ Forge coordination:** The deletion cascade should be a background task (Celery / Cloud Task) for orgs with many members/projects/sessions. The API endpoint returns 202 Accepted with a task ID, not 200 after a 30-second synchronous cascade.

---

## §3 — Migration

### 3.1 Additive migration (migration 050)

1. **Create `entitlements` table** with all columns from §2.1, including the two partial unique indexes (`uq_entitlements_one_active_per_owner`, `uq_entitlements_source_ref`) and the `UNIQUE` constraint on `(source, source_ref)`.
2. **Create `pending_license_claim` table** — lightweight migration-era table: `id` PK, `license_id` FK→`helm_licenses.id` UNIQUE, `org_name` TEXT NOT NULL, `contact_email` TEXT, `tier` TEXT NOT NULL, `seats_limit` INT, `expires_at` TIMESTAMPTZ, `created_at` TIMESTAMPTZ NOT NULL DEFAULT now(). This table exists solely to hold unmatched licenses from the backfill; new licenses post-migration go through the standard activation path.
3. **Add `HelmLicense.org_id`** — nullable FK to `organizations.id`, UNIQUE constraint (one org per license).
4. **Backfill entitlements from existing data:**
   - For each `User` with `tier != 'free'` and NO `OrgMember` row: INSERT entitlement (`owner_type='user'`, `owner_id=user.id`, `source='manual'`, `tier=user.tier`, `status='active'`).
   - For each `Organization`: INSERT entitlement (`owner_type='org'`, `owner_id=org.id`, `source='stripe'` if `stripe_subscription_id` else `'manual'`, `tier=org.tier`, `seats_limit=org.seats_limit`, `storage_limit_bytes=org.storage_limit_bytes`, `status='active'`).
   - **Self-hosted license migration — chosen path:** Do NOT auto-create orgs or unbound entitlements for active HelmLicenses. Instead:
     1. **Auto-link high-confidence matches:** For each active `HelmLicense` where an `Organization` already exists with matching `org_name` AND `contact_email` matches an org member's email: INSERT entitlement (`owner_type='org'`, `owner_id=<matched org>.id`, `source='helm_license'`, `source_ref=license.id`, `tier=license.tier`, `seats_limit=license.seats_limit`, `current_period_end=license.expires_at`, `status='active'`), SET `HelmLicense.org_id = <matched org>.id`.
     2. **Unmatched active licenses:** Create a **`pending_license_claim`** record (new lightweight table: `license_id` FK, `org_name`, `contact_email`, `tier`, `seats_limit`, `expires_at`, `created_at`). This is NOT an org — it's a claim token waiting for a user. No entitlement row is created (owner_id would be NULL, violating the design).
     3. **First authenticated activation** (`POST /api/v1/org/activate` — §2.2): atomically creates the org + entitlement + OrgMember(owner) + binds the license in a single transaction. The endpoint accepts a license key; it resolves both `HelmLicense` rows (new-style, admin-issued) and `pending_license_claim` rows (migration-era, waiting for activation).
   - **Edge case — NULL or invalid tier in source data:** Coerce `NULL` tier and unrecognized tier strings to `'free'` with a diagnostic log entry. The migration must not fail on bad data, but must not silently propagate it either. Each coercion is recorded in the migration output.
   - **NULL `expires_at` = perpetual:** When `current_period_end` is NULL, the entitlement never expires. This is the norm for admin-provisioned and manual entitlements. The resolution query uses `ORDER BY current_period_end DESC NULLS FIRST` so perpetual entitlements sort ahead of time-limited ones (defensive — the partial unique index makes ordering irrelevant at runtime).
   - **⚠️ Compass flag:** The email-campaign copy for pending-claim license holders must change from "your org is ready" to "activate to set up your org." Atlas does not edit the Compass product doc; this is noted here for Compass to pick up.
5. **Backfill `OrgMember` roles — deterministic SINGLE-owner rule:**

   Exactly ONE member per org is promoted to `owner`. The backfill runs this precedence chain per org:

   1. **Creator from AdminAction audit log:** `SELECT user_id FROM admin_actions WHERE action = 'admin_create_org' AND target_type = 'organization' AND target_id = <org_id> ORDER BY created_at LIMIT 1`. If that user is an active `OrgMember` with `role='admin'` in this org → set to `'owner'`.
   2. **Earliest admin by join date:** `SELECT user_id FROM org_members WHERE org_id = <org_id> AND role = 'admin' ORDER BY COALESCE(created_at, invited_at) ASC LIMIT 1` → set to `'owner'`.
   3. **Lowest-id admin (deterministic fallback):** `SELECT user_id FROM org_members WHERE org_id = <org_id> AND role = 'admin' ORDER BY user_id ASC LIMIT 1` → set to `'owner'`.

   All OTHER admins remain `role='admin'`. After the backfill, the partial unique index `uq_org_members_one_owner_per_org` is created (step 7), structurally enforcing the invariant going forward.
6. **Add `entitlement_id` nullable FK to `users` and `organizations`** — a denormalized pointer to the active entitlement for fast resolution. Nullable during migration, populated in step 7.
7. **Set `entitlement_id`** on each User/Organization to the backfilled entitlement row.
8. **Create partial unique index on `org_members`:**
   ```sql
   CREATE UNIQUE INDEX uq_org_members_one_owner_per_org
   ON org_members(org_id) WHERE role = 'owner';
   ```
   This enforces the single-owner invariant structurally after the deterministic backfill has selected exactly one owner per org.

### 3.2 Rollback path

- `User.tier`, `Organization.tier`, `HelmLicense.tier` are NOT dropped — they become denormalized cache columns populated from the entitlement on write. Rollback = revert `get_effective_tier` to read the old columns.
- **No platform-admin via cache guard (Sentinel MEDIUM-3):** The write path that populates `User.tier` from the entitlement MUST assert that the entitlement's tier is in `{free, starter, pro, team, enterprise}` — never `admin`. A CHECK constraint on `entitlements.tier` (add in migration 050) enforces this at the DB level. The application-layer `resolve_entitlement` function also asserts `tier != 'admin'` before writing to `User.tier`. Platform-admin status is grantable ONLY via the existing admin path.
- No data loss. Every backfilled entitlement has a `source_ref` that traces back to the original row.
- Migration is reversible: drop `entitlements` table, drop `pending_license_claim` table, drop `HelmLicense.org_id`, drop `entitlement_id` FKs, drop the `uq_org_members_one_owner_per_org` partial unique index, drop the `org_audit_events` table.

**Downgrade handling for `owner` role:**

The `owner` role string is additive — old code that reads `OrgMember.role` sees `"owner"` as an unrecognized string. Two options, both documented here for the operator:

- **Option A (recommended): Downgrade owner → admin.** An Alembic downgrade step in the migration sets all `role='owner'` rows back to `role='admin'`. The partial unique index is dropped first. All orgs retain at least one admin; no org is orphaned. On re-upgrade, the deterministic single-owner backfill runs again and selects the same owner (stable precedence: AdminAction → earliest admin → lowest-id).
- **Option B: Keep `owner` string, let old code tolerate it.** Old code that does `role == 'admin'` checks will fail closed (owner can't perform admin actions). This is safe but degraded — owners lose management access until re-upgrade. Option A is preferred because it preserves full admin functionality during the downgrade window.

The Alembic downgrade step implements Option A: `UPDATE org_members SET role = 'admin' WHERE role = 'owner'` before dropping the partial unique index.

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

**Reviewed by Sentinel — R2 conditions applied.** Flag each item as CLEAR / RESOLVED / DEFERRED.

### 4.1 License activation trust boundary

- [x] **License-key forgery:** License key format entropy verified by Sentinel. Non-oracular `redeem-info` endpoint prevents enumeration. (RESOLVED)
- [x] **Replay attack:** Atomic rowcount-1 `UPDATE helm_licenses SET org_id = :org WHERE org_id IS NULL` is the linearization point. Concurrent activations → first wins, second rolls back with zero orphans (Sentinel MEDIUM-1 bind-first). Partial unique index `uq_entitlements_source_ref` is the structural backstop. (RESOLVED)
- [x] **Cross-org license theft:** REQUIRED email-token verification (CEO decision #1). License key alone is insufficient — activator must prove control of `HelmLicense.contact_email` via single-use short-TTL token. Only bypass is admin-assisted pre-provision (staff trust anchor). (RESOLVED — Sentinel HIGH-1)
- [ ] **Rate limiting:** Activation + verify endpoints must be rate-limited. Implementation detail — add to P3. (DEFERRED to implementation)

### 4.2 Org ownership and privilege escalation

- [x] **Self-service admin creation:** Activation grants `owner` role only after email verification. Server-authoritative: caller cannot set `new_role='owner'` in `perform_role_change` (Sentinel HIGH-3). (RESOLVED)
- [x] **Owner transfer:** Two-step atomic ACCEPT with re-auth/email-confirm on INITIATE + re-validation of initiator-still-owner + target-still-admin under `SELECT FOR UPDATE`. Target must be existing admin. (RESOLVED — Sentinel HIGH-3)
- [x] **Admin→owner promotion:** `perform_role_change` rejects `new_role='owner'` unconditionally. Ownership changes ONLY via dedicated `/owner/transfer` endpoint. Partial unique index `uq_org_members_one_owner_per_org` is the structural backstop. (RESOLVED)
- [x] **Cross-org visibility:** `GET /orgs/{org_id}/entitlement` verifies caller membership server-side before returning data. `resolve_entitlement` scoped to caller's proven `owner_type`/`owner_id`. Denormalized `entitlement_id` is a hint only — never read for authz. (RESOLVED — Sentinel MEDIUM-2)

### 4.3 License lifecycle

- [x] **Expired license → FREE downgrade:** Data preserved, readable/exportable during 30-day grace. Post-grace: read-only. Entitlement rows never hard-deleted. (RESOLVED — Shield M-1, M-2)
- [x] **Revoked license:** Same grace + retention path as expiry. Reactivation = new entitlement row (terminal statuses never reactivated). (RESOLVED)
- [ ] **License transfer between orgs:** Not supported in v1. If needed later, requires new Sentinel review. (DEFERRED)

### 4.4 User deactivation and org safety

- [x] **Deactivated user who is last org owner:** In-txn check at deactivation time (Sentinel MEDIUM-4). If owner has another admin → auto-promote longest-tenured admin. If owner is sole admin → BLOCK deactivation with 409. Admin force-transfer endpoint for recovery. (RESOLVED)
- [x] **Deactivated user who is last org admin (non-owner):** Deactivation proceeds; user removed from roster. If an owner exists, owner can invite/promote replacement. (RESOLVED)

### 4.5 Admin surface changes

- [x] **`require_admin` remains platform-global.** Admin force-transfer endpoint is `require_admin`-gated + AdminAction-logged. No new admin endpoints that bypass platform-admin auth. (RESOLVED)
- [x] **Org owner is NOT a platform admin.** Owner powers are org-scoped. No entitlement tier can set `User.tier='admin'` (Sentinel MEDIUM-3, CHECK constraint + app-layer assertion). (RESOLVED)

---

## §5 — Decisions

### Settled (CEO decisions — these are closed, not open)

**OD-1: Introduce `owner` role?** → **YES.** Settled. See §2.4.1. The `owner` role with partial unique index, two-step transfer, and server-authoritative guards is the binding design.

**OD-3: Self-service activation vs. admin-assisted only?** → **Self-service with REQUIRED email-token verification.** Settled per CEO decision #1. See §2.2. Activation must prove control of `HelmLicense.contact_email`. Admin-assisted pre-provision remains as the staff trust-anchor path.

**OD-4: How do SaaS Stripe and self-hosted HelmLicense unify?** → **Unify at `entitlements` table.** Settled. See §2.6 (unification) + §2.8 (Stripe sync contract, Ledger-owned).

**Pricing: canonical tier ladder.** Settled per CEO decision #3. See §2.9. Free $0 / Starter $4.99 / Pro $14.99 / Team $14.99/user / Enterprise custom. No "business" tier.

**Org deletion: member sessions revert to personal scope.** Settled per CEO decision #2. See §2.10. Each member's sessions go to that member's personal scope; owner sessions follow the owner; KB destroyed with audit-logged counts.

### Deferred (still open for future design)

**OD-2: One entitlement record vs. reconcile existing fields?**
**Decision:** New `entitlements` table (§2.1). The current three-field model is too fragmented to reconcile in place without data loss. The table is additive, backfills from existing data, and leaves existing columns as denormalized caches.

**OD-5: Multi-org membership?**
**Decision:** DEFER. The current one-user-one-org constraint stays. Relaxing it touches auth, session routing, dashboard UX, and project ownership — a separate design. The `entitlements` table is designed to support it later.

**OD-6: `HelmLicense` rename?**
**Decision:** Keep `HelmLicense` for now. Renaming a table with FK references is high-touch. Defer to implementation — if the rename complicates migration, keep the name and add a comment.

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

### Ledger (billing/entitlement) — ✅ R2 conditions applied
- [x] H1: Stripe → entitlement sync contract defined (§2.8): checkout.session.completed→UPSERT, subscription.updated(active)→update, subscription.updated(past_due)→status only, subscription.deleted→canceled. Entitlement write + StripeEvent insert in ONE atomic txn.
- [x] H2: Stripe→license source-flip ordering: admin txn writes new license active THEN cancels Stripe; subscription.deleted no-ops when active entitlement source≠stripe / source_ref≠sub
- [x] H3: Seats endpoint contract (§2.8): source=stripe→Subscription.modify, source=helm_license→403. Bounds: Team 3–50, Enterprise Cloud min 20
- [x] M2/M4: current_period_end semantics (§2.5): Stripe=renewal (never expiry-swept), HelmLicense=expiry. Plan panel reads entitlement.{tier,source,status}
- [ ] Seat change self-service: Ledger to implement Stripe Subscription.modify + webhook round-trip
- [ ] Billing page in dashboard: what entitlement fields to surface? (Ledger + Prism)
- [ ] Invoice/payment-method fields: Ledger's domain — not in this design

### Shield (compliance) — ✅ R2 conditions applied
- [x] C-1: `org_audit_events` table defined (§2.7), modeled on `ProjectMergeAudit`, `org_id ON DELETE SET NULL`
- [x] C-2: Append-only — no UPDATE/DELETE routes, shared `emit_org_audit_event()` inside mutating txn, no-mutation contract test
- [x] C-3: Cascade vs retention — deletion event written in fresh committed txn surviving cascade; deleted-org audit rows retained per tier policy
- [x] H-1: MUST-log event set binding (§2.7): membership, invites, license/entitlement, org settings, org lifecycle, billing
- [x] H-3: Org deletion member data — each member's sessions revert to personal scope; KB destroyed with audit-logged counts (§2.10, CEO decision #2)
- [x] M-1: Entitlement retention — never hard-delete expired/revoked rows, status transitions only, retain per tier audit window (§2.5)
- [x] M-2: Grace semantics — data always readable/exportable during grace; expiry never deletes (§2.5)
- [ ] DLP implications: entitlement tier/status is not PII but is business-sensitive
- [ ] Data residency: entitlement data is in the same DB as the org — no new residency concern

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

---

## §8 — R1 + R2 Amendments

### R1 (Codex Review) — NEEDS-CHANGES → amended

Codex R1: **NEEDS-CHANGES** → amended. Summary of resolutions:

### Finding 1 (HIGH) — Owner-role backfill breaks "exactly one owner per org"

- **Deterministic single-owner backfill** (§3.1 step 5): Three-tier precedence — (1) creator from `AdminAction` where `action='admin_create_org'`, (2) earliest `OrgMember` admin by `COALESCE(created_at, invited_at)`, (3) lowest-id admin. All other admins stay `admin`. No more "set all existing admins to owner."
- **Partial unique index** (§2.4.1, §3.1 step 8): `CREATE UNIQUE INDEX uq_org_members_one_owner_per_org ON org_members(org_id) WHERE role = 'owner'` — structurally enforces the invariant.
- **Activation grants `owner`** (§2.2 step 4): Changed from `role='admin'` to `role='owner'`. Matches Compass product doc + the activation-creates-owner intent.
- **Owner handling in `perform_role_change`** (§2.4.3): Full specification — owner counts as administrative for access gating; owner cannot be targeted by an admin (403); owner self-demotion requires ≥1 other admin; owner→member prohibited without transfer; promoting admin→owner is the ownership transfer path; last-admin guard extended to count `role IN ('owner', 'admin')`.
- **Owner handling in `perform_member_removal`** (§2.4.3): Full specification — owner cannot be removed (409 "transfer ownership first"); last-admin guard extended to count both roles.
- **Org with owner + zero admins is valid** (§2.4.1): The extended `_count_admins()` includes both roles, so owner alone satisfies the last-admin guard.

### Finding 2 (HIGH) — Entitlements table lacks constraints

- **Partial unique index — one active per owner** (§2.1): `CREATE UNIQUE INDEX uq_entitlements_one_active_per_owner ON entitlements(owner_type, owner_id) WHERE status = 'active'`. Runtime resolution hits exactly one row; the `ORDER BY current_period_end` tiebreak is for historical rows / defensive fallback only.
- **Unique external binding** (§2.1): `CREATE UNIQUE INDEX uq_entitlements_source_ref ON entitlements(source, source_ref) WHERE source_ref IS NOT NULL`. Each Stripe subscription or Helm license maps to exactly one entitlement.
- **Stripe-XOR-license invariant** (§2.1): Defined the in-transaction status transition rule (`active` → `canceled`/`expired`/`revoked`; no reactivation; `past_due` is Stripe-only transient). Replacing one entitlement transitions the prior in the SAME transaction.
- **`ORDER BY` clarification** (§2.1): Explicitly documented as historical-row defense, not runtime disambiguation.

### Finding 3 (MEDIUM) — Self-hosted license migration path

- **Chosen path** (§3.1 step 4): Do NOT auto-create orgs. For unmatched active licenses, create `pending_license_claim` records (new lightweight table) — not unbound entitlements, not full orgs. Auto-link ONLY high-confidence matches (`org_name` + `contact_email`). First authenticated activation atomically creates the org (single transaction, same `POST /api/v1/org/activate` endpoint).
- **Compass flag** (§3.1 step 4): Email-campaign copy must change from "your org is ready" to "activate to set up your org" for pending-claim cases.

### Tightening (Codex-assessed)

- **NULL/invalid tier coercion** (§3.1 step 4): Coerce to `'free'` with diagnostic log entry. Migration must not fail on bad data, must not silently propagate.
- **NULL `expires_at` = perpetual** (§3.1 step 4): Documented with `NULLS FIRST` semantics. Perpetual is the norm for admin-provisioned/manual entitlements.
- **Activation race guard** (§2.2 step 5): Single transaction with rowcount-1 `UPDATE helm_licenses SET org_id = :org WHERE id = :key AND org_id IS NULL AND status = 'active' AND not-expired`. Roll back org + entitlement + OrgMember if rowcount = 0.
- **`/me` enrichment — single query** (§2.3): Explicitly requires one joined query / `get_user_context`-style resolver, not per-field lookups.
- **Migration downgrade for `owner`** (§3.2): Option A (recommended): Alembic downgrade sets all `role='owner'` → `role='admin'` before dropping the partial unique index. Old code sees standard `admin`/`member` roles. On re-upgrade, the deterministic backfill restores the same owner. Option B (keep string, tolerate) documented as degraded but safe.

### R2 (Ledger + Shield + Sentinel Reviews) — APPROVED-WITH-CONDITIONS → conditions folded

Three reviews landed after R1. All three were APPROVED-WITH-CONDITIONS. CEO made three binding decisions (baked in as settled below). Condition resolutions grouped by reviewer:

**Sentinel conditions resolved:**

| ID | Severity | Condition | Resolution |
|----|---------|-----------|------------|
| HIGH-1 | Activation = required email-token flow | Rewrote §2.2: two-phase activation with non-oracular `GET /redeem-info`, bind-first `UPDATE helm_licenses SET org_id` atomic claim, email verification via single-use short-TTL token or exact-match contact_email, admin-assisted bypass. `verification_method` recorded. |
| HIGH-3 | Owner guards server-authoritative | §2.4.3: `perform_role_change` rejects `new_role='owner'` unconditionally. Ownership moves ONLY via dedicated transfer endpoint. Two-step ACCEPT is single atomic txn with `SELECT FOR UPDATE` re-validation of initiator-still-owner + target-still-admin. Re-auth/email-confirm on INITIATE. Owner immutable/can't be removed. Admin force-transfer for recovery (§2.4.2). |
| MEDIUM-1 | Bind-first activation | §2.2 step 2: `UPDATE helm_licenses SET org_id = :org WHERE org_id IS NULL` with rowcount==0 check BEFORE creating org/entitlement/OrgMember. Race losers roll back with zero orphans. |
| MEDIUM-2 | Entitlement isolation | §2.1: `GET /orgs/{org_id}/entitlement` verifies caller membership server-side. `owner_id` derived from OrgMember row, never client-supplied. Denormalized `entitlement_id` is a hint only — never read for authz. |
| MEDIUM-3 | No platform-admin via cache | §2.1 + §3.2: Entitlement tier catalog = `{free, starter, pro, team, enterprise}`. `admin` is NOT an entitlement tier. CHECK constraint on `entitlements.tier` + app-layer assertion in `resolve_entitlement`. |
| MEDIUM-4 | Deactivated last owner | §2.5: In-txn check at deactivation time — if owner has another admin, auto-promote; if sole admin, BLOCK deactivation with 409. Admin force-transfer endpoint for recovery. |
| LOW-2/3 | License-key hygiene | §2.1 + §2.7: Only key prefix stored (ApiKey.key_prefix pattern). Full keys never logged/echoed/stored. Entitlements partial unique index is the Stripe-XOR-license race guard. |

**Shield conditions resolved:**

| ID | Severity | Condition | Resolution |
|----|---------|-----------|------------|
| C-1 | Define `org_audit_events` table NOW | New §2.7: full schema modeled on `ProjectMergeAudit`, `org_id ON DELETE SET NULL`, not reusing `AdminAction`. |
| C-2 | Append-only + immutability | §2.7: No UPDATE/DELETE routes. Single shared `emit_org_audit_event()` inside mutating txn. No-mutation contract test. True immutability = Forge deployment follow-up (GCS object-lock). |
| C-3 | Cascade vs retention | §2.7: Deletion event written in fresh committed txn surviving cascade. Deleted-org audit rows retained per tier policy. |
| H-1 | MUST-log event set | §2.7: Binding catalog — membership (7 types), invites (5), license/entitlement (8), org settings, org lifecycle (3), billing. One row per mutation. |
| H-3 | Org deletion member data | §2.10 (CEO decision #2): Each member's sessions revert to personal scope. Owner-authored sessions follow owner. KB destroyed with audit-logged counts. |
| M-1 | Entitlement retention | §2.5: Never hard-delete entitlement rows. Status transitions only. Retain per tier audit window (6yr Team+, 1yr Free/Starter/Pro). |
| M-2 | Grace semantics | §2.5: Data always readable/exportable during grace. Expiry never deletes. |

**Ledger conditions resolved:**

| ID | Severity | Condition | Resolution |
|----|---------|-----------|------------|
| H1 | Stripe→entitlement sync contract | New §2.8: Full state machine (checkout→UPSERT, sub.updated→update, past_due→status only, sub.deleted→canceled). Entitlement write + StripeEvent in ONE atomic txn. |
| H2 | Stripe→license source-flip ordering | §2.8: Admin txn writes new license active THEN cancels Stripe. subscription.deleted no-ops when active entitlement source≠stripe. |
| H3 | Seats endpoint contract | §2.8: source=stripe→Subscription.modify (webhook writes back), source=helm_license→403. Bounds: Team 3–50, Enterprise Cloud min 20. |
| M2/M4 | current_period_end semantics | §2.5: Stripe = renewal (never expiry-swept), HelmLicense = expiry. Plan panel reads entitlement.{tier,source,status}. |

**CEO decisions (baked in as settled):**

1. **Email verification on activation is REQUIRED** (§2.2): Activation must prove control of `HelmLicense.contact_email`. Hard requirement, not soft warning.
2. **Member sessions revert to personal scope on org delete** (§2.10): Each member's own sessions go to that member's personal scope. Never auto-transferred to owner.
3. **Dashboard prices are canonical** (§2.9): Free $0 / Starter $4.99 / Pro $14.99 / Team $14.99/user / Enterprise custom. Drop "business" tier. `docs/pricing.md` rewrite is a Scribe follow-up.
