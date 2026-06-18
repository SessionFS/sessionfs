# Licensing + Org-Management Redesign — Product Shape

Compass (product lead). Companion to Atlas's technical binding design (`licensing-org-redesign.md`). This doc owns the product, commercial, and UX direction; Atlas owns data model and implementation. Decisive — not a survey.

---

## 1. What "Enterprise" Means Commercially

### One tier, two delivery models

**Enterprise** is a single tier with a single feature set. How the customer pays for and runs it splits into two delivery models:

| | Cloud Enterprise | Self-Hosted Enterprise |
|---|---|---|
| **How they buy** | Sales-led (large Stripe sub) or self-serve upgrade from Team | Signed license agreement → license key issued by SessionFS staff |
| **How it runs** | SessionFS-managed cloud (api.sessionfs.dev) | Customer-managed infrastructure (Helm chart, own PG/S3) |
| **Entitlement source** | Stripe subscription → `Organization.tier = "enterprise"` | `HelmLicense` → linked `Organization.tier = "enterprise"` |
| **Billing** | Stripe (monthly/annual invoice) | Off-platform (contract, PO, Net-30/60) |
| **Min seats** | 20 | 20 |
| **Typical ACV** | $35/user/mo (~$8,400/yr for 20 seats) | $35/user/mo, annual contract |
| **Support** | Dedicated Slack + named AM | Same |
| **SLA** | 99.9% managed | Customer-operated |

**Recommendation: Keep two delivery models.** They serve different procurement paths. A company that can't use SaaS (compliance, air-gap, data residency) buys self-hosted. A company that wants zero-ops buys cloud. Both get the same features. The unification is in the *entitlement model* (Section 2), not the delivery model.

### The enterprise customer journey

1. **Evaluation:** Prospect tries Team (self-serve, Stripe, 14-day trial) or receives a time-limited trial license (self-hosted).
2. **Purchase:** For cloud, sales converts the Team sub to Enterprise via Stripe (price change + seat bump). For self-hosted, sales issues a `HelmLicense` with `license_type=paid`, `tier=enterprise`, `seats_limit=N`, `expires_at=<contract end>`.
3. **Activation:** Customer receives a license key (self-hosted) or sees their org flip to Enterprise (cloud). **The headline fix (Section 3): they activate this themselves, not via staff ticket.**
4. **Administration:** An admin (the person who redeemed the license, or a designated admin added by staff during sales-led provisioning) manages the org — invites members, assigns roles, views seat usage, sees plan/license status.
5. **Renewal/Expansion:** Cloud: handled via Stripe (self-serve or sales-assisted). Self-hosted: sales issues a renewed/extended license key. In both cases, the org's entitlement updates automatically.

---

## 2. Entitlement Product Model

### The tier ladder (confirmed)

| Tier | Price | Seats | Storage/user | Key gates |
|---|---|---|---|---|
| **Free** | $0 | 1 (solo) | 0 bytes cloud | Capture, resume, local search, 14-day cloud retention |
| **Starter** | $4.99/mo | 1 (solo) | 500 MB | Cloud sync, dashboard, manual judge, MCP local |
| **Pro** | $14.99/mo | 1 (solo) | 500 MB | Starter + autosync, DLP secrets, handoff, project context, agent personas, MCP remote |
| **Team** | $14.99/user/mo | 3–50 | 1 GB/user | Pro + team management, shared storage pool, agent tickets/runs, team handoff, org settings |
| **Enterprise** | Custom (~$35/user/mo) | 20+ | Unlimited | Team + self-hosted, SAML/OIDC SSO, DLP HIPAA, SIEM export, 6yr audit retention, IP allowlisting, SLA |

**Recommendation: Drop the phantom "business" tier from README/docs.** It's never existed in code. Five tiers is the right number. Add `STARTER` to the pricing page (currently missing — code has it, docs/dashboard don't consistently surface it).

**Pricing page alignment needed:** `docs/pricing.md` ($12 Pro, $20 Team) ≠ dashboard `BillingPage.tsx` ($14.99 Pro, $14.99/user Team) ≠ `tiers.py` (no prices). **Decision: Dashboard prices are canonical.** They're what customers see. Update `docs/pricing.md` to match. Flag Ledger for final Stripe price IDs.

### Single source of truth: `Organization.tier`

Today entitlement is a three-way mess:

