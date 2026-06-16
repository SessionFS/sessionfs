# Shield-SR Code-Level Security Review — Multi-Repo Projects

**Reviewer:** Shield-SR (pre-release security review agent)
**Branch under review:** `feat/multi-repo-projects` (6 commits, +8449/−144, 29 files)
**Base:** `develop`
**Date:** 2026-06-16
**Inputs:** `docs/design/multi-repo-projects.md` §8.2 binding checklist; `docs/security/multi-repo-projects-security-review.md` (Sentinel S1 F1–F6 + L1–L4, S2 APPROVED + N1/N2). DESIGN already passed Codex R5 + Sentinel S1→S2. This pass verifies the **code** implements the security conditions.

---

## Verdict

**APPROVED-WITH-CONDITIONS.**

Every HIGH/MEDIUM security control from the Sentinel review (F1–F6, merge authz, service-key boundary, cross-org/cross-project isolation, migration safety, dry-run zero-write) is **correctly implemented in code and backed by passing negative tests** — with one exception: **F6 app-layer rate limiting on link/merge is not implemented** (MEDIUM, checklist item F6). Two LOW audit items (L1 denial logging via AdminAction; L4 plain-string attestation snapshot surviving user deletion) are also unimplemented. None of these is a CRITICAL/HIGH blocker, the abuse surface F6 protects is largely neutralized by F1 (admin + verification gate) and F3 (opaque 409), and Sentinel S2 already designated edge rate-limiting a Forge follow-up. I therefore approve the merge to develop **with the F6 app-layer limiter and L1/L4 audit hardening recorded as required follow-ups for Atlas/Forge before the merge endpoint is exposed broadly**.

**Findings: 0 CRITICAL / 0 HIGH / 1 MEDIUM (F6) / 3 LOW (L1, L4, weak-F1-test).**

---

## Standard release gates

| Gate | Result |
|------|--------|
| pip-audit (`.venv`) | PASS — No known vulnerabilities |
| npm audit --omit=dev (dashboard) | PASS — 0 vulnerabilities |
| bandit -ll (new/changed src) | PASS — 0 HIGH/MEDIUM (2 LOW = SQLAlchemy `== True` comparisons, benign) |
| ruff (changed src) | PASS — All checks passed |
| Hardcoded-secret scan (`git diff develop..HEAD`) | PASS — none |
| Key Decisions (no WS/Redis, no server-side LLM keys) | PASS — merge dedup is exact-match string compare, no LLM call; no new long-lived secrets |
| Test suites (4 multi-repo files) | PASS — 103 passed |

---

## Per-area findings (§8.2 checklist)

### F1 — repo-link anti-hijack — VERIFIED
- `link_repo` (`routes/projects.py:580-581`) gates on `user_is_project_admin` BEFORE anything else. The forgeable `Session.git_remote_normalized` predicate-#3 path is **not** in the link authz — only project-admin standing is. A forged `workspace.json` tag alone cannot link a global-unique remote.
- `verify_repo_ownership` (`github_app.py:79-145`) implements **N1** correctly: installations are filtered by `GitHubInstallation.user_id == user_id` — the linker's OWN installations only; no global/foreign token. Failure → `(False, 'owner_attested', None, None)`.
- `verified=true` is produced ONLY on the `github_app` 200 path (`github_app.py:132-139`); `owner_attested` and `legacy_backfill` are always `verified=false`. Matches S2 MED-3.
- Displacement (`routes/projects.py:682-733`): verified-beats-unverified **DELETEs** the unverified row (never `project_id=NULL`), frees the UNIQUE slot via `flush()`, then either marks holder `repo_reclaimed_at` (zero repos remaining) or promotes the oldest remaining repo to primary. **No auto-import of the displaced project's data** into the claimant (squatter-poisoning guard upheld). Verified↔verified → 409; unverified→any → 409.
- **N2** (GitHub API call OUTSIDE the swap txn): verification (step 3, line 607-610) runs BEFORE the `with_for_update()` holder lock (step 4, line 615-619). No live HTTP inside the lock.
- Tests: `test_link_repo_denied_non_admin` (403, load-bearing F1), `test_link_repo_owner_attested_happy_path` (verified=false), `test_link_repo_github_app_verified`, `test_verified_displaces_unverified_holder_repo_reclaimed`, `..._has_other_repos`, `test_verified_vs_verified_409`, `test_unverified_cannot_displace_any`.

