"""Request-path safety for MCP tools.

MCP tool arguments are chosen by the model (and therefore reachable by
prompt-injected content). Any argument interpolated into an `/api/v1/...`
URL path must go through one of these helpers so it can never change which
endpoint the request — carrying the user's own API key — is sent to.
"""

from __future__ import annotations

from typing import Any

# --- Repository-identifier validation (security) -------------------------------
# Every project-scoped MCP tool interpolates a caller-supplied git remote into
# `/api/v1/projects/{...}`. `normalize_git_remote`'s fallback returns the raw
# string, so a remote such as `../admin/users` turned any of these tools into an
# authenticated proxy to a different API endpoint.
#
# Design: validate the KEY THAT IS REQUESTED, not the shape of the input. The
# server stores `normalize_git_remote(raw remote)` for each project, so the
# lookup key is computed the same way (exact parity by construction), and the
# only thing that can change which endpoint a request reaches is a segment of
# that key: a `.`/`..` segment (raw or percent-decoded) that the HTTP client
# collapses, or an encoded separator hiding inside a segment. Earlier versions
# also matched the input against a grammar of remote shapes; that grammar never
# added safety and repeatedly rejected valid remotes the server accepts.


def _strict_repository_identifier(git_remote: str) -> str:
    """Return the percent-encoded project key for a caller-supplied git remote,
    or raise ValueError if it could redirect the request.

    The key is exactly the server's `normalize_git_remote()` of the remote, so
    every remote the server can store resolves (absolute and relative paths,
    any URL scheme, scp forms with or without a user, `@`/`:`/`~`/`+` in paths,
    IPv6 hosts, trailing slashes). A remote is refused only when it carries
    whitespace, control characters, a backslash, `?` or `#`, or when a segment
    of the resulting key is `.`/`..` or decodes to a separator or control
    character. Each segment is then percent-encoded.
    """
    from urllib.parse import quote, unquote

    from sessionfs.server.github_app import normalize_git_remote

    original = (git_remote or "").strip()
    if (
        not original
        or "\\" in original
        or "?" in original
        or "#" in original
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in original)
    ):
        raise ValueError("Could not parse git remote URL.")
    normalized = normalize_git_remote(original)
    if not normalized:
        raise ValueError("Could not parse git remote URL.")
    segments = normalized.split("/")
    for segment in segments:
        decoded = unquote(segment)
        if (
            segment in {".", ".."}
            or decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            raise ValueError("Could not parse git remote URL.")
    return "/".join(quote(segment, safe="") for segment in segments)


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
