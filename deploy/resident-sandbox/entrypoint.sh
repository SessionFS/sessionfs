#!/bin/sh
# Entrypoint for the implementer-resident sandbox (design §3.7.1, F8).
#
# Verifies the containment invariants that CAN be checked locally, then execs
# the resident runner. These checks are defense-in-depth; the load-bearing
# controls are the container flags (--read-only --cap-drop=ALL
# --security-opt=no-new-privileges, restricted egress) and the operator NOT
# providing protected-branch credentials — see docs/operations/resident-sandbox.md.
set -eu

WORKTREE="${RESIDENT_WORKTREE:-/workspace/worktree}"

fail() { echo "resident-sandbox: $1" >&2; exit 1; }

# 1. Never run as root.
[ "$(id -u)" != "0" ] || fail "must not run as root (use USER resident / --user 10001)."

# 2. A git worktree must be mounted.
[ -d "$WORKTREE" ] || fail "worktree not mounted at $WORKTREE (mount the operator-created git worktree there)."
cd "$WORKTREE"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "$WORKTREE is not a git worktree."

# 3. Must NOT be sitting on a protected branch — the implementer only ever
#    operates on its own resident/<queue>/<ticket> branch.
current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
case "$current_branch" in
	main|master|develop|prod|production|release/*)
		fail "refusing to run on protected branch '$current_branch' — check out a resident/<queue>/<ticket> branch first."
		;;
esac

# 4. Warn (not fail) if a remote with a push URL is configured — the operator
#    should provide read-only remotes or a credential scoped to non-protected
#    branches only. We cannot verify the credential's scope from here.
if git remote -v 2>/dev/null | grep -q '(push)'; then
	echo "resident-sandbox: note — a push remote is configured. Ensure its" >&2
	echo "  credential CANNOT reach protected branches (F8 is an operator" >&2
	echo "  responsibility; the pre-push hook is only a backstop)." >&2
fi

# 5. This is the IMPLEMENTER sandbox. The implement-mode runner ships in R3;
#    until it does, `sfs resident run` only handles the reviewer loop and would
#    fail every implement directive. Refuse to start rather than churn, so the
#    profile is honest about its dependency.
if ! sfs resident run --help 2>&1 | grep -qi 'implement'; then
	fail "the implementer runner (R3) is not available in this sfs build. This sandbox is the deployment TARGET for it; do not point it at an implement_until_done queue with a reviewer-only build."
fi

echo "resident-sandbox: worktree=$WORKTREE branch=$current_branch user=$(id -un) — starting resident."

# Exec the resident runner in implement mode. Config (queue/profile/resident_id/
# LLM) comes from the environment / mounted config per the runbook.
exec sfs resident run "$@"
