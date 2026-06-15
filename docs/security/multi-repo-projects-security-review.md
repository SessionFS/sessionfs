# Security Review — Multi-Repo Projects (§8 Sentinel Pre-Build Pass)

**Reviewer:** Sentinel (security owner)
**Design under review:** `docs/design/multi-repo-projects.md` (R5, Codex VERIFIED-CLEAN for correctness) + `docs/design/multi-repo-projects-product.md`
**Date:** 2026-06-15
**Status of design:** Codex-clean for *correctness*; this is the independent *security* dimension required before any implementation code is written (design §8 / §6.5).
**Scope:** `project_repos` join table, link/unlink/list-repos endpoints, `sfs project merge`, the 16-site resolver rewrite, tombstone redirect, service-key behavior post-merge.

---

## Verdict

**APPROVED-WITH-CONDITIONS.**

The data-model and merge transaction design are sound and the author wrote a genuinely good security checklist (§8.2). However, the design **inherits and amplifies a pre-existing trust gap**: `git_remote_normalized` is extracted from the *client-supplied* `workspace.json` inside the uploaded session archive (`routes/sessions.py:789-795` → `normalize_git_remote`), with **zero repo-ownership verification anywhere in the codebase**. The design's anti-hijacking control ("user must have captured a session on the target remote") is therefore **forgeable**, and the design over-states `provider_repo_id` as a server-derived mitigation that the current infrastructure cannot actually deliver for most repos. Several response contracts (409 `existing_project_id`, 410 `merged_into`) also introduce **cross-tenant enumeration leaks** that the design does not gate behind an access check.

None of these are CRITICAL blockers to the *schema/merge* work, but **F1, F2, F5, F6 below are MUST-FIX in the implementation** and one (F1) requires a design-doc amendment by Atlas. The merge engine itself is APPROVED as designed.

**Must-fix findings: 6** (1 HIGH, 4 MEDIUM, 1 MEDIUM-design-amendment). Plus 4 LOW / defense-in-depth.

---

## Threat Model (concise)

- **Assets:** project memory (KB claims, wiki, personas, tickets, rules, agent runs, retrieval audit), the global repo→project namespace, cross-org isolation boundary.
- **Actors:** authenticated free-tier user (the primary attacker — multi-repo is FREE for all tiers, so the cheapest attacker has full access to link/merge), malicious teammate, compromised user token, service key (cloud agent/CI), former org member, external prober.
- **Entry points:** `POST /projects/{id}/repos`, `DELETE …/repos/{id}`, `GET …/repos`, `POST /projects/{id}/merge`, the session sync path that *populates* `git_remote_normalized`, every resolver that now routes through `project_repos` + tombstone redirect.
- **Trust boundaries crossed:** client→server (forgeable workspace metadata), user→project, project→project (merge), org→org (the data-stays-access-revoked invariant), user-key→service-key.

---

## Findings

### F1 — HIGH — Repo-link anti-hijack control is forgeable (no repo-ownership verification)

**Area:** §1 Repo hijacking, §6.1/§6.2.

**Design claim:** "The linking user must have captured at least one session on the target remote, OR be the project owner / org admin" prevents linking repos you've never worked on.

**Ground truth in code:**
- `routes/sessions.py:789-795` extracts `git_remote_normalized` from the uploaded archive's `workspace.json` via `normalize_git_remote(workspace_data.get("git_remote", ""))`. `normalize_git_remote` (`github_app.py:25-43`) only string-normalizes — it does **not** verify the caller owns or can push to the repo.
- `_check_repo_access` (`routes/projects.py:74-82`) and `user_can_access_project` predicate #3 (`auth/project_access.py:55-65`) both grant access purely on `Session.git_remote_normalized == <remote>` for a session the user owns.
- Grep for any repo-ownership verification (`verify_repo`, collaborator check, installation-token repo check) returns **nothing**.

