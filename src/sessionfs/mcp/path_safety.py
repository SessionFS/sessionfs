"""Request-path safety for MCP tools.

MCP tool arguments are chosen by the model (and therefore reachable by
prompt-injected content). Any argument interpolated into an `/api/v1/...`
URL path must go through one of these helpers so it can never change which
endpoint the request — carrying the user's own API key — is sent to.
"""

from __future__ import annotations

import re
from typing import Any

# --- Repository-identifier validation (security) -------------------------------
# Every project-scoped MCP tool interpolates a caller-supplied git remote into
# `/api/v1/projects/{...}`. `normalize_git_remote`'s fallback returns the raw
# string, so a remote such as `../admin/users` turned any of these tools into an
# authenticated proxy to a different API endpoint. Validate and percent-encode.
# Any URL scheme git accepts (https, ssh, git, git+ssh, ...), case-insensitive.
# Non-http(s) schemes fall through `normalize_git_remote` verbatim, so the
# normalized output is re-checked below as well.
_REMOTE_URL_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://(?P<host>[^/?#\s]+)/(?P<path>[^?#]+?)/?$"
)
# Hostless local-file remotes (`file:///srv/repos/foo.git`) are valid git and
# are stored verbatim by the server; only the file scheme may omit the host.
_REMOTE_FILE_RE = re.compile(r"^[Ff][Ii][Ll][Ee]://(?P<host>[^/?#\s]*)/(?P<path>[^?#]+?)/?$")
_REMOTE_SCP_RE = re.compile(r"^[^@\s/:]+@(?P<host>[^:\s/]+):(?P<path>[^?#]+?)/?$")
# User-less scp syntax (`github.com:owner/repo.git`) is valid git and is stored
# verbatim by the server: host has no `/`, path has no `:`.
_REMOTE_SCP_NOUSER_RE = re.compile(r"^(?P<host>[^@\s/:]+):(?P<path>[^?#:@\s]+?)/?$")
_REMOTE_BARE_RE = re.compile(r"^(?P<path>[^?#:@\s]+?)/?$")


def _strict_repository_identifier(git_remote: str) -> str:
    """Validate a caller-supplied git remote and return the normalized
    repository path used in `/api/v1/projects/{...}`, each segment
    percent-encoded.

    The check is REJECT-ONLY: it never narrows the syntax the server's
    `normalize_git_remote` accepts (`+`, `~`, percent-encoded characters and
    IPv6 hosts all pass). A remote is refused only for traversal or injection —
    an empty, `.` or `..` segment (also after percent-decoding), `?`, `#`,
    whitespace, control characters or backslashes.
    """
    from urllib.parse import quote, unquote

    # Surrounding whitespace is never part of a remote. A trailing "/" IS kept for
    # the lookup: the server stores normalize_git_remote(raw), which preserves it
    # ("https://h/org/repo/" -> "org/repo/"), and the key must match exactly.
    original = (git_remote or "").strip()
    value = original.rstrip("/")
    if (
        not value
        or "\\" in value
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Could not parse git remote URL.")
    match = (
        _REMOTE_URL_RE.match(value)
        or _REMOTE_FILE_RE.match(value)
        or _REMOTE_SCP_RE.match(value)
        or _REMOTE_SCP_NOUSER_RE.match(value)
        or _REMOTE_BARE_RE.match(value)
    )
    if match is None:
        raise ValueError("Could not parse git remote URL.")
    path = match.group("path")
    if path.endswith(".git"):
        path = path[:-4]
    # Absolute-path remotes are valid git (`git@host:/srv/git/project.git`, a
    # local `/srv/git/project.git`): allow ONE leading separator. Interior empty
    # segments (`a//b`, `//srv`) and dot segments are still rejected below.
    if path.startswith("/"):
        path = path[1:]
    for segment in path.split("/"):
        decoded = unquote(segment)
        if (
            not segment
            or segment in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))
        ):
            raise ValueError("Could not parse git remote URL.")
    # The lookup key must be EXACTLY what the server stored for the project, so
    # after strict validation reuse the server's own normalization.
    from sessionfs.server.github_app import normalize_git_remote

    normalized = normalize_git_remote(original)
    if not normalized or any(ord(char) < 33 or ord(char) == 127 for char in normalized):
        raise ValueError("Could not parse git remote URL.")
    # `normalize_git_remote` returns non-http(s) URLs verbatim, host included,
    # so re-check the OUTPUT: no `.`/`..` segment (raw or percent-decoded) may
    # reach the request path, e.g. `ssh://../admin`.
    for segment in normalized.split("/"):
        if unquote(segment) in {".", ".."} or segment in {".", ".."}:
            raise ValueError("Could not parse git remote URL.")
    return "/".join(quote(segment, safe="") for segment in normalized.split("/"))


def _path_segment(value: Any, *, allow_slash: bool = False) -> str:
    """Validate and percent-encode a caller-supplied identifier (ticket id,
    persona name, wiki slug, handoff id, ...) before it is interpolated into
    an `/api/v1/...` URL path.

    Same threat as `_strict_repository_identifier`: MCP tool arguments are
    chosen by the model, so a value such as `../../../admin/users` (or one
    carrying `?`/`#`) would otherwise be collapsed by the HTTP client into a
    request — with the user's API key — against a different endpoint. Dot
    segments (also percent-decoded), empty segments, backslashes and control
    characters are rejected; everything else is percent-encoded so `/`, `?`
    and `#` can never change the request target. `allow_slash` is only for
    wiki slugs, whose server route is `{slug:path}`.
    """
    from urllib.parse import quote, unquote

    text = "" if value is None else str(value)
    if (
        not text
        or "\\" in text
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise ValueError("Invalid identifier.")
    segments = text.split("/") if allow_slash else [text]
    for segment in segments:
        decoded = unquote(segment)
        if (
            not segment
            or segment in {".", ".."}
            or decoded.strip() in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
        ):
            raise ValueError("Invalid identifier.")
    return "/".join(quote(segment, safe="") for segment in segments)