### F2 — provider_repo_id server-derived — VERIFIED (structurally enforced)
- `LinkRepoRequest` (`routes/projects.py:76-86`) has ONLY `git_remote` + `is_primary`. `provider_repo_id` / `provider` are not fields; Pydantic drops any caller-supplied value at the model boundary — stronger than a runtime check. Server populates them only from the GitHub App response.

### F3 — 409 existing_project_id gated — VERIFIED
- Both 409 branches (`routes/projects.py:632-681`) compute `caller_can_see_holder = user_can_access_project(holder project)` and include `existing_project_id` ONLY when true; unauthorized/cross-org callers get an opaque 409 with no identifier. Test: `test_link_repo_409_cross_org_opaque` asserts no `existing_project_id`; `test_link_repo_409_same_org_full_response` asserts the full body for an authorized caller.

### F4 — tombstone 410 access-check-first — VERIFIED
- `get_project` (`routes/projects.py:878-914`) resolves with `follow_tombstone=False`, then for a tombstone runs `user_can_access_project` on the SOURCE first; unauthorized → opaque 404 (no `merged_into` leak); authorized → re-checks access on the resolved target (F5) before returning 410+`merged_into`. Test: `test_tombstone_access_check_on_source_before_disclose`.

### F5 — resolver resolves-but-never-authorizes — VERIFIED
- `project_resolver.py` resolvers contain no authorization; every rewritten site re-authorizes on the **resolved/redirected** project: `get_project`/`update_project_context` (projects.py), `_get_project_or_404` (rules.py, follows tombstone + `user_can_access_project`), handoff helpers prefer `session.project_id` then `resolve_project_by_remote`, A1 in sessions.py. Hop-cap (`_HOP_CAP=8`) **raises** `ProjectResolutionLoopError` (never a silent Project), routes map to 409. Test: `test_resolver_redirect_re_auth_denies_unauthorized_caller`, tombstone single/multi-hop resolver tests.
- **P2-fix (commit 6847366) does NOT over-broaden access.** `user_can_access_project` predicate #3 (`auth/project_access.py:64-82`) and `_accessible_project_ids` (`handoff_helpers.py:120-164`) match sessions on `OR(Session.project_id == project.id, Session.git_remote == project primary remote, Session.git_remote IN project's linked repo remotes)` — **all gated on `Session.user_id == user_id`, all scoped to the SAME project's repos**. No cross-project or cross-org match is introduced: a session only ever grants access to the one project whose repo it is linked to. This is the same forgeable-session-tag *read* weakness Sentinel explicitly scoped out as residual R-A; the multi-repo change does not widen it.

### F6 — rate limits on link/merge — NOT IMPLEMENTED (MEDIUM follow-up)
- `link_repo`, `unlink_repo`, and `merge_project` have **no** `SlidingWindowRateLimiter` or any app-layer limiter (confirmed by grep — only `auth.py` and `sessions.py` use the limiter). Checklist item F6 requires an app-layer per-user/per-org sliding-window cap on link + merge (incl. dry-run).
- **Severity assessment — MEDIUM, non-blocking:** the abuse vectors F6 guards are materially reduced post-F1/F3: link now requires `user_is_project_admin` + verification (no cheap free-tier namespace squatting at scale), and the F3 probing oracle is closed (opaque 409). Residual is dry-run merge amplification and authenticated admin-gated link attempts. Sentinel S2 closed F6 "at design level" and designated durable edge limiting (Cloud Armor) a Forge follow-up.
- **Follow-up (Atlas):** wire a `SlidingWindowRateLimiter` on `POST /repos` and `POST /merge` (both dry-run and execute). **Follow-up (Forge):** durable edge/multi-replica rate limiting.