```
User.tier          ← set at signup, never changes unless billing updates it
Organization.tier  ← set at org create, only changes via Stripe webhook
HelmLicense.tier   ← set at license create, never linked to anything
```

A user with a valid enterprise license can have `User.tier = "free"` and no org, so they see a "create team" button that errors. The org they should own doesn't exist because nobody created it.

**New model — Organization is the entitlement container:**

```
┌─────────────────────────────────────────────────┐
│                 Organization                     │
│  .tier          ← resolves from ONE source      │
│  .seats_limit   ← resolves from ONE source      │
│  .entitlement_source: "stripe" | "license" |    │
│                       "manual"                  │
│  .entitlement_ref: stripe_sub_id | license_id   │
│  .entitlement_expires_at                        │
│  .features       ← derived from tier (cached)   │
└─────────────────────────────────────────────────┘
```

**Resolution order** (Atlas implements; product defines the logic):

1. If org has `entitlement_source = "license"` → read `HelmLicense` row → tier, seats, expiry from license.
2. If org has `entitlement_source = "stripe"` → read Stripe subscription → tier, seats, expiry from subscription.
3. If org has `entitlement_source = "manual"` → use org's stored values directly (admin-assigned, internal use).
4. Solo user (no org) → `User.tier` applies. User.tier only governs *personal* entitlement; org membership overrides it.

**Why this works:** The org is always the container. A user's effective tier is always `org.tier` if they're an org member, `user.tier` otherwise. The current `get_effective_tier()` in `tier_gate.py` already does this — we're just making the org's own tier resolution coherent.

### How a customer sees their plan

**Org Settings → "Plan & License" panel** (new, replaces implicit tier display):

Shows:
- **Current plan badge:** "Enterprise" (with tier color)
- **Delivery model:** "Cloud" or "Self-Hosted"
- **Seat usage:** "18 / 25 seats" with progress bar. Warns at 80%, errors at 100% (blocks new invites).
- **Billing/contract:** Stripe → "Next invoice $630 on Jul 15" with "Manage Billing →" link. License → "License sfs_helm_abc123 — expires Dec 31, 2026 (197 days)" with status pill (active/warning/expired/degraded).
- **Features:** Collapsible list of what this tier includes, with checkmarks.
- **Upgrade CTA:** Cloud → "Add seats" / "Change plan" (opens Stripe Portal). Self-hosted → "Contact sales to modify your license" (mailto: link + form).
- **Admin-only:** The panel is visible to all members (transparency), but action buttons are admin-only.

**Solo users** (Free/Starter/Pro) see the same panel under their personal Settings, showing their individual tier. No org chrome.

---

## 3. Self-Service Activation / Onboarding (The Headline Fix)

Today a self-hosted enterprise customer gets a license key for their Helm deployment, but there's no path to get an org in the cloud dashboard. The license validates against the Helm chart; it never touches the user's account. The customer has to email support, who manually creates an org and adds the customer as admin.

### Primary path: License redemption (self-service)

**Step 1 — Customer receives license key.** Via email from sales, or from their procurement portal. The email contains the key + a deep link: `https://app.sessionfs.dev/activate?key=sfs_helm_abc123`.

**Step 2 — Customer logs in (or signs up).** If not already a SessionFS user, they create an account (email + password, or Google/GitHub OAuth). The activation key is carried through the auth flow.

**Step 3 — License validation.** The activation page calls `POST /api/v1/licenses/{key}/redeem-info` (new endpoint, unauthenticated) which returns:
```json
{
  "valid": true,
  "org_name": "Acme Corp",
  "tier": "enterprise",
  "seats": 25,
  "expires_at": "2027-06-17T00:00:00Z",
  "contact_email": "admin@acme.com"
}
```
If the license is invalid/revoked/expired, the page shows an error with a "Contact support" fallback.

**Step 4 — Org creation.** The user sees a pre-filled form:
- Org name (from license, editable)
- Org slug (auto-derived, editable)
- Confirmation: "You will be the **owner** of this organization."

On submit: `POST /api/v1/licenses/{key}/redeem` (authenticated). This atomically:
1. Validates the license key again (prevents race with revocation).
2. Creates the `Organization` row with `entitlement_source = "license"`, `entitlement_ref = <license_id>`.
3. Links `HelmLicense.activated_by_user_id` and `HelmLicense.activated_at` to the user.
4. Creates `OrgMember` row with `role = "owner"`.
5. Sets `User.default_org_id` to the new org.

