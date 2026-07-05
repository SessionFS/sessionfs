# Operating the implementer resident in a sandbox (F8)

The **implementer resident** writes code. Unlike the reviewer resident (which only
posts verdicts), it creates and modifies files and commits them. This document is
the operator's guide to running it inside a containment boundary so it can
**propose** changes but never **dispose** of them.

## The honest boundary (read this first)

SessionFS the **server never brokers git**. It only ever sees tickets, comments,
and *diff-refs* (a branch name, a commit SHA, a changed-path list, a PR URL —
metadata, never the code). This is a hard design constraint (C7): a compromise of
the SessionFS server cannot reach your source.

The consequence, stated plainly: **"the implementer cannot push to a protected
branch or merge" is YOUR responsibility as the operator, not a guarantee the
product enforces.** The server has no way to police git operations that happen
entirely on your host. What SessionFS provides is:

- this **hardened sandbox profile** (`deploy/resident-sandbox/`), and
- these docs,

as the *mechanism*. You are responsible for deploying the resident inside the
boundary. The controls below are layered so that no single mistake is fatal, but
the **load-bearing** ones are marked.

## What the containment must achieve (design §3.7.1)

1. **Worktree isolation.** The implementer operates in a *dedicated git worktree*
   on a *resident-owned branch* `resident/<queue>/<ticket_id>` — never your
   primary working tree, never `develop`/`main`/any protected branch. File writes
   and commits are confined to that worktree + branch.
2. **Sandboxed execution.** Anything it runs (tests, builds, linters) runs in a
   least-privilege container: non-root, read-only root filesystem, all Linux
   capabilities dropped, no new privileges, and **no operator secrets** beyond a
   minimal grant you configure.
3. **Propose, never dispose (load-bearing).** The credentials available inside the
   sandbox must be unable to push to a protected branch, merge, release, or
   deploy. The human merge gate stays.

## Step 1 — Create the isolated checkout (on the host)

