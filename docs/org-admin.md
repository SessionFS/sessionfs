# Org Admin Guide

*v0.10.0+ — the Org Admin Console.*

This guide covers the dashboard surfaces and CLI commands an org admin uses to
manage members, project ownership, and team-wide defaults. It assumes you
already have an org (created via `sfs org create` or the dashboard signup
flow) and a paid SessionFS tier (orgs are team+ only).

## Members

Manage who can see and act on org-scoped projects from the **Members** card on
the Organization page (`/settings/organization`).

### Invite

Admins invite teammates by email. The invitee receives an email with a link to
accept; clicking through creates their SessionFS account if they don't have
one yet, then enrolls them in the org at the chosen role.

* **Role: member** — can see all org-scoped projects, capture sessions against
  them, and read the org's KB.
* **Role: admin** — everything `member` can do, plus invite/remove other
  members, change roles, initiate org → org project transfers, and edit org
  settings.

Seats are enforced on accept: if the org has hit its seat limit (per its tier
plan) the invite acceptance fails with a clear error.

### Remove

Removing a member **revokes access** but **never destroys data**. This is the
CEO-mandated invariant for v0.10.0 — the dashboard confirm modal spells it out
verbatim:

> Access will be revoked. Data stays.
>
> - Their sessions stay under their account (sessions are user-owned).
> - Their org-scoped projects auto-transfer to you with an audit trail.
> - Their knowledge-base entries stay in this org's KB with authorship preserved.
> - Pending transfers tied to their org standing here will be cancelled.

Mechanically, when you remove a member the server:

1. Auto-transfers every org-scoped project they owned to **you** (the removing
   admin). Each transfer writes a durable `ProjectTransfer` row so the move is
   auditable.
2. Cancels every pending transfer the removed member was either initiating
   from this org *or* receiving into a personal/org bucket tied to projects
   that just changed ownership. This prevents stale invitations from claiming
   projects the user no longer controls.
3. Clears the removed member's `default_org_id` pointer if it was this org —
   so the next `sfs project init` defaults to personal scope until they pick a
   new default.
4. **Does not** touch their sessions, knowledge-base entries, or anything else
   they authored. The removal is a permissions change, not a deletion.

### Promote / demote

Toggle a member between `admin` and `member` role from the Members table. Two
guards apply:

* You can't demote yourself (use another admin or transfer the org first).
* You can't demote the last admin. The dashboard disables the button with a
  tooltip; the server also enforces the rule with a `SELECT ... FOR UPDATE`
  lock so concurrent cross-demotions can't both succeed.

The same admin→member demotion path also cancels every pending outgoing
transfer the user had initiated *from this org*. Their authorization to move
projects out is being revoked along with their admin role.

## Project transfers

Org admins can move project ownership between scopes:

* **Personal → Org** — the project owner initiates. Auto-accepts when the
  owner is a member of the destination org (one click, no second party).
* **Org → Org** — an admin of the **source** org initiates and must also be a
  member of the destination org. An admin of the destination org accepts via
  the **Transfers inbox**.
* **Org → Personal** — an admin of the source org initiates. The target user
  (the project's owner) accepts via their inbox.

The inbox lives at `/transfers` in the dashboard. It surfaces:

* **Incoming pending** — transfers waiting on you with Accept / Reject.
* **Outgoing pending** — transfers you initiated, with Cancel (recall before
  the target acts).

CLI parity: `sfs project transfer --to <dest>` initiates from the cwd repo;
`sfs project transfer --accept|--reject|--cancel <id>` acts on a specific
transfer; `sfs project transfers --direction outgoing` lists. See
`docs/cli-reference.md`.

State machine: `pending → accepted | rejected | cancelled`. Every state
transition mutates the same row in place (no separate audit table) so the
ProjectTransfer row itself IS the audit log. Atomic `UPDATE ... WHERE
state='pending'` with rowcount check prevents double-accept races.

If the project is hard-deleted between initiate and accept, the audit row
survives with `project_id = NULL` and a `project_name_snapshot` for the
display label. The transfer can no longer be accepted but the historical
record persists.

## Org settings

The **Org defaults** card on the Organization page edits the creation
defaults that new org-scoped projects inherit at create time:

* **KB retention (days)** — how long knowledge entries are kept before being
  pruned. Range 1–730.
* **KB compile word budget** — maximum words injected into a compiled context
  document. Range 100–50000.
* **KB section page limit** — maximum pages a single KB section can generate.
  Range 1–200.

Leave a field blank to fall back to the server's built-in default (180 / 2000
/ 30 respectively).

