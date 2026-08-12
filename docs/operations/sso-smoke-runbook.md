# Real-IdP SSO smoke test — runbook (tk_c8e00bac24094dda)

SSO has been proven only against a mocked OIDC provider. This runbook is the
end-to-end validation against REAL IdPs. **Tenant creation and credential
entry are HUMAN steps** (accounts + secrets stay with the operator).

## Prereqs (human)
1. Free Okta developer tenant (developer.okta.com) and/or Google Workspace.
2. In the IdP, create an OIDC Web app: redirect URI
   `https://api.sessionfs.dev/api/v1/auth/sso/callback` (or your self-hosted
   API origin + the same path). Note issuer, client_id, client_secret.
3. Put the client secret in the API's environment under a var of your
   choosing (e.g. `OKTA_SMOKE_SECRET`) — the provider config references it as
   `env:OKTA_SMOKE_SECRET`; the secret value never enters SessionFS.
4. A paid-tier org you own, with a test domain you can publish DNS TXT for.

## The matrix (run per IdP)
| # | Step | Pass criteria |
|---|------|--------------|
| 1 | Dashboard → Org → SSO → Configure provider (issuer/client_id/`env:` ref) | Provider saved; ref shown as a REFERENCE |
| 2 | Add domain → publish the shown TXT record → Verify | Domain flips to verified |
| 3 | CLI login: `POST /auth/sso/start {org_slug}` → browser → IdP → callback JSON | api_key minted; `sso_minted=true` |
| 4 | Browser login: login page → org slug → SSO button → IdP → `/sso/callback` | Lands signed-in; no key in any URL; single-use code (refresh the callback URL → dead) |
| 5 | JIT: sign in as an IdP user with a verified-domain email not yet in the org | Member created, seat consumed, role=member |
| 6 | Enforcement ON → password login for a member | Blocked; owner still passes; service keys unaffected |
| 7 | Break-glass: owner issues grant → target admin password login | Works for 1h; revoke kills it |
| 8 | Deprovision: remove the member | ExternalIdentity deactivated; sso_minted keys revoked |
| 9 | Negative: wrong nonce/state replay (re-use a callback URL) | Constant 400 |
| 10 | Helm deploy (`api.sso.enabled=true` + values) → repeat steps 3–4 | Same behavior self-hosted |

Record results in the ticket; every deviation becomes a follow-up ticket.