Never point the sandbox at your primary checkout. Create a **dedicated clone** on
the resident branch. (The design calls for a worktree; inside a container a
dedicated clone is the clean realization of the same worktree-isolation intent —
a clone has its own self-contained `.git`, so it works across the container mount
boundary, whereas a `git worktree`'s `.git` file points outside the mount.)

```bash
# Once per ticket the implementer will work:
TICKET=tk_xxxxxxxx
QUEUE=wq_xxxxxxxx
CHECKOUT="/srv/residents/${QUEUE}/${TICKET}"

git clone --single-branch --branch main <your-repo-url> "$CHECKOUT"
git -C "$CHECKOUT" checkout -b "resident/${QUEUE}/${TICKET}"

# Match the sandbox user (the image runs as fixed UID/GID 10001) so the
# container can write files + commit in the ONLY writable path it gets.
sudo chown -R 10001:10001 "$CHECKOUT"
```

The clone at `/srv/residents/<queue>/<ticket>` is the **only** path the container
gets write access to. It is isolated from your primary tree and pinned to the
resident branch; without a push credential (Step 2) it cannot reach a protected
branch.

## Step 2 — Deny protected-branch credentials (LOAD-BEARING)

Pick ONE of these, strongest first. The goal: the credential inside the sandbox
**cannot reach a protected branch**.

- **No push remote at all (strongest).** Remove the push URL from the worktree's
  remote. The reviewer is co-located and reads the branch/diff locally (C8), and
  you open the PR from the host after the resident proposes:
  ```bash
  git -C "/srv/residents/${QUEUE}/${TICKET}" remote set-url --push origin no_push
  ```
- **A push credential scoped to non-protected branches only.** A GitHub App
  installation / deploy key whose branch protection rules forbid pushes to
  `main`/`develop`/`release/*` and forbid merges. The resident can push its
  `resident/...` branch to open a PR; it cannot touch protected branches.
- **Never** mount your personal SSH key, a `GITHUB_TOKEN` with `contents:write`
  on protected branches, or any merge/admin token into the container.

The image ships a `pre-push` hook and `push.default=nothing` as **defense-in-depth
only** — they are a backstop, not a substitute for the above.

## Step 3 — Run the sandbox (hardened flags are LOAD-BEARING)

```bash
docker build -f deploy/resident-sandbox/Dockerfile.implementer \
  -t sfs-resident-implementer deploy/resident-sandbox/

docker run --rm \
  --user 10001:10001 \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=512 \
  --memory=2g --cpus=2 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --tmpfs /home/resident:rw,nosuid,size=64m \
  -v "/srv/residents/${QUEUE}/${TICKET}:/workspace/worktree:rw" \
  -v "$HOME/.sessionfs/residents/impl.toml:/home/resident/.sessionfs/residents/impl.toml:ro" \
  -v "$HOME/.sessionfs/profiles/impl-org.toml:/home/resident/.sessionfs/profiles/impl-org.toml:ro" \
  -e RESIDENT_LLM_API_KEY \
  sfs-resident-implementer --config impl --resident-id res_xxx --org-id org_xxx
```

The **auth-profile mount is required**: the resident config's `org_profile`
(here `impl-org`) names the profile that supplies the SessionFS **service key**,
and the runner resolves it from `~/.sessionfs/profiles/<name>.toml`. Because
`/home/resident` is a fresh tmpfs, you must mount that profile in (read-only) or
the resident exits before its first heartbeat. Mount the profile whose name
matches the config's `org_profile`.

### Required service-key scopes

Provision the implementer's service key with the full implementer scope set —
NOT just the write path. In particular the implementer **reads the full ticket**
(description + acceptance criteria) before writing code, so it needs
`tickets:read`; without it every directive fails closed on a 403 before the LLM
is ever called. The scopes (design §4.1):

- `work_queues:read`, `work_queues:write` — drive the heartbeat + settle
- `tickets:read`, `tickets:write` — hydrate the full ticket + post the diff-ref
- `knowledge:read`, `knowledge:write` — hydrate + write back durable learnings
- `sessions:read` — session lineage / its own checkpoints
- `agent_runs:write` — optional per-wake execution audit

The implementer's key must **not** be registered as a `trusted_reviewer` — the
server rejects binding one `service_key_id` to both roles (F4), which is what
guarantees the self-review prohibition.

Notes:
- `--read-only` + the two `--tmpfs` mounts keep everything writable except the
  worktree ephemeral to the container.
- `--cap-drop=ALL --security-opt=no-new-privileges` remove privilege-escalation
  paths.
- **Restrict network egress** to only the SessionFS API host and your LLM
  endpoint. Prefer a locked-down Docker network / firewall (this is the analog of
  the VPC egress firewall used for the cloud API); the container should not be
  able to reach arbitrary hosts.
- The **operator LLM key** is passed by env (`RESIDENT_LLM_API_KEY`) and lives
  ONLY in the container — it is never sent to SessionFS. The **SessionFS service
  key** comes from the mounted profile and is used ONLY for the SessionFS API.
  The two never cross.

## Step 4 — Propose, then a human merges

The implementer commits to `resident/<queue>/<ticket>` and transitions the ticket
to `review` with a diff-ref (branch/SHA/changed paths). Then:

1. The **independent** reviewer resident (a *different* trusted service key,
   co-located) reads the diff locally and posts a verdict via the settle-path.
   The server's self-review prohibition (F1) rejects a close where the reviewer
   identity equals the implementer identity.
2. Only after a trusted `VERIFIED-CLEAN` does the queue **item** auto-close — and
   it is stamped a server-set **`resident-reviewed, NOT human-reviewed`** marker
   so nothing looks human-approved that wasn't.
3. **A human performs the merge.** There is no automated merge in v1.

## Step 5 — The merge is not a rubber stamp (mandatory checklist)

Because a resident-reviewer `VERIFIED-CLEAN` can auto-close the queue item, the
**human merge is the sole remaining human touchpoint** for the change. It must not
be a rubber stamp — especially for high-risk classes. Before merging a
resident-authored change, confirm:

- [ ] The item carries the `resident-reviewed, NOT human-reviewed` marker — you
      are the first human to look at this code.
- [ ] The independent reviewer identity ≠ the implementer identity (no
      self-review), and the clean verdict postdates the implementer's writeback.
- [ ] You have read the actual diff locally (not just the ticket summary).
- [ ] Tests / CI pass on the branch in your environment, not only in the sandbox.
- [ ] **DB migrations / schema / infra / auth changes get extra scrutiny** — the
      class behind real prod incidents where automated review passed but
      environment-specific bugs slipped. Run migrations against a
      production-accurate database, not only SQLite.
- [ ] The change is scoped to what the ticket asked for; no unrelated edits.

## Kill switch

To stop a misbehaving resident immediately, an org owner/admin **retires** it
(`sfs admin ... ` / the residents API), which **revokes its service key** server-
side — it can no longer authenticate for anything. Pause is the reversible,
work-queue-only stop; retire is the hard stop. You can also quarantine a
suspected-poisoned implementer mind (owner/admin only).
