"""TokenVerifier contract tests.

Generates an RSA keypair once per session, mints tokens with the
private key, verifies them with the matching `StaticKeyTokenVerifier`.
No Auth0 traffic. Covers:

- Valid token → claims extracted.
- Wrong audience / wrong issuer → AuthenticationError.
- Expired token → AuthenticationError.
- Signature wrong key → AuthenticationError.
- Missing tenant claim → AuthorizationError (→ HTTP 403).
- Non-UUID tenant claim → AuthorizationError.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shotcut.auth.tokens import (
    AuthenticationError,
    AuthorizationError,
    StaticKeyTokenVerifier,
    sign_test_token,
)


TENANT_CLAIM = "https://shotcut.lumenstech.com/tenant_id"
ISSUER = "https://test.auth0.com/"
AUDIENCE = "https://api.shotcut.test"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """RSA keypair — generated once per module so tests share the cost."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def verifier(keypair: tuple[str, str]) -> StaticKeyTokenVerifier:
    _priv, public = keypair
    return StaticKeyTokenVerifier(
        public_key=public,
        issuer=ISSUER,
        audience=AUDIENCE,
        tenant_claim=TENANT_CLAIM,
    )


async def test_valid_token_yields_claims(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    private, _ = keypair
    tenant_id = uuid.uuid4()
    token = sign_test_token(
        private_key=private,
        issuer=ISSUER,
        audience=AUDIENCE,
        subject="auth0|user-123",
        tenant_id=tenant_id,
        tenant_claim=TENANT_CLAIM,
    )
    claims = await verifier.verify(token)
    assert claims.sub == "auth0|user-123"
    assert claims.tenant_id == tenant_id


async def test_wrong_audience_is_authentication_error(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    private, _ = keypair
    token = sign_test_token(
        private_key=private,
        issuer=ISSUER,
        audience="https://wrong-audience",
        subject="u",
        tenant_id=uuid.uuid4(),
        tenant_claim=TENANT_CLAIM,
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


async def test_wrong_issuer_is_authentication_error(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    private, _ = keypair
    token = sign_test_token(
        private_key=private,
        issuer="https://imposter/",
        audience=AUDIENCE,
        subject="u",
        tenant_id=uuid.uuid4(),
        tenant_claim=TENANT_CLAIM,
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


async def test_expired_token_is_authentication_error(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    private, _ = keypair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u",
            "iat": now - 7200,
            "exp": now - 3600,  # expired an hour ago
            TENANT_CLAIM: str(uuid.uuid4()),
        },
        private,
        algorithm="RS256",
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


async def test_wrong_signing_key_is_authentication_error(
    verifier: StaticKeyTokenVerifier,
) -> None:
    """A token signed by a different key than the verifier trusts → 401."""
    rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue_pem = rogue_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    token = sign_test_token(
        private_key=rogue_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        subject="u",
        tenant_id=uuid.uuid4(),
        tenant_claim=TENANT_CLAIM,
    )
    with pytest.raises(AuthenticationError):
        await verifier.verify(token)


async def test_missing_tenant_claim_is_authorization_error(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    """Valid signature + missing tenant claim → 403 (not 401).

    The JWT is cryptographically fine, so the *authentication* side
    succeeded. What's missing is a required authorization attribute.
    """
    private, _ = keypair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-no-tenant",
            "iat": now,
            "exp": now + 3600,
            # no TENANT_CLAIM
        },
        private,
        algorithm="RS256",
    )
    with pytest.raises(AuthorizationError):
        await verifier.verify(token)


async def test_non_uuid_tenant_claim_is_authorization_error(
    verifier: StaticKeyTokenVerifier, keypair: tuple[str, str]
) -> None:
    private, _ = keypair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u",
            "iat": now,
            "exp": now + 3600,
            TENANT_CLAIM: "not-a-uuid",
        },
        private,
        algorithm="RS256",
    )
    with pytest.raises(AuthorizationError):
        await verifier.verify(token)
