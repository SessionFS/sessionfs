# High-risk merge checklist — resident-authored changes (MANDATORY)

**Status: binding governance gate (CEO condition b).** This checklist is a
**non-rubber-stamp** requirement for merging any change that an *implementer
resident* authored and a *reviewer resident* auto-closed, when the change falls
in a **high-risk class**. It is owned by Scribe (this doc) and enforced as a
Shield governance gate.

## Why this exists

A resident implementer can only **propose**; it never merges. The gate is:

> implementer "done" → `waiting_review` → **independent** trusted-reviewer
> re-derived close → **human merge**

The gate is **uniform** across all change classes (no per-class auto-close
carve-out). The deliberate consequence: a resident-reviewer `VERIFIED-CLEAN` can
auto-close the queue **item**, so the **human merge is the sole remaining human
touchpoint**. That makes the merge **load-bearing** — especially for the class
behind two real prod incidents this cycle (DB migrations, where automated review
passed but environment-specific bugs slipped through).

Every resident-auto-closed item carries a server-set, queryable marker
**`auto_close_review_kind = 'resident_trusted'`** — surfaced as
**"resident-reviewed, NOT human-reviewed"**. If you see that marker, **you are
the first human to look at this code.** Do not merge on the green status alone.

## High-risk classes (this checklist is MANDATORY for these)

- **Database migrations / schema changes** (the weakest class — run against a
  production-accurate DB, not just SQLite)
- **Authentication / authorization / session logic**
- **Cryptography / secret handling**
- **CI/CD / build / release pipeline**
- **Dependency bumps** (new transitive code entering the supply chain)
- **Infrastructure / Terraform / deploy config**

For changes outside these classes, the checklist is strongly recommended but not
gated.

## The checklist (complete every item before merging)

- [ ] **Provenance.** The item shows `auto_close_review_kind = 'resident_trusted'`
      — you understand no human reviewed it yet.
- [ ] **Independence.** The closing trusted reviewer identity ≠ the implementer
      identity (server-enforced self-review prohibition), and the clean verdict
      **postdates** the implementer's latest writeback.
- [ ] **You read the actual diff locally** — not the ticket summary, not the
      diff-ref. Pull the `resident/<queue>/<ticket>` branch and read it.
- [ ] **Tests / CI pass in YOUR environment**, not only in the resident sandbox.
- [ ] **DB migrations: run upgrade AND downgrade against a production-accurate
      database** (PostgreSQL with the real prior schema), not SQLite. Confirm no
      object-already-exists / dialect-divergence surprises.
- [ ] **Auth/crypto: trace the authz path by hand.** Confirm no fail-open, no
      bypass, no secret in logs or in the diff.
- [ ] **Dependency bumps: review the changelog + advisories** for the bumped
      package; confirm no unexpected transitive additions.
- [ ] **Scope.** The change does only what the ticket asked — no unrelated edits,
      no drive-by refactors, no touched files outside the stated scope.
- [ ] **No injected instructions took effect.** The ticket/comments are untrusted
      input to the resident; confirm the change reflects the real task and not a
      prompt-injection redirect (odd filenames, unexpected exfil-shaped code).
- [ ] **You, a named human, take ownership of the merge.** Record who merged.

## Enforcement

- **Shield** gates high-risk merges on this checklist being followed; a merge of
  a `resident_trusted`-marked high-risk item without it is a governance
  violation.
- There is **no automated merge** in v1. Every merge is a human action.
- The resident can be stopped at any time: an org owner/admin **retires** it
  (revokes its service key) or **pauses** its queue; a poisoned implementer mind
  can be **quarantined** (owner/admin only). See the operator guide.
