# SessionFS AI Context - GitHub App

## What is this?

When you open a pull request, SessionFS can automatically post a comment showing
the AI sessions that contributed to the code changes. This gives reviewers
visibility into the reasoning, tools, and models used during development.

The comment appears as an "AI Context" section on your PR and includes:

- **Session titles** -- what the developer was working on
- **Tool and model** -- which AI tool (Claude Code, Codex, Cursor, etc.) and model were used
- **Message count** -- how many messages were exchanged
- **Trust score** (optional) -- an automated audit score reflecting factual accuracy

The comment updates automatically when new commits are pushed to the branch.

## Why does this help?

Code review is harder when you cannot see the context behind the changes.
SessionFS bridges that gap by linking the PR to the conversation that produced it.
Reviewers can click through to the full session to understand:

- What the developer asked the AI to do
- What approaches were considered and discarded
- Which files were read, edited, or created
- Whether tool calls succeeded or failed

This is especially valuable for teams where multiple people hand off AI sessions
to continue each other's work.

## How it works

1. **During development**, the SessionFS daemon captures your AI tool sessions
   and records the git remote, branch, and commit metadata.

2. **When you push and open a PR**, GitHub sends a webhook to SessionFS.

3. **SessionFS matches** the PR's repository and branch to any sessions that were
   synced from that context.

4. **A comment is posted** (or updated) on the PR with a summary table of matched
   sessions.

No code is sent to SessionFS. Only session metadata (titles, message counts,
tool names) is used to build the comment. The full session content is only
accessible to authenticated users via the SessionFS dashboard.

## Installation

1. In the SessionFS dashboard, open **Settings > GitHub Integration** and click
   **Connect GitHub**.

2. GitHub asks you to install the app and to sign in. Choose which repositories
   the app can access (all or selected), then approve.

3. GitHub sends you back to SessionFS, which asks you to sign in to GitHub once
   more (a separate, PKCE-protected authorization; usually a single click).
   SessionFS links the installation to your account only after GitHub confirms
   that the GitHub account you signed in with can access that installation. If
   you installed the app on an organization, sign in as an owner of that
   organization: SessionFS checks your membership role live.

4. Configure your preferences:
   - **Auto-comment on PRs** -- enable or disable automatic comments
   - **Include trust scores** -- show audit trust scores in the comment
   - **Include session links** -- link session titles to the SessionFS dashboard

Already installed the app? Use **Link existing installation** instead; you only
sign in to GitHub. To install the app on another account or organization, use
**Add installation**; to change which repositories an installation covers, use
**Configure on GitHub**. If your GitHub account can link several installations,
SessionFS asks you to pick one. Each sign-in links exactly one installation.

### What a link can do

- **Any verified link** (including organization members who are not owners)
  drives PR comments for that installation.
