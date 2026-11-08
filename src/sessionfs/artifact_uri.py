"""Artifact URI validation shared by the server and the ``sfs artifact`` CLI
(GitHub #122). Shape checks only: SessionFS never fetches an artifact URI.
Server-independent on purpose so the CLI can refuse a bad URI before
sending anything."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_URI = 2048
ALLOWED_SCHEMES = ("s3", "gs", "mlflow", "models", "runs", "hf", "https", "vertex")
# Query keys that carry a credential (pre-signed URLs, SAS tokens, API keys).
_CRED_EXACT = frozenset(
    {
        "sig", "signature", "token", "key", "sv", "se", "sp", "st", "spr", "srt",
        "skoid", "sktid", "access_token", "id_token", "api_key", "apikey",
        "password", "passwd", "secret", "credential", "credentials", "auth",
        "authorization", "policy", "expires", "awsaccesskeyid",
    }
)
_CRED_PREFIXES = ("x-amz-", "x-goog-", "x-ms-")
_CRED_SUBSTRINGS = ("token", "secret", "password", "signature", "credential", "apikey", "api_key")
_HF_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,500}$")


class ArtifactUriError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_uri(raw: Any) -> str:
    """Return the URI with its scheme lower-cased, or raise ``ArtifactUriError``.

    Shape checks only; the URI is never fetched. Refuses userinfo and query
    keys that look like credentials (``uri_has_credentials``) so a pre-signed
    URL can never be stored.
    """
    if not isinstance(raw, str):
        raise ArtifactUriError("invalid_uri", "external_uri must be a string")
    uri = raw.strip()
    if not uri:
        raise ArtifactUriError("invalid_uri", "external_uri is required")
    if len(uri) > MAX_URI:
        raise ArtifactUriError("invalid_uri", f"external_uri must be at most {MAX_URI} characters")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in uri):
        raise ArtifactUriError("invalid_uri", "external_uri must not contain spaces or control characters")
    scheme, sep, rest = uri.partition(":")
    scheme = scheme.lower()
    if not sep or scheme not in ALLOWED_SCHEMES:
        raise ArtifactUriError(
            "invalid_uri_scheme",
            f"external_uri scheme must be one of: {', '.join(ALLOWED_SCHEMES)}",
        )
    uri = f"{scheme}:{rest}"
    if not rest.strip("/"):
        raise ArtifactUriError("invalid_uri", "external_uri has nothing after the scheme")
    try:
        parts = urlsplit(uri)
    except ValueError as exc:
        raise ArtifactUriError("invalid_uri", f"external_uri could not be parsed: {exc}") from exc
    if "@" in (parts.netloc or ""):
        raise ArtifactUriError(
            "uri_has_credentials", "external_uri must not contain a user name or password"
        )
    if scheme in ("https", "s3", "gs", "hf") and not parts.netloc:
        raise ArtifactUriError("invalid_uri", f"a {scheme}:// URI needs a host or bucket")
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.strip().lower()
        if (
            k in _CRED_EXACT
            or k.startswith(_CRED_PREFIXES)
            or any(s in k for s in _CRED_SUBSTRINGS)
        ):
            raise ArtifactUriError(
                "uri_has_credentials",
                "external_uri carries a credential in its query string; store the plain location instead",
            )
    return uri


def link_url(uri: str) -> str | None:
    """A clickable link for the dashboard: ``https`` as is and ``hf://`` mapped
    to huggingface.co. Everything else is copy-only (None)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme == "https" and parts.netloc:
        return uri
    if parts.scheme == "hf":
        path = (parts.netloc + parts.path).strip("/")
        if path and _HF_PATH_RE.fullmatch(path) and ".." not in path:
            return f"https://huggingface.co/{path}"
    return None