**Step 5 — Post-activation.** The user lands on the org page with a "Invite your team" prompt. The org is live. They can immediately invite colleagues.

### Admin-assisted path (fallback for sales-led deals)

For deals where the customer wants white-glove setup:
1. Sales creates the license **and** pre-creates the org during contract signing (new admin endpoint: `POST /api/v1/admin/orgs/pre-provision` with license key + admin email + org name).
2. The designated admin receives an email: "Your SessionFS Enterprise org is ready. Log in to claim your admin account."
3. They log in → see a pending org claim → accept → become owner.
4. This is the same `OrgInvite` flow extended with an `invite_kind = "org_claim"` variant. The invite doesn't expire until the org is claimed (no 7-day limit for claim invites).

### Edge cases addressed

- **License already redeemed:** Show "This license was activated by `user@acme.com` on Jun 17, 2026. Contact your org admin to be invited." Don't allow double-redemption.
- **Wrong person redeems:** The license has a `contact_email`. If the redeeming user's email doesn't match, show a soft warning ("This license is registered to admin@acme.com. Your email is bob@acme.com. Continue anyway?") — not a hard block (people change roles, use aliases). The org becomes theirs. Sales can reassign via admin endpoint if needed.
- **Multiple people from same company each create an org:** The license can only be redeemed once → only one org gets created. Other employees see "Acme Corp already has an organization. Request an invite from your admin." We detect this via the license key, not domain matching (domain matching is fragile and has false positives for agencies, contractors, personal emails).

---

## 4. Org-Management Console Redesign (UX)

### Information architecture

The org page (`/org`) gets tabs, matching the existing dashboard design language:

```
┌──────────────────────────────────────────────────────────┐
│  Acme Corp                                    [Invite]  │
│  acme-corp · Enterprise · Self-Hosted                    │
│                                                          │
│  [Plan]  [Members]  [Audit]  [Settings]                  │
└──────────────────────────────────────────────────────────┘
```

### Tab 1: Plan & License

See Section 2 "How a customer sees their plan." This is the default tab. Admin sees action buttons; members see read-only version.

### Tab 2: Members

**Redesigned from the current single-list view:**

- **Search/filter bar:** Filter by role (All / Owner / Admin / Member), search by email.
- **Member list (table):**
  - Avatar + email + display name
  - Role badge (colored: Owner = brand/gold, Admin = brand/15, Member = border)
  - Joined date
  - Last active (from session activity, not real-time — "2 hours ago", "3 days ago")
  - Actions column (admin-only): Change role dropdown, Remove button
- **Owner row** is pinned at top, has a distinct visual treatment. Remove button is disabled with tooltip: "Ownership must be transferred before removal."
- **Transfer ownership** is a dedicated action (owner-only): "Transfer Ownership" button opens a modal with a member dropdown + confirmation text input ("type the org name to confirm").
- **Invite button** (top right) opens the existing invite form (email + role dropdown). Now includes a "Send invite" button and a "Copy invite link" option for sharing via Slack/etc.
- **Pending invites** section below the member list (same as current).
- **Seat usage bar** at the top of the tab: "18 / 25 seats used" with a progress bar. If at capacity, the Invite button becomes "Add seats →" (cloud) or "Contact sales →" (self-hosted).

### Roles: Owner + Admin + Member

**Recommendation: Add Owner as an explicit role above Admin.**

| Role | Level | Can manage members | Can manage billing | Can transfer ownership | Can delete org | Can see all sessions |
|---|---|---|---|---|---|---|
| **Owner** | 100 | Yes (all) | Yes | Yes | Yes | Yes |
| **Admin** | 50 | Yes (member↔admin) | Yes (cloud) | No | No | Yes |
| **Member** | 10 | No | No | No | No | Own + team sessions (per policy) |

**Rules:**
- Exactly one owner per org. Owner is set at org creation (license redemption or Stripe checkout).
- Owner can transfer ownership to any admin. This is a two-step confirm: owner initiates, target admin accepts. Until accepted, the owner retains the role.
- Owner cannot be removed by admins. Only the owner can remove themselves (via transfer).
- Admins can promote members to admin, demote admins to member, remove members, invite new members.
- The "last admin" guard from v0.10.0 (Org Members management) stays: can't remove the last admin unless an owner exists.
- New: can't remove the last admin **if there's no owner**. (If the owner left without transferring, this is an error state — Atlas designs the recovery path.)