- **Repository verification** (proving a project's repository belongs to you)
  uses the installation only when GitHub showed, within the last 7 days, that
  you are the account the app is installed on or an active owner of the
  organization. After 7 days the dashboard shows **Re-verify with GitHub**;
  until you re-verify, repositories are recorded as owner-attested. A
  repository is verified only when it is one of the repositories the
  installation was granted (being able to read a public repository is not
  enough), and only for `github.com/owner/repo` remotes; other hosts and
  nested group paths are always recorded as owner-attested.
- An organization owner who signs in can take over a link held by someone
  whose proof is older, member-level, or was never verified.

### Re-verifying an older link

Installations linked before SessionFS checked ownership with GitHub show
**Verify ownership with GitHub** in the dashboard. PR comments keep working
without it, but SessionFS does not use an unverified link to verify repository
ownership. Click the button and sign in to GitHub to restore it.

## Permissions

The GitHub App requests minimal permissions:

| Permission | Access | Purpose |
|------------|--------|---------|
| Pull requests | Read & Write | Post and update comments |
| Metadata | Read | Identify repositories and branches |
| Members (organization) | Read | Check, when you link an organization installation, that you are an active owner of that organization |

**Members: Read is required** for organization owners to get repository
verification. When it was added, every existing installation had to approve
the updated permissions (GitHub shows the request to the organization owners
under **Settings > GitHub Apps**). Until an installation approves it, the
membership check fails and SessionFS treats everyone who links it as a plain
member: PR comments work, repository verification does not.

The app does **not** request access to your code, issues, or any other repository
content.

## Privacy

- SessionFS does not read or store your source code.
- Only session metadata (title, tool, message count) appears in the PR comment.
- Full session content requires authentication on the SessionFS dashboard.
- You can disable auto-commenting at any time from the settings page.
- Uninstalling the app removes all future comment posting. Existing comments
  remain on the PR but will no longer be updated.

## App configuration (operators)

If you run your own SessionFS server with your own GitHub App, configure the
App like this (GitHub > Settings > Developer settings > GitHub Apps > your app):

| Setting | Value |
|---------|-------|
| Callback URLs | Exactly two, **in this order**: 1. `https://<api-host>/api/v1/settings/github/oauth/install-return` 2. `https://<api-host>/api/v1/settings/github/oauth/callback`. Set the second one as `SFS_GITHUB_OAUTH_CALLBACK_URL`. The order matters (see below). |
| Request user authorization (OAuth) during installation | **OFF**. It must stay off: GitHub would then issue an authorization code during installation without a PKCE challenge. |
| Setup URL | `https://<api-host>/api/v1/settings/github/oauth/install-return` |
| Redirect on update | **Off**. Changing an installation's repositories on GitHub should not send the browser to the install-return endpoint, which only accepts returns from an install started in SessionFS. |
| Organization permissions > Members | Read-only (required, see above) |
| Client secret | Generate one and store it in your secret manager; inject it as `SFS_GITHUB_APP_CLIENT_SECRET`. |

Why two endpoints, and why the order matters: `install-return` only learns
which installation was just created and never exchanges a code. SessionFS
then starts its own PKCE authorization that names `/oauth/callback`
explicitly as its `redirect_uri`, and exchanges the code with that same
`redirect_uri`. GitHub binds every code to the `redirect_uri` it was issued
for.

If "Request user authorization (OAuth) during installation" were turned on,
GitHub would ignore the Setup URL and send an authorization code WITHOUT a
PKCE challenge to the app's **first** callback URL. Keeping `install-return`
first means such a code is bound to `install-return`, which never exchanges
codes (it refuses the flow and logs the misconfiguration), and GitHub refuses
to redeem it at `/oauth/callback`. If `/oauth/callback` were listed first, a
leaked installation-time code could be redeemed there with anyone's verifier.

### Webhook retries

Each distinct signed webhook payload (identified by a hash of the signed
body alone; the event and delivery headers are not signed) is processed at
most once, and only when its body has the full shape of the event it claims
to be. If the work
fails (for example GitHub's API is briefly unavailable while posting a PR
comment), the payload is kept and retried with backoff (1, 5, 15, 60
minutes; at most 5 attempts). The server has no built-in scheduler: retries
run a couple at a time whenever a new webhook arrives, and in bulk when an
admin calls `POST /api/v1/admin/github-webhook-deliveries/sweep`. Calling the
sweep endpoint every few minutes from a scheduler is recommended; it also
republishes any PR comment left behind its newest content (for example after
a GitHub error while it was being edited).

A redelivery from GitHub's UI follows the same rules: it cannot bypass the
backoff or the attempt limit (it gets `{"status": "deferred"}`). To retry a
delivery that gave up, an admin calls
`POST /api/v1/admin/github-webhook-deliveries/<fingerprint>/retry`.

Retention: stored payloads are dropped after 30 days; the compact replay
fingerprint of every delivery is kept for 400 days, so a captured, validly
signed delivery cannot be replayed later. Signatures themselves never expire,
so **rotate the App's webhook secret at least once a year**: a rotation
invalidates every signature made with the old secret.

## Removing the app

To uninstall, go to your GitHub organization or account settings under
**Applications > Installed GitHub Apps** and click **Configure** next to
SessionFS AI Context, then **Uninstall**.