**Attack:** A free-tier attacker fabricates a `.sfs` session whose `workspace.json` declares `git_remote = github.com/facebook/react` (or a victim's private `acme/secret-backend`), syncs it (creating a `Session` row tagged with that remote they own), then calls `POST /projects/{their_project}/repos {git_remote: ".../react"}`. The "captured a session on the remote" gate passes because the gate trusts the forged tag. The attacker now **claims the globally-unique `react` remote** under their project.

**Impact:**
1. **Denial-of-service / namespace squatting (primary).** Because `UNIQUE(git_remote_normalized)` on `project_repos` is global, the legitimate owner of `facebook/react` can never link or create a project for that remote — they get `409 repo_already_linked`. The attacker has reserved another org's/user's repo namespace. This is *new* damage that 1:1 projects did not allow at this severity (today a forged session only grants the forger read on *their own* phantom project; now it locks a global resource).
2. **Resolver capture.** Post-link, `resolve_project_by_remote(react)` routes legitimate future sessions/handoffs for that remote toward the attacker's project anchor (until the global unique blocks a competing link). Combined with predicate #3, victims who later sync real sessions on that remote may be auto-routed (`_resolve_project_id_for_session`) into the attacker's project_id.

**Severity rationale:** Cross-tenant *namespace* control + practical DoS of a global unique resource, exploitable by the cheapest actor (free tier). HIGH, not CRITICAL, only because it does not by itself exfiltrate the victim's existing data (the victim's already-synced sessions stay under their own rows).

