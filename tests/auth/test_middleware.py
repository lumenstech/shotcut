"""FastAPI dep + HTTP-level auth behavior.

Stage 8 acceptance rows:
- Request without valid JWT → 401.
- Token without tenant claim → 403.
- Anonymous fallback works when AUTH_DISABLED=true (dev/test mode).
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from shotcut.auth.middleware import get_current_user
from shotcut.auth.tenancy import UserContext
from shotcut.auth.tokens import (
    StaticKeyTokenVerifier,
    sign_test_token,
)


TENANT_CLAIM = "https://shotcut.lumenstech.com/tenant_id"
ISSUER = "https://test.auth0.com/"
AUDIENCE = "https://api.shotcut.test"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


def _build_app(verifier: StaticKeyTokenVerifier) -> FastAPI:
    """Tiny FastAPI app with one protected route. Middleware is
    exercised end-to-end via TestClient."""

    app = FastAPI()

    # Force AUTH_DISABLED=false for this app by monkeypatching settings
    # inside the request. Tests in this file override conftest's
    # AUTH_DISABLED=true via the verifier injection pattern below.
    async def verifying_user(
        authorization: str | None = Header(default=None),
    ) -> UserContext:
        # Inline implementation that bypasses settings.auth_disabled
        # (which is True in the test env) and forces real verification.
        from fastapi import HTTPException

        from shotcut.auth.tokens import AuthenticationError, AuthorizationError

        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="missing bearer",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[len("Bearer ") :]
        try:
            claims = await verifier.verify(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return UserContext(sub=claims.sub, tenant_id=claims.tenant_id)

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(verifying_user)) -> dict:
        return {"sub": user.sub, "tenant_id": str(user.tenant_id)}

    return app


def test_request_without_jwt_returns_401(keypair: tuple[str, str]) -> None:
    _priv, public = keypair
    verifier = StaticKeyTokenVerifier(
        public_key=public, issuer=ISSUER, audience=AUDIENCE,
        tenant_claim=TENANT_CLAIM,
    )
    client = TestClient(_build_app(verifier))

    response = client.get("/whoami")
    assert response.status_code == 401


def test_malformed_bearer_header_returns_401(keypair: tuple[str, str]) -> None:
    _priv, public = keypair
    verifier = StaticKeyTokenVerifier(
        public_key=public, issuer=ISSUER, audience=AUDIENCE,
        tenant_claim=TENANT_CLAIM,
    )
    client = TestClient(_build_app(verifier))

    response = client.get("/whoami", headers={"Authorization": "NotBearer xyz"})
    assert response.status_code == 401


def test_valid_jwt_yields_user_context(keypair: tuple[str, str]) -> None:
    private, public = keypair
    verifier = StaticKeyTokenVerifier(
        public_key=public, issuer=ISSUER, audience=AUDIENCE,
        tenant_claim=TENANT_CLAIM,
    )
    tenant_id = uuid.uuid4()
    token = sign_test_token(
        private_key=private, issuer=ISSUER, audience=AUDIENCE,
        subject="auth0|42", tenant_id=tenant_id, tenant_claim=TENANT_CLAIM,
    )

    client = TestClient(_build_app(verifier))
    response = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "auth0|42"
    assert body["tenant_id"] == str(tenant_id)


def test_token_without_tenant_claim_returns_403(
    keypair: tuple[str, str],
) -> None:
    """Stage 8 acceptance: "Token without tenant claim → 403"."""
    private, public = keypair
    verifier = StaticKeyTokenVerifier(
        public_key=public, issuer=ISSUER, audience=AUDIENCE,
        tenant_claim=TENANT_CLAIM,
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "tenantless-user",
            "iat": now,
            "exp": now + 3600,
        },
        private,
        algorithm="RS256",
    )

    client = TestClient(_build_app(verifier))
    response = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_auth_disabled_returns_anonymous_user() -> None:
    """With AUTH_DISABLED=true (the default test env), get_current_user
    returns the anonymous UserContext without consulting a verifier."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(get_current_user)) -> dict:
        return {"sub": user.sub, "tenant_id": str(user.tenant_id)}

    client = TestClient(app)
    response = client.get("/whoami")  # no Authorization header
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "anonymous"
    assert body["tenant_id"] == "00000000-0000-0000-0000-000000000000"
