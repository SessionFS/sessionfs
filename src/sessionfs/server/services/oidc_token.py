"""OIDC id_token validation — hardened per Sentinel HIGH 4.

§3.2 step 4 of the SSO design.  Every check in this module is binding — the
security predicates are the crux of the entire SSO feature.

Checks performed (in order):
  1. alg pinned to the asymmetric set advertised in the JWKS —
     EXPLICITLY REJECT alg:none and any HS* (HMAC-with-public-key attack)
  2. iss EXACTLY equals the expected issuer resolved from the OidcLoginAttempt
     row's OrgIdentityProvider — NEVER the token's self-asserted iss
  3. aud must contain our client_id; if multi-entry array, require azp==client_id
  4. exp not passed, iat present and not beyond allowed skew,
     nbf (if present) not beyond allowed skew
  5. nonce must equal the expected nonce from the attempt row
  6. email_verified is strict JSON boolean True (string "true" / 1 → rejected)
  7. sub is present (required for identity linking)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import jwt
from jwt import PyJWKSet

logger = logging.getLogger("sessionfs.api")

# ---------------------------------------------------------------------------
# Allowed asymmetric algorithms — anything not in this set (including
# 'none' and HS*) is REJECTED.
# ---------------------------------------------------------------------------

_ALLOWED_ALGS: frozenset[str] = frozenset({
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
})


class TokenValidationError(Exception):
    """id_token validation failed with a specific *reason* code.

    Every rejection site in this module raises TokenValidationError with a
    distinct reason string matched by tests.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


# ---------------------------------------------------------------------------
# JWKS helpers
# ---------------------------------------------------------------------------


def _parse_jwks(jwks: PyJWKSet | str | dict) -> PyJWKSet:
    """Normalise JWKS input to a PyJWKSet."""
    if isinstance(jwks, PyJWKSet):
        return jwks
    if isinstance(jwks, str):
        try:
            jwks = json.loads(jwks)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TokenValidationError(
                "jwks_parse_error", f"Could not parse JWKS JSON: {exc}"
            ) from exc
    if isinstance(jwks, dict):
        try:
            return PyJWKSet.from_dict(jwks)
        except Exception as exc:
            raise TokenValidationError(
                "jwks_parse_error", f"Could not parse JWKS dict: {exc}"
            ) from exc
    raise TokenValidationError(
        "jwks_parse_error", f"Unsupported JWKS type: {type(jwks)}"
    )


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


