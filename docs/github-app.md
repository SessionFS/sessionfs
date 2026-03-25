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

1. Visit [github.com/apps/sessionfs-ai-context](https://github.com/apps/sessionfs-ai-context)
   and click **Install**.

2. Choose which repositories the app can access (all or selected).

3. In the SessionFS dashboard under **Settings > GitHub Integration**, verify the
   connection is active and configure your preferences:
   - **Auto-comment on PRs** -- enable or disable automatic comments
   - **Include trust scores** -- show audit trust scores in the comment
   - **Include session links** -- link session titles to the SessionFS dashboard

## Permissions

The GitHub App requests minimal permissions:

| Permission | Access | Purpose |
|------------|--------|---------|
| Pull requests | Read & Write | Post and update comments |
| Metadata | Read | Identify repositories and branches |

The app does **not** request access to your code, issues, or any other repository
content.

## Privacy

- SessionFS does not read or store your source code.
- Only session metadata (title, tool, message count) appears in the PR comment.
- Full session content requires authentication on the SessionFS dashboard.
- You can disable auto-commenting at any time from the settings page.
- Uninstalling the app removes all future comment posting. Existing comments
  remain on the PR but will no longer be updated.

## Removing the app

To uninstall, go to your GitHub organization or account settings under
**Applications > Installed GitHub Apps** and click **Configure** next to
SessionFS AI Context, then **Uninstall**.
