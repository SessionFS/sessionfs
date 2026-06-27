# Design: Org SSO Foundation — OIDC-First Identity Control Plane

- **Ticket:** tk_657df5f1a1c64bb8 — "Design org SSO foundation: OIDC-first identity control plane"
- **Author:** Compass (product intent + acceptance boundaries)
- **Status:** Binding design (R2) — Sentinel review **APPROVED-WITH-CONDITIONS** (tk_9670836d96894642) folded in. Pending final Atlas (backend) + Shield (compliance) + Ledger (packaging) confirmation that R2 conditions are buildable as written.
- **Audience:** Atlas owns the *how* (routes, migrations, transaction shapes); Sentinel/Shield own the threat model sign-off; this doc defines the *what* and the *acceptance boundaries*.
- **Visibility:** INTERNAL. `docs/design/` is tracked on `develop` and stripped from public `main` by the release sanitizer. References internal architecture freely.

---

## §0 — Revision History

- **R2.1 — 2026-06-27 (Sentinel re-confirm, APPROVED-WITH-CONDITIONS):** Sentinel re-reviewed R2 + migration 056. All 6 original HIGHs verified RESOLVED in spec; migration 056 verified CORRECT. Two new MEDIUMs folded here: **N1** — the §3.2.1 same-origin pin for `jwks_uri`/`token_endpoint` was wrong and breaks Google Workspace (a v1 certify target); replaced with a **per-host IP-range guard** that trusts cross-*public*-host endpoints advertised by the verified-issuer discovery doc while still rejecting internal/private/metadata hosts (§3.2.1 item 3, §10). **N2** — migration 056 now **dedupes duplicate `(org_id, user_id)` rows before** creating `uq_org_members_org_user` (duplicate memberships are a known prod state; the bare index creation would abort the migrate-job) — folded into migration 056 + test. **N3** (doc nit) — `ExternalIdentity` model docstring corrected to `(org_idp_id, subject)`. **Tier:** all-paid acceptable, conditioned on (a) Forge platform egress-restriction (metadata server unreachable) as a **hard release gate** for the all-paid surface, and (b) N1's per-host guard landing with a passing internal-host-rejection + public-cross-host-allow test before P2 ships. N1 round-trips back to Sentinel on P2 implementation.
- **R2 — 2026-06-27 (Compass):** Folded in Sentinel's APPROVED-WITH-CONDITIONS review (tk_9670836d96894642). Six HIGH conditions are now binding spec: (1) deprovisioning genuinely revokes access — chose the **deprovision-revokes-keys** model AND §4.2 also requires an active `ExternalIdentity` under enforcement (§4.2, §5.4); (2) SSRF guard on all issuer-derived server-side fetches — https-only, private/link-local/metadata/loopback rejection, origin-pinned `jwks_uri`/`token_endpoint`, no internal redirects, hard-block not warn (§3.2, §7); (3) **identity key re-scoped** from global `(provider_issuer, subject)` to **`(org_idp_id, subject)`** to survive shared-issuer IdPs (Google Workspace) — this **amends shipped P1**; a discrete **P1-fix migration 056** is required (§2.3, §3.3, P1-fix callout); (4) hardened `id_token` validation — asymmetric-alg allowlist, reject `alg:none`/`HS*`, `azp` check on multi-`aud`, `iat`/`nbf`, strict-boolean `email_verified`, expected-issuer resolved from the attempt row not the token (§3.2); (5) service-key minting under enforcement requires an `sso_minted` admin session; corrected the §4.4 enforcement *claim* to "interactive human login is via SSO" (§4.2, §4.4, §5.2); (6) `explicit_confirm` into a pre-existing **unverified** account revokes all that row's pre-existing keys, flips `email_verified=true`, emits a security audit event (§3.3.1). MED conditions folded: durable `SsoBreakGlassGrant` table (§2.5, §4.4); browser-bound `state`/`nonce` cookie mandatory, server-store-only option removed (§3.2); JIT↔pending-invite reconciliation + `uq_org_members_org_user` (§2.6, §3.4); case-insensitive email match + legacy email-normalization migration (§3.3, P1-fix); subdomain enforcement semantics defined (§4.3). LOW/confirm items folded across §3.2, §4.2, §7, §8, §10. Updated §7 risk table, §8 audit vocabulary, §10 test strategy. §11 v1/v2 scope unchanged.
- **R1 — initial binding design (Compass):** OIDC authorization-code + PKCE, three core tables + `OidcLoginAttempt` + `api_keys.sso_minted`, JIT provisioning, org enforcement, domain verification, break-glass, anti-takeover linking. (Shipped P1: models + migration 055 with `uq_external_identity_issuer_sub`.)

---

## ⚠️ P1-FIX (migration 056) — DISCRETE WORK ITEM, DO FIRST

> **Sentinel HIGH finding 3 amends shipped P1.** Migration 055 created `external_identities` with `uq_external_identity_issuer_sub UNIQUE (provider_issuer, subject)`. That global key is **wrong for shared-issuer IdPs**: Google Workspace presents a single issuer (`https://accounts.google.com`) for *every* tenant, so two different customer orgs that both use Google Workspace would collide on `(provider_issuer, subject)` — and worse, a returning subject could resolve to the wrong org. **Before P2 builds the login flow**, ship a discrete **migration 056** that:
> 1. Adds `external_identities.org_idp_id` participation to the uniqueness: **DROP `uq_external_identity_issuer_sub`; CREATE `uq_external_identity_idp_sub UNIQUE (org_idp_id, subject)`.** (`org_idp_id` already FKs the IdP row, which is org-scoped, so this is equivalent to `(org_id, provider_issuer, subject)` while being narrower and join-free.) See §2.3.
> 2. Normalizes legacy `users.email` to lowercase (one-time data migration) so the case-insensitive JIT match in §3.3 cannot hit a unique-violation 500 on a mixed-case legacy row. See §3.3 / §3.4.
>
> Migration 056 is **strictly additive to the chain** but **alters a P1 index**; because P1 shipped on `develop` with no production SSO rows yet (`external_identities` is empty — SSO is unreleased), the index swap carries no data-rewrite risk. Atlas owns the migration; it is the first build task of R2, gating P2/P3/P4.

---

## §1 — Problem Statement / Why Now

### 1.1 The gap

`saml_sso` is already a declared Enterprise feature flag (`src/sessionfs/server/tiers.py:129`, inside `Tier.ENTERPRISE["features"]`) with **zero implementation**. There is no SSO data model, no OIDC/SAML route, no identity-provider config surface, and no enforcement path. Today the *only* way a human authenticates is:

1. `POST /api/v1/auth/signup` (`routes/auth.py:156`) — the only unauthenticated endpoint — creates a `User` row + the first `ApiKey` and returns the raw key once.
2. Every subsequent call presents that key as `Authorization: Bearer <raw_key>` and is resolved in `auth/dependencies.py:_authenticate_and_build_context` (hash lookup against `api_keys.key_hash`).
3. Email verification is a separate one-shot JWT link (`routes/auth.py:verify_email`) that flips `users.email_verified`.

There is **no magic-link login, no OAuth, no password, no social login** in the product today. Auth is API-key-only. This is a hard fact that shapes the whole design: SSO is **net-new interactive login**, not a re-skin of an existing flow.

### 1.2 Why now

- **Enterprise buyers require SSO.** It is table-stakes in every enterprise security questionnaire. We are already selling Enterprise (self-hosted + HIPAA + governance, `tiers.py:99`) and shipping the v0.11.0 entitlements/owner control plane, but cannot satisfy "members authenticate through our IdP" — the single most common procurement blocker.
- **We just built the org control plane SSO plugs into.** v0.11.0 gave us: `Entitlement` as the single source of truth (`models.py:2204`), the `owner` role + `uq_org_members_one_owner_per_org` single-owner invariant (`models.py:734`), `org_owner_transfer` two-step transfer + last-owner guards (`routes/org_members.py:perform_role_change`/`perform_member_removal`), and `OrgAuditEvent` append-only audit (`models.py:2298`). SSO is the natural next layer on that foundation.
- **The flag is a latent promise.** `saml_sso` appearing in tier output is a claim we cannot currently honor. Either we implement it or we are mis-advertising. Implement.

### 1.3 Job-to-be-done

> *As an enterprise security admin, I want my org's members to sign in to SessionFS through our corporate IdP (Okta / Entra / Google Workspace), and I want to be able to **require** that — so that offboarding a person in the IdP removes their SessionFS access, and so that no member can authenticate with a standalone password or unmanaged credential.*

Two distinct jobs, sequenced:
- **v1 (this design):** OIDC login + JIT provisioning + org enforcement + domain verification + break-glass recovery.
- **v2 (deferred, model stays forward-compatible):** SAML, SCIM, group→role mapping, IdP-initiated login, session-policy controls.

### 1.4 Non-goals (v1)

- Not a social login / consumer sign-in convenience feature. This is an **org identity control plane**.
- Not SAML, not SCIM, not group mapping, not IdP-initiated. Deferred to v2 (§7).
- Not replacing API keys or service keys. SSO is **additive** to the existing auth seam (§5).
- Not a session-revocation / session-policy engine (idle timeout, device binding). v2.

---

## §2 — Data Model

All new tables are additive. New SQLAlchemy models live in `db/models.py`; one additive Alembic migration (next number in sequence; chain is at 052 today). Forward-compatibility for SAML/SCIM is a first-class constraint, called out per table.

### 2.1 `OrgIdentityProvider` — the IdP config

One enabled IdP per org in v1 (enforced by a partial unique index, same shape as `uq_entitlements_one_active_per_owner`). The table is **protocol-tagged** so SAML rows can coexist in v2 without a schema change.

