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
    # ---- 1. alg check (MUST run before decode so we never verify with
    #        the wrong algorithm) -----------------------------------------
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except Exception as exc:
        raise TokenValidationError(
            "token_parse_error", f"Could not parse id_token header: {exc}"
        ) from exc

    alg = unverified_header.get("alg")
    if not alg:
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
    if not kid:
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
            id_token,
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
                "require": ["exp", "iat"],
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
