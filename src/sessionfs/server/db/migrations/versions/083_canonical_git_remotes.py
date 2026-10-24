"""Rewrite stored git remotes to the canonical form (GitHub #88).

Revision ID: 083
Revises: 082

normalize_git_remote now maps every URL form of a repository to one value
(ssh://, git://, git+ssh://, scp remotes with any user or none, uppercase
schemes, trailing slashes, a trailing .git on bare paths). Before, those
forms were stored verbatim, so the same repository could become two
projects. This revision rewrites the values already stored:

- sessions.git_remote_normalized: every non-canonical value, except values
  a kept project row still holds (below).
- projects.git_remote_normalized and project_repos.git_remote_normalized
  (each globally unique): rewritten only when the canonical value is free.
  A row that would collide (another project already holds it, or the same
  project has a second repo row with it) keeps its old value and is printed
  here; a collision across projects is a real duplicate for an operator to
  merge with `sfs project merge` / `sfs admin override project-merge`. The
  kept project stays reachable by id and by its exact old remote
  (resolve_project_by_remote tries the exact value first), and sessions
  carrying that old value are not rewritten, so it keeps them.

Values beginning with 'merged:' or 'reclaimed:' are pre-073 placeholders,
not remotes, and are left alone.

The normalizer is copied below on purpose: a migration must keep doing what
it did when it was written, whatever the application code later becomes.

Downgrade is a no-op: the original spellings are not kept, and the
canonical values remain valid remotes.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "083"
down_revision = "082"
branch_labels = None
depends_on = None

_URL_REMOTE = re.compile(
    r"^(https?|ssh|git|git\+ssh|ssh\+git)://(?:([^@/]*)@)?[^/]+/(.+)$",
    re.IGNORECASE,
)
_SCP_REMOTE = re.compile(r"^(?:([^@/:]+)@)?([^/:]{2,}):(?!//)(.+)$")
_PLACEHOLDER_PREFIXES = ("merged:", "reclaimed:")


def _canonical(value: str) -> str:
    url = value.strip()
    m = _URL_REMOTE.match(url)
    if m:
        scheme, user, path = m.group(1).lower(), m.group(2), m.group(3)
        # ~user on a forge over https (sourcehut) is a namespace; over ssh
        # without the git user it is someone's home directory.
        if path.startswith("~") and user != "git" and not scheme.startswith("http"):
            path = url
    else:
        m = _SCP_REMOTE.match(url)
        if m is None:
            path = url
        else:
            user, path = m.group(1), m.group(3)
            if user != "git" and path.startswith(("/", "~")):
                path = url
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.lower()


def _changes(rows) -> list[tuple[object, str, str]]:
    out = []
    for row_id, value in rows:
        if not value or value.lower().startswith(_PLACEHOLDER_PREFIXES):
            continue
        new = _canonical(value)
        if new and new != value:
            out.append((row_id, value, new))
    return out


def upgrade() -> None:
    conn = op.get_bind()

    repo_rows = conn.execute(
        sa.text("SELECT id, project_id, git_remote_normalized FROM project_repos")
    ).fetchall()
    legacy_rows = conn.execute(
        sa.text(
            "SELECT id, git_remote_normalized FROM projects "
            "WHERE git_remote_normalized IS NOT NULL"
        )
    ).fetchall()
    # Each column is unique on its own: value -> the project holding it.
    repo_holder = {value: project_id for _, project_id, value in repo_rows}
    legacy_holder = {value: project_id for project_id, value in legacy_rows}
    kept: set[str] = set()  # non-canonical values left in place

    project_of_repo = {row_id: project_id for row_id, project_id, _ in repo_rows}
    for row_id, old, new in _changes((r[0], r[2]) for r in repo_rows):
        project_id = project_of_repo[row_id]
        clash = repo_holder.get(new)
        if clash is None and legacy_holder.get(new, project_id) != project_id:
            clash = legacy_holder[new]
        if clash is not None:
            # Another repo row (this project's or another's) already has it.
            hint = "duplicate repo row, unlink it" if clash == project_id else "merge them"
            print(f"083: kept project_repos remote {old!r} on project {project_id}: "
                  f"{new!r} already belongs to project {clash} ({hint})")
            kept.add(old)
            continue
        conn.execute(
            sa.text("UPDATE project_repos SET git_remote_normalized = :new WHERE id = :id"),
            {"new": new, "id": row_id},
        )
        del repo_holder[old]
        repo_holder[new] = project_id
    for project_id, old, new in _changes(legacy_rows):
        clash = legacy_holder.get(new)
        if clash is None and repo_holder.get(new, project_id) != project_id:
            clash = repo_holder[new]
        if clash is not None:
            print(f"083: kept projects remote {old!r} on project {project_id}: "
                  f"{new!r} already belongs to project {clash} (merge them)")
            kept.add(old)
            continue
        conn.execute(
            sa.text("UPDATE projects SET git_remote_normalized = :new WHERE id = :id"),
            {"new": new, "id": project_id},
        )
        del legacy_holder[old]
        legacy_holder[new] = project_id

    # Sessions: rewrite every non-canonical value except those a kept project
    # row still holds, so a duplicate left for merging keeps its sessions.
    # Only values that can differ from their canonical form are read.
    session_rows = conn.execute(
        sa.text(
            "SELECT id, git_remote_normalized FROM sessions "
            "WHERE git_remote_normalized LIKE '%:%' "
            "OR git_remote_normalized LIKE '%/' "
            "OR LOWER(git_remote_normalized) LIKE '%.git'"
        )
    ).fetchall()
    updates = [
        {"new": new, "id": row_id}
        for row_id, old, new in _changes(session_rows)
        if old not in kept
    ]
    if updates:
        conn.execute(
            sa.text("UPDATE sessions SET git_remote_normalized = :new WHERE id = :id"),
            updates,
        )
        print(f"083: rewrote {len(updates)} session remotes")


def downgrade() -> None:
    pass