```
org_identity_providers
├── id: String(64) PK  ("oidp_" + token_hex(12))
├── org_id: FK→organizations.id ON DELETE CASCADE, NOT NULL, indexed
├── protocol: String(20) NOT NULL DEFAULT 'oidc'   -- 'oidc' (v1) | 'saml' (v2)  ← forward-compat tag
├── display_name: String(100) NOT NULL              -- "Acme Okta", shown on the login picker
├── issuer: String(500) NOT NULL                    -- OIDC issuer URL; discovery doc at {issuer}/.well-known/openid-configuration
├── client_id: String(255) NOT NULL
├── client_secret_ref: String(255) NOT NULL         -- GCP Secret Manager resource name, NEVER the secret itself (§7 risk)
├── allowed_scopes: Text NOT NULL DEFAULT '["openid","email","profile"]'  -- JSON list
├── discovery_cache: Text NULL                       -- cached OIDC discovery doc (JSON), TTL via discovery_fetched_at
├── discovery_fetched_at: TIMESTAMPTZ NULL
├── jwks_cache: Text NULL                            -- cached JWKS (JSON) for id_token signature verification
├── jwks_fetched_at: TIMESTAMPTZ NULL
├── enabled: Boolean NOT NULL DEFAULT false          -- config exists but not live until explicitly enabled
├── enforced: Boolean NOT NULL DEFAULT false         -- §4 enforcement toggle (separate from enabled)
├── created_by_user_id: FK→users.id ON DELETE SET NULL
├── created_at / updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX uq_org_idp_one_enabled_per_org  UNIQUE (org_id) WHERE enabled = true
```

**Secret storage (binding):** `client_secret_ref` holds a **GCP Secret Manager resource name** (e.g. `projects/<n>/secrets/oidp-<id>-client-secret/versions/latest`). The raw client secret is **never** persisted to the DB, never returned in any API response, never logged. This mirrors the project rule "Secrets stored in GCP Secret Manager only" and the existing `client_secret`-handling discipline. On self-hosted Helm deployments where Secret Manager is unavailable, the operator supplies the secret via a mounted K8s secret referenced by `client_secret_ref` (a `k8s://<namespace>/<secret>/<key>` URI scheme) — same indirection, never the plaintext in PG. Write path accepts the raw secret once, stores it in the secret backend, persists only the ref.

**Why `enabled` AND `enforced` are separate booleans:** an admin must be able to configure + test SSO (enabled) before flipping the org into mandatory SSO (enforced). Conflating them is how orgs lock themselves out. This separation is the precondition for break-glass safety (§4.4).

### 2.2 `OrgDomainVerification` — proof an org owns an email domain

JIT provisioning and enforcement key off **verified email domains**. An org must prove it controls a domain before SSO logins for that domain auto-join the org.

```
org_domain_verifications
├── id: String(64) PK  ("odv_" + token_hex(12))
├── org_id: FK→organizations.id ON DELETE CASCADE, NOT NULL, indexed
├── domain: String(255) NOT NULL                     -- normalized lowercase, no leading '@', e.g. "acme.com"
├── method: String(20) NOT NULL DEFAULT 'dns_txt'    -- 'dns_txt' (v1) | 'meta_tag'(future)
├── verification_token: String(128) NOT NULL         -- random token the org publishes; "sessionfs-domain-verification=<token>"
├── status: String(20) NOT NULL DEFAULT 'pending'    -- pending | verified | failed
├── verified_at: TIMESTAMPTZ NULL
├── verified_by_user_id: FK→users.id ON DELETE SET NULL
├── last_checked_at: TIMESTAMPTZ NULL
├── created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX uq_org_domain_global_verified  UNIQUE (domain) WHERE status = 'verified'   -- a domain is claimed by AT MOST ONE org
INDEX idx_org_domain_org (org_id)
```

**Global-uniqueness on `domain` WHERE verified (binding, critical):** the partial unique index makes a verified domain claimable by **exactly one org** — this is the anti-cross-tenant-hijack control. Two orgs cannot both claim `acme.com`. First-verified wins; a second org attempting to verify the same domain fails at the index. This mirrors the `git_remote_normalized` global-uniqueness pattern used for multi-repo projects.

**Proof mechanism (v1 = DNS TXT):** the org publishes a TXT record `sessionfs-domain-verification=<verification_token>` at the apex (or `_sessionfs-challenge.<domain>`). A verify endpoint does a server-side DNS lookup, confirms the token, flips `status='verified'` inside a transaction that the partial unique index serializes. Re-verification (token rotation) is supported; revocation flips back to `failed` and frees the domain.

**Free-email-provider denylist (binding):** the verify endpoint **rejects** public/consumer domains (`gmail.com`, `outlook.com`, `yahoo.com`, `icloud.com`, `hotmail.com`, …) from a maintained denylist. An org must not be able to claim `gmail.com` and then JIT-provision every Gmail user into its tenant. This is a hard gate, not advisory.

### 2.3 `ExternalIdentity` — links an IdP subject to a SessionFS User

```
external_identities
├── id: String(64) PK  ("eid_" + token_hex(12))
├── user_id: FK→users.id ON DELETE CASCADE, NOT NULL, indexed
├── org_idp_id: FK→org_identity_providers.id ON DELETE CASCADE, NOT NULL, indexed
├── provider_issuer: String(500) NOT NULL            -- denormalized issuer, snapshotted (survives idp config edit)
├── subject: String(255) NOT NULL                    -- the OIDC `sub` claim — stable, opaque, IdP-assigned
├── email_at_link: String(255) NOT NULL              -- email asserted by IdP at link time (audit)
├── linked_at: TIMESTAMPTZ NOT NULL DEFAULT now()
├── link_method: String(30) NOT NULL                 -- 'verified_email_match' | 'jit_provision' | 'explicit_confirm'
├── last_login_at: TIMESTAMPTZ NULL
├── deactivated_at: TIMESTAMPTZ NULL                  -- set when IdP/admin deprovisions (§5.4)
INDEX uq_external_identity_idp_sub  UNIQUE (org_idp_id, subject)   -- one IdP subject → one identity row, SCOPED TO THE IdP (R2)
INDEX idx_external_identity_user (user_id)
```

**Identity key is `(org_idp_id, subject)`, NOT `(provider_issuer, subject)` and NOT email (binding, R2 — Sentinel HIGH 3):** the OIDC `sub` is the stable, opaque, IdP-assigned identifier, but it is only unique **within a given issuer's tenant**. **Shared-issuer IdPs break a global `(provider_issuer, subject)` key:** Google Workspace presents one issuer (`https://accounts.google.com`) for *all* customer tenants, so org A's IdP row and org B's IdP row share an issuer — two orgs' subjects could collide, and a returning subject could resolve into the wrong org. Scoping uniqueness to **`(org_idp_id, subject)`** (where `org_idp_id` already FKs the org-scoped IdP row) keys the identity *within the org's configured IdP*, which is the correct tenancy boundary. This is equivalent to `(org_id, provider_issuer, subject)` but narrower and join-free. **R1's migration 055 shipped the global key; migration 056 (see P1-FIX callout) swaps it.** `provider_issuer` is still snapshotted on the row (survives IdP config edits, audit, and the §3.2 issuer-pin check) — it just is no longer part of the uniqueness key.

**Email is mutable at the IdP and must never be the join key for an existing link** — it is used only for the *initial* linking decision (§3.3).