**Implementation note for Atlas:** The current `OrgRole` enum has `ADMIN` and `MEMBER`. Add `OWNER`. The `ROLE_LEVEL` map becomes `{MEMBER: 10, ADMIN: 50, OWNER: 100}`. The existing `has_minimum_role()` function works unchanged. Migration needed: assign the `OWNER` role to the user who created the org (currently they're `ADMIN`). For Stripe-created orgs, the billing user becomes owner.

### Tab 3: Audit

Read-only feed of org-level events. Admin-only tab (not visible to members).

Shows, in reverse chronological order:
- Member joined / left / was removed
- Role changes (who changed whose role, old → new)
- Invites sent / accepted / declined / expired
- License changes (activated, renewed, expired, tier changed)
- Billing events (plan changed, seats added, payment failed — cloud only)
- Org settings changes (who changed what)

Filterable by event type and date range. Exportable as CSV (Enterprise feature).

**Implementation:** New `org_audit_events` table. This is a product requirement — Atlas designs the schema. Events are append-only, never modified.

### Tab 4: Settings

Existing `OrgSettingsTab` content (KB retention, compile defaults) plus:

- **Org profile:** Name, slug (editable by admin).
- **Danger zone:** "Delete Organization" button (owner-only, with confirmation + typing org name). This is a hard delete — Atlas designs the cascading cleanup.
- **SSO configuration (future — flag for Atlas/Sentinel):** SAML/OIDC metadata URL, domain enforcement toggle, "Require SSO for all members" checkbox. This tab is gated to Enterprise tier. Non-Enterprise orgs see an upgrade prompt instead.

### Non-admin member view

Members see:
- **Plan tab:** Read-only. Shows tier, seats, features. No action buttons except "Leave Organization."
- **Members tab:** Read-only. Shows member list + roles. No invite/remove/role-change controls.
- **Settings tab:** Org name only. No edit controls.
- **No Audit tab.**

### Unassigned users (Free/Starter/Pro)

If a user isn't in an org, the `/org` page shows a contextual empty state:
- **Free/Starter/Pro:** "Organizations are available on Team and Enterprise plans. Upgrade to create or join one." With an "Upgrade" CTA (links to billing page).
- **User with a pending org invite:** "You've been invited to join Acme Corp as an Admin. [Accept] [Decline]" — the invite acceptance surface moves here from email-only.

---

## 5. Self-Upgrade Within Bounds

### Cloud (Stripe-billed)

**Self-service for:** Changing tier within the same delivery model, adding seats.

- **Tier upgrade:** Team → Enterprise requires sales contact (contractual minimum seats, custom pricing). Free → Starter, Starter → Pro, Pro → Team are self-serve via Stripe Checkout.
- **Seat addition:** Team org admin can add seats via Stripe Customer Portal (updates subscription quantity). The org's `seats_limit` updates via the `customer.subscription.updated` webhook (already implemented).
- **Downgrade:** Team → Pro (below min seats) requires contacting support. This is intentional friction — prevents accidental data loss (downgrade would drop team features, handoffs, shared storage).

**Recommendation: Keep Stripe self-service for the existing paths. Flag for Ledger:** Enterprise cloud subscriptions need a Stripe product/price. Currently Enterprise is "contact sales" only — there's no Stripe price ID for it, which is correct for sales-led deals. But if we ever want self-serve Enterprise (e.g., a 20-seat Team org clicking "Upgrade to Enterprise"), Ledger needs to create the Enterprise Stripe price.

### Self-hosted (license-based)

**No self-service for tier or seat changes.** The license is a contract. Changing seats or tier means a new or amended contract.

What the admin CAN do:
- See their current license status (tier, seats, expiry, grace period).
- See a "Contact sales" button with pre-populated email (org name + license key in subject).
- Renew an expiring license via a "Request renewal" form (sends email to sales + creates an internal ticket).
- See a 30-day expiry warning in the dashboard banner.

What the admin CANNOT do:
- Add seats themselves (no "+" button on seats).
- Change tier themselves.
- Extend the license themselves.

**Rationale:** Self-hosted enterprise is a B2B contract relationship, not a self-serve SaaS. The product should make the contract status transparent and the next step obvious, but shouldn't pretend the customer can modify the contract unilaterally.

### Within-bounds visibility (applies to both)

When an org hits seat capacity:
- Invite button becomes disabled with "25/25 seats used. Contact sales to add seats."
- Org owner + admins receive an email notification at 80% and 100% usage.
- Existing members are unaffected. New invites are blocked.
- For cloud: admin can click through to Stripe Portal to add seats (self-serve).
- For self-hosted: admin sees the "Contact sales" CTA.

When a license expires (self-hosted):
- 30 days before: dashboard banner + email to owner + admins.
- Expiry date: license enters 14-day grace period. Org tier drops to "free" (degraded mode). Features revert to Free tier. Data is preserved but inaccessible.
- Grace period ends: hard stop. API returns 403. Helm chart won't start.
- Cloud orgs on expired Stripe subscriptions follow the existing `customer.subscription.deleted` → downgrade to free path.

---

## 6. Rollout / Migration (Product Lens)

### Who is affected

| Segment | Current state | Migration impact |
|---|---|---|
| **Free/Starter/Pro solo users** | `User.tier` only, no org | **None.** They keep working as-is. User.tier continues to govern their entitlement. |
| **Team orgs (Stripe-billed)** | `Organization.tier = "team"`, `entitlement_source` implicit | **Minimal.** Migration sets `entitlement_source = "stripe"`, `entitlement_ref = stripe_subscription_id`. No visible change. |
| **Enterprise orgs (admin-created)** | `Organization.tier = "enterprise"`, no license link | **Backfill.** If a corresponding `HelmLicense` exists (matched by org name + contact email), link them. If not, set `entitlement_source = "manual"`. These are pre-existing admin-assisted setups. |
| **Self-hosted license holders (no org)** | `HelmLicense` exists, no org, user may or may not have a SessionFS account | **The gap we're closing.** These customers currently have a license that only works for Helm validation. Migration: for each `HelmLicense` with `status = "active"` and no linked org, create an `Organization` + set the `contact_email` user as owner. If the user doesn't have an account, on their next login they'll see the pending org claim. |
| **New enterprise prospects** | N/A | They get the full self-service redemption flow from day one. |

### Migration steps (product sequence)

1. **Backend migration** (Atlas): Add `entitlement_source`, `entitlement_ref`, `entitlement_expires_at` to `Organization`. Add `activated_by_user_id`, `activated_at`, `linked_org_id` to `HelmLicense`. Add `OrgRole.OWNER`. Create `org_audit_events` table.
2. **Data backfill** (automated, runs post-migration):
   - All Stripe-billed orgs → `entitlement_source = "stripe"`, `entitlement_ref = stripe_subscription_id`.
   - All `HelmLicense` rows → attempt to match to existing org by `org_name` + `contact_email`. If match found, link + set `entitlement_source = "license"`. If no match, flag for "pending org creation."
   - All existing org creators → role upgraded from `admin` to `owner`.
3. **Dashboard deploy:** New Plan tab, Members tab redesign, Audit tab (Enterprise-only), self-service activation page at `/activate`.
4. **Email campaign to existing self-hosted license holders:** "Your SessionFS Enterprise org is ready. Log in at app.sessionfs.dev to manage your team, invite members, and view your license status." Includes a direct link to `/activate?key=<their-key>`.
5. **Documentation:** New docs page "Enterprise Onboarding" covering both self-service and admin-assisted paths.

### Rollback plan

The migration is additive (new columns, new table). Reverting removes the new UI but doesn't break existing functionality. `User.tier` and `Organization.tier` remain as fallback fields. The new resolution logic is a server-side change; if rolled back, the old `get_effective_tier()` behavior resumes.

---

## 7. Risks and Edge Cases

### Product risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Wrong person redeems license** | Medium | High — org is owned by wrong user | Soft warning on email mismatch. Sales can reassign via admin endpoint. Audit trail records original redemption. |
| **Multiple orgs for same company** | Medium | Medium — fragmented team, confused users | One license → one org. For cloud self-serve, domain-based suggestions ("acme.com already has an org — request invite?"). Not a hard block (false positives). |
| **License expiry breaks everything** | Low | High — team locked out | 30-day email warnings. 14-day grace period with degraded tier (data preserved). Hard stop only after grace. |
| **Cloud↔self-hosted parity gap** | Medium | Medium — customer expects feature X, it's only in cloud | Document the delta clearly in the Plan tab. Cloud-only: Stripe billing, managed SLA. Self-hosted-only: custom patterns, SIEM format export. Feature set is otherwise identical. |
| **Owner account deleted / inaccessible** | Low | High — no one can manage the org | Require owner transfer before account deletion. If owner is unreachable (left company, lost access), admin-assisted recovery: sales verifies identity, runs admin endpoint to force-transfer ownership. |
| **Stripe↔license entitlement conflict** | Low | Critical — org has both Stripe sub AND linked license | Enforce mutual exclusivity: `entitlement_source` is single-value. An org is either Stripe-billed OR license-entitled, never both. If an org on Stripe upgrades to Enterprise via sales, the Stripe sub is cancelled and the org flips to license-based. |
| **Free tier users confused by org prompts** | Medium | Low — noise, not data loss | Solo users who aren't in an org and haven't been invited see a single dismissible banner: "Working with a team? SessionFS Team plan includes shared sessions, handoff, and team dashboard. Learn more →" Not a modal, not persistent. |

### Self-hosted specific risks

- **Cluster ID mismatch:** The Helm chart validates the license on startup. The cloud dashboard has no concept of "clusters." These are separate concerns. The license key is valid for both Helm validation AND cloud org activation. No cluster check in the activation flow (the org is for team management, not infra entitlement).
- **Air-gapped deployments:** A customer who runs SessionFS fully air-gapped can't use the cloud dashboard at all. Their license validation is Helm-only. They don't get the org management UI. This is an accepted limitation — air-gapped deployments are a future problem.

### Compliance flags (for Shield)

- **Audit trail:** The org audit log must be immutable and exportable (Enterprise feature: SIEM export). Member join/leave, role changes, and license events are compliance-relevant.
- **License activation PII:** The activation flow captures `activated_by_user_id` + timestamp. This is PII. Covered by existing privacy policy + data retention.
- **SSO/SCIM (future):** When SAML/OIDC is implemented (currently a Tier feature flag, not built), Shield owns the security review. Product flag: this is an Enterprise-only feature, gated behind `saml_sso` in the feature matrix.

---

## Summary of Key Decisions

1. **Enterprise = one tier, two delivery models** (Cloud + Self-Hosted). Single feature set.
2. **Organization is the single source of truth for entitlement.** `Organization.tier` resolves from exactly one source: Stripe subscription, HelmLicense, or manual assignment.
3. **Self-service license redemption** is the headline fix: `/activate?key=...` → validate → create org → user becomes owner. Staff-assisted fallback preserved.
4. **Add Owner role** above Admin. Exactly one owner per org. Owner can't be removed by admins. Ownership is transferable.
5. **Self-upgrade within bounds:** Cloud = self-serve via Stripe (existing). Self-hosted = sales-gated (license is a contract). Both get transparent usage visibility.
6. **Tier ladder:** 5 tiers confirmed. Drop "business" from docs. Add Starter to pricing page. Dashboard prices are canonical.
7. **Rollout:** Additive migration. Backfill existing HelmLicenses → orgs. Email campaign to existing license holders. No breaking changes for Free/Starter/Pro/Team users.

---

## Open Questions for Atlas

These are product requirements that need technical binding decisions from Atlas in the companion design doc:

1. **`HelmLicense` ↔ `Organization` link:** Should this be a FK on `HelmLicense.activated_org_id` (one license → one org), or a join table (future: one license → multiple orgs for conglomerates)? Product says one-to-one is correct for v1.
2. **Owner transfer acceptance:** Should the target admin have to accept before the transfer takes effect (two-step), or is immediate transfer with email notification sufficient? Product prefers two-step (prevents accidental/malicious transfer by compromised owner account).
3. **Org deletion cascading:** When an owner deletes the org, what happens to: projects (transfer to personal? archive?), sessions (orphaned? deleted?), knowledge base (export? destroy?). Product says: projects transfer to owner's personal scope, sessions are preserved, KB is destroyed. Flag for Shield review.
4. **`entitlement_source` as enum vs string:** Product doesn't care. Atlas picks based on migration complexity.
5. **Audit event retention:** Product says keep all events for the org's lifetime. No automatic pruning. Enterprise feature: SIEM export.

---

*Compass — product direction. Atlas owns binding technical design in `docs/design/licensing-org-redesign.md`. Shield reviews security posture. Ledger owns Stripe pricing/configuration.*
