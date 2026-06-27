"""SSO login flow integration tests — the full security test matrix.

Covers:
  - SSRF guard on issuer/jwks_uri/token_endpoint (boundary tests)
  - State/nonce replay, PKCE mismatch
  - id_token validation (alg/iss/aud/exp/nonce/email_verified/sub)
  - Account resolution + anti-takeover linking
  - JIT provisioning + invite reconciliation
  - sso_minted key minting
  - Open-redirect validation
"""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ApiKey,
    ExternalIdentity,
    OidcLoginAttempt,
    OrgDomainVerification,
    OrgIdentityProvider,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.oidc_fetch import (
    _set_test_transport,
)


# ---------------------------------------------------------------------------
# RSA key generation for test JWTs
# ---------------------------------------------------------------------------

def _generate_test_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _private_key_to_jwk(key: rsa.RSAPrivateKey) -> dict:
    """Serialize an RSA private key as a JWK (for signing test tokens)."""
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    public_key: RSAPublicKey = key.public_key()  # type: ignore[assignment]
    pub_nums = public_key.public_numbers()
    import base64
    def _b64url(x: int) -> str:
        n_bytes = (x.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(
            x.to_bytes(n_bytes, "big")
        ).rstrip(b"=").decode()
    return {
        "kty": "RSA",
        "use": "sig",
        "kid": "test-key-1",
        "alg": "RS256",
        "n": _b64url(pub_nums.n),
        "e": _b64url(pub_nums.e),
    }


def _make_jwks_response(*keys: rsa.RSAPrivateKey) -> dict:
    return {"keys": [_private_key_to_jwk(k) for k in keys]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rsa_key():
    """Generate a fresh RSA key pair per test."""
    return _generate_test_rsa_key()


@pytest.fixture
async def org(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{secrets.token_hex(8)}",
        name="Test Org Inc.",
        slug=f"test-org-{secrets.token_hex(4)}",
        tier="team",
    )
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.fixture
async def idp(db_session: AsyncSession, org: Organization) -> OrgIdentityProvider:
    idp = OrgIdentityProvider(
        id=f"oidp_{secrets.token_hex(12)}",
        org_id=org.id,
        protocol="oidc",
        display_name="Test Okta",
        issuer="https://example.okta.com",
        client_id="test-client-id",
        client_secret_ref="env:TEST_OIDC_CLIENT_SECRET",
        allowed_scopes='["openid","email","profile"]',
        enabled=True,
    )
    db_session.add(idp)
    await db_session.commit()
    await db_session.refresh(idp)
    return idp


@pytest.fixture
async def verified_domain(
    db_session: AsyncSession, org: Organization,
) -> OrgDomainVerification:
    dv = OrgDomainVerification(
        id=f"odv_{secrets.token_hex(12)}",
        org_id=org.id,
        domain="example.com",
        verification_token=secrets.token_urlsafe(16),
        status="verified",
        verified_at=datetime.now(timezone.utc),
    )
    db_session.add(dv)
    await db_session.commit()
    await db_session.refresh(dv)
    return dv


@pytest.fixture
async def existing_user(
    db_session: AsyncSession, org: Organization,
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="alice@example.com",
        display_name="Alice",
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def org_member(
    db_session: AsyncSession, org: Organization, existing_user: User,
) -> OrgMember:
    member = OrgMember(
        org_id=org.id,
        user_id=existing_user.id,
        role="member",
        joined_at=datetime.now(timezone.utc),
    )
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(member)
    return member


@pytest.fixture
async def unverified_user(
    db_session: AsyncSession, org: Organization,
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="bob@example.com",
        display_name="Bob Unverified",
        tier="free",
        email_verified=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Mock discovery + token endpoint helpers
# ---------------------------------------------------------------------------


class MockOidcTransport(httpx.AsyncHTTPTransport):
    """Transport that simulates a real OIDC IdP.

    Supports:
      - /.well-known/openid-configuration → discovery doc
      - /token → token endpoint (code exchange)
      - /keys → JWKS endpoint

    Configured per-test via the `routes` dict.
    """

    def __init__(self, routes: dict[str, dict | callable]):
        super().__init__()
        self._routes = routes
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request):
        self.requests.append(request)
        url = str(request.url).split("?")[0]  # strip query

        handler = self._routes.get(url)
        if handler is None:
            # Try prefix match
            for prefix, h in self._routes.items():
                if url.startswith(prefix):
                    handler = h
                    break

        if handler is None:
            return httpx.Response(
                404, content=b'{"error":"not_found"}', request=request
            )

        if callable(handler):
            status, body, headers = handler(request)
        else:
            status = 200
            body = json.dumps(handler).encode()
            headers = {"content-type": "application/json"}

        return httpx.Response(status, content=body, headers=headers, request=request)


# ---------------------------------------------------------------------------
# id_token validation tests (unit-level, against validate_id_token directly)
# ---------------------------------------------------------------------------


class TestIdTokenValidation:
    """Tests for oidc_token.validate_id_token — all the hardened checks."""

    def test_happy_path(self, rsa_key):
        """RS256-signed token with all valid claims."""
        from sessionfs.server.services.oidc_token import validate_id_token

        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce-value",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        result = validate_id_token(
            token, jwks=jwks,
            client_id="test-client-id",
            expected_issuer="https://example.okta.com",
            expected_nonce="test-nonce-value",
        )
        assert result["sub"] == "user-123"

    def test_alg_none_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, "secret", algorithm="HS256", headers={"kid": "test-key-1"})
        # Remove signature, set alg:none
        parts = token.split(".")
        none_token = f"{parts[0]}.{parts[1]}."
        import base64
        none_header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        none_token = f"{none_header}.{parts[1]}."

        with pytest.raises(TokenValidationError, match="alg_not_allowed"):
            validate_id_token(
                none_token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_hs256_rejected(self, rsa_key):
        """HMAC-signed token (HS256) rejected — alg-confusion defense."""
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        # Sign with a secret using HS256 — attacker tries HMAC-with-public-key
        token = jwt.encode(claims, "shared-secret", algorithm="HS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="alg_not_allowed"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_issuer_mismatch_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://evil.com",  # Not the expected issuer!
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="issuer_mismatch"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_invalid_aud_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "wrong-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="invalid_aud"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_multi_aud_without_azp_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": ["test-client-id", "other-client"],
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="invalid_azp"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_multi_aud_with_valid_azp_accepted(self, rsa_key):
        from sessionfs.server.services.oidc_token import validate_id_token
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": ["test-client-id", "other-client"],
            "azp": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        result = validate_id_token(
            token, jwks=jwks,
            client_id="test-client-id",
            expected_issuer="https://example.okta.com",
            expected_nonce="test-nonce",
        )
        assert result["azp"] == "test-client-id"

    def test_expired_token_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp()),
            "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="token_expired"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_missing_exp_rejected(self, rsa_key):
        """A token with NO exp claim must be rejected (OIDC mandates exp;
        PyJWT only verifies exp when present, so we REQUIRE it)."""
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            # exp DELIBERATELY OMITTED
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="claim_missing"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_nonce_mismatch_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "wrong-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="nonce_mismatch"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="expected-nonce",
            )

    def test_email_verified_string_rejected(self, rsa_key):
        """String 'true' (not JSON boolean) → rejected as unverified (R2 strict)."""
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": "true",  # STRING, not boolean
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="idp_email_unverified"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_email_verified_false_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": False,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="idp_email_unverified"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_missing_sub_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="sub_missing"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )

    def test_bad_signature_rejected(self, rsa_key):
        from sessionfs.server.services.oidc_token import (
            TokenValidationError,
            validate_id_token,
        )
        jwks = _make_jwks_response(rsa_key)
        other_key = _generate_test_rsa_key()
        claims = {
            "iss": "https://example.okta.com",
            "aud": "test-client-id",
            "sub": "user-123",
            "email": "alice@example.com",
            "email_verified": True,
            "nonce": "test-nonce",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="invalid_signature"):
            validate_id_token(
                token, jwks=jwks,
                client_id="test-client-id",
                expected_issuer="https://example.okta.com",
                expected_nonce="test-nonce",
            )


# ---------------------------------------------------------------------------
# Full login flow tests (database-backed)
# ---------------------------------------------------------------------------


class TestSsoLoginFlow:
    """End-to-end SSO login flow tests against the API."""

    def teardown_method(self):
        _set_test_transport(None)

    def _make_id_token(
        self,
        rsa_key,
        *,
        iss="https://example.okta.com",
        aud="test-client-id",
        sub="idp-user-123",
        email="alice@example.com",
        email_verified=True,
        nonce: str,
        iat=None,
        exp=None,
    ) -> str:
        if iat is None:
            iat = int(datetime.now(timezone.utc).timestamp())
        if exp is None:
            exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        claims = {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "nonce": nonce,
            "iat": iat,
            "exp": exp,
        }
        return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-key-1"})

    # ---- State replay rejected ----

    async def test_state_replay_rejected(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """Consuming the same state twice → second callback rejected."""
        # 1. Create an OidcLoginAttempt manually (simulating /start)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        # Set up mock OIDC transport
        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(rsa_key, nonce=nonce)

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        # Mock DNS resolution
        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            # Set env for client secret resolution
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                # First callback — should succeed
                resp1 = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code-1", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp1.status_code == 200
                body1 = resp1.json()
                assert body1["pending_confirmation"] is False
                assert body1["api_key"] is not None

                # Second callback with same state — must fail (already consumed)
                resp2 = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code-2", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp2.status_code == 400
                assert "already used" in resp2.text.lower() or "expired" in resp2.text.lower()

    # ---- State browser mismatch ----

    async def test_state_browser_mismatch_rejected(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """Cookie state != query state → rejected."""
        state_db = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state_db,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        # Cookie has different state → mismatch
        cookie_value = json.dumps({
            "state": "different-state-value",
            "nonce": nonce,
            "code_verifier": code_verifier,
            "org_idp_id": idp.id,
        })

        resp = await client.get(
            "/api/v1/auth/sso/callback",
            params={"code": "auth-code", "state": state_db},
            cookies={"sso_state": cookie_value},
        )
        assert resp.status_code == 400
        assert "state_browser_mismatch" in resp.text.lower()

    # ---- Missing cookie rejected ----

    async def test_missing_cookie_rejected(
        self, client, db_session, org, idp,
    ):
        resp = await client.get(
            "/api/v1/auth/sso/callback",
            params={"code": "auth-code", "state": "some-state"},
        )
        assert resp.status_code == 400
        assert "state_browser_mismatch" in resp.text.lower()

    # ---- PKCE mismatch rejected ----

    async def test_pkce_mismatch_rejected(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """Verifier hash doesn't match stored hash → rejected."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        real_verifier = secrets.token_urlsafe(32)
        wrong_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(real_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        # Cookie has WRONG verifier
        cookie_value = json.dumps({
            "state": state,
            "nonce": nonce,
            "code_verifier": wrong_verifier,
            "org_idp_id": idp.id,
        })

        resp = await client.get(
            "/api/v1/auth/sso/callback",
            params={"code": "auth-code", "state": state},
            cookies={"sso_state": cookie_value},
        )
        assert resp.status_code == 400
        assert "PKCE" in resp.text

    # ---- Auto-link happy path (verified email + verified user + org member) ----

    async def test_auto_link_happy_path(
        self, client, db_session, org, idp, verified_domain,
        existing_user, org_member, rsa_key,
    ):
        """Existing verified user + org member → auto-linked via verified_email_match."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="idp-alice-1", email="alice@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["pending_confirmation"] is False
                assert body["link_method"] == "verified_email_match"
                assert body["user_id"] == existing_user.id
                assert body["api_key"] is not None

                # Verify ExternalIdentity created
                identity_result = await db_session.execute(
                    select(ExternalIdentity).where(
                        ExternalIdentity.org_idp_id == idp.id,
                        ExternalIdentity.subject =="idp-alice-1",
                    )
                )
                identity = identity_result.scalar_one_or_none()
                assert identity is not None
                assert identity.link_method == "verified_email_match"

    # ---- Reject auto-link: existing user email_verified=false ----

    async def test_no_auto_link_unverified_existing_user(
        self, client, db_session, org, idp, verified_domain,
        unverified_user, rsa_key,
    ):
        """Existing user with email_verified=false → pending confirmation, not auto-link."""
        # Add unverified user to org (they must be a member)
        member = OrgMember(
            org_id=org.id,
            user_id=unverified_user.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
        db_session.add(member)
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="idp-bob-1", email="bob@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["pending_confirmation"] is True
                assert body["api_key"] is None  # No key minted for pending
                assert body["link_method"] == "explicit_confirm_required"

    # ---- Reject auto-link: existing user not org member ----

    async def test_no_auto_link_not_org_member(
        self, client, db_session, org, idp, verified_domain,
        existing_user, rsa_key,
    ):
        """Verified user but NOT an org member → pending confirmation."""
        # Don't create org member for this test
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="idp-alice-2", email=existing_user.email, nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["pending_confirmation"] is True
                assert body["link_method"] == "explicit_confirm_required"

    # ---- JIT provision ----

    async def test_jit_provision_new_user(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """No existing user → JIT provision: create User + OrgMember(member)."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="new-user-sub", email="charlie@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["pending_confirmation"] is False
                assert body["link_method"] == "jit_provision"
                assert body["email"] == "charlie@example.com"
                assert body["api_key"] is not None

                # Verify User was created with email_verified=True
                user_result = await db_session.execute(
                    select(User).where(User.email == "charlie@example.com")
                )
                new_user = user_result.scalar_one_or_none()
                assert new_user is not None
                assert new_user.email_verified is True

                # Verify OrgMember(member) — never admin
                member_result = await db_session.execute(
                    select(OrgMember).where(
                        OrgMember.org_id == org.id,
                        OrgMember.user_id == new_user.id,
                    )
                )
                member = member_result.scalar_one_or_none()
                assert member is not None
                assert member.role == "member"

    # ---- JIT with pending invite reconciliation ----

    async def test_jit_reconciles_pending_invite_admin_role(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """JIT + pending OrgInvite with admin role → member gets admin (honored)."""
        # Create a pending invite for admin role
        invite = OrgInvite(
            id=f"inv_{secrets.token_hex(12)}",
            org_id=org.id,
            email="dave@example.com",
            role="admin",
            invited_by=str(uuid.uuid4()),  # some admin user
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="dave-sub", email="dave@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["link_method"] == "jit_provision"

                # Verify OrgMember got the invited admin role
                user_result = await db_session.execute(
                    select(User).where(User.email == "dave@example.com")
                )
                new_user = user_result.scalar_one_or_none()
                member_result = await db_session.execute(
                    select(OrgMember).where(
                        OrgMember.org_id == org.id,
                        OrgMember.user_id == new_user.id,
                    )
                )
                member = member_result.scalar_one_or_none()
                assert member is not None
                assert member.role == "admin"  # honored from invite

                # Verify invite was consumed
                invite_check = await db_session.execute(
                    select(OrgInvite).where(OrgInvite.id == invite.id)
                )
                assert invite_check.scalar_one_or_none() is None

    # ---- sso_minted key ----

    async def test_sso_minted_key_created(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """Success path mints an ApiKey with sso_minted=True."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="eve-sub", email="eve@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["api_key"] is not None

                # Verify the API key has sso_minted=True
                from sessionfs.server.auth.keys import hash_api_key
                key_hash = hash_api_key(body["api_key"])
                key_result = await db_session.execute(
                    select(ApiKey).where(ApiKey.key_hash == key_hash)
                )
                api_key = key_result.scalar_one_or_none()
                assert api_key is not None
                assert api_key.sso_minted is True
                assert api_key.key_kind == "user"

    # ---- Domain not verified for org ----

    async def test_domain_not_verified_rejected(
        self, client, db_session, org, idp, rsa_key,  # no verified_domain fixture
    ):
        """JIT with unverified domain → rejected."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="outsider-sub", email="outsider@unverified.org", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 403
                assert "domain" in resp.text.lower()

    # ---- Returning identity (existing ExternalIdentity) ----

    async def test_returning_identity_logs_in(
        self, client, db_session, org, idp, verified_domain,
        existing_user, org_member, rsa_key,
    ):
        """Pre-linked ExternalIdentity → logs in with existing_link."""
        # Create an ExternalIdentity first
        eid = ExternalIdentity(
            id=f"eid_{secrets.token_hex(12)}",
            user_id=existing_user.id,
            org_idp_id=idp.id,
            provider_issuer=idp.issuer,
            subject="returning-user-sub",
            email_at_link="alice@example.com",
            link_method="verified_email_match",
            linked_at=datetime.now(timezone.utc),
            last_login_at=None,
        )
        db_session.add(eid)
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="returning-user-sub", email="alice@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["link_method"] == "existing_link"
                assert body["user_id"] == existing_user.id

    # ---- Deactivated identity rejected ----

    async def test_deactivated_identity_rejected(
        self, client, db_session, org, idp, verified_domain,
        existing_user, org_member, rsa_key,
    ):
        """Deactivated ExternalIdentity → rejected (account_deactivated)."""
        eid = ExternalIdentity(
            id=f"eid_{secrets.token_hex(12)}",
            user_id=existing_user.id,
            org_idp_id=idp.id,
            provider_issuer=idp.issuer,
            subject="deactivated-user-sub",
            email_at_link="alice@example.com",
            link_method="verified_email_match",
            linked_at=datetime.now(timezone.utc),
            deactivated_at=datetime.now(timezone.utc),  # DEACTIVATED
        )
        db_session.add(eid)
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = self._make_id_token(
            rsa_key, sub="deactivated-user-sub", email="alice@example.com", nonce=nonce,
        )

        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "access-token-xyz",
                "id_token": id_token_str,
                "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state,
                    "nonce": nonce,
                    "code_verifier": code_verifier,
                    "org_idp_id": idp.id,
                })

                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 403
                assert "deactivated" in resp.text.lower()

    # ---- Expired attempt rejected ----

    async def test_expired_attempt_rejected(
        self, client, db_session, org, idp, verified_domain, existing_user, org_member,
    ):
        """Expired OidcLoginAttempt → rejected."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)

        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id,
            org_id=org.id,
            provider_id=idp.id,
            state=state,
            nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending",
            expires_at=now - timedelta(minutes=1),  # ALREADY EXPIRED
            created_at=now - timedelta(minutes=15),
        )
        db_session.add(attempt)
        await db_session.commit()

        cookie_value = json.dumps({
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "org_idp_id": idp.id,
        })

        resp = await client.get(
            "/api/v1/auth/sso/callback",
            params={"code": "auth-code", "state": state},
            cookies={"sso_state": cookie_value},
        )
        assert resp.status_code == 400
        assert "expired" in resp.text.lower()


# ---------------------------------------------------------------------------
# Open-redirect validation tests
# ---------------------------------------------------------------------------


class TestRedirectValidation:
    """Tests for redirect_after allowlist validation."""

    async def test_relative_path_allowed(self):
        from sessionfs.server.routes.auth_sso import _validate_redirect_after
        result = _validate_redirect_after("/dashboard/sessions")
        assert result == "/dashboard/sessions"

    async def test_relative_path_must_start_with_slash(self):
        from sessionfs.server.routes.auth_sso import _validate_redirect_after
        import pytest as _pytest
        from fastapi import HTTPException
        with _pytest.raises(HTTPException):
            _validate_redirect_after("evil.js")

    async def test_absolute_url_off_allowlist_rejected(self):
        from sessionfs.server.routes.auth_sso import _validate_redirect_after
        import pytest as _pytest
        from fastapi import HTTPException
        with _pytest.raises(HTTPException):
            _validate_redirect_after("https://evil.com/phishing")

    async def test_none_redirect_allowed(self):
        from sessionfs.server.routes.auth_sso import _validate_redirect_after
        result = _validate_redirect_after(None)
        assert result is None

    async def test_protocol_relative_and_backslash_rejected(self):
        """Browser-normalized protocol-relative redirects must be rejected
        (//evil.com, /\\evil.com, /\\/evil.com) — Sentinel MED-2."""
        from sessionfs.server.routes.auth_sso import _validate_redirect_after
        import pytest as _pytest
        from fastapi import HTTPException
        for evil in ("//evil.com", "/\\evil.com", "/\\/evil.com", "\\\\evil.com"):
            with _pytest.raises(HTTPException):
                _validate_redirect_after(evil)


# ---------------------------------------------------------------------------
# sso_start route tests
# ---------------------------------------------------------------------------


class TestSsoStart:
    """Tests for POST /api/v1/auth/sso/start."""

    def teardown_method(self):
        _set_test_transport(None)

    async def test_start_returns_authorize_url(
        self, client, org, idp, verified_domain,
    ):
        """Start returns a valid authorize URL."""
        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/oauth2/authorize",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            resp = await client.post(
                "/api/v1/auth/sso/start",
                json={"org_slug": org.slug, "redirect_after": "/dashboard"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "authorize_url" in body
            assert "https://example.okta.com/oauth2/authorize" in body["authorize_url"]
            assert "response_type=code" in body["authorize_url"]
            assert "code_challenge_method=S256" in body["authorize_url"]

            # Verify cookie was set
            assert "set-cookie" in resp.headers
            assert "sso_state" in resp.headers["set-cookie"]
            assert "HttpOnly" in resp.headers["set-cookie"]

    async def test_start_nonexistent_org_404(self, client):
        resp = await client.post(
            "/api/v1/auth/sso/start",
            json={"org_slug": "nonexistent-org"},
        )
        assert resp.status_code == 404

    async def test_start_missing_params_400(self, client):
        """Neither org_slug nor org_idp_id → 400."""
        resp = await client.post(
            "/api/v1/auth/sso/start",
            json={},
        )
        assert resp.status_code == 400

    async def test_start_redirect_after_rejects_off_allowlist(
        self, client, org, idp,
    ):
        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            resp = await client.post(
                "/api/v1/auth/sso/start",
                json={
                    "org_slug": org.slug,
                    "redirect_after": "https://evil.com/phish",
                },
            )
            assert resp.status_code == 400
            assert "redirect_after" in resp.text.lower()

    # ---- JIT seat enforcement (§3.4) ----

    async def test_jit_rejected_at_seat_cap(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """JIT login is refused when the org is at its seat cap (§3.4)."""
        # Fill the org to its default seat cap (5) with real members.
        for i in range(org.seats_limit):
            u = User(
                id=str(uuid.uuid4()),
                email=f"seat{i}@example.com",
                display_name=f"Seat {i}",
                tier="free",
                email_verified=True,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(u)
            db_session.add(OrgMember(
                org_id=org.id, user_id=u.id, role="member",
                joined_at=datetime.now(timezone.utc),
            ))
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id, org_id=org.id, provider_id=idp.id,
            state=state, nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending", expires_at=now + timedelta(minutes=10), created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = jwt.encode(
            {
                "iss": "https://example.okta.com", "aud": "test-client-id",
                "sub": "overflow-sub", "email": "overflow@example.com",
                "email_verified": True, "nonce": nonce,
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            rsa_key, algorithm="RS256", headers={"kid": "test-key-1"},
        )
        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "x", "id_token": id_token_str, "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state, "nonce": nonce,
                    "code_verifier": code_verifier, "org_idp_id": idp.id,
                })
                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 403
                assert "seat_limit_reached" in resp.text
                # No user was created for the overflow login.
                overflow = (await db_session.execute(
                    select(User).where(User.email == "overflow@example.com")
                )).scalar_one_or_none()
                assert overflow is None

    # ---- Existing-but-inactive account is not JIT-duplicated ----

    async def test_existing_inactive_user_rejected(
        self, client, db_session, org, idp, verified_domain, rsa_key,
    ):
        """An existing INACTIVE account with the IdP email must be refused —
        JIT must not create a duplicate-email user."""
        inactive = User(
            id=str(uuid.uuid4()),
            email="dormant@example.com",
            display_name="Dormant",
            tier="free",
            email_verified=True,
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(inactive)
        await db_session.commit()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        attempt = OidcLoginAttempt(
            id=f"ola_{secrets.token_hex(12)}",
            org_idp_id=idp.id, org_id=org.id, provider_id=idp.id,
            state=state, nonce=nonce,
            pkce_verifier_hash=hashlib.sha256(code_verifier.encode()).hexdigest(),
            status="pending", expires_at=now + timedelta(minutes=10), created_at=now,
        )
        db_session.add(attempt)
        await db_session.commit()

        jwks = _make_jwks_response(rsa_key)
        id_token_str = jwt.encode(
            {
                "iss": "https://example.okta.com", "aud": "test-client-id",
                "sub": "dormant-sub", "email": "dormant@example.com",
                "email_verified": True, "nonce": nonce,
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            rsa_key, algorithm="RS256", headers={"kid": "test-key-1"},
        )
        transport_routes = {
            "https://example.okta.com/.well-known/openid-configuration": {
                "issuer": "https://example.okta.com",
                "authorization_endpoint": "https://example.okta.com/auth",
                "token_endpoint": "https://example.okta.com/token",
                "jwks_uri": "https://example.okta.com/keys",
            },
            "https://example.okta.com/keys": jwks,
            "https://example.okta.com/token": {
                "access_token": "x", "id_token": id_token_str, "token_type": "Bearer",
            },
        }
        _set_test_transport(MockOidcTransport(transport_routes))

        with mock.patch(
            "sessionfs.server.services.oidc_fetch.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))],
        ):
            with mock.patch.dict("os.environ", {"TEST_OIDC_CLIENT_SECRET": "test-secret"}):
                cookie_value = json.dumps({
                    "state": state, "nonce": nonce,
                    "code_verifier": code_verifier, "org_idp_id": idp.id,
                })
                resp = await client.get(
                    "/api/v1/auth/sso/callback",
                    params={"code": "auth-code", "state": state},
                    cookies={"sso_state": cookie_value},
                )
                assert resp.status_code == 403
                assert "inactive" in resp.text.lower()
                # Still exactly ONE user with this email — no JIT duplicate.
                rows = (await db_session.execute(
                    select(User).where(User.email == "dormant@example.com")
                )).scalars().all()
                assert len(rows) == 1
                assert rows[0].id == inactive.id