def _decode_verified(
    token: str,
    jwks: PyJWKSet | str | dict,
    *,
    require: tuple[str, ...] = ("exp", "iat"),
) -> dict:
    """Steps shared by every JWT this server accepts from an external
    issuer: pinned asymmetric alg (alg:none and HS* refused), kid-resolved
    JWKS key, signature, exp/iat verification and required-claim presence.
    iss/aud and issuer-specific claims are checked by the caller."""
    # ---- 1. alg check (MUST run before decode so we never verify with
    #        the wrong algorithm) -----------------------------------------
    try:
        unverified_header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise TokenValidationError(
            "token_parse_error", f"Could not parse id_token header: {exc}"
        ) from exc

    alg = unverified_header.get("alg")
    if not isinstance(alg, str) or not alg:
        raise TokenValidationError("alg_missing", "id_token header missing alg")
    if alg == "none":
        raise TokenValidationError("alg_not_allowed", "alg:none is forbidden")
    if alg.startswith("HS"):
        raise TokenValidationError(
            "alg_not_allowed", f"HMAC algorithm {alg} not allowed for id_token"
        )
    if alg not in _ALLOWED_ALGS:
        raise TokenValidationError(
            "alg_not_allowed",
            f"Algorithm {alg} not in allowed asymmetric set",
        )

    # ---- 2. Resolve the specific signing key from the JWKS via kid -----
    # PyJWT 2.13's jwt.decode() does NOT auto-resolve keys from a
    # PyJWKSet.  We must extract the matching PyJWK by kid and pass
    # that individual key.  The PyJWK object carries its own algorithm
    # binding (the JWKS "alg" field), which _verify_signature uses in
    # preference to the token header's alg — providing an additional
    # layer of alg-confusion defense.
    key_set = _parse_jwks(jwks)
    kid = unverified_header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenValidationError(
            "kid_missing", "id_token header missing kid"
        )
    try:
        signing_key = key_set[kid]
    except KeyError as exc:
        raise TokenValidationError(
            "kid_not_found",
            f"No key in JWKS for kid={kid!r}",
        ) from exc

    # ---- 3. Signature + standard claims decode -------------------------
    # iss/aud are checked manually afterwards so we can apply the
    # stricter Sentinel rules (iss from attempt row, multi-aud azp).
    try:
        claims = jwt.decode(
            token,
            key=signing_key,
            algorithms=[alg],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": False,   # manual check — multi-aud + azp
                "verify_iss": False,   # manual check — against attempt row
                # OIDC mandates exp; PyJWT only *verifies* exp if present, so
                # REQUIRE exp + iat presence — a token without exp must never
                # be treated as non-expiring.
                "require": list(require),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenValidationError("token_expired", str(exc)) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenValidationError("claim_missing", str(exc)) from exc
    except jwt.InvalidSignatureError as exc:
        raise TokenValidationError("invalid_signature", str(exc)) from exc
    except jwt.InvalidTokenError as exc:
        # Catch-all for PyJWT errors (missing claims, bad format, etc.)
        raise TokenValidationError(
            "id_token_verification_failed", str(exc)
        ) from exc
    return claims


def validate_id_token(
    id_token: str,
    jwks: PyJWKSet | str | dict,
    *,
    client_id: str,
    expected_issuer: str,
    expected_nonce: str,
    max_skew: int = 60,
) -> dict:
    """Validate an OIDC id_token against ALL security checks.

    Returns the decoded claims dict on success.
    Raises TokenValidationError with a specific ``reason`` on any failure.

    The caller MUST pass:
      - *expected_issuer* resolved from the OidcLoginAttempt →
        OrgIdentityProvider row, NEVER from the token
      - *expected_nonce* from the same attempt row
    """
    claims = _decode_verified(id_token, jwks)

    # ---- 3. iss — MUST equal expected issuer from attempt row -----------
    token_iss = claims.get("iss", "")
    if token_iss != expected_issuer:
        raise TokenValidationError(
            "issuer_mismatch",
            f"Token iss={token_iss!r} != expected issuer={expected_issuer!r}",
        )

    # ---- 4. aud — string or array; multi-aud requires azp --------------
    token_aud = claims.get("aud", "")
    if isinstance(token_aud, list):
        if client_id not in token_aud:
            raise TokenValidationError(
                "invalid_aud",
                f"client_id {client_id} not in id_token aud array {token_aud}",
            )
        if len(token_aud) > 1:
            azp = claims.get("azp", "")
            if azp != client_id:
                raise TokenValidationError(
                    "invalid_azp",
                    f"Multi-aud token requires azp={client_id}, got azp={azp!r}",
                )
    else:
        if token_aud != client_id:
            raise TokenValidationError(
                "invalid_aud",
                f"id_token aud={token_aud!r} != client_id={client_id!r}",
            )

    # ---- 5. iat — must be present, not in the future beyond skew ---------
    now_ts = datetime.now(timezone.utc).timestamp()
    iat = claims.get("iat", 0)
    if iat > now_ts + max_skew:
        raise TokenValidationError(
            "invalid_iat",
            f"id_token iat={iat} is {iat - now_ts:.0f}s in the future "
            f"(max skew {max_skew}s)",
        )

    # ---- 6. nbf — if present, must not be in the future beyond skew -----
    nbf = claims.get("nbf")
    if nbf is not None and isinstance(nbf, (int, float)) and nbf > now_ts + max_skew:
        raise TokenValidationError(
            "invalid_nbf",
            f"id_token nbf={nbf} is {nbf - now_ts:.0f}s in the future "
            f"(max skew {max_skew}s)",
        )

    # ---- 7. nonce — MUST equal the expected nonce -----------------------
    token_nonce = claims.get("nonce", "")
    if token_nonce != expected_nonce:
        raise TokenValidationError(
            "nonce_mismatch",
            f"Token nonce={token_nonce!r} != expected nonce",
        )

    # ---- 8. email_verified — strict JSON boolean True ------------------
    email_verified = claims.get("email_verified")
    if email_verified is not True:
        raise TokenValidationError(
            "idp_email_unverified",
            f"email_verified is {email_verified!r}, must be strictly True "
            f"(JSON boolean, not string)",
        )

    # ---- 9. sub — required for identity linking -------------------------
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise TokenValidationError(
            "sub_missing", "id_token missing required sub claim"
        )

    return claims


# ---------------------------------------------------------------------------
# GitHub Actions workload identity tokens (marketplace trusted publishing)
# ---------------------------------------------------------------------------

GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"

# Claims trusted publishing binds to. Numeric ids arrive as JSON strings.
_GITHUB_REQUIRED_STRING_CLAIMS = (
    "jti",
    "sub",
    "repository",
    "repository_id",
    "repository_owner",
    "repository_owner_id",
    # The workflow file the run was STARTED from (the caller). For a job in
    # a reusable workflow, job_workflow_ref names the callee instead; it is
    # optional and validated below when present.
    "workflow_ref",
    "event_name",
    "ref",
    "sha",
    "run_id",
)
_GITHUB_NUMERIC_CLAIMS = ("repository_id", "repository_owner_id", "run_id")
_MAX_CLAIM_LENGTH = 512


def validate_github_actions_token(
    token: str,
    jwks: PyJWKSet | str | dict,
    *,
    audience: str,
    max_age_seconds: int = 300,
    max_lifetime_seconds: int = 3600,
    max_skew: int = 60,
) -> dict:
    """Validate a GitHub Actions OIDC token for trusted publishing.

    Shares the signature path with id_token validation (pinned asymmetric
    algs, kid-resolved key, exp + iat required), then:

    - iss EXACTLY ``https://token.actions.githubusercontent.com`` (the issuer
      is a constant, never taken from the token);
    - aud a single value equal to *audience* (a list must be exactly one
      element, so a token minted for several relying parties is refused);
    - acceptance window: iat no older than *max_age_seconds* (+ skew) and the
      token's own lifetime no longer than *max_lifetime_seconds*;
    - the repository / caller workflow (``workflow_ref``) / event / ref /
      commit / run claims present, strings, bounded, and the numeric ids all
      digits; ``job_workflow_ref`` (the callee of a reusable workflow) is
      optional but must be well formed when present;
    - ``sha`` a full 40-hex commit.

    Returns the claims. Raises TokenValidationError with a reason code."""
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise TokenValidationError("token_parse_error", "token missing or too large")
    claims = _decode_verified(token, jwks, require=("exp", "iat", "jti"))

    if claims.get("iss") != GITHUB_ACTIONS_ISSUER:
        raise TokenValidationError("issuer_mismatch", "token was not issued by GitHub Actions")

    token_aud = claims.get("aud")
    if isinstance(token_aud, list):
        if len(token_aud) != 1:
            raise TokenValidationError("invalid_aud", "token must carry exactly one audience")
        token_aud = token_aud[0]
    if not isinstance(token_aud, str) or token_aud != audience:
        raise TokenValidationError("invalid_aud", "token audience is not this server")

    now_ts = datetime.now(timezone.utc).timestamp()
    iat = claims.get("iat")
    exp = claims.get("exp")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise TokenValidationError("claim_missing", "iat and exp must be numeric")
    if iat > now_ts + max_skew:
        raise TokenValidationError("invalid_iat", "token iat is in the future")
    if now_ts - iat > max_age_seconds + max_skew:
        raise TokenValidationError("token_too_old", "token is outside the acceptance window")
    if exp - iat > max_lifetime_seconds:
        raise TokenValidationError("token_lifetime", "token lifetime exceeds the allowed maximum")

    for name in _GITHUB_REQUIRED_STRING_CLAIMS:
        value = claims.get(name)
        if not isinstance(value, str) or not value or len(value) > _MAX_CLAIM_LENGTH:
            raise TokenValidationError("claim_missing", f"required claim {name!r} missing")
    for name in _GITHUB_NUMERIC_CLAIMS:
        if not claims[name].isascii() or not claims[name].isdigit():
            raise TokenValidationError("claim_invalid", f"claim {name!r} must be numeric")
    sha = claims["sha"]
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise TokenValidationError("claim_invalid", "claim 'sha' must be a 40-hex commit")
    for name in ("job_workflow_ref", "ref_type", "run_attempt", "runner_environment"):
        value = claims.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > _MAX_CLAIM_LENGTH):
            raise TokenValidationError("claim_invalid", f"claim {name!r} is malformed")
    environment = claims.get("environment")
    if environment is not None and (
        not isinstance(environment, str) or len(environment) > _MAX_CLAIM_LENGTH
    ):
        raise TokenValidationError("claim_invalid", "claim 'environment' is malformed")
    return claims


