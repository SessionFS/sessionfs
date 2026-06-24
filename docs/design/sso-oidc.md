# Design: Org SSO Foundation — OIDC-First Identity Control Plane

- **Ticket:** tk_657df5f1a1c64bb8 — "Design org SSO foundation: OIDC-first identity control plane"
- **Author:** Compass (product intent + acceptance boundaries)
- **Status:** Binding design — pending Atlas (backend), Sentinel (authz/threat model), Shield (compliance), Ledger (packaging) review.
- **Audience:** Atlas owns the *how* (routes, migrations, transaction shapes); Sentinel/Shield own the threat model sign-off; this doc defines the *what* and the *acceptance boundaries*.
- **Visibility:** INTERNAL. `docs/design/` is tracked on `develop` and stripped from public `main` by the release sanitizer. References internal architecture freely.

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
INDEX uq_external_identity_issuer_sub  UNIQUE (provider_issuer, subject)   -- one IdP subject → one identity row
INDEX idx_external_identity_user (user_id)
```

**Identity key is `(provider_issuer, subject)`, NOT email (binding):** the OIDC `sub` is the stable, opaque, IdP-assigned identifier. Email is **mutable** at the IdP and must never be the join key for an existing link — it is only used for the *initial* linking decision (§3.3). The unique index on `(provider_issuer, subject)` guarantees an IdP subject maps to exactly one SessionFS identity.

**"One user may have multiple" (per ticket):** a single `User` may hold several `ExternalIdentity` rows (e.g. a consultant who is in two customer orgs' IdPs, or a user migrated across IdPs). The unique constraint is on the IdP-side key, not on `user_id` — so a user can collect multiple external identities, but no IdP subject can fan out to two users.

### 2.4 Forward-compatibility for SAML / SCIM (v2)

- **SAML:** `OrgIdentityProvider.protocol` already discriminates. SAML rows add SAML-specific columns in a v2 migration (`saml_metadata_url`, `saml_entity_id`, `saml_x509_cert_ref`) — additive, no rewrite. `ExternalIdentity.subject` holds the SAML `NameID`; `provider_issuer` holds the SAML `EntityID`. The `(provider_issuer, subject)` identity key is protocol-agnostic.
- **SCIM:** SCIM provisions/deprovisions users out-of-band (no interactive login). It writes the same `ExternalIdentity` + `OrgMember` rows this design creates via JIT, plus a v2 `scim_external_id` column on `external_identities` and a `ScimToken` table for the bearer token SCIM uses. The `deactivated_at` column already exists for the SCIM deprovision signal (§5.4).
- **Group→role mapping:** a v2 `OrgIdpGroupMapping` table (`org_idp_id`, `idp_group_claim_value`, `org_role`) consumes the `groups` claim. v1 deliberately does **not** read group claims — JIT always provisions as `member` (§3.4).

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
├── state: String(128) NOT NULL          -- random; returned in callback, matched exactly
├── pkce_code_verifier_hash: String(128) NOT NULL  -- hash of the verifier; raw verifier held only in the signed state cookie/store
├── nonce: String(128) NOT NULL          -- echoed in id_token `nonce` claim, matched (replay defense)
├── redirect_after: String(500) NULL     -- post-login destination, validated against an ALLOWLIST (§7 open-redirect)
├── status: String(20) NOT NULL DEFAULT 'pending'   -- pending | consumed | expired
├── expires_at: TIMESTAMPTZ NOT NULL     -- short TTL, e.g. 10 min
├── created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX idx_oidc_login_attempt_state (state, status, expires_at)
```