**Required mitigation (implementation):**
- The forged-session gate is **not** an acceptable sole control for *claiming a global-unique resource*. For the link endpoint, require **one** of:
  - (a) **Project owner / org-admin only**, AND the remote is one the *project already legitimately owns a verified relationship to* — i.e., drop predicate-#3-style "captured a session" as sufficient for linking a *new global-unique* remote; OR
  - (b) **Proof of repo control** when the GitHub App is installed: confirm via installation token that the linking user/org has access to the repo (`GET /repos/{owner}/{repo}` with the installation token succeeds AND the installation belongs to the linker's account/org). When the App is not installed (self-hosted/unknown provider), fall back to (a) — owner/admin + an explicit "I attest I control this repo" with the link recorded in `project_repos.added_by_user_id` and an audit row, and document that unverified links are owner-attested.
- Either way, **do not let a forged session tag alone reserve a global-unique remote.**

**Design-doc amendment needed (Atlas):** §6.1 item 3 and §4.1 link authz currently present "captured a session on the remote" as a sufficient anti-hijack control. Rewrite to make repo-control verification (or owner/admin-attested fallback) the binding rule, and explicitly note that `git_remote_normalized` is attacker-controlled client metadata that cannot be trusted as proof of repo ownership.

---

### F2 — MEDIUM — `provider_repo_id` "server-derived" mitigation is largely unrealizable on current infra; do not rely on it for hijack/DoS defense

**Area:** §6 Q6 provider_repo_id, §3.1, §6.2, Appendix B.5.

**Design claim:** `provider_repo_id` is "server-derived… resolved from the provided `git_remote` using the configured provider (e.g. GitHub App installation context already available server-side)," giving reservation/DoS resistance and rename-bypass detection via `UNIQUE(provider, provider_repo_id)`.

**Ground truth in code:**
- There is **no per-repo provider-ID mapping** stored. `GitHubInstallation` (`models.py:526-540`) stores `id` (installation id), `account_login`, `account_type` — **not a repo list and not repo IDs.** There is no `installation_repos` table.
- Deriving a `provider_repo_id` for an arbitrary `owner/repo` requires a *live* GitHub API call with an installation token — which only works if **the App is installed on that repo**. SessionFS users are not required to install the GitHub App; many (CLI-only, GitLab, self-hosted) never will. The design itself concedes "If server-side resolution fails… stored as NULL."

**Consequence:** For the common case, `provider_repo_id` will be **NULL**, so the partial `UNIQUE(provider, provider_repo_id)` index never fires. The *only* live uniqueness guard is `UNIQUE(git_remote_normalized)`. Therefore the design's stated mitigations that lean on provider_repo_id — "prevents reservation/DoS attacks via forged ID" (§6.2) and "rename-bypass detection" (Appendix B.5) — **do not hold for the majority of links.** This is a security over-statement, not a code bug, but it matters because F1's real defense must NOT be assumed to come from provider_repo_id.

**Required mitigation:**
- **Correctness of the contract is already right** (caller input ignored — good, keep that). The fix is to **stop crediting provider_repo_id as the hijack/DoS defense** in the design. The binding anti-hijack control is F1's verification. provider_repo_id is a *best-effort rename-survival* nicety only.
- If/when derivation runs, it must use a token scoped to the **linker's own installation** — never a global app token that could resolve a repo the linker has no access to (that would itself become a confused-deputy oracle). If the only available installation does not cover the repo, store NULL; do not cross-installation-resolve.

**Design-doc amendment (Atlas):** soften §6.2 / Appendix B.5 language so provider_repo_id is documented as best-effort (frequently NULL), and the global `UNIQUE(git_remote_normalized)` + F1 verification are the load-bearing controls.

---

### F3 — MEDIUM — 409 `repo_already_linked` leaks `existing_project_id` cross-tenant (enumeration oracle)

**Area:** §4.1 link response contract, own threat-model.

**Design contract:** `409 → {"error":"repo_already_linked","existing_project_id":"…", "message":"… already linked to project X …"}`.

**Problem:** Any authenticated free-tier user can `POST …/repos {git_remote: <any remote>}` and learn from the 409 whether that remote is linked and **the project_id that owns it — including projects in other orgs they have no access to.** `project_id` is an opaque ID, but it is a cross-tenant existence + linkage oracle (e.g., "is `acme-private/payments` onboarded to SessionFS, and what's its project handle"), and the returned project_id can be fed to other ID-taking surfaces to probe further.

**Required mitigation:**
- On 409, **do not return `existing_project_id` or any owning-project identifier unless the caller passes `user_can_access_project` for that existing project.** For unauthorized callers, return a generic `409 {"error":"repo_already_linked"}` with no cross-tenant detail (or `403`/`404`-style opacity). Only owners/org-admins/members of the *owning* project see the unlink-or-merge guidance with the project handle.
- Add a cross-org negative test: user in org A probes a remote owned by org B → response contains no org-B project_id.

---

### F4 — MEDIUM — Tombstone 410 `merged_into` must run the access check on the source/tombstone BEFORE disclosing the target

**Area:** §5 Tombstone (410 info leak), §6.5 item 7.

**Ground truth in code:** Today `get_project` (`routes/projects.py:362-397`) resolves the project, THEN calls `user_can_access_project`. The design's A2 says "return `410 Gone` with `{merged_into: target_id}` when the resolved project is a tombstone." The design does **not** state that the access check must precede the 410 body. If implemented as resolve→detect-tombstone→410 before authz, an unauthorized caller who knows a *source* remote learns the **target project_id** (and confirms a merge happened, who-merged-into-whom topology).

**Required mitigation (implementation):**
- The 410 tombstone response is a privileged disclosure. **Run the access predicate against the SOURCE (tombstone) project first.** If the caller lacks access to the source project, return the same `404`/`403` shape a non-tombstone inaccessible project returns — **never `merged_into`.** Only a caller who could have read the source pre-merge may learn the target id.
- Mirror this in the resolver: `resolve_project_by_remote(..., follow_tombstone=True)` transparently redirects, but the **route must still enforce access on the resolved target** — following a tombstone chain must never grant access the caller would not have had on the target directly (see F5).
- Tests: (a) source-owner gets 410+merged_into; (b) stranger gets opaque 404, no target id; (c) former org member who lost access after a transfer gets opaque response.

---

### F5 — MEDIUM — Tombstone redirect must re-authorize on the TARGET; redirect is not an access grant

**Area:** §7 Resolver tombstone-redirect, §3.3.

**Design behavior:** `resolve_project_by_remote` / `resolve_project_by_id` follow `merged_into_project_id` and return the *target* project. The risk: a caller who had access to the source (e.g., via forged-session predicate #3 on the source's remote, or as a former member) is now transparently handed the **target** project object. If the calling route trusts "I resolved a project for this remote, therefore the caller may use it," the redirect becomes a **privilege-escalation bridge** into the target's data — which may contain another team's merged-in memory.

**Ground truth:** Access is enforced per-route by `user_can_access_project(db, user_id, resolved_project)` AFTER resolution (good pattern, e.g. `projects.py:382`). The design must make this **mandatory at every one of the 16 rewritten sites** — resolution returns a candidate; authorization is a separate, non-skippable step against the *returned* (target) project, not the input remote.

**Required mitigation (implementation):**
- Binding rule for the resolver rewrite: **`resolve_project_by_*` returns a project; it never authorizes.** Every Group-A site that previously did `where(remote).scalar_one_or_none()` followed by an access check must keep the access check, and the check must run against the **resolved/redirected** project, not the source remote/id.
- Specifically audit B1/B3 (`user_can_access_project` predicate #3 changing to `Session.project_id == project.id`): after the swap, confirm a user whose only tie was a forged session on the *source* remote does NOT inherit `project_id` rows pointing at the target (post-merge their sessions get reassigned to target via merge step 10/17 — verify this does not silently grant predicate #1/#3 access to the merged target's data). Add a test: pre-merge stranger-with-source-session → post-merge → must not read target KB.

**Cycle safety (no separate finding):** §5.2 preconditions reject merging a project whose `merged_into_project_id IS NOT NULL` on either side, so A→B→A cannot be created. The resolver's `while project.merged_into_project_id` loop is still unbounded in principle — add a **hop cap (e.g. 16) with a logged error** as defense-in-depth in case data corruption or a future code path bypasses the precondition. (LOW, folded into F5 mitigations.)

---

### F6 — MEDIUM — No rate limiting / abuse control on link + merge (free-tier, in-memory limiter only)

**Area:** §8 destructive-op abuse, Sentinel invariant ("public endpoints rate-limited at a layer that survives horizontal scaling").

**Ground truth:** The only limiter is `auth/rate_limit.py` `SlidingWindowRateLimiter` — **in-memory, per-replica**, explicitly insufficient for multi-replica Cloud Run. The design specifies **no** rate limit on link or merge.

**Abuse paths:**
- **Link probing** (compounds F3): unbounded `POST …/repos` to enumerate which remotes are linked and capture global-unique remotes (F1) at scale.
- **Merge dry-run flood:** `POST …/merge {dry_run:true}` runs full collision detection (multiple SELECTs across personas/pages/KB/links) with **zero write** but real DB cost — a cheap amplification DoS, and it requires owning/admin of two projects (lower risk but free-tier-reachable by creating throwaway projects).
- **Repeated real merges** are self-limiting (source becomes a tombstone, 400 on re-merge), so merge-execute is naturally bounded. Dry-run and link are not.

**Required mitigation (implementation + Forge follow-up):**
- App-layer: apply a sensible per-user/per-org sliding-window cap to `POST …/repos` and `POST …/merge` (both dry-run and execute). Treat link as a sensitive mutation, not a read.
- **Forge ticket:** edge rate limiting (Cloud Armor / API Gateway) for the link + merge routes so the control survives horizontal scaling. In-memory limiter is convenience only. (Sentinel → Forge.)

---

### Lower-severity / defense-in-depth

- **L1 — LOW — Audit: record security-relevant DENIALS, not just validated attempts.** §5.1/§5.11 (R4) narrows merge-audit to *validated execute attempts only*; cross-org/already-merged/404 rejections are left to "standard request/access logging." For a destructive ownership-reassignment op, a **rejected cross-org merge attempt** is exactly the signal a SOC wants. Implement the design's own optional `AdminAction` note for cross-org merge denials and unauthorized link attempts. (Sentinel-driven follow-up the design already flagged — promote from optional to recommended.)
- **L2 — LOW — `sfs security scan` coverage.** No new local secret files here, but if link/merge ever caches provider tokens locally, ensure `0600`. Not v1-relevant; note for parity with the v0.10.29 Shield-SR LOW (profiles perms).
- **L3 — LOW — Merge with active transfer / pending invites.** Appendix B.4 already requires pending transfers resolved before merge — good. Add: also block merge if the source has a **pending project_transfer** in EITHER direction (a merge that strands a pending cross-scope transfer could move data the transfer recipient was about to gain/lose rights to). Enforce in Phase-1 preconditions.
- **L4 — LOW — `added_by_user_id` is `ON DELETE SET NULL`.** Acceptable for FK hygiene, but the link's *attestation* (who claimed this repo, F1 fallback) must survive user deletion for forensics. Capture the linker id/email snapshot in an audit row, not only the nullable FK.

---

## What the design got RIGHT (no action)

- **Merge atomicity** — single `db.begin()` Phase-3, dry-run zero-write, separate-session attempt/outcome audit surviving rollback (§5.12/§5.13). This correctly applies the v0.10.13 `/rebuild` incident lesson. **APPROVED.**
- **Cross-org merge denial** (`source.org_id != target.org_id → 400`, §5.12) — matches the data-stays-access-revoked invariant; grounded against real `Project.org_id`. **APPROVED.**
- **Merge authz model** (own both OR org-admin of both, same org) maps cleanly onto the real `user_is_project_admin` (`auth/project_access.py:68-96`) / owner check. Implementation MUST use `user_is_project_admin` for the org-admin branch (role=='admin' via OrgMember), and check it independently for BOTH source and target. **APPROVED as designed; see checklist.**
- **Service-key non-auto-update post-merge** (Appendix B.1) — a key scoped `project_ids:[source]` losing access when source becomes a tombstone is the **correct fail-closed choice.** `assert_service_key_can_access_project` (`auth/dependencies.py:323-370`) checks org match + allowlist against the resolved project; after merge the source tombstone is no longer a normal target, and the target is not in the key's allowlist → `project_not_in_allowlist` 403. Multi-repo does not widen service-key scope (boundary is the *project*, not the repo). **APPROVED.** *Condition:* verify the resolver does NOT silently redirect a service key from source→target and thereby bypass the allowlist (this is the service-key instance of F5 — the allowlist check must run on the *resolved target* id; since target ∉ allowlist, it correctly denies — add an explicit test).
- **Ticket reassign-in-place** — `_validate_dependencies_same_project` + `_check_dependency_cycle` exist (`routes/tickets.py:723,756`); both source and target ticket sets were same-project-acyclic at creation and reference no cross-set ids, so the post-merge union under one project_id stays valid and acyclic. **APPROVED.**
- **`assert_service_key_handoff_boundary` invariant upgrade** (§2.6) — with global `UNIQUE(git_remote_normalized)` the `len(matching) > 1` branch (`handoff_helpers.py:529`) becomes unreachable; keeping it as a defense-in-depth assertion is correct. **APPROVED.**

---

## Binding Implementation Security Checklist (Shield-SR will verify at code review)

Each item is a gate; "verified" requires a passing negative test where a test is feasible.

1. **[F1] Repo-link does not trust forged session tags.** Linking a global-unique remote requires owner/org-admin AND repo-control proof (installation-token repo check) OR an explicit owner-attested fallback recorded in audit. A fabricated `workspace.json` remote alone MUST NOT permit linking a remote the caller does not control. *Test:* user with a forged session on `victim/private` is denied link (403), and cannot reserve the global remote.
2. **[F1] Design doc amended** (§6.1/§4.1) to state git_remote is attacker-controlled and verification is the binding control. (Atlas — pre-implementation.)
3. **[F2] provider_repo_id never trusted from caller input** (already in contract — keep). Derivation, when attempted, uses ONLY the linker's own installation token; cross-installation resolution is forbidden; failure → NULL. Design language de-credits provider_repo_id as the hijack/DoS defense. *Test:* caller-supplied `provider_repo_id` in the link body is ignored.
4. **[F3] 409 `repo_already_linked` discloses `existing_project_id` ONLY to callers who pass `user_can_access_project` on the owning project.** Unauthorized/cross-org callers get an opaque 409. *Test:* org-A user probing an org-B remote sees no org-B project id.
5. **[F4] Tombstone 410 `merged_into` is gated by an access check on the SOURCE project first.** Strangers/former-members get an opaque 404/403, never the target id. *Tests:* source-owner→410+target; stranger→opaque.
6. **[F5] Resolver authorizes nothing.** Every one of the 16 rewritten sites runs `user_can_access_project` (or the service-key boundary) against the **resolved/redirected target** project, not the input remote/id. Tombstone-follow loop has a hop cap (≤16) with logged error. *Tests:* (a) following a tombstone does not grant target access the caller lacked; (b) service key scoped to source ∉ target allowlist is denied after merge.
7. **[F6] Link + merge (incl. dry-run) are rate-limited** at the app layer; Forge ticket filed for edge rate limiting that survives multi-replica. *Test:* burst of link/merge-dry-run is throttled.
8. **[Merge authz] BOTH source and target authorized independently** via owner-of-each OR `user_is_project_admin` of each; cross-org/personal-mix denied (400); both `merged_into_project_id IS NULL` precondition enforced. *Tests:* non-owner of one side denied; cross-org denied; double-merge denied.
9. **[Service key] Multi-repo does not widen service-key scope.** Boundary remains the project; `assert_service_key_can_access_project` runs on the resolved target. Post-merge, a `project_ids:[source]`-scoped key is denied on the target. *Test:* present.
10. **[Audit] Cross-org merge denials and unauthorized link attempts are recorded** (AdminAction or equivalent), not only validated attempts. Linker attestation snapshot survives user deletion. (L1, L4.)
11. **[Migration] Backfill 049** creates exactly one `is_primary` repo per existing project; empty/NULL `git_remote_normalized` projects produce no orphan rows and resolve safely; downgrade is clean. Dual-read fallback queries `project_repos` first (no shadowing).
12. **[Dry-run] Provably zero DB writes** — no audit row, no row locks beyond reads (re-verify §5.12 Phase-1 path). *Test:* row-count delta == 0.
13. **[B.4/L3] Merge blocked while a project_transfer is pending in either direction** on either side.
14. **[Regression] No raw secrets / cross-tenant identifiers in 4xx bodies, logs, or merge-audit stats.** project_id is the only id that may appear, and only to authorized callers (see #4).

---

## Residual Risk & Owners

- **Forged `git_remote_normalized` is a pre-existing platform weakness** (predates this feature; predicate #3 already trusts it for read access). This review hard-gates it for the *new* global-unique link/claim path (F1). A broader follow-up — verifying repo provenance at session-sync time, or down-weighting predicate #3 generally — is a separate Sentinel ticket and should be filed regardless of this feature. **Owner: Sentinel (follow-up ticket).**
- **Edge rate limiting** for link/merge — **Owner: Forge** (F6).
- **Design-doc amendments** for F1/F2 language — **Owner: Atlas** (must land before implementation; do not edit the binding doc as part of this review).

---

**SENTINEL VERDICT: APPROVED-WITH-CONDITIONS** — merge engine and cross-org/service-key model are sound; ship only after the 6 must-fix conditions (forgeable-link verification F1, provider_repo_id de-crediting F2, 409/410 enumeration leaks F3/F4, resolver-authorizes-nothing F5, link/merge rate limiting F6) are implemented and the 14-item checklist passes. Must-fix: 6 (1 HIGH, 5 MEDIUM).

---

# Re-review (S2) — Amendment Pass

**Reviewer:** Sentinel (security owner)
**Design under review:** `docs/design/multi-repo-projects.md` as amended by Atlas (commit `3556cd2`, S1 amendment row + new §11 Security Conditions).
**Date:** 2026-06-15
**Trigger:** S1 returned APPROVED-WITH-CONDITIONS (1 HIGH F1 + 5 MED F2–F6 + 4 LOW). Atlas folded all conditions into the binding doc. This pass verifies each condition is actually closed and adversarially reviews the NEW mechanism F1 introduces (verified-vs-unverified displacement).

## Verdict

**APPROVED.** All 6 must-fix conditions and the 4 LOW items are genuinely addressed in the amended design. The new verified-ownership / displacement mechanism is sound — no new must-fix finding. Two non-blocking implementation notes (N1, N2) and one residual-risk acknowledgement (R-A) are recorded below for the build phase; none gate the design. Build may proceed once Shield-SR's code-review checklist (§8.2) passes against the actual implementation.

**New must-fix findings: 0.**

## Condition-by-condition confirmation

### F1 (HIGH) — verified-vs-unverified ownership model — CLOSED

The hijack hole is closed at the design level. The binding rule is now: linking a global-unique remote requires `user_is_project_admin` on the target project **AND** a verification stamp (§4.1, §6.1, §11/F1). A forged `workspace.json` session tag is explicitly stated as attacker-controlled and is no longer sufficient to claim a remote — the "captured a session" predicate (the S1 attack vector) is dropped from the link authz path entirely. Verified against code:

- `auth/project_access.py:68-96` `user_is_project_admin` is real and is owner-OR-`OrgMember.role=='admin'`, service-keys excluded — exactly the actor-side check the design binds to. Good.
- `github_app.py:46-76` `get_installation_token(installation_id)` exists, so the `github_app` verification path is realizable. `GitHubInstallation` (`models.py:526-540`) is keyed by `id` (installation id) and bound to a nullable `user_id` — so the design's F2 rule "use ONLY the linker's own installation token" maps cleanly onto `GitHubInstallation.user_id == linker.id`. Good.

**Adversarial review of the NEW mechanism:**

- **(a) Can `github_app` verification be forged/bypassed?** No design-level forgery path. Verification is a server-side installation-token call to GitHub (`GET /repos/{owner}/{repo}` or the installation's accessible-repo set); the client supplies no proof material that the server trusts. The one thing the implementation MUST get right (recorded as **N1** below): the verification must confirm the repo is reachable by **the linker's own installation** (`GitHubInstallation.user_id == linker.id`), not by *any* installation the server holds a token for. Using a global/foreign app token would turn the check into a confused-deputy oracle — S1/F2 already forbids cross-installation resolution; the displacement path must obey the same rule. Design language (§6.2 "Server derivation uses ONLY the linker's own installation token; cross-installation resolution is forbidden") covers this; it is a build-time gate, not a design hole.
- **(b) Can displacement be ABUSED?** The displacement matrix (§4.1, §6.2) only permits `verified(github_app)` to displace `unverified(owner_attested|legacy_backfill)`. To gain `github_app` verification an attacker must actually pass the GitHub installation check for that repo — i.e. genuinely control it. So an attacker cannot displace a legitimate holder unless they truly own the repo, in which case displacement is the *correct* outcome (the real owner reclaims a squatted remote). Verified↔verified is a 409 (no auto-displacement, manual/support resolution) — so a second genuine installation cannot grief the first. Grief-via-repeated-displace is bounded: once a verified row holds the remote, further verified requests hit the 409 path (no churn), and F6 rate limits cap attempt volume. **No abuse path.**
- **(c) Does `owner_attested` (unverified) still allow squatting a public remote globally until a verified claimant appears?** Yes — residual, and it is acceptable and now documented. An owner-attested link reserves the global-unique remote without proof. But: (i) it requires `user_is_project_admin` standing (not the cheap forged-session path S1 flagged), raising the bar materially; (ii) it is fully displaceable by any later `github_app`-verified claimant via the swap, so the squat cannot survive a real owner showing up; (iii) the `added_by_user_id` FK + L4 audit snapshot make the squatter attributable. The blast radius is "temporary namespace reservation, auto-reversible by the true owner" — a denial-of-convenience, not data access. This is the correct residual posture for the self-hosted/non-GitHub/app-not-installed reality where no oracle-free proof exists. Recorded as **R-A**.
- **(d) TOCTOU/atomicity in the swap?** The procedure (§4.1) is `SELECT … FOR UPDATE` the existing row (and its project row) → unlink → audit → insert → commit, all in one transaction. The row lock plus the `UNIQUE(git_remote_normalized)` constraint serialize concurrent claimants: a second displacer blocks on the FOR UPDATE, and on resume re-reads the now-verified holder and falls to the 409 path. The unique constraint is the backstop if the lock is ever bypassed. **No TOCTOU hole at the design level.** Build note **N2**: the verification (GitHub API call) should be performed BEFORE opening the swap transaction (or its result captured before the FOR UPDATE), so a slow external call does not hold the row lock for the network round-trip — a liveness/lock-contention concern, not a correctness one.
- **(e) Can `legacy_backfill` rows be wrongly displaced?** They are displaceable only by `github_app`-verified claims (§6.2 / §3.2 backfill note) — i.e. only by someone who genuinely controls the repo. An unverified claim cannot touch them. This is intentional and correct: it lets a real owner reclaim a remote that a pre-feature squatter grabbed via the old forged-session path, while protecting legitimate existing projects from any unverified challenger. **Correct.**

F1 is closed; the new mechanism is sound.

### F2 (MED) — de-credit provider_repo_id — CLOSED
§3.1 schema comments, §4.1, §6.2, and Appendix B.5 now all state provider_repo_id is server-derived (caller input ignored), frequently NULL, and a best-effort rename-survival nicety — NOT the hijack/DoS defense. Verified against code: `GitHubInstallation` (`models.py:526-540`) indeed stores no repo list and no repo IDs, so the S1 claim that derivation requires a live per-repo API call (and is NULL for the common case) holds. The load-bearing control is correctly relocated to F1's verification. Closed.

### F3 (MED) — 409 existing_project_id leak — CLOSED
§4.1 and §11/F3 gate `existing_project_id` behind `user_can_access_project` on the *owning* project; unauthorized/cross-org callers get an opaque 409 with no identifier. Negative test specified (org-A probing org-B → no id). Closed.

### F4 (MED) — tombstone 410 access-check-first — CLOSED
§5.9 and §3.3/A2 now resolve with `follow_tombstone=False` first, run `user_can_access_project` on the SOURCE (tombstone) before disclosing `merged_into`, and return opaque 404/403 to unauthorized callers. Both the by-id and by-remote routes are covered. Tests specified. Closed.

### F5 (MED) — resolver authorizes nothing + re-auth on target + hop-cap — CLOSED
§3.3 makes the binding rule explicit in the resolver docstring ("RESOLVES; it NEVER authorizes"); all 16 rewritten sites are instructed to run the access check against the resolved/redirected target. Hop-cap (≤8, logged error, fail-safe return) is present on both `resolve_project_by_remote` and `resolve_project_by_id`. The service-key corollary (allowlist check runs on the resolved target; source-scoped key denied on target post-merge) is captured in Appendix B.1 + checklist item 9. Closed. (Note: F5 closure is design-binding; the actual 16-site enforcement is a Shield-SR code-review gate — §8.2 item F5.)

### F6 (MED) — rate limits on link + merge — CLOSED (design) + Forge follow-up
§6.6 specifies app-layer per-user/per-project sliding-window caps on link, unlink, and merge (incl. dry-run), with example thresholds, and mandates a Forge ticket for durable edge/multi-replica limiting (Cloud Armor / API Gateway), to be referenced in the implementation commit. This matches the S1 requirement that in-memory limits are convenience-only. Closed at design level; Forge edge-limiting remains an owned follow-up (R-A list).

### 4 LOW — all addressed
- **L1** (audit denials) — promoted from optional to recommended in §5.1; cross-org merge + unauthorized link denials routed through AdminAction. Addressed.
- **L2** (`sfs security scan` parity) — noted; no new local secret files in v1. Addressed.
- **L3** (block merge on pending transfer) — added to Phase-1 preconditions in §5.12 pseudocode for BOTH source and target, in either direction. Verified in pseudocode (lines for `source_transfer`/`target_transfer` → 400). Addressed.
- **L4** (attestation survives user deletion) — §5.1 + §3.1 comments require a plain-string user_id + email snapshot in the audit row, FK kept ON DELETE SET NULL for hygiene only. Addressed.

## Non-blocking implementation notes (for Shield-SR / Atlas at build)

- **N1 — Verification must use the linker's OWN installation.** The `github_app` path must verify the repo against `GitHubInstallation.user_id == linker.id` (or the linker's org installation), never an arbitrary server-held token. A foreign/global app token would be a confused-deputy oracle and could mint `verified=true` for a repo the linker does not control — which would then (wrongly) enable displacement. The design already forbids cross-installation resolution (§6.2); this note makes the binding explicit for the displacement path too. Shield-SR: gate at code review.
- **N2 — Do the GitHub API call OUTSIDE the swap transaction.** Perform verification before `SELECT … FOR UPDATE` so the external round-trip does not hold the row lock. Liveness only; correctness is unaffected (the unique constraint + re-read on the displaced path are the backstops).
- **R-A — Residual risks (acknowledged, not blocking):** (1) owner-attested squatting of a remote until a verified owner reclaims it — bounded, auto-reversible, attributable; acceptable. (2) Edge rate limiting depends on the Forge ticket landing; until then app-layer limits are per-replica only. (3) The broader pre-existing forged-`git_remote_normalized` weakness (predicate #3 trusts it for *read* access, and project-CREATE still trusts it for squatting) is out of scope for this feature — §6.2 notes the create-path hardening as a follow-up; the separate predicate-#3 down-weighting remains a Sentinel-owned follow-up ticket as recorded in S1.

## S2 verdict

**SENTINEL RE-REVIEW VERDICT: APPROVED** — all 6 must-fix conditions (F1–F6) and 4 LOW are closed in the amended design; the new verified-vs-unverified displacement mechanism is adversarially sound (no forgery, no abusive/grief displacement, atomic swap with no TOCTOU, legacy rows protected from unverified challengers). New must-fix findings: 0. Build may proceed; Shield-SR verifies the §8.2 checklist (esp. F1 verification-via-own-installation, F5 16-site re-authorization) against the implementation.