### Merge authz (§6.4) — VERIFIED
- `merge_project` (`routes/projects.py:1026-1031`) checks `user_is_project_admin` independently for BOTH source and target → 403 if either fails. `_validate_preconditions` (`merge.py:110-138`) denies cross-org/personal-mix (`source.org_id != target.org_id` → 400), denies double-merge (either `merged_into_project_id` set → 400), and blocks pending project transfers on either side (L3, 400).
- Atomic + dry-run zero-write: `merge_projects` (`merge.py:711-720`) returns the plan with ZERO writes on dry-run (no audit row, reads only). Execute writes a `started` audit row in a **separate** session (survives rollback), runs all 17 steps + `source.merged_into_project_id`/catch-up session reassign inside one transaction committed once at line 804; the exception handler outcome-updates the audit row to `failed` in a fresh session. Tests: `test_merge_dry_run_zero_writes`, `test_merge_precondition_cross_org`, `..._already_merged`, `..._pending_transfer`.

### Service-key boundary — VERIFIED
- `assert_service_key_handoff_boundary` (`handoff_helpers.py:516+`) anchors on `source_session.project_id` first, denies orphan-handoff mutation, and routes the legacy fallback through the multi-repo resolver; the `len(matching) > 1` ambiguity guard is now a DB-enforced invariant kept as defense-in-depth. Boundary remains the project, not the repo — multi-repo does not widen service-key scope.

### Cross-project / cross-org isolation — VERIFIED
- Mandatory leak tests present and passing: `test_link_repo_409_cross_org_opaque`, `test_link_repo_cross_org_rejected`, `test_list_repos_denied_for_stranger`, `test_resolver_redirect_re_auth_denies_unauthorized_caller`, `test_merge_precondition_cross_org`.

### Migration 049 — VERIFIED
- Backfill (`049_...py:232-263`) inserts exactly one `is_primary=true, verified=false, verification_method='legacy_backfill'` row per project, skipping `NULL`/`''` remotes (no orphan rows). Partial unique indexes (primary, provider_repo_id) use cross-DB `postgresql_where`/`sqlite_where`. Downgrade drops the new tables/columns cleanly. Dual-read queries `project_repos` first, legacy column as fallback. Tests: `test_migration_049_up_*`, `_downgrade_cleans_up`, `_idempotent_upgrade`.

---

## LOW findings (record as Atlas follow-ups; non-blocking)

- **L1 — AdminAction denial logging absent.** No `AdminAction` row is written for verified-beats-unverified displacement or cross-org merge denials (grep: zero AdminAction usage in the new code). Checklist item 10 / Sentinel L1 promote this from optional to recommended for SOC visibility of ownership-reassignment attacks. The merge `started`/`completed`/`failed` audit row exists but only for validated execute attempts, not denials.
- **L4 — Attestation snapshot does not survive user deletion.** `ProjectRepo.added_by_user_id` and `ProjectMergeAudit.initiated_by_user_id` are FKs (`ON DELETE SET NULL`). There is no plain-string `user_id + email` snapshot captured at link/merge time, so forensic attribution is lost if the linker's user row is deleted. Add a snapshot column/field per Sentinel L4 + §5.1.
- **L3-test/F1-test — weak F1 hijack test.** `test_link_repo_hijack_denied_forged_session` asserts the OWNER can link (201/409) rather than directly asserting a forged-session non-admin is blocked; it leans on `test_link_repo_denied_non_admin` for the real assertion. Functionality is correctly gated in code; the test name oversells what it checks. Tighten to assert a non-admin with a forged session on the target remote gets 403.

---

## Conclusion

The load-bearing anti-hijack (F1 + N1 + N2), enumeration-leak gating (F3/F4), resolver-authorizes-nothing (F5) including the P2-fix not over-broadening access, merge authz/atomicity/dry-run, service-key boundary, cross-org/cross-project isolation, and migration safety are all correctly implemented and tested. The only MEDIUM gap is F6 app-layer rate limiting (mitigated by F1/F3, Forge edge-limiting already an owned follow-up), plus two LOW audit-hardening items (L1, L4). No CRITICAL or HIGH finding. Safe to merge to develop with the F6/L1/L4 follow-ups tracked.

**SHIELD-SR VERDICT: APPROVED-WITH-CONDITIONS — 0 CRITICAL / 0 HIGH / 1 MEDIUM (F6) / 3 LOW (L1, L4, F1-test).**
