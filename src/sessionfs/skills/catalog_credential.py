"""Credential helper for SessionFS native catalogs (git credential protocol).

git runs ``sfs catalog credential get`` and writes ``key=value`` lines
(``protocol``, ``host``, optionally ``path``) to stdin. The helper answers
ONLY for the configured SessionFS API origin and only over https (plain
http is allowed for loopback hosts, i.e. local development and tests), so
the API key never goes to another host even if the helper were configured
globally.

``store`` and ``erase`` are ignored. That alone does not keep the key out
of other credential stores: git asks EVERY configured helper to ``store``
a credential that worked. ``sfs catalog add`` therefore configures
``credential.<origin>.helper`` as an empty value (resetting the helper list
for the SessionFS origin, so a system or global osxkeychain / manager /
store helper is not consulted) followed by this helper.

Known gap (§7.7 asks for ``marketplace:read``-only keys): personal API keys
are always full-scope (``POST /api/v1/auth/me/api-keys`` mints
``scopes=["*"]`` only), so the helper hands git the active profile's key.
It never leaves this host's git process for any other origin.
"""

from __future__ import annotations

from urllib.parse import urlsplit

USERNAME = "sessionfs"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def parse_request(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def origin_of(api_url: str) -> tuple[str, str]:
    """(scheme, host[:port]) of the API URL, lower-cased."""
    parts = urlsplit(api_url)
    host = (parts.hostname or "").lower()
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return parts.scheme.lower(), host


def credential_response(request: dict[str, str], *, api_url: str, api_key: str) -> str:
    """The helper's stdout for a ``get`` request ('' = no answer)."""
    if not api_key or not api_url:
        return ""
    scheme, host = origin_of(api_url)
    protocol = request.get("protocol", "").lower()
    requested_host = request.get("host", "").lower()
    if protocol != scheme or requested_host != host:
        return ""
    bare_host = requested_host.rsplit(":", 1)[0] if not requested_host.startswith("[") else requested_host
    if protocol != "https" and not (protocol == "http" and bare_host in _LOOPBACK):
        return ""
    path = request.get("path")
    if path is not None and not path.lstrip("/").startswith("api/v1/git/"):
        return ""
    if any(char in api_key for char in "\r\n\0"):
        return ""
    return f"username={USERNAME}\npassword={api_key}\n"
