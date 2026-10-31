# GitHub merge check

SessionFS can post a **"SessionFS review loop"** check run on a pull request
so a branch-protection rule can block merging until the ticket's review loop
has produced a trusted clean verdict for the exact head commit. Readiness is
computed with the same function the ticket's readiness endpoint uses, so the
check run can never disagree with what the dashboard shows.

This is a visibility signal, not a merge gate of its own: even a `success`
check run only means "ready for a human to merge". SessionFS never merges,
squashes, or pushes on your behalf.

## What it does

For a pull request whose repository is reachable through a linked
`GitHubInstallation`, SessionFS maintains **one** check run named
"SessionFS review loop" on the PR's head commit:

- **`success`** when the head commit is ready to merge.
- **`action_required`** when there is a trusted strict clean verdict but it is
  not yet ready (for example the verdict is bound to an older commit).
- **`neutral`** when there is no trusted strict clean verdict yet.

Readiness is strict. For the check run to be green, all of these must hold for
that exact commit:

- a trusted **VERIFIED-CLEAN** verdict exists in the review loop;
- the verdict is bound to the **same commit** the check run is on;
- the reviewer is **not the implementer** (a service key counts as the user who
  minted it, so a service-key reviewer and a user-only implementer record for
  the same person are the same identity);
- there are **no open harmful findings**.

When the check run is not green, its summary lists the reasons (for example
"trusted clean verdict is not bound to the requested sha"). The summary is
built only from fixed boilerplate and those reasons — it never contains email
addresses, dollar figures, or comment text.

A **new head commit gets its own check run**. The run is looked up by
`(repository, head_sha, name)` on GitHub rather than stored locally, so pushing
a new commit leaves the old run exactly as it was; an earlier commit's run is
never flipped green after the fact.

## Setup

1. **Install the GitHub App** (see
   [github-app.md](../github-app.md)) with these permissions:

   | Permission | Access | Purpose |
   |------------|--------|---------|
   | Checks | Read & Write | Create and update check runs |
   | Pull requests | Read | Find the PR and read its head commit |
   | Contents | Read | Verify repository access |
   | Metadata | Read | Identify repositories and branches |

   And subscribe to these webhook events:

   | Event | Purpose |
   |-------|---------|
   | Pull request | Re-check on opened / synchronize and record PR metadata |
   | Release | Record published releases |

   The webhook also powers PR comments (Pull requests: Read & Write for that);
   the merge check itself only needs Pull requests: Read.

2. **Accept the updated permissions.** The Checks permission is newer than the
   original comment-only install. GitHub shows the request to the organization
   owners under **Settings > GitHub Apps**; until an installation approves it,
   the app cannot create check runs and SessionFS falls back to a comment (see
   below). On a personal account you approve the update on the app's page.

3. **Require the check** in the repository's branch protection rule (GitHub →
   repo → **Settings → Branches → Add branch protection rule**): tick
   **Require status checks to pass before merging**, search for
   **"SessionFS review loop"**, and add it. The check must be required for it to
   block a merge; unrequired it is still visible on the PR.

No other configuration is needed: SessionFS re-evaluates the check
automatically on every `pull_request` `opened` / `synchronize` event and after
a verdict is recorded, and it can be triggered on demand from the API.

## Degraded comment mode

If the installation has not been granted **Checks: Read & Write** (the
check-run API answers `403`), SessionFS degrades to the existing PR-comment
machinery and posts the same summary as a comment instead of a check run. The
comment cannot block a merge, so this mode keeps visibility but not the branch
protection gate. Approve the updated permission to move back to check runs.

## API

Recompute and post the check run on demand (an editor, or a service key with
`loops:write` on the project):

```
POST /api/v1/projects/{project_id}/tickets/{ticket_id}/merge-check
```

The response reports what happened:

```json
{"repo": "acme/repo", "head_sha": "0123…4567", "ready": false, "posted": "check_run"}
```

`posted` is `check_run`, `comment`, or `skipped`; `skipped` carries a `reason`
(for example "no GitHub App installation for the repository owner" or
"repository not granted to the installation"). The endpoint never raises a
GitHub error to the caller — upstream failures are logged without tokens and
reported as a skipped result.

## What it does not do

- It never sends email or charges money.
- It never merges, closes, or otherwise mutates the PR.
- It does not read or store source code; only the readiness reasons are sent in
  the check-run summary.
- A human still has to click merge — including on a green check run.