# ---------------------------------------------------------------------------
# GitLab CI ID tokens (marketplace trusted publishing)
# ---------------------------------------------------------------------------

# Claims trusted publishing binds to or records. GitLab sends ids and booleans
# as JSON strings. ``ref`` is the short name; ``ref_path`` the full ref.
_GITLAB_REQUIRED_STRING_CLAIMS = (
    "jti",
    "sub",
    "namespace_id",
    "namespace_path",
    "project_id",
    "project_path",
    "pipeline_id",
    "pipeline_source",
    "job_id",
    "ref",
    "ref_type",
    "ref_path",
    "ref_protected",
    "sha",
)
_GITLAB_NUMERIC_CLAIMS = ("namespace_id", "project_id", "pipeline_id", "job_id")
_GITLAB_OPTIONAL_STRING_CLAIMS = (
    "environment",
    "environment_protected",
    "deployment_tier",
    "user_id",
    "runner_environment",
    "ci_config_ref_uri",
    "ci_config_sha",
    "project_visibility",
)
_GITLAB_REF_PREFIX = {"branch": "refs/heads/", "tag": "refs/tags/"}


def validate_gitlab_ci_token(
    token: str,
    jwks: PyJWKSet | str | dict,
    *,
    issuer: str,
    audience: str,
    max_age_seconds: int = 3600,
    max_lifetime_seconds: int = 86400 + 300,
    max_skew: int = 60,
) -> dict:
    """Validate a GitLab CI ID token (``id_tokens:``) for trusted publishing.

    Same signature path as every external JWT here (pinned asymmetric algs,
    kid-resolved key, exp + iat + jti required; nbf verified when present),
    then:

    - iss EXACTLY *issuer* - the caller passes an allowlisted origin, never a
      value taken from the token alone;
    - aud a single value equal to *audience*;
    - acceptance window: GitLab mints the token when the JOB STARTS (not on
      demand like GitHub), so iat may be up to *max_age_seconds* old (the
      publish step must run within that window of job start); the token's own
      lifetime (job timeout + a few minutes) is capped at
      *max_lifetime_seconds*;
    - project / namespace / pipeline / job / ref claims present, strings,
      bounded, numeric ids all digits; ``ref_type`` branch|tag and
      ``ref_path`` exactly ``refs/heads/<ref>`` or ``refs/tags/<ref>``;
      ``ref_protected`` the string "true" or "false";
    - ``sha`` a full 40-hex commit.

    Binding (project id, ref pattern, protected ref, environment, pipeline
    source) happens against the trusted publisher row, not here.
    Returns the claims. Raises TokenValidationError with a reason code."""
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise TokenValidationError("token_parse_error", "token missing or too large")
    claims = _decode_verified(token, jwks, require=("exp", "iat", "jti"))

    if claims.get("iss") != issuer:
        raise TokenValidationError(
            "issuer_mismatch", "token was not issued by the expected GitLab instance"
        )

    token_aud = claims.get("aud")
    if isinstance(token_aud, list):
        if len(token_aud) != 1:
            raise TokenValidationError("invalid_aud", "token must carry exactly one audience")
        token_aud = token_aud[0]
    if not isinstance(token_aud, str) or token_aud != audience:
        raise TokenValidationError("invalid_aud", "token audience is not this server")

    now_ts = datetime.now(timezone.utc).timestamp()
    iat = claims.get("iat")
    exp = claims.get("exp")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise TokenValidationError("claim_missing", "iat and exp must be numeric")
    if iat > now_ts + max_skew:
        raise TokenValidationError("invalid_iat", "token iat is in the future")
    if now_ts - iat > max_age_seconds + max_skew:
        raise TokenValidationError("token_too_old", "token is outside the acceptance window")
    if exp - iat > max_lifetime_seconds:
        raise TokenValidationError("token_lifetime", "token lifetime exceeds the allowed maximum")

    for name in _GITLAB_REQUIRED_STRING_CLAIMS:
        value = claims.get(name)
        if not isinstance(value, str) or not value or len(value) > _MAX_CLAIM_LENGTH:
            raise TokenValidationError("claim_missing", f"required claim {name!r} missing")
    for name in _GITLAB_NUMERIC_CLAIMS:
        # At most 18 digits: always fits the BIGINT columns it is compared to.
        if not claims[name].isascii() or not claims[name].isdigit() or len(claims[name]) > 18:
            raise TokenValidationError("claim_invalid", f"claim {name!r} must be numeric")
    prefix = _GITLAB_REF_PREFIX.get(claims["ref_type"])
    if prefix is None:
        raise TokenValidationError("claim_invalid", "claim 'ref_type' must be branch or tag")
    if claims["ref_path"] != prefix + claims["ref"]:
        raise TokenValidationError(
            "claim_invalid", "claims 'ref', 'ref_type' and 'ref_path' disagree"
        )
    if claims["ref_protected"] not in ("true", "false"):
        raise TokenValidationError("claim_invalid", "claim 'ref_protected' must be 'true' or 'false'")
    sha = claims["sha"]
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise TokenValidationError("claim_invalid", "claim 'sha' must be a 40-hex commit")
    for name in _GITLAB_OPTIONAL_STRING_CLAIMS:
        value = claims.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > _MAX_CLAIM_LENGTH):
            raise TokenValidationError("claim_invalid", f"claim {name!r} is malformed")
    return claims
