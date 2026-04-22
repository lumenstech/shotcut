"""FastAPI dependencies for auth.

Single entry point for routes: `Depends(get_current_user)` attaches a
`UserContext` to the handler. Routes that need to inject a stub identity
for tests (TestClient) override `get_current_user` via
`app.dependency_overrides`; this is why the dep is a plain module-level
function and not a method on some application class.

The `get_verifier` dep constructs the process-wide `TokenVerifier`
from config on first call and caches it. Tests override it to inject a
`StaticKeyTokenVerifier` pointed at a test keypair.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Header, HTTPException, status

from shotcut.auth.tenancy import UserContext
from shotcut.auth.tokens import (
    AuthenticationError,
    AuthorizationError,
    JWKSTokenVerifier,
    TokenVerifier,
)
from shotcut.config import settings


@lru_cache(maxsize=1)
def get_verifier() -> TokenVerifier:
    """Construct the JWKS-backed verifier from settings.

    If `settings.auth_disabled` is True, the app short-circuits in
    `get_current_user` and never calls this — the absence of issuer /
    audience / jwks_url in dev configs therefore doesn't blow up.
    """
    return JWKSTokenVerifier(
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
        jwks_url=settings.auth_jwks_url,
        tenant_claim=settings.auth_tenant_claim,
    )


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> UserContext:
    """Resolve the request's bearer token to a `UserContext`.

    Behavior:
    - `AUTH_DISABLED=true` → returns `UserContext.anonymous()` without
      touching the verifier. Dev only.
    - Missing or non-Bearer Authorization header → 401.
    - Signature / iss / aud / exp / malformed token → 401.
    - Valid token missing tenant claim → 403.
    - Otherwise → `UserContext(sub, tenant_id)`.

    The verifier is resolved lazily inside this function (not via
    `Depends(get_verifier)` on the signature) so AUTH_DISABLED mode
    never tries to construct a JWKS client. Tests that want to exercise
    real verification override `get_verifier` via
    `app.dependency_overrides` and monkeypatch this function to call
    the override.
    """
    if settings.auth_disabled:
        return UserContext.anonymous()

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer ") :]

    verifier = get_verifier()
    try:
        claims = await verifier.verify(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    return UserContext(sub=claims.sub, tenant_id=claims.tenant_id)