**Phase A — `GET /api/v1/auth/sso/{org_slug}/start`** (unauthenticated):
1. Resolve the org's enabled `OrgIdentityProvider`. 404 if none (non-oracular: "SSO is not configured for this organization").
2. Fetch/refresh the OIDC discovery doc + JWKS (cached on the IdP row, TTL'd).
3. Generate `state`, `nonce`, PKCE `code_verifier`/`code_challenge`. Insert an `OidcLoginAttempt` row (committed). Store the raw `code_verifier` in a short-lived, `HttpOnly`+`Secure`+`SameSite=Lax` signed cookie (or server-side keyed by `state`); persist only its hash.
4. Validate `redirect_after` against the allowlist; reject anything off-allowlist (§7).
5. 302 to the IdP's authorization endpoint with `response_type=code`, `scope`, `state`, `nonce`, `code_challenge`, `code_challenge_method=S256`, `redirect_uri` = our fixed callback.

**Phase B — `GET /api/v1/auth/sso/callback`** (unauthenticated, the IdP redirect target):
1. Read `state` + `code` from query. Look up the `OidcLoginAttempt` by `state`; reject if missing/expired/already consumed (atomic `UPDATE ... WHERE status='pending'` rowcount-1 consume guard — replay defense, identical to the activation Phase-B consume).
2. Verify the PKCE verifier (from cookie/store) against the stored hash.
3. Exchange `code` + `code_verifier` at the IdP token endpoint (server-to-server, presents `client_id` + the Secret-Manager-resolved client secret).
4. **Verify the `id_token`:** signature against cached JWKS; `iss` == the IdP's `issuer`; `aud` == our `client_id`; `exp` not passed; `nonce` == the stored nonce (replay defense). Reject on any mismatch.
5. Extract `sub`, `email`, `email_verified` (the IdP's email-verified claim).
6. Run the **account resolution / linking** logic (§3.3).
7. On success, mint a SessionFS session for the resolved `User` and hand back credentials (§3.5). Mark the attempt `consumed`.

### 3.3 Account resolution + linking — ANTI-TAKEOVER (the critical security area)

This is the single most dangerous part of SSO. An attacker who controls an IdP `sub` must **never** be able to link it to a victim's existing email-verified SessionFS account and thereby steal it. The exact decision tree (binding):

```
INPUT: idp_issuer, sub, idp_email, idp_email_verified (claim), org (from the IdP row)

1. EXISTING LINK?
   existing = SELECT * FROM external_identities WHERE provider_issuer=idp_issuer AND subject=sub
   IF existing AND existing.deactivated_at IS NULL:
       → this sub is already linked. Log in as existing.user_id. DONE.
       (email changes at the IdP do NOT re-trigger linking — sub is the key.)
   IF existing AND existing.deactivated_at IS NOT NULL:
       → identity was deprovisioned. REJECT (account_deactivated). Re-provision is admin-only. DONE.

2. NO LINK YET — must we link to an existing User, or JIT-create?

   2a. HARD PRECONDITION: the IdP MUST assert email_verified == true.
       IF NOT idp_email_verified: REJECT (idp_email_unverified).
       We never trust an unverified IdP email for any linking or provisioning decision.

   2b. normalized_email = lower(idp_email); domain = part after '@'
       IF domain NOT IN org's verified domains (org_domain_verifications WHERE status='verified'):
           REJECT (email_domain_not_verified_for_org).
       (Even with a valid IdP token, the email's domain must belong to THIS org. Stops a
        misconfigured IdP from provisioning arbitrary external emails into the tenant.)

   2c. existing_user = SELECT * FROM users WHERE email = normalized_email

   2d. IF existing_user IS NULL:
           → JIT PROVISION (§3.4). Create User (email_verified=true, sourced from the
             verified IdP claim + verified domain), create ExternalIdentity
             (link_method='jit_provision'), create OrgMember(role='member'). DONE.

   2e. IF existing_user EXISTS:
           --- THE TAKEOVER DECISION POINT ---
           ALLOW automatic link ONLY IF ALL of:
              (i)   existing_user.email == normalized_email          (exact match)
              (ii)  existing_user.email_verified == true             (the SessionFS side is itself verified)
              (iii) existing_user is ALREADY a member of `org`        (OrgMember exists for this org)
           IF all three hold:
               → link: INSERT ExternalIdentity(link_method='verified_email_match'). Log in. DONE.
           ELSE:
               → DO NOT auto-link. Require EXPLICIT LINKING CONFIRMATION (§3.3.1).
```

**Why all three conditions for auto-link (rationale, binding):**
- **(i) exact verified-email match** is the baseline.
- **(ii) the existing SessionFS account's email must itself be verified.** If a SessionFS user signed up with `victim@acme.com` but never clicked the verification link (`users.email_verified == false`), that email is *unproven on our side*. Auto-linking an IdP `sub` to an unverified-email account is exactly the takeover vector: an attacker who pre-registers `victim@acme.com` (unverified) and later the real victim logs in via SSO could otherwise collide. Requiring `email_verified=true` on the existing row closes this. An unverified existing account routes to explicit confirmation, never silent link.
- **(iii) the existing user is already an org member.** A verified-email match for a user who is *not* in the org should not silently fold an outside account into the tenant on the IdP's say-so. This is the conservative default; explicit confirmation handles the legitimate "I already had a personal SessionFS account, now my employer turned on SSO" case (§3.3.1).

#### 3.3.1 Explicit linking confirmation (the safe fallback)

When auto-link is denied but a legitimate match is plausible, we do **not** log the IdP identity straight into the existing account. Instead:
- The user must prove control of **both** sides. The flow: the SSO session is held in a pending state; we send a one-time confirmation link to the `users.email` on file (the SessionFS side) — same mechanism class as `routes/auth.py:verify_email`. Clicking it (proving control of the existing account's mailbox) + the active just-completed IdP session together authorize the link. Only then is the `ExternalIdentity` row written with `link_method='explicit_confirm'`.
- This guarantees an attacker cannot link without **simultaneously** controlling the IdP subject AND the existing account's verified mailbox.

### 3.4 JIT provisioning

On first SSO login by a verified-domain user with no existing account (§3.3 step 2d):
- Create `User(email=normalized_email, email_verified=true, display_name=<IdP name claim>)`. `email_verified=true` is justified because the IdP asserted `email_verified` **and** the domain is org-verified — two independent proofs.
- Create `OrgMember(org_id, user_id, role='member')`. **JIT always provisions `member`** — never `admin`/`owner`. Role elevation is a deliberate org-admin action. (Group→role mapping is v2.)
- Create `ExternalIdentity(link_method='jit_provision')`.
- **Seat enforcement:** JIT provisioning consumes a seat. It MUST respect `Organization.seats_limit` / the resolved `Entitlement.seats_limit`. If the org is at its seat cap, JIT login is **rejected** with `seat_limit_reached` (the org admin must add seats or remove a member). Reuse the seat-count discipline already in the invite-accept path (`org_members.py`). This prevents an org from silently exceeding its paid seat count via SSO.
- All of the above happen in **one transaction**.

### 3.5 What the SSO login returns

Because the product is API-key-based, the SSO callback must bridge into a credential the CLI/dashboard already understand. v1 approach:
- **Dashboard:** the callback sets the dashboard's existing session cookie/token and 302s to `redirect_after`. The dashboard already calls `/api/v1/auth/me` to hydrate identity.
- **CLI:** `sfs auth login --sso --org <slug>` opens the browser to the `start` URL with a loopback `redirect_after`; the callback hands the CLI a freshly-minted **user `ApiKey`** (the existing `create_api_key` mechanism, `routes/auth.py:289`) scoped to that user, returned once. The key is the bridge — the CLI keeps working unchanged after login. Under enforcement (§4) these SSO-minted keys are the *only* keys an enforced human may mint.

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
        IF the ApiKey was NOT minted via an SSO session  (api_keys.sso_minted == true; see below)
           AND the user holds no active, non-deactivated ExternalIdentity for enforced_org:
               REJECT 403 sso_required  { org_slug, sso_start_url }
```
- **Service keys are categorically exempt.** `ctx.key_kind == 'service'` → the enforcement block is skipped entirely. Service keys are non-human, org-bound (`api_keys.org_id`), and scoped (`require_scope`). They MUST keep working under enforcement — a CI reviewer/implementer runner authenticating with a service key is not a person and is not subject to SSO. **This is a hard acceptance criterion.**
- **New column `api_keys.sso_minted: Boolean DEFAULT false`** marks keys minted by the SSO callback (§3.5). Under enforcement, a human's pre-SSO legacy keys stop working (they must re-login via SSO to mint a fresh key); but the moment they re-login the new key carries `sso_minted=true` and works. This gives a clean cutover: turning on enforcement invalidates unmanaged human keys but is recoverable by a single SSO login.
- **Grace/transition:** turning on enforcement does NOT delete existing keys; it gates them at auth time. An org admin can preview "N human keys will require re-login" before flipping `enforced`.

### 4.3 Allowed-domain / domain-verification behavior

- Enforcement only applies to **verified** domains (`org_domain_verifications.status='verified'`). An org cannot enforce against a domain it hasn't proven (the verify gate blocks it; the enforce toggle is disabled in the UI until ≥1 verified domain exists).
- A user whose email is on a verified-but-non-enforcing org's domain is unaffected.
- Multi-domain orgs: enforcement covers **all** verified domains of the org.

### 4.4 Break-glass owner/admin recovery (binding — must never lock out the owner)

A misconfigured IdP (wrong issuer, expired client secret, IdP outage) must **never** permanently lock an org owner out of their own org. This ties directly to the v0.11.0 last-owner guards (`org_members.py:perform_role_change` / `perform_member_removal`, `uq_org_members_one_owner_per_org`). Rules:

1. **The `owner` is always exempt from SSO enforcement at the auth layer.** The §4.2 enforcement check has a hard carve-out: `IF user is the owner of enforced_org → SKIP enforcement`. The owner can ALWAYS authenticate with a user API key, even when SSO is broken. The owner is the break-glass account by construction.
   - Rationale: there is exactly one owner per org (`uq_org_members_one_owner_per_org`), ownership only moves via the audited `org_owner_transfer` two-step, and the owner already cannot be removed/demoted into a last-owner-less state. Making the owner the recovery anchor reuses an invariant we already enforce.
2. **Admins get a break-glass code path, not a blanket exemption.** Admins ARE subject to enforcement (so an enterprise can require *all* staff including admins to use SSO). But the **owner** can issue a time-boxed **break-glass grant** to a specific admin (e.g. 1 hour) via `POST /api/v1/orgs/{org_id}/sso/break-glass` (owner-only) — recorded as an `OrgAuditEvent` (`sso_break_glass_issued`), letting that admin authenticate with an API key while the IdP is fixed.
3. **Enforcement can be disabled by the owner at any time** via `PUT .../sso` with `enforced=false`. Because the owner is never gated (rule 1), the owner can always reach this endpoint to turn enforcement off. There is no state in which the org is unrecoverable.
4. **Disabling/deleting the IdP** (`enabled=false`) auto-clears `enforced` (you cannot enforce against a disabled IdP). Audited as `sso_disabled`.

This satisfies the ticket's "an owner must never be locked out by a misconfigured IdP" with a concrete, invariant-backed path.

---

## §5 — Interactions With Existing Systems

### 5.1 Org membership + owner/admin safety

- JIT provisioning creates `OrgMember(role='member')` only (§3.4) — never elevates.
- SSO never bypasses the v0.11.0 owner/admin guards. Role changes still go through `perform_role_change`; ownership still moves only via `org_owner_transfer`. SSO is an *authentication* layer; it does not touch *authorization*/role logic.
- The single-owner invariant (`uq_org_members_one_owner_per_org`) is the backbone of break-glass (§4.4).

### 5.2 Service keys (binding — must keep working)

- Service keys (`api_keys.key_kind='service'`, org-bound, scoped via `require_scope`) are **categorically exempt** from SSO enforcement (§4.2). The enforcement check is gated on `key_kind == 'user'`.
- Service-key creation/rotation (`/api/v1/orgs/{org_id}/service-keys`) is **unaffected** by SSO — they are minted by an org admin, not by a human login. A service key is not a person and has no IdP identity.
- `assert_service_key_can_access_project` and the cross-org boundary logic are untouched.

### 5.3 Active tickets / agent runs / work queues (binding)

- A service-key reviewer/implementer runner (Codex/DeepSeek/CI agent) authenticating with a service key calls `require_scope('agent_runs:write')` etc. and is **never** subject to SSO enforcement (§5.2). Turning on org SSO MUST NOT break CI agent runs, ticket mutation by service keys, or work-queue execution. This is explicitly tested (§8).
- Human-initiated ticket/agent-run actions go through the user-key path; if the human is enforced and hasn't SSO-logged-in, they get `sso_required` and re-login — the work itself is unaffected once they hold an SSO-minted key.

### 5.4 Deprovisioning (when SSO removes a user / IdP says deactivated)

- **v1 signal (interactive):** when a deprovisioned IdP user attempts SSO login, the IdP refuses to issue a token / the `sub` no longer resolves → they simply cannot get in. Additionally, the §3.3 step-1 `deactivated_at` check rejects a tombstoned identity.
- **Admin-driven deprovision (v1):** an org admin removing a member (`perform_member_removal`) sets `external_identities.deactivated_at` for that user's identities in the org, and (under enforcement) their `sso_minted` keys stop resolving because their `ExternalIdentity` is deactivated and they're no longer an org member. Removal revokes access immediately.
- **SCIM push deprovision (v2):** SCIM `DELETE`/`active=false` sets `deactivated_at` out-of-band. The column exists now precisely so v2 SCIM is additive.
- **Deactivating the whole IdP** does not delete users (data-stays, access-revoked invariant); it disables SSO login. Enforced human keys then fail until re-config; the owner break-glass path (§4.4) keeps the org recoverable.

### 5.5 Billing tiers

- OIDC SSO is gated behind an Enterprise feature flag (§6). The IdP config endpoints call `check_feature(ctx, 'oidc_sso')` (or reuse `saml_sso` — see open question §9). Tier is resolved through the existing entitlement-first chain (`tier_gate.get_effective_tier`, entitlement → org fallback).

### 5.6 `/me` enrichment

`routes/auth.py:get_me` gains optional fields so the dashboard can render SSO state: `sso_enforced` (is the user's org enforcing), `external_identities` (count / providers). No change to the existing tier/org enrichment.

---

## §6 — Recommended Tier Gate

**Recommendation: OIDC SSO is Enterprise-only**, consistent with the existing `saml_sso` placement (`tiers.py:99`, Enterprise only).

- Add a distinct feature flag **`oidc_sso`** to `Tier.ENTERPRISE["features"]` rather than overloading `saml_sso`. Rationale: SAML and OIDC are separately purchasable/marketable capabilities, and a v2 SAML ship should not appear "already included" just because OIDC shipped. Keep `saml_sso` reserved for the v2 SAML implementation.
- Gate **all** IdP-config, domain-verification, and enforcement-toggle endpoints behind `check_feature(ctx, 'oidc_sso')` + `check_role(ctx, 'admin')` (owner/admin). The **login** endpoints (`/sso/{slug}/start`, `/sso/callback`) are unauthenticated by nature but only function for an org that has the feature (the IdP row can only exist if an Enterprise admin created it).
- **Open question for CEO (§9):** whether to also extend OIDC SSO to **Team** tier as a competitive wedge. SSO-at-Team is increasingly a mid-market expectation and a differentiator vs. "SSO tax" competitors. Defaulting to Enterprise-only is the safe revenue-protecting choice; CEO/Ledger decide.

---

## §7 — Security Risks + Mitigations

| Risk | Mitigation (binding) |
|------|----------------------|
| **Account takeover via linking** | §3.3 decision tree: auto-link ONLY on (exact email match) AND (existing account email_verified) AND (already org member); IdP `email_verified` claim required; everything else routes to explicit dual-control confirmation (§3.3.1). Identity key is `(issuer, sub)`, never mutable email. |
| **Open-redirect on callback** | `redirect_after` validated against a strict allowlist (dashboard origin, registered CLI loopback) at BOTH `start` and `callback`. The OIDC `redirect_uri` is a single fixed, pre-registered callback URL — never derived from request input. |
| **IdP spoofing** | `id_token` verified: signature against the IdP's published JWKS, `iss` == configured `issuer`, `aud` == our `client_id`. Discovery + JWKS fetched over TLS from the configured issuer only. |
| **Enforcement bypass** | Signup rejects enforced-domain emails (`sso_enforced_use_sso`); user API keys gated unless `sso_minted` or backed by an active `ExternalIdentity`; owner is the only deliberate carve-out (break-glass). Service-key exemption is by `key_kind`, not a guessable flag. |
| **Replay / CSRF on OIDC `state`** | `state` is a durable single-use row (`OidcLoginAttempt`) consumed with an atomic `UPDATE WHERE status='pending'` rowcount-1 guard (mirrors `ActivationAttempt`); short TTL; `nonce` echoed in and matched against `id_token`; PKCE `code_verifier` bound to the attempt. |
| **Secret storage** | `client_secret_ref` → GCP Secret Manager (or K8s secret on self-hosted); raw secret never in PG, responses, or logs. Matches the project's "secrets in Secret Manager only" rule. |
| **Mixed-IdP confusion** | One enabled IdP per org (`uq_org_idp_one_enabled_per_org`). `ExternalIdentity` snapshots `provider_issuer`; a user with identities across multiple orgs' IdPs is keyed per `(issuer, sub)` with no cross-talk. |
| **Domain hijack / cross-tenant claim** | `uq_org_domain_global_verified` makes a verified domain claimable by exactly one org; DNS-TXT proof required; free-email-provider denylist (§2.2) blocks claiming consumer domains. |
| **Seat-cap evasion via JIT** | JIT respects `Entitlement.seats_limit`; `seat_limit_reached` rejection (§3.4). |
| **Audit-log gaps** | Every SSO mutation + login outcome emits an `OrgAuditEvent` (§8 / §10). |

---

## §8 — Audit Events (mirrors the `OrgAuditEvent` append-only pattern)

All SSO events are written via the existing append-only `OrgAuditEvent` mechanism (`models.py:2298`; construction pattern at `org_members.py:1089`) — INSERT-only, inside the mutating transaction, `org_id ON DELETE SET NULL` so rows survive org deletion. No new audit table; we extend the `event_type` vocabulary and add `target_type` values `'idp'` and `'external_identity'`.

**New `event_type` values (binding MUST-log set):**

| Domain | Event types |
|--------|-------------|
| IdP config | `sso_idp_created`, `sso_idp_updated`, `sso_idp_enabled`, `sso_idp_disabled`, `sso_client_secret_rotated` |
| Enforcement | `sso_enforcement_enabled`, `sso_enforcement_disabled`, `sso_break_glass_issued` |
| Domain | `sso_domain_verification_started`, `sso_domain_verified`, `sso_domain_verification_failed`, `sso_domain_revoked` |
| Login | `sso_login_succeeded`, `sso_login_failed` (with `before`/`after` carrying reason code: `idp_email_unverified`, `email_domain_not_verified_for_org`, `account_deactivated`, `seat_limit_reached`, `id_token_verification_failed`, `state_replay`, …) |
| Linking | `sso_identity_linked` (records `link_method`), `sso_identity_link_denied`, `sso_identity_deactivated` |
| Provisioning | `sso_user_jit_provisioned` |

**Secret hygiene (binding, mirrors `OrgAuditEvent` license-key rule §2.7 of licensing-org-redesign):** never write the client secret, the `id_token`, the `code`, or the raw `state`/`nonce`/PKCE verifier into audit rows or logs. Store only references, reason codes, the `sub`, and the `email_at_link`.

---

## §9 — Migration / Back-compat Impact

- **Strictly additive.** New tables (`org_identity_providers`, `org_domain_verifications`, `external_identities`, `oidc_login_attempts`); new columns `api_keys.sso_minted` (DEFAULT false). One additive Alembic migration appended to the 052 chain. No data backfill required — SSO is opt-in per org.
- **Existing API-key + email users keep working unchanged.** No org enables SSO by default; nothing changes until an Enterprise admin configures an IdP, verifies a domain, and flips enforce. `key_kind='user'` keys behave exactly as today unless an *enforcing* org claims the user's domain (and even then, owner + service keys are exempt, and a single SSO re-login restores access).
- **Service keys: zero change.** Categorically exempt (§5.2).
- **Self-hosted (Helm):** `client_secret_ref` indirection works via mounted K8s secrets; no Secret Manager dependency forced on self-hosted operators.

---

## §10 — Test Strategy

**Linking / anti-takeover (highest-priority, must-pass):**
- Auto-link allowed only when all three conditions hold; each of the three independently flipped → routed to explicit confirmation, never silent link.
- IdP `email_verified=false` → always rejected (`idp_email_unverified`), no link, no provision.
- Attacker pre-registers `victim@acme.com` (email_verified=false) then victim SSO-logs-in → NO auto-link (condition ii fails); explicit dual-control required.
- `(issuer, sub)` already linked → email change at IdP does NOT re-link; logs into the same user.
- Deactivated identity → `account_deactivated` rejection.

**Enforcement:**
- Enforced human user with legacy (non-SSO-minted) key + no ExternalIdentity → `sso_required`.
- Same user after SSO login (sso_minted key) → succeeds.
- **Service key under enforcement → succeeds** (categorical exemption). Agent-run + ticket-mutation + work-queue execution by a service key under an enforcing org → all succeed.
- Signup with an enforced-domain email → `sso_enforced_use_sso`.

**Break-glass (must-pass):**
- Owner with a broken/disabled IdP can still authenticate with a user key and reach `PUT .../sso enforced=false`.
- Owner-issued break-glass grant lets a specific admin authenticate for the TTL; expires correctly; audited.
- Last-owner guards (`uq_org_members_one_owner_per_org`) hold across SSO config changes.

**OIDC protocol:**
- `state` replay (reused/expired/consumed) → rejected (rowcount-1 consume guard).
- `nonce` mismatch in `id_token` → rejected.
- `id_token` bad signature / wrong `iss` / wrong `aud` / expired → rejected.
- `redirect_after` off-allowlist → rejected at both `start` and `callback`.

**Domain verification:**
- DNS TXT happy path; wrong token; consumer-domain denylist rejection; global-uniqueness collision (second org cannot verify a domain already verified by another).

**JIT:**
- New verified-domain user → provisioned as `member`, seat consumed; at seat cap → `seat_limit_reached`.

**Tier gate:** non-Enterprise org cannot create an IdP (`upgrade_required`).

**Audit:** every mutation + login outcome emits exactly one `OrgAuditEvent`; no UPDATE/DELETE path on the audit table; no secret material in any audit row.

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

Ship **OIDC authorization-code + PKCE** as an Enterprise org control plane. Three core tables — `OrgIdentityProvider` (protocol-tagged, secret in Secret Manager via `client_secret_ref`), `OrgDomainVerification` (globally-unique verified domains via DNS TXT, consumer-domain denylist), `ExternalIdentity` (keyed on `(issuer, sub)`, never email) — plus `OidcLoginAttempt` for CSRF/replay (reusing the proven `ActivationAttempt` durable-single-use-token shape) and one new `api_keys.sso_minted` column. Login bridges into the existing API-key credential so the CLI/dashboard are unchanged. Enforcement hooks **only** the `key_kind=='user'` path in `auth/dependencies.py`, leaving service keys (and therefore CI agent runs / work queues) categorically untouched. The org **owner** is the invariant-backed break-glass account (one owner per org, ownership only via audited transfer). Anti-takeover is the crown-jewel control: auto-link only on verified-email-match AND existing-account-verified AND already-org-member; everything else requires dual-control confirmation. All mutations and login outcomes flow through the append-only `OrgAuditEvent`.

---

## §13 — Open Questions for the CEO

1. **Which IdPs do we certify for v1?** Recommendation: certify **Okta**, **Microsoft Entra ID**, and **Google Workspace** explicitly (run the full happy-path + edge tests against each), and document **generic OIDC** as "best-effort, supported but uncertified." These three cover the overwhelming majority of enterprise procurement. Confirm the set.
2. **Is enforcement owner-only, or owner+admin?** Recommendation: **owner-only flips `enforced`** (highest-blast-radius action, tied to the single-owner break-glass invariant); **admins can configure** the IdP and verify domains but not flip enforcement. Break-glass grants are **owner-only**. Confirm.
3. **Is OIDC SSO strictly Enterprise, or also Team?** Recommendation default: **Enterprise-only** (matches existing `saml_sso` placement, protects the enterprise packaging). Open to extending to **Team** as a mid-market wedge if Ledger/CEO judge the competitive upside outweighs the de-bundling. Decide with Ledger.
4. **New `oidc_sso` flag vs. reuse `saml_sso`?** Recommendation: **new `oidc_sso` flag** so a future SAML ship is separately marketable and we don't imply SAML is included. Confirm.
5. **Break-glass admin grant TTL + scope** — recommendation 1 hour, single admin, owner-issued, audited. Confirm the default TTL.

---

## §14 — Handoff Tickets (on approval)

- **Atlas** — backend: migration (4 tables + `api_keys.sso_minted`), models, `routes/sso.py` (config + domain + enforcement endpoints), `routes/auth_sso.py` (`/start`, `/callback`), `id_token` verification + JWKS cache, Secret Manager integration, enforcement hook in `auth/dependencies.py`, JIT + linking service, `OrgAuditEvent` event vocabulary.
- **Sentinel** — authz/threat-model sign-off on §3.3 linking, §4 enforcement, §7 risk table; adversarial review of the takeover and bypass paths.
- **Shield** — compliance: audit-event completeness (§8), secret-handling, customer-facing SSO language, retention tie-in to `audit_retention_6yr` / `compliance_exports`.
- **Prism** — dashboard: IdP config form, domain-verification wizard (show TXT record + check), enforcement toggle with "N human keys will require re-login" preview, break-glass UI.
- **Ledger** — confirm tier gate (Enterprise-only vs Team), `oidc_sso` flag packaging.
- **Forge** — Secret Manager + K8s-secret indirection for self-hosted; storage/logging immutability for audit (deployment follow-up).
- **Scribe** — public SSO setup docs per certified IdP.