**Inheritance semantics:** these are **creation defaults**, not live
inherited values. When a teammate creates a new project under your org, the
new project's per-project columns are seeded from the current org defaults.
Changing the org defaults later does not retroactively update existing
projects — change a project's own KB knobs from the project settings page to
override.

DLP policy edit lives on its own panel (existing `/api/v1/dlp/policy`
surface). That route remains feature-gated on `dlp_secrets` (PRO+).

## Default org

If you belong to multiple orgs, set a **default org** so `sfs project init`
picks the right scope when you create a new project without passing `--org`.

```bash
# Set default
sfs config default-org org_acme_4f3d

# Show current
sfs config default-org

# Clear
sfs config default-org --clear
```

The default is stored server-side (User.default_org_id) and validated against
membership — you cannot set a default for an org you don't belong to. Where it
actually applies in v0.10.0:

* **`sfs project init`** reads `default_org_id` from `/api/v1/auth/me` when
  the user passes neither `--org` nor `--personal`; the new project picks up
  that scope. `--personal` always wins; `--org <id>` always wins.

What it deliberately does NOT do in v0.10.0:

* The daemon does **not** consult `default_org_id` during session capture or
  sync. Session→project linkage is purely git-remote-based (see "Multi-org
  session routing" below). A captured session in a workspace that has no
  matching `Project` row stays unlinked (`session.project_id = NULL`)
  regardless of your default org — run `sfs project init` first to create the
  project, then re-sync to pick up the linkage.

## Multi-org session routing

A captured session is linked to a project (and therefore to that project's
org scope) via the workspace's git remote. The server resolves the linkage at
sync time:

1. Extract `git_remote_normalized` from the session's workspace.json.
2. Look up `Project` where `git_remote_normalized` matches.
3. If found AND the caller has access (owner OR org member of
   `project.org_id`), the new `Session` row gets `project_id = project.id`.
4. Otherwise `project_id = NULL` — the session still uploads (sessions are
   user-owned) but isn't attached to a project.

Access is re-evaluated on **every sync**, not just the first one. If you lose
membership in an org after capturing org-linked sessions, the next re-sync
will clear those sessions' `project_id` to NULL. The historical link survives
in immutable session metadata where present but the live database state
reflects current access.

## Compliance

Every org-admin action that affects ownership or membership leaves a durable
audit trail:

* **Member removal / role change** — `OrgMember` row mutated; auto-transfers
  write `ProjectTransfer` rows.
* **Project transfer** — `ProjectTransfer` row created on initiate, mutated
  on accept/reject/cancel, never deleted.

Project transfer history is exposed via `GET /api/v1/transfers?state=accepted`
(or any state). The dashboard surfaces incoming and outgoing pending lists
plus an audit-row marker when the source project has been hard-deleted.

## Tier

The org admin console (members + transfers + settings) is available on the
**team** tier and above. Free and starter tiers can capture and sync personal
sessions but cannot create or join orgs.

Per-feature gating (DLP, knowledge-base compile, etc.) is independent and
documented in `docs/pricing.md`.

## See also

* `docs/cli-reference.md` — full CLI command reference, including the new
  `sfs project transfer` / `sfs config default-org` commands.
* `docs/quickstart.md` — basic capture and sync setup.
* `docs/project-context.md` — shared project context / knowledge base.
* `docs/troubleshooting.md` — diagnostic flow for common multi-org issues.