**"One user may have multiple" (per ticket):** a single `User` may hold several `ExternalIdentity` rows (e.g. a consultant who is in two customer orgs' IdPs, or a user migrated across IdPs). The unique constraint is on the IdP-side key, not on `user_id` — so a user can collect multiple external identities, but no `(org_idp_id, subject)` pair can fan out to two users.

**Resolution always carries the IdP context (binding, R2):** because the key is `(org_idp_id, subject)`, the §3.3 existing-link lookup MUST be performed with the `org_idp_id` of the IdP that minted the token currently being processed — never a global `WHERE subject=…` scan. A returning identity is therefore resolved *within its own org's IdP context*, closing the shared-issuer cross-tenant resolution gap.

### 2.4 Forward-compatibility for SAML / SCIM (v2)

- **SAML:** `OrgIdentityProvider.protocol` already discriminates. SAML rows add SAML-specific columns in a v2 migration (`saml_metadata_url`, `saml_entity_id`, `saml_x509_cert_ref`) — additive, no rewrite. `ExternalIdentity.subject` holds the SAML `NameID`; `provider_issuer` holds the SAML `EntityID`. The `(provider_issuer, subject)` identity key is protocol-agnostic.
- **SCIM:** SCIM provisions/deprovisions users out-of-band (no interactive login). It writes the same `ExternalIdentity` + `OrgMember` rows this design creates via JIT, plus a v2 `scim_external_id` column on `external_identities` and a `ScimToken` table for the bearer token SCIM uses. The `deactivated_at` column already exists for the SCIM deprovision signal (§5.4).
- **Group→role mapping:** a v2 `OrgIdpGroupMapping` table (`org_idp_id`, `idp_group_claim_value`, `org_role`) consumes the `groups` claim. v1 deliberately does **not** read group claims — JIT always provisions as `member` (§3.4).

### 2.5 `SsoBreakGlassGrant` — durable, server-consulted admin break-glass (R2 — Sentinel MED)

R1 specified break-glass for admins as an **audit-only** `OrgAuditEvent`. Sentinel: an audit row is an immutable *record*, not an *enforcement input* — the §4.2 auth check cannot consult it cheaply, and "is this grant still valid / has it expired / was it revoked" has no server-side source of truth. R2 replaces the audit-only grant with a durable table that §4.2 **CONSULTS** at auth time (server-side expiry), while the `OrgAuditEvent` remains the immutable record.

```
sso_break_glass_grants
├── id: String(64) PK  ("sbg_" + token_hex(12))
├── org_id: FK→organizations.id ON DELETE CASCADE, NOT NULL, indexed
├── admin_user_id: FK→users.id ON DELETE CASCADE, NOT NULL    -- the admin this grant exempts
├── issued_by_user_id: FK→users.id ON DELETE SET NULL          -- the OWNER who issued it (owner-only)
├── expires_at: TIMESTAMPTZ NOT NULL                           -- server-side expiry; §4.2 checks now() < expires_at
├── revoked_at: TIMESTAMPTZ NULL                               -- owner can revoke early
├── created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX uq_break_glass_one_active_per_admin  UNIQUE (org_id, admin_user_id) WHERE revoked_at IS NULL AND expires_at > now()
```

> Note: a `WHERE … expires_at > now()` partial index is not portable (the predicate is non-immutable on PG and unsupported on SQLite). Atlas implements "single active grant per admin" as **`uq_break_glass_one_active_per_admin UNIQUE (org_id, admin_user_id) WHERE revoked_at IS NULL`** plus an in-transaction `SELECT … FOR UPDATE` that revokes/expires the prior row before inserting a new active grant — mirroring the `org_owner_transfer` one-pending pattern. The owner is **always exempt by construction** (§4.4 rule 1) and never needs a grant.

**§4.2 consults this table:** under enforcement, before rejecting an admin's user key, the check looks for a `SsoBreakGlassGrant` row for `(enforced_org, user)` with `revoked_at IS NULL AND now() < expires_at`. If found → admit (still audited). Expiry is enforced **server-side at read time** — a stale grant never admits.

### 2.6 `OrgMember` uniqueness gap (R2 — Sentinel MED, blocks JIT reconciliation)

JIT provisioning (§3.4) and pending-invite reconciliation (§3.4) both INSERT/UPDATE `OrgMember`. Sentinel found `org_members` currently has **no unique `(org_id, user_id)` constraint**, so a race (concurrent JIT logins, or JIT racing an invite-accept) can create **duplicate memberships → double seat consumption**. R2 requires **`uq_org_members_org_user UNIQUE (org_id, user_id)`** be added (in migration 056 alongside the index swap, or its own additive migration) so the membership upsert in §3.4 has a real conflict target. If a duplicate already exists in legacy data, dedupe in the same migration (keep the highest-privilege/oldest row). The JIT path then does an `INSERT … ON CONFLICT (org_id, user_id) DO NOTHING` / dedupe-in-txn rather than a blind INSERT.

---

## §3 — OIDC Login + Account Linking Flow

### 3.1 Protocol: Authorization Code + PKCE

v1 uses **authorization-code flow with PKCE** (RFC 7636), even though we have a confidential client (we hold the client secret). PKCE is defense-in-depth against code interception and is required by the modern OAuth 2.1 baseline; certifying IdPs all support it. No implicit flow, no hybrid flow.

### 3.2 The state machine (mirrors the activation Phase A/B pattern)

This deliberately reuses the durable single-use-token shape already proven in `routes/activation.py` (`ActivationAttempt`: row committed before anything external happens, token hashed never stored raw, rowcount-1 consume guard).

**New table `OidcLoginAttempt`** (the CSRF/replay anchor — §7):

```
oidc_login_attempts
├── id: String(64) PK
├── org_idp_id: FK→org_identity_providers.id ON DELETE CASCADE
├── state: String(128) NOT NULL          -- random; returned in callback, matched exactly AND against the browser cookie (R2)
├── pkce_code_verifier_hash: String(128) NOT NULL  -- hash of the verifier; raw verifier held only in the HttpOnly state cookie
├── nonce: String(128) NOT NULL          -- echoed in id_token `nonce` claim, matched (replay defense); also echoed in the browser cookie (R2)
├── redirect_after: String(500) NULL     -- post-login destination, validated against an ALLOWLIST (§7 open-redirect)
├── status: String(20) NOT NULL DEFAULT 'pending'   -- pending | consumed | expired
├── expires_at: TIMESTAMPTZ NOT NULL     -- short TTL, e.g. 10 min
├── created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX idx_oidc_login_attempt_state (state, status, expires_at)
```

**Phase A — `GET /api/v1/auth/sso/{org_slug}/start`** (unauthenticated):
1. Resolve the org's enabled `OrgIdentityProvider`. 404 if none (non-oracular: "SSO is not configured for this organization").
2. Fetch/refresh the OIDC discovery doc + JWKS (cached on the IdP row, TTL'd) — **through the SSRF guard (§3.2.1).**
3. Generate `state`, `nonce`, PKCE `code_verifier`/`code_challenge`. Insert an `OidcLoginAttempt` row (committed). Store `state`, `nonce`, and the raw `code_verifier` in a short-lived **`HttpOnly`+`Secure`+`SameSite=Lax` signed cookie** scoped to the callback path; persist only the verifier *hash* server-side. **The server-store-only option from R1 is REMOVED (R2 — Sentinel MED):** the browser-bound cookie is now **mandatory** so the callback can prove the same browser that began the flow is completing it — without it, an attacker who learns/forces a `state` can complete a login-CSRF / session-fixation against a victim's browser. State that exists *only* server-side keyed by `state` does not bind the browser.
4. Validate `redirect_after` against an **exact-origin allowlist** (dashboard origin string-equality, registered CLI loopback); reject anything off-allowlist (§7). `redirect_after` is never substring/prefix-matched.
5. 302 to the IdP's authorization endpoint with `response_type=code`, `scope`, `state`, `nonce`, `code_challenge`, `code_challenge_method=S256`, `redirect_uri` = our fixed, pre-registered callback.

**Phase B — `GET /api/v1/auth/sso/callback`** (unauthenticated, the IdP redirect target):
1. Read `state` + `code` from query. **Compare `state` against the value in the browser cookie (R2 — must match) AND** look up the `OidcLoginAttempt` by `state`; reject if missing/expired/already consumed (atomic `UPDATE ... WHERE status='pending'` rowcount-1 consume guard — replay defense, identical to the activation Phase-B consume). A `state` that is valid server-side but absent/mismatched in the cookie is rejected (`state_browser_mismatch`).
2. Verify the PKCE verifier (from the cookie) against the stored hash.
3. **Resolve the EXPECTED issuer + endpoints from the `OidcLoginAttempt` → `org_idp_id` → `OrgIdentityProvider` row (R2 — mix-up defense), never from anything in the inbound request or the token.** Exchange `code` + `code_verifier` at the IdP `token_endpoint` (server-to-server through the SSRF guard §3.2.1; presents `client_id` + the Secret-Manager-resolved client secret).
4. **Verify the `id_token` (R2 — hardened, Sentinel HIGH 4):**
   - **Signature** against the cached JWKS, with the **allowed algorithm set pinned to the asymmetric algs advertised in the JWKS** (e.g. `RS256`/`ES256`). **Explicitly reject `alg:none` and any `HS*` (HMAC) alg** — the HMAC-with-public-key alg-confusion attack must be impossible. The verifier never selects the alg from the token header alone.
   - **`iss` == the EXPECTED issuer resolved from the attempt row** (step 3) — NOT the token's self-asserted `iss`, and NOT the IdP whose callback URL was hit. (Token-issuer mix-up defense.)
   - **`aud`:** may be a string OR an array. `aud` must contain our `client_id`; **if `aud` is an array with more than one entry, `azp` MUST be present and `== client_id`** (multi-audience confusion defense).
   - **`exp` not passed; `iat` present and not in the future (small skew allowed); `nbf` (if present) not in the future.**
   - **`nonce` == the stored nonce** (replay defense).
   - Reject on ANY mismatch with a specific reason code (§8).
5. Extract `sub`, `email`, `email_verified`. **`email_verified` is treated as STRICT boolean `true`** — the literal JSON boolean. A string `"true"`, `1`, or any truthy-but-not-boolean value is treated as **unverified** and rejected at §3.3 step 2a. (Some IdPs emit string claims; we do not coerce.)
6. Run the **account resolution / linking** logic (§3.3), using the attempt's `org_idp_id` as the IdP/org context.
7. On success, mint a SessionFS session for the resolved `User` and hand back credentials (§3.5). Mark the attempt `consumed`; clear the browser cookie.

#### 3.2.1 SSRF guard on all issuer-derived server-side fetches (R2 — Sentinel HIGH 2, BINDING)

Every server-side HTTP fetch whose target is derived from the **admin-supplied `issuer`** — the discovery document (`{issuer}/.well-known/openid-configuration`), the `jwks_uri`, and the `token_endpoint` — is a server-side request to an attacker-influenced URL and MUST pass a hardened SSRF guard. **CEO decision 3 (all paid tiers, §6/§13) raises this surface materially** — more orgs, more admin-supplied issuers — so this is a **hard block, not a warning**:

1. **https-only.** Reject any non-`https` scheme outright (no `http`, `file`, `gopher`, etc.).
2. **Resolve-then-check the host.** Resolve the hostname to its IP(s) and **reject** if any resolved address is loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), or the cloud metadata address (`169.254.169.254`, `fd00:ec2::254`). Guard against DNS-rebinding by pinning the connection to the validated IP (resolve once, connect to that IP, or re-validate on connect).
3. **Re-run the full IP-range guard on the discovery-advertised `jwks_uri` and `token_endpoint` — do NOT same-origin-pin them (R2-N1, Sentinel re-confirm).** A naive "advertised endpoints MUST share the `issuer` origin" rule is **wrong for real OIDC and breaks Google Workspace** (a named v1 certify target): issuer `https://accounts.google.com` legitimately advertises `token_endpoint=https://oauth2.googleapis.com/token` and `jwks_uri=https://www.googleapis.com/oauth2/v3/certs` — different hosts. The correct guard: (a) fetch the discovery doc **only** from the exact configured `issuer` URL through the item-2 guard, then (b) run the **complete item-1 + item-2 checks** (https-only, resolve-then-reject loopback/link-local/private/metadata, pin-to-validated-IP) **independently on each advertised `jwks_uri`/`token_endpoint` host**. The advertised endpoints are trusted because the discovery doc arrived from the verified issuer origin over an already-guarded channel — **not** because they are same-origin. An internal/private/metadata host advertised in the discovery doc is still rejected; a different *public* host is allowed. (Optional hardening: allowlist the discovered hosts onto the `OrgIdentityProvider` row at config time so later refreshes can't silently repoint.) **Implementer note: dropping the SSRF check entirely to "fix" the Google-Workspace breakage reopens HIGH 2 — the check moves from same-origin to per-host IP-range, it is never removed.**
4. **Follow no redirects to internal hosts.** Disable automatic redirect following on these fetches (or re-run the full guard on every hop). A `302` to `169.254.169.254` must never be followed.
5. Applies on **every** fetch, including cache refreshes — not just first configuration.

**Follow-up for Forge (flagged):** network-egress restriction at the platform layer (Cloud Run egress controls / VPC-SC / explicit allowlist) as defense-in-depth behind the application guard, so even a guard bypass cannot reach the metadata server. Filed as a deployment follow-up ticket on approval.

### 3.3 Account resolution + linking — ANTI-TAKEOVER (the critical security area)

This is the single most dangerous part of SSO. An attacker who controls an IdP `sub` must **never** be able to link it to a victim's existing email-verified SessionFS account and thereby steal it. The exact decision tree (binding):

```
INPUT: org_idp_id, idp_issuer, sub, idp_email, idp_email_verified (STRICT bool), org (derived from org_idp_id)
       (org_idp_id comes from the OidcLoginAttempt row — §3.2 step 6 — NOT from the token.)

1. EXISTING LINK?  (R2 — scoped to the IdP, never a global subject scan)
   existing = SELECT * FROM external_identities WHERE org_idp_id = org_idp_id AND subject = sub
   IF existing AND existing.deactivated_at IS NULL:
       → this sub is already linked WITHIN THIS ORG'S IdP. Log in as existing.user_id. DONE.
       (email changes at the IdP do NOT re-trigger linking — (org_idp_id, sub) is the key.
        Shared-issuer IdPs (Google Workspace) resolve to the right org because the lookup is
        scoped by org_idp_id, not by the globally-shared issuer.)
   IF existing AND existing.deactivated_at IS NOT NULL:
       → identity was deprovisioned. REJECT (account_deactivated). Re-provision is admin-only. DONE.

2. NO LINK YET — must we link to an existing User, or JIT-create?

   2a. HARD PRECONDITION: the IdP MUST assert email_verified === true (STRICT JSON boolean — §3.2 step 5).
       IF NOT idp_email_verified: REJECT (idp_email_unverified).
       We never trust an unverified (or string-"true") IdP email for any linking or provisioning decision.

   2b. normalized_email = lower(idp_email); domain = part after '@'
       IF domain NOT IN org's verified domains (per the subdomain rule §4.3):
           REJECT (email_domain_not_verified_for_org).
       (Even with a valid IdP token, the email's domain must belong to THIS org. Stops a
        misconfigured IdP from provisioning arbitrary external emails into the tenant.)

   2c. existing_user = SELECT * FROM users WHERE lower(email) = normalized_email   (CASE-INSENSITIVE — R2)
       (Legacy users.email rows are lowercase-normalized by migration 056 so this comparison
        is collision-free and cannot raise a unique-violation 500 on JIT insert. §3.4 / P1-FIX.)

   2d. IF existing_user IS NULL:
           → JIT PROVISION (§3.4). Create User (email_verified=true, sourced from the
             verified IdP claim + verified domain), create ExternalIdentity
             (link_method='jit_provision'), upsert OrgMember(role='member'), reconcile any
             pending invite. DONE.

   2e. IF existing_user EXISTS:
           --- THE TAKEOVER DECISION POINT ---
           ALLOW automatic link ONLY IF ALL of:
              (i)   lower(existing_user.email) == normalized_email   (case-insensitive exact match)
              (ii)  existing_user.email_verified == true             (the SessionFS side is itself verified)
              (iii) existing_user is ALREADY a member of `org`        (OrgMember exists for this org)
           IF all three hold:
               → link: INSERT ExternalIdentity(link_method='verified_email_match'). Log in. DONE.
           ELSE:
               → DO NOT auto-link. Require EXPLICIT LINKING CONFIRMATION (§3.3.1).
                 (The unverified-existing-account case is the pre-seeding vector — §3.3.1 handles it.)
```

**Why all three conditions for auto-link (rationale, binding):**
- **(i) exact verified-email match** is the baseline.
- **(ii) the existing SessionFS account's email must itself be verified.** If a SessionFS user signed up with `victim@acme.com` but never clicked the verification link (`users.email_verified == false`), that email is *unproven on our side*. Auto-linking an IdP `sub` to an unverified-email account is exactly the takeover vector: an attacker who pre-registers `victim@acme.com` (unverified) and later the real victim logs in via SSO could otherwise collide. Requiring `email_verified=true` on the existing row closes this. An unverified existing account routes to explicit confirmation, never silent link.
- **(iii) the existing user is already an org member.** A verified-email match for a user who is *not* in the org should not silently fold an outside account into the tenant on the IdP's say-so. This is the conservative default; explicit confirmation handles the legitimate "I already had a personal SessionFS account, now my employer turned on SSO" case (§3.3.1).

#### 3.3.1 Explicit linking confirmation (the safe fallback)

When auto-link is denied but a legitimate match is plausible, we do **not** log the IdP identity straight into the existing account. Instead:
- The user must prove control of **both** sides. The flow: the SSO session is held in a pending state; we send a one-time confirmation link to the `users.email` on file (the SessionFS side) — same mechanism class as `routes/auth.py:verify_email`. Clicking it (proving control of the existing account's mailbox) + the active just-completed IdP session together authorize the link. Only then is the `ExternalIdentity` row written with `link_method='explicit_confirm'`.
- This guarantees an attacker cannot link without **simultaneously** controlling the IdP subject AND the existing account's verified mailbox.

**Pre-seeding defense (R2 — Sentinel HIGH 6, BINDING):** the dangerous case is `explicit_confirm` folding into an existing account that was **previously unverified** (`users.email_verified == false`). An attacker can pre-register `victim@acme.com` *before* the org turns on SSO, leaving it unverified, hoping to later fold the victim's real IdP identity (and any credentials/data already attached to the pre-seeded row) together. To neutralize this, **at the moment an `explicit_confirm` link completes into a pre-existing account, in the SAME transaction:**
1. **Revoke ALL pre-existing API keys on that user row** (set `revoked_at` + `revoke_reason='sso_explicit_confirm_reseed_guard'` on every `api_keys` row for the user, including any pre-seeded keys). The user re-mints a fresh key via the very SSO login that is completing — so legitimate users are uninterrupted, but an attacker's pre-planted key dies.
2. **Flip `email_verified=true`** (the confirmation link just proved mailbox control, so the email is now genuinely verified — and the account leaves the unverified-pre-seed state permanently).
3. **Emit a security audit event** `sso_identity_link_reseed_guard` (an `OrgAuditEvent`, §8) recording the user, the `org_idp_id`, the `sub`, the count of keys revoked, and the prior `email_verified` value — so a pre-seeding attempt is loud and forensically reconstructable.

The same key-revocation + audit applies whenever `explicit_confirm` lands on an account whose `email_verified` was `false` at link time, regardless of how the account was created. A verified-at-link account (which by §3.3 would have auto-linked anyway and only reaches explicit_confirm via the not-yet-a-member branch) does not need key revocation, but the audit event is still emitted for completeness.

### 3.4 JIT provisioning

On first SSO login by a verified-domain user with no existing account (§3.3 step 2d):
- Create `User(email=normalized_email, email_verified=true, display_name=<IdP name claim>)` — `normalized_email` is already lowercased (§3.3 step 2b); legacy rows are normalized by migration 056 so the insert cannot collide case-insensitively. `email_verified=true` is justified because the IdP asserted `email_verified` **and** the domain is org-verified — two independent proofs.
- **Upsert `OrgMember(org_id, user_id, role='member')`** via `INSERT … ON CONFLICT (org_id, user_id) DO NOTHING` against the new `uq_org_members_org_user` constraint (§2.6) — never a blind INSERT — so concurrent JIT logins or a JIT racing an invite-accept cannot create a duplicate membership / double seat. **JIT always provisions `member`** — never `admin`/`owner`. Role elevation is a deliberate org-admin action. (Group→role mapping is v2.)
- **Pending-invite reconciliation (R2 — Sentinel MED):** before/while creating the membership, look up any **pending `OrgInvite`** matching `(org_id, lower(email))`. If one exists:
  - **Consume/cancel it** (mark accepted/cancelled) so it cannot be redeemed a second time — JIT already created the membership; a leftover pending invite is a double-seat / stale-link hazard.
  - **If the pending invite carried a HIGHER role than `member`** (e.g. the admin invited this person as `admin`), the membership must NOT be silently downgraded to `member`, and JIT must NOT silently elevate either. R2 rule: **honor the invited role on the reconciled membership** (the invite is an explicit admin act), capped below `owner` (JIT/invite never creates an owner — ownership only via `org_owner_transfer`). The reconciliation is the one place a JIT login may seat a non-`member` role, and only because a prior explicit admin invite authorized it.
  - Dedupe defensively: if a membership somehow already exists, do not add a second; reconcile onto the existing row.
- Create `ExternalIdentity(org_idp_id, link_method='jit_provision')`.
- **Seat enforcement:** JIT provisioning consumes a seat. It MUST respect `Organization.seats_limit` / the resolved `Entitlement.seats_limit`. If the org is at its seat cap, JIT login is **rejected** with `seat_limit_reached` (the org admin must add seats or remove a member). Reuse the seat-count discipline already in the invite-accept path (`org_members.py`). A reconciled pending invite does **not** double-count — the seat the invite reserved is the seat JIT consumes. This prevents an org from silently exceeding its paid seat count via SSO.
- All of the above happen in **one transaction**.

### 3.5 What the SSO login returns

Because the product is API-key-based, the SSO callback must bridge into a credential the CLI/dashboard already understand. v1 approach:
- **Dashboard:** the callback sets the dashboard's existing session cookie/token and 302s to `redirect_after`. The dashboard already calls `/api/v1/auth/me` to hydrate identity.
- **CLI:** `sfs auth login --sso --org <slug>` opens the browser to the `start` URL with a loopback `redirect_after`; the callback hands the CLI a freshly-minted **user `ApiKey`** (the existing `create_api_key` mechanism, `routes/auth.py:289`) scoped to that user, returned once. The key is the bridge — the CLI keeps working unchanged after login. Under enforcement (§4) these SSO-minted keys are the *only* keys an enforced human may mint.

**`api_keys.sso_minted` is server-set ONLY (R2 — Sentinel LOW, BINDING):** the `sso_minted=true` flag is written **exclusively** by the SSO callback's key-mint path. It is **never** accepted as input on any route — not on `create_api_key`, not on the personal-key surface (`/api/v1/auth/me/api-keys`), not on the admin mint-on-behalf path, not on service-key creation. Any request body field named `sso_minted` (or equivalent) is ignored/stripped. Because §4.2 enforcement keys off this flag, a client-settable `sso_minted` would be a trivial enforcement bypass. The column is treated like an internal provenance stamp, on par with `key_kind`.

---

## §4 — Org-Level SSO Enforcement

### 4.1 Semantics

When an org sets `OrgIdentityProvider.enforced = true`:
- **Any human whose email domain is one of the org's verified domains MUST authenticate via SSO.** Specifically:
  - The **password/magic-link path is gated** — but there is no password/magic-link path today, so this reduces to: such a user cannot create a fresh standalone account that bypasses SSO. `POST /api/v1/auth/signup` rejects (`sso_enforced_use_sso`) when the email's domain is an enforced verified domain.
  - **API-key login is gated for enforced human users.** This is the teeth of enforcement. See §4.2 — it must be done carefully so service keys are untouched.
- Enforcement is **scoped to the org's verified domains only.** A user whose email domain is not claimed by any enforcing org is unaffected. A consultant in a non-enforced org is unaffected.

### 4.2 How API-key gating works WITHOUT breaking service keys (binding)

The auth seam (`auth/dependencies.py`) already cleanly separates two actor classes via `AuthContext.key_kind` (`'user'` | `'service'`) and the two dependency tracks (`get_current_user` rejects service keys; `require_scope` admits them). Enforcement hooks **only the human (`key_kind == 'user'`) path**:

In `_authenticate_and_build_context`, after the user is resolved and `AuthContext` is built, add an **enforcement check applied only when `ctx.key_kind == 'user'`**:
```
IF ctx.key_kind == 'user':
    enforced_org = org for which (user's email domain is a verified domain AND that org's IdP.enforced)
    IF enforced_org is not None:
        IF user is the OWNER of enforced_org:                      # §4.4 rule 1 — always exempt
            ADMIT (still audited on owner-exempt auth — §4.4)
        ELIF an active SsoBreakGlassGrant exists for (enforced_org, user):   # §2.5 — server-consulted, server-side expiry
            ADMIT (audited)
        ELSE:
            # R2 — Sentinel HIGH 1: sso_minted ALONE is NOT sufficient.
            # The key must be sso_minted AND backed by a live IdP identity that is STILL a member.
            IF api_keys.sso_minted == true
               AND user holds an ACTIVE (deactivated_at IS NULL) ExternalIdentity for enforced_org's IdP
               AND user is still an OrgMember of enforced_org:
                   ADMIT
            ELSE:
                   REJECT 403 sso_required  { org_slug, sso_start_url }
```

**R2 — Sentinel HIGH 1 (deprovisioning MUST revoke access — the product's core SSO promise):** R1's check admitted a key on `sso_minted == true` **OR** an active ExternalIdentity. That is the bug: a key minted via SSO **stays valid forever** even after the IdP/admin deprovisions the person — exactly the offboarding scenario SSO exists to solve. R2 closes this with **both** belt and suspenders:

1. **§4.2 requires a LIVE identity (this section).** Under enforcement, a `key_kind=='user'` key passes only if it is `sso_minted` **AND** the user still holds a non-deactivated `ExternalIdentity` for the enforcing org's IdP **AND** is still an `OrgMember`. The instant the identity is deactivated or the membership is removed, the key stops resolving — no separate revocation needed for the *enforced* path.
2. **Deprovision ALSO explicitly revokes the user's `sso_minted` keys (§5.4).** Because not every consumer reads through the enforced path identically, member-removal and IdP-deactivation **proactively revoke** the user's `sso_minted` API keys (set `revoked_at`). This is the cleaner, unambiguous model and the one R2 adopts as primary: **deprovisioning is an explicit, immediate key-revocation event**, and §4.2's live-identity requirement is the backstop. Spelled out in §5.4.

- **Service keys are categorically exempt from the SSO *enforcement* check.** `ctx.key_kind == 'service'` → the enforcement block above is skipped entirely. Service keys are non-human, org-bound (`api_keys.org_id`), and scoped (`require_scope`). They MUST keep working under enforcement — a CI reviewer/implementer runner authenticating with a service key is not a person and is not subject to SSO. **This is a hard acceptance criterion.** (The residual that a *human* can mint and interactively drive a service key is addressed at the mint boundary below, §4.2.1 — not by gating service-key *use* at auth time.)
- **New column `api_keys.sso_minted: Boolean DEFAULT false`** (server-set only — §3.5) marks keys minted by the SSO callback (§3.5). Under enforcement, a human's pre-SSO legacy keys stop working (they must re-login via SSO to mint a fresh key); but the moment they re-login the new key carries `sso_minted=true` AND a live ExternalIdentity, and works. This gives a clean cutover: turning on enforcement invalidates unmanaged human keys but is recoverable by a single SSO login.
- **Grace/transition:** turning on enforcement does NOT delete existing keys; it gates them at auth time. An org admin can preview "N human keys will require re-login" before flipping `enforced`.

#### 4.2.1 Service-key minting boundary under enforcement (R2 — Sentinel HIGH 5)

Sentinel: a human admin can, under enforcement, **mint a service key and then interactively use it** — fully bypassing SSO, because service-key *use* is categorically exempt (and must remain so for CI). The fix is at the **mint** boundary, not the use boundary:

- **Under enforcement, minting a service key — the `POST /api/v1/orgs/{org_id}/service-keys` (and rotate) path — REQUIRES that the acting admin's current session is itself `sso_minted` (and SSO-valid per §4.2).** A human admin holding only a legacy/non-SSO key cannot create or rotate a service key while their org enforces SSO; they must SSO-login first. This ensures the *human gateway* to minting automation credentials is itself behind SSO, so an admin cannot sidestep their own org's SSO mandate by hiding behind a freshly-minted service key.
- **Pre-existing service keys are a documented residual.** Service keys that already exist when enforcement is turned on continue to function (they must — CI depends on them) and are not retroactively gated. An org that wants to fully rotate its automation behind SSO-minted-admin provenance rotates its service keys after enabling enforcement. This residual is explicitly called out for Shield to certify the precise claim (§4.4).
- This boundary lives in the service-key route, not in `_authenticate_and_build_context` — it does not touch the hot auth path and does not affect service-key *use*.

### 4.3 Allowed-domain / domain-verification behavior

- Enforcement only applies to **verified** domains (`org_domain_verifications.status='verified'`). An org cannot enforce against a domain it hasn't proven (the verify gate blocks it; the enforce toggle is disabled in the UI until ≥1 verified domain exists).
- A user whose email is on a verified-but-non-enforcing org's domain is unaffected.
- Multi-domain orgs: enforcement covers **all** verified domains of the org.

**Subdomain semantics (R2 — Sentinel MED, BINDING rule):** verifying `acme.com` matches `acme.com` **exactly — it does NOT automatically cover subdomains** like `eng.acme.com`. Domain matching for both JIT (§3.3 step 2b) and enforcement is an **exact, case-insensitive equality** on the full email domain against the set of verified-domain rows. Rationale: subdomains are frequently delegated to teams or third parties an org does not fully control; auto-covering them would let an org's enforcement/JIT reach an email namespace it never proved. An org that wants `eng.acme.com` **verifies it as a separate `OrgDomainVerification` row** (each subdomain proven independently via its own DNS TXT). This is the conservative, no-surprises rule; wildcard/subtree verification is explicitly a **v2** consideration (a future `include_subdomains` flag on the verification row), not v1.

### 4.4 Break-glass owner/admin recovery (binding — must never lock out the owner)

A misconfigured IdP (wrong issuer, expired client secret, IdP outage) must **never** permanently lock an org owner out of their own org. This ties directly to the v0.11.0 last-owner guards (`org_members.py:perform_role_change` / `perform_member_removal`, `uq_org_members_one_owner_per_org`). Rules:

1. **The `owner` is always exempt from SSO enforcement at the auth layer.** The §4.2 enforcement check has a hard carve-out: `IF user is the owner of enforced_org → ADMIT`. The owner can ALWAYS authenticate with a user API key, even when SSO is broken. The owner is the break-glass account by construction.
   - Rationale: there is exactly one owner per org (`uq_org_members_one_owner_per_org`), ownership only moves via the audited `org_owner_transfer` two-step, and the owner already cannot be removed/demoted into a last-owner-less state. Making the owner the recovery anchor reuses an invariant we already enforce.
   - **R2 — Sentinel LOW (recommended):** because the owner is the permanent SSO carve-out, owner-exempt authentications should be **loudly audited** (every owner auth under an enforcing org emits/aggregates an audit signal, `sso_owner_exempt_auth`), and we **recommend the owner enable MFA on their SessionFS account** (a future first-party MFA control; documented as a customer-facing recommendation now). The owner is the highest-value standing exemption and should be the most observable account.
2. **Admins get a DURABLE break-glass grant, server-consulted — not a blanket exemption (R2 — Sentinel MED).** Admins ARE subject to enforcement (so an enterprise can require *all* staff including admins to use SSO). The **owner** can issue a time-boxed **break-glass grant** to a specific admin (default 1 hour, §13) via `POST /api/v1/orgs/{org_id}/sso/break-glass` (owner-only). R1 recorded this as an audit row only; **R2 persists it as a durable `SsoBreakGlassGrant` row (§2.5) that §4.2 CONSULTS at auth time with server-side expiry** (single active grant per admin; owner can revoke early). The immutable `OrgAuditEvent` (`sso_break_glass_issued`, plus `sso_break_glass_revoked`/`sso_break_glass_used`) remains the audit record. This lets the granted admin authenticate with an API key while the IdP is fixed, and the grant **actually expires server-side** rather than relying on an un-consulted audit log.
3. **Enforcement can be disabled by the owner at any time** via `PUT .../sso` with `enforced=false`. Because the owner is never gated (rule 1), the owner can always reach this endpoint to turn enforcement off. There is no state in which the org is unrecoverable.
4. **Disabling/deleting the IdP** (`enabled=false`) auto-clears `enforced` (you cannot enforce against a disabled IdP). Audited as `sso_disabled`.

This satisfies the ticket's "an owner must never be locked out by a misconfigured IdP" with a concrete, invariant-backed path.

**Corrected enforcement claim (R2 — Sentinel HIGH 5, BINDING — Shield certifies this exact wording):** R1 implied enforcement means "no key works except an SSO-minted one." That over-claims. The **precise, certifiable claim** is:

> **Under SSO enforcement, interactive *human* login to SessionFS is via the org's SSO.** A human's user API keys are valid only when SSO-minted **and** backed by a live, non-deactivated IdP identity that is still an org member (§4.2). The owner is a deliberate, audited break-glass exemption; granted admins are a durable, expiring, owner-issued exemption (§2.5). **Service keys (non-human automation) remain valid** — they are categorically out of scope of human SSO. Newly minting a service key under enforcement requires an SSO-minted admin session (§4.2.1); **service keys that pre-date enforcement are a documented residual** until rotated.

Shield owns certifying that customer-facing SSO language matches this claim exactly — neither "no key works" (false, service keys work) nor a weaker statement that hides the live-identity requirement.

---

## §5 — Interactions With Existing Systems

### 5.1 Org membership + owner/admin safety

- JIT provisioning creates `OrgMember(role='member')` only (§3.4) — never elevates.
- SSO never bypasses the v0.11.0 owner/admin guards. Role changes still go through `perform_role_change`; ownership still moves only via `org_owner_transfer`. SSO is an *authentication* layer; it does not touch *authorization*/role logic.
- The single-owner invariant (`uq_org_members_one_owner_per_org`) is the backbone of break-glass (§4.4).

### 5.2 Service keys (binding — must keep working)

- Service keys (`api_keys.key_kind='service'`, org-bound, scoped via `require_scope`) are **categorically exempt** from SSO enforcement at *use* time (§4.2). The enforcement check is gated on `key_kind == 'user'`.
- **Service-key creation/rotation (`/api/v1/orgs/{org_id}/service-keys`) gains ONE constraint under enforcement (R2 — §4.2.1):** the acting admin's session must itself be SSO-minted to *mint or rotate* a service key while the org enforces SSO. The minted service key's subsequent *use* remains fully exempt. Without enforcement, service-key mint/rotate is unchanged. A service key is not a person and has no IdP identity.
- **Pre-existing service keys** (minted before enforcement) keep working and are a documented residual (§4.4) until the org rotates them.
- `assert_service_key_can_access_project` and the cross-org boundary logic are untouched.
- **`client_secret_ref` namespace validation (R2 — Sentinel LOW):** when an admin sets/updates an IdP's `client_secret_ref`, the server validates the ref resolves **within the org's own secret namespace** (the per-org Secret Manager prefix / the org-scoped K8s namespace), so one org cannot point its IdP config at another org's (or a platform) secret. Reject refs outside the org's namespace.

### 5.3 Active tickets / agent runs / work queues (binding)

- A service-key reviewer/implementer runner (Codex/DeepSeek/CI agent) authenticating with a service key calls `require_scope('agent_runs:write')` etc. and is **never** subject to SSO enforcement (§5.2). Turning on org SSO MUST NOT break CI agent runs, ticket mutation by service keys, or work-queue execution. This is explicitly tested (§8).
- Human-initiated ticket/agent-run actions go through the user-key path; if the human is enforced and hasn't SSO-logged-in, they get `sso_required` and re-login — the work itself is unaffected once they hold an SSO-minted key.

### 5.4 Deprovisioning (when SSO removes a user / IdP says deactivated)

**R2 — Sentinel HIGH 1 (deprovisioning is an explicit, immediate access-revocation event — BINDING, the core SSO promise):** R1 left a hole where a deactivated user's `sso_minted` key kept working because §4.2 admitted on `sso_minted` alone. R2 closes it on **both** sides: §4.2 now requires a *live* ExternalIdentity + membership (§4.2), **and** the deprovision paths below proactively **revoke the user's `sso_minted` keys**. The chosen, primary model is **deprovision-revokes-keys**.

- **`perform_member_removal` changes (BINDING):** when an org admin removes a member (the v0.11.0 `org_members.py:perform_member_removal` path), in the **same transaction** that removes the `OrgMember`:
  1. Set `external_identities.deactivated_at = now()` for **every** `ExternalIdentity` of that user whose `org_idp_id` belongs to **this org** (the user may be in other orgs' IdPs — only this org's identities are tombstoned).
  2. **Revoke the user's `sso_minted` API keys** that were minted under this org's SSO (set `revoked_at = now()`, `revoke_reason='member_removed'`). Concretely: revoke the user's `key_kind='user'` keys with `sso_minted=true`. (A user who is only in this one org has all their SSO keys revoked; a multi-org consultant keeps keys minted under other orgs' SSO — keys carry their minting org via the SSO callback provenance, so revocation is scoped to this org's minted keys.)
  3. Emit `sso_identity_deactivated` + a key-revocation audit signal (§8).
  Net effect: **member removal revokes SessionFS access immediately**, not "eventually at next auth," and not "only under enforcement."
- **IdP-deactivate changes (BINDING):** deactivating a user's identity at the IdP level — the admin disabling a single `ExternalIdentity` (a future per-identity admin action) — sets `deactivated_at` AND revokes that user's `sso_minted` keys for this org, identically to member-removal step 2–3. Disabling the **whole IdP** (`enabled=false`) does not delete users (data-stays, access-revoked invariant) and auto-clears `enforced` (§4.4 rule 4); it disables further SSO login. Enforced human keys then fail (their identities can no longer be re-validated/re-minted) until re-config; the owner break-glass path (§4.4) keeps the org recoverable.
- **v1 interactive signal:** when a deprovisioned IdP user attempts SSO login, the IdP refuses to issue a token / the `sub` no longer resolves → they simply cannot get in. Additionally, the §3.3 step-1 `deactivated_at` check rejects a tombstoned identity (`account_deactivated`).
- **SCIM push deprovision (v2):** SCIM `DELETE`/`active=false` sets `deactivated_at` out-of-band and reuses the **same key-revocation routine** member-removal calls. The column exists now precisely so v2 SCIM is additive — and R2 makes the revocation a shared helper so SCIM inherits it for free.

**Future email-change endpoint (R2 — Sentinel LOW, pre-empt):** there is no email-change endpoint today, but if one is added it must NOT let a user move their email *out of* an enforced verified domain to dodge enforcement, nor *into* one to hijack JIT matching. Any future email-change must re-run the enforcement-domain check and re-validate/break existing `ExternalIdentity` links on email change. Flagged so the future endpoint does not silently undermine §4.2.

### 5.5 Billing tiers

- OIDC SSO is gated behind the `oidc_sso` feature flag on every paid tier (CEO decision 3, R2; §6). The IdP config endpoints call `check_feature(ctx, 'oidc_sso')` (a distinct flag, not `saml_sso` — see §13 Q4). Tier is resolved through the existing entitlement-first chain (`tier_gate.get_effective_tier`, entitlement → org fallback).

### 5.6 `/me` enrichment

`routes/auth.py:get_me` gains optional fields so the dashboard can render SSO state: `sso_enforced` (is the user's org enforcing), `external_identities` (count / providers). No change to the existing tier/org enrichment.

---

## §6 — Recommended Tier Gate

**Decision (R2): OIDC SSO is gated on `oidc_sso`, present on ALL PAID TIERS** (CEO decision 3). R1 recommended Enterprise-only (matching the existing `saml_sso` placement, `tiers.py:99`); the CEO overrode this to make SSO a mid-market wedge vs. "SSO tax" competitors. The free tier remains excluded.

- Add a distinct feature flag **`oidc_sso`** to every paid tier's `["features"]` rather than overloading `saml_sso`. Rationale: SAML and OIDC are separately purchasable/marketable capabilities, and a v2 SAML ship should not appear "already included" just because OIDC shipped. Keep `saml_sso` reserved for the v2 SAML implementation.
- Gate **all** IdP-config, domain-verification, and enforcement-toggle endpoints behind `check_feature(ctx, 'oidc_sso')` + `check_role(ctx, 'admin')` (owner/admin). The **login** endpoints (`/sso/{slug}/start`, `/sso/callback`) are unauthenticated by nature but only function for an org that has the feature (the IdP row can only exist if a paid-tier admin created it).
- **CEO decision 3 (R2):** OIDC SSO is extended to **all paid tiers** (not Enterprise-only) as a mid-market wedge vs. "SSO tax" competitors. **Security consequence Sentinel flagged:** widening to all paid tiers materially raises the count of orgs supplying their own `issuer`, which raises the SSRF attack surface — therefore the §3.2.1 SSRF guard is a **hard block, not a warning** (already binding above). The tier gate becomes `check_feature(ctx, 'oidc_sso')` present on every paid tier; the free tier remains excluded. Ledger confirms the exact tier list and whether `oidc_sso` is a separate add-on line.

---

## §7 — Security Risks + Mitigations

| Risk | Mitigation (binding) |
|------|----------------------|
| **SSRF via admin-supplied issuer** (R2 — HIGH 2) | §3.2.1 hardened guard on **every** issuer-derived fetch (discovery, `jwks_uri`, `token_endpoint`): **https-only; resolve-then-reject loopback/link-local/private/metadata (169.254.169.254) ranges; `jwks_uri`/`token_endpoint` pinned to the issuer origin; no redirects to internal hosts**; DNS-rebind hardened. **Hard block, not warn** (CEO decision 3 = all paid tiers raises the surface). Forge egress-restriction follow-up flagged. |
| **Account takeover via linking** | §3.3 decision tree: auto-link ONLY on (case-insensitive verified email match) AND (existing account email_verified) AND (already org member); strict-boolean IdP `email_verified` required; everything else routes to explicit dual-control confirmation (§3.3.1). **Pre-seeded unverified account**: `explicit_confirm` revokes ALL that row's pre-existing keys + flips `email_verified` + audits (§3.3.1, R2 HIGH 6). Identity key is `(org_idp_id, sub)`, never mutable email. |
| **Open-redirect on callback** | `redirect_after` validated against a strict **exact-origin** allowlist (dashboard origin, registered CLI loopback) at BOTH `start` and `callback`. The OIDC `redirect_uri` is a single fixed, pre-registered callback URL — never derived from request input. |
| **IdP spoofing / token mix-up / alg-confusion** (R2 — HIGH 4) | `id_token` hardened (§3.2 step 4): signature against cached JWKS with **alg pinned to the asymmetric JWKS set**, **`alg:none` and `HS*` rejected**; **`iss` matched against the EXPECTED issuer resolved from the `OidcLoginAttempt` row, not the token's self-asserted `iss`**; `aud` array → require `azp==client_id`; `iat`/`nbf` validated; `exp` enforced; `nonce` matched. Discovery + JWKS fetched only via the §3.2.1 SSRF-guarded path. |
| **Enforcement bypass** | Signup rejects enforced-domain emails (`sso_enforced_use_sso`); user API keys gated unless **`sso_minted` AND backed by a live (non-deactivated) `ExternalIdentity` AND still a member** (R2 HIGH 1 — not `sso_minted` alone); `sso_minted` is **server-set only**, never client-settable (§3.5); **minting a service key under enforcement requires an SSO-minted admin session** (§4.2.1, R2 HIGH 5); owner is the only standing carve-out (audited break-glass). Service-key *use* exemption is by `key_kind`, not a guessable flag. |
| **Deprovisioning fails to revoke access** (R2 — HIGH 1) | Member-removal + IdP-deactivate **tombstone the org's `ExternalIdentity` rows AND revoke the user's `sso_minted` keys** in-transaction (§5.4); §4.2 additionally requires a live identity. Deprovision = immediate access revocation, enforced and unenforced alike. |
| **Login-CSRF / session-fixation** (R2 — MED) | `state`+`nonce`+PKCE verifier bound to an **HttpOnly+Secure+SameSite=Lax browser cookie**, compared at callback (`state_browser_mismatch` on miss). The R1 server-store-only option is **removed** — server-side `state` alone does not bind the browser. |
| **Replay / CSRF on OIDC `state`** | `state` is a durable single-use row (`OidcLoginAttempt`) consumed with an atomic `UPDATE WHERE status='pending'` rowcount-1 guard (mirrors `ActivationAttempt`); short TTL; `nonce` echoed in and matched against `id_token`; PKCE `code_verifier` bound to the attempt; **edge rate-limited (Cloud Armor) on `/start` + `/callback`** (R2 — mirrors the activation gate); `oidc_login_attempts` retention sweeper. |
| **Secret storage** | `client_secret_ref` → GCP Secret Manager (or K8s secret on self-hosted); raw secret never in PG, responses, or logs. **Ref validated to the org's own secret namespace** (R2 — §5.2). Matches the project's "secrets in Secret Manager only" rule. |
| **Mixed-IdP / shared-issuer cross-tenant confusion** (R2 — HIGH 3) | One enabled IdP per org (`uq_org_idp_one_enabled_per_org`). Identity keyed on **`(org_idp_id, sub)`** (migration 056), so a **shared issuer (Google Workspace) cannot collide or resolve across tenants**; resolution always carries the IdP/org context (§2.3, §3.3 step 1). |
| **Domain hijack / cross-tenant claim** | `uq_org_domain_global_verified` makes a verified domain claimable by exactly one org; DNS-TXT proof required; free-email-provider denylist (§2.2) blocks claiming consumer domains; **subdomains are NOT auto-covered** (each verified independently — §4.3). |
| **Seat-cap evasion via JIT** | JIT respects `Entitlement.seats_limit`; `seat_limit_reached` rejection (§3.4); pending-invite reconciliation + `uq_org_members_org_user` prevent double-seat (§2.6, §3.4). |
| **Audit-log gaps** | Every SSO mutation + login outcome emits an `OrgAuditEvent` (§8 / §10), including the R2 reseed-guard, key-revocation, and break-glass-use events. |

---

## §8 — Audit Events (mirrors the `OrgAuditEvent` append-only pattern)

All SSO events are written via the existing append-only `OrgAuditEvent` mechanism (`models.py:2298`; construction pattern at `org_members.py:1089`) — INSERT-only, inside the mutating transaction, `org_id ON DELETE SET NULL` so rows survive org deletion. No new audit table; we extend the `event_type` vocabulary and add `target_type` values `'idp'` and `'external_identity'`.

**New `event_type` values (binding MUST-log set):**

| Domain | Event types |
|--------|-------------|
| IdP config | `sso_idp_created`, `sso_idp_updated`, `sso_idp_enabled`, `sso_idp_disabled`, `sso_client_secret_rotated` |
| Enforcement | `sso_enforcement_enabled`, `sso_enforcement_disabled`, `sso_break_glass_issued`, **`sso_break_glass_revoked`** (R2), **`sso_break_glass_used`** (R2), **`sso_owner_exempt_auth`** (R2 — owner authed under enforcement) |
| Domain | `sso_domain_verification_started`, `sso_domain_verified`, `sso_domain_verification_failed`, `sso_domain_revoked` |
| Login | `sso_login_succeeded`, `sso_login_failed` (with `before`/`after` carrying reason code: `idp_email_unverified`, `email_domain_not_verified_for_org`, `account_deactivated`, `seat_limit_reached`, `id_token_verification_failed`, `state_replay`, **`state_browser_mismatch`** (R2), **`issuer_mismatch`** (R2), **`alg_not_allowed`** (R2), …) |
| Linking | `sso_identity_linked` (records `link_method`), `sso_identity_link_denied`, `sso_identity_deactivated`, **`sso_identity_link_reseed_guard`** (R2 — explicit_confirm into a previously-unverified account; records keys-revoked count + prior `email_verified`) |
| Deprovision (R2) | **`sso_keys_revoked_on_deprovision`** (records count + reason: `member_removed` \| `idp_identity_deactivated` \| `scim_deprovision`) |
| Provisioning | `sso_user_jit_provisioned`, **`sso_invite_reconciled`** (R2 — pending invite consumed during JIT; records honored role) |

**Secret hygiene (binding, mirrors `OrgAuditEvent` license-key rule §2.7 of licensing-org-redesign):** never write the client secret, the `id_token`, the `code`, or the raw `state`/`nonce`/PKCE verifier into audit rows or logs. Store only references, reason codes, the `sub`, and the `email_at_link`.

---

## §9 — Migration / Back-compat Impact

- **P1 (shipped):** migration 055 created `org_identity_providers`, `org_domain_verifications`, `external_identities`, `oidc_login_attempts`, and `api_keys.sso_minted` (DEFAULT false) — additive, appended to the 052 chain.
- **P1-fix (R2 — migration 056, DO FIRST):** **DROP `uq_external_identity_issuer_sub`; CREATE `uq_external_identity_idp_sub UNIQUE (org_idp_id, subject)`** (§2.3 / P1-FIX callout); **ADD `uq_org_members_org_user UNIQUE (org_id, user_id)`** with legacy dedupe (§2.6); **CREATE `sso_break_glass_grants`** (§2.5); **one-time lowercase-normalize `users.email`** so case-insensitive JIT match is collision-free (§3.3/§3.4). Safe because `external_identities` is empty in production (SSO unreleased) — the index swap carries no data-rewrite risk; the email normalization touches legacy rows only.
- **Strictly additive otherwise.** No org enables SSO by default; nothing changes until an Enterprise (R2: all-paid-tier per CEO decision 3 — confirm §13) admin configures an IdP, verifies a domain, and flips enforce. `key_kind='user'` keys behave exactly as today unless an *enforcing* org claims the user's domain (and even then, owner + service keys are exempt, and a single SSO re-login restores access — **and deprovision now revokes the SSO-minted keys immediately**, §5.4).
- **Service keys: near-zero change.** Use is categorically exempt (§5.2); the only delta is that minting/rotating a service key *while the org enforces SSO* requires an SSO-minted admin session (§4.2.1).
- **Self-hosted (Helm):** `client_secret_ref` indirection works via mounted K8s secrets; no Secret Manager dependency forced on self-hosted operators. The §3.2.1 SSRF guard applies equally on self-hosted (operators with strict internal networks get the Forge egress-restriction follow-up as defense-in-depth).

---

## §10 — Test Strategy

**Linking / anti-takeover (highest-priority, must-pass):**
- Auto-link allowed only when all three conditions hold; each of the three independently flipped → routed to explicit confirmation, never silent link.
- IdP `email_verified=false` → always rejected (`idp_email_unverified`), no link, no provision. **`email_verified` as string `"true"` → treated as unverified → rejected (R2 strict-boolean).**
- **Pre-seeding (R2 HIGH 6, must-pass):** attacker pre-registers `victim@acme.com` (email_verified=false) with a pre-planted API key → victim later `explicit_confirm`-links → the pre-planted key is **revoked**, `email_verified` flips to `true`, `sso_identity_link_reseed_guard` audit emitted; the attacker's key no longer authenticates.
- `(org_idp_id, sub)` already linked → email change at IdP does NOT re-link; logs into the same user.
- **Shared-issuer isolation (R2 HIGH 3, must-pass):** two orgs both on Google Workspace (same issuer), same/overlapping `sub` space → identities resolve to the **correct org** via `(org_idp_id, sub)`; no cross-tenant resolution; no unique-constraint collision.
- **Case-insensitive match (R2):** existing `Victim@Acme.com` legacy row + IdP asserts `victim@acme.com` → matches the same user; no duplicate-user / no unique-violation 500 (post-056 normalization).
- Deactivated identity → `account_deactivated` rejection.

**Enforcement + deprovisioning (R2 — must-pass):**
- Enforced human user with legacy (non-SSO-minted) key + no ExternalIdentity → `sso_required`.
- Same user after SSO login (sso_minted key + live identity) → succeeds.
- **Deprovision revokes access (R2 HIGH 1):** SSO-minted user authenticates fine → admin removes the member → the user's `sso_minted` key **immediately stops authenticating** (both because the key is revoked AND because §4.2's live-identity check fails); `ExternalIdentity.deactivated_at` set; `sso_keys_revoked_on_deprovision` audit emitted. The pre-R2 "sso_minted key lives forever after offboarding" case is the headline regression test.
- **`sso_minted` not client-settable (R2):** a create-key / me-key / admin-mint / service-key request carrying `sso_minted=true` in the body → flag ignored; the minted key is NOT treated as SSO-minted.
- **Service key under enforcement → succeeds** (categorical use-exemption). Agent-run + ticket-mutation + work-queue execution by a service key under an enforcing org → all succeed.
- **Service-key MINT under enforcement (R2 HIGH 5):** admin with a non-SSO-minted session tries to mint/rotate a service key while org enforces → rejected (must SSO-login first); admin with an SSO-minted session → succeeds; a service key that pre-dates enforcement keeps working (documented residual).
- Signup with an enforced-domain email → `sso_enforced_use_sso`.

**Break-glass (must-pass):**
- Owner with a broken/disabled IdP can still authenticate with a user key and reach `PUT .../sso enforced=false`; `sso_owner_exempt_auth` audit emitted.
- **Durable grant (R2 MED):** owner-issued `SsoBreakGlassGrant` lets a specific admin authenticate; **expires server-side at `expires_at`** (a request after expiry → `sso_required`); owner early-revoke takes effect immediately; single-active-grant-per-admin enforced; `sso_break_glass_used`/`_revoked` audited.
- Last-owner guards (`uq_org_members_one_owner_per_org`) hold across SSO config changes.

**OIDC protocol (R2 — hardened):**
- `state` replay (reused/expired/consumed) → rejected (rowcount-1 consume guard).
- **Browser-bound `state` (R2 MED):** callback with a server-valid `state` but missing/mismatched cookie → `state_browser_mismatch`.
- `nonce` mismatch in `id_token` → rejected.
- **`id_token` alg-confusion (R2 HIGH 4):** `alg:none` → rejected; `HS256` signed with the public key → rejected; only the asymmetric JWKS algs accepted.
- **Issuer mix-up (R2 HIGH 4):** token whose self-asserted `iss` differs from the attempt-row's expected issuer → rejected (`issuer_mismatch`), even if signature is otherwise valid for some configured IdP.
- **`aud` array (R2):** multi-entry `aud` without `azp==client_id` → rejected; with correct `azp` → accepted.
- `id_token` bad signature / wrong `aud` / expired / future `iat`/`nbf` → rejected.
- `redirect_after` off-allowlist (and prefix-of-allowed-but-not-exact) → rejected at both `start` and `callback`.

**SSRF guard (R2 HIGH 2 — must-pass):**
- Issuer / discovery / `jwks_uri` / `token_endpoint` resolving to `127.0.0.1`, `169.254.169.254`, `10.x`, `192.168.x`, `fe80::`, or `::1` → fetch **rejected** (hard block).
- Non-https issuer → rejected.
- Discovery doc advertising a `jwks_uri`/`token_endpoint` on an **internal/private/metadata** host → rejected (per-host IP-range guard, R2-N1).
- Discovery doc advertising a `jwks_uri`/`token_endpoint` on a **different *public* host** (e.g. Google's `oauth2.googleapis.com` / `www.googleapis.com` for issuer `accounts.google.com`) → **allowed** — Google Workspace must log in (R2-N1 regression).
- A `302` redirect to an internal host during any fetch → not followed → rejected.
- DNS-rebind (hostname resolves public on first lookup, private on second) → connection pinned/re-validated → rejected.

**Domain verification:**
- DNS TXT happy path; wrong token; consumer-domain denylist rejection; global-uniqueness collision (second org cannot verify a domain already verified by another).
- **Subdomain rule (R2):** verifying `acme.com` does NOT auto-cover `eng.acme.com` (JIT for `x@eng.acme.com` → `email_domain_not_verified_for_org` until `eng.acme.com` is independently verified).

**JIT + invite reconciliation:**
- New verified-domain user → provisioned as `member`, seat consumed; at seat cap → `seat_limit_reached`.
- **Pending-invite reconciliation (R2 MED):** JIT login for a user with a matching pending `OrgInvite` → invite consumed/cancelled, single membership, **no double seat**; an invite carrying `admin` role → membership honored at `admin` (not silently downgraded, not silently elevated beyond the invited role; never `owner`).
- **Concurrency:** two simultaneous JIT logins for the same email → exactly one membership (`uq_org_members_org_user`), no double seat.

**Tier gate:** org below the gated tier cannot create an IdP (`upgrade_required`). (R2: gate is per CEO decision 3 — all paid tiers if confirmed, §13.)

**Audit:** every mutation + login outcome emits exactly one `OrgAuditEvent`; no UPDATE/DELETE path on the audit table; no secret/`id_token`/`code`/raw-`state` material in any audit row; the R2 events (`sso_identity_link_reseed_guard`, `sso_keys_revoked_on_deprovision`, `sso_break_glass_used`/`_revoked`, `sso_owner_exempt_auth`, `sso_invite_reconciled`) all fire on their paths.

---

## §11 — v1 vs v2 Scope (explicit)

| Capability | v1 (this design) | v2 (deferred, model forward-compatible) |
|------------|------------------|------------------------------------------|
| OIDC authorization-code + PKCE login | ✅ | — |
| JIT provisioning (member only) | ✅ | — |
| Org enforcement (require SSO) | ✅ | — |
| Domain verification (DNS TXT) | ✅ | + meta-tag method |
| Break-glass owner/admin recovery | ✅ | — |
| Anti-takeover linking + explicit confirm | ✅ | — |
| SAML | ❌ | ✅ (`protocol='saml'`, additive columns) |
| SCIM provisioning/deprovisioning | ❌ | ✅ (`deactivated_at` ready; + `ScimToken`) |
| Group → role mapping | ❌ (JIT = member) | ✅ (`OrgIdpGroupMapping`, reads `groups` claim) |
| IdP-initiated login | ❌ | ✅ |
| Session-policy controls (idle timeout, device) | ❌ | ✅ |

---

## §12 — Recommended Primary Design (summary)

Ship **OIDC authorization-code + PKCE** as a **paid-tier** org control plane (CEO decision 3, R2). Three core tables — `OrgIdentityProvider` (protocol-tagged, secret in Secret Manager via `client_secret_ref`), `OrgDomainVerification` (globally-unique verified domains via DNS TXT, consumer-domain denylist, exact-domain matching), `ExternalIdentity` (keyed on **`(org_idp_id, sub)`** per R2, never email — survives shared-issuer IdPs) — plus `OidcLoginAttempt` for CSRF/replay (reusing the proven `ActivationAttempt` durable-single-use-token shape, **browser-bound via an HttpOnly cookie**) and `SsoBreakGlassGrant` (durable, server-consulted admin break-glass) and the `api_keys.sso_minted` column. Login bridges into the existing API-key credential so the CLI/dashboard are unchanged. Enforcement hooks **only** the `key_kind=='user'` path in `auth/dependencies.py` and requires **`sso_minted` AND a live IdP identity AND current membership** (R2), leaving service keys (and therefore CI agent runs / work queues) categorically untouched at *use* time. **Deprovisioning explicitly revokes the user's SSO-minted keys** so offboarding genuinely cuts access (R2). All issuer-derived server-side fetches pass a **hard SSRF guard** (R2). The org **owner** is the invariant-backed break-glass account (one owner per org, ownership only via audited transfer). Anti-takeover is the crown-jewel control: auto-link only on verified-email-match AND existing-account-verified AND already-org-member; everything else requires dual-control confirmation, and an `explicit_confirm` into a previously-unverified account revokes that row's pre-existing keys (R2 pre-seed defense). All mutations and login outcomes flow through the append-only `OrgAuditEvent`. **R2 P1-fix: migration 056 re-scopes the identity key + adds the membership uniqueness + break-glass table + email normalization, and must ship before P2.**

---

## §13 — Open Questions for the CEO

1. **Which IdPs do we certify for v1?** Recommendation: certify **Okta**, **Microsoft Entra ID**, and **Google Workspace** explicitly (run the full happy-path + edge tests against each), and document **generic OIDC** as "best-effort, supported but uncertified." These three cover the overwhelming majority of enterprise procurement. Confirm the set.
2. **Is enforcement owner-only, or owner+admin?** Recommendation: **owner-only flips `enforced`** (highest-blast-radius action, tied to the single-owner break-glass invariant); **admins can configure** the IdP and verify domains but not flip enforcement. Break-glass grants are **owner-only**. Confirm.
3. **Is OIDC SSO strictly Enterprise, or also Team?** **DECIDED (CEO, R2): all paid tiers** — OIDC SSO is a mid-market wedge, not an Enterprise-only capability. Security note: this widened surface is why the §3.2.1 SSRF guard is a hard block (Sentinel HIGH 2). Remaining for Ledger: the exact paid-tier list and whether `oidc_sso` is bundled or a separate add-on line.
4. **New `oidc_sso` flag vs. reuse `saml_sso`?** Recommendation: **new `oidc_sso` flag** so a future SAML ship is separately marketable and we don't imply SAML is included. Confirm.
5. **Break-glass admin grant TTL + scope** — recommendation 1 hour, single admin, owner-issued, audited. Confirm the default TTL.

---

## §14 — Handoff Tickets (on approval)

- **Atlas — P1-fix FIRST (R2, gates P2/P3/P4):** migration **056** — drop `uq_external_identity_issuer_sub`, create `uq_external_identity_idp_sub UNIQUE (org_idp_id, subject)`; add `uq_org_members_org_user UNIQUE (org_id, user_id)` + legacy dedupe; create `sso_break_glass_grants`; one-time lowercase-normalize `users.email` (§2.3, §2.5, §2.6, §3.3, P1-FIX callout).
- **Atlas** — backend (P2/P3/P4): models, `routes/sso.py` (config + domain + enforcement endpoints), `routes/auth_sso.py` (`/start`, `/callback`), **§3.2.1 SSRF-guarded fetch layer**, **hardened `id_token` verification (alg-pin, issuer-from-attempt, azp, strict-boolean email_verified)** + JWKS cache, Secret Manager integration + namespace validation, **browser-bound state cookie**, enforcement hook in `auth/dependencies.py` (**live-identity requirement + break-glass-grant consult**), **service-key mint boundary (§4.2.1)**, **deprovision key-revocation in `perform_member_removal` + shared helper (§5.4)**, JIT + linking service (**case-insensitive match, pre-seed reseed-guard, invite reconciliation**), `OrgAuditEvent` event vocabulary (R2 additions §8).
- **Forge** — **R2 SSRF egress follow-up:** platform-layer network-egress restriction (Cloud Run egress controls / VPC-SC / explicit allowlist) so the metadata server is unreachable even on an application-guard bypass; Cloud Armor edge rate-limit on `/start` + `/callback` (mirror activation) + `oidc_login_attempts` retention sweeper.
- **Sentinel** — authz/threat-model **re-review of R2 changes** (deprovision revocation, SSRF guard, identity re-scope, id_token hardening, service-key mint boundary, reseed-guard); confirm the 6 HIGH conditions are discharged as written.
- **Shield** — compliance: audit-event completeness (§8 incl. R2 events), secret-handling, **certify the precise enforcement claim verbatim (§4.4 — "interactive human login is via SSO"; service keys work; pre-enforcement service keys are a documented residual)**, customer-facing SSO language, retention tie-in to `audit_retention_6yr` / `compliance_exports`.
- **Prism** — dashboard: IdP config form, domain-verification wizard (show TXT record + check), enforcement toggle with "N human keys will require re-login" preview, break-glass UI.
- **Ledger** — confirm the exact paid-tier list for the `oidc_sso` gate (CEO decided all-paid-tiers, R2 §6/§13) and whether `oidc_sso` is bundled or a separate add-on line.
- **Forge** — Secret Manager + K8s-secret indirection for self-hosted; storage/logging immutability for audit (deployment follow-up).
- **Scribe** — public SSO setup docs per certified IdP.
