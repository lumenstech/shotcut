"""JWT verification — pluggable backends so tests don't hit Auth0.

Two concrete verifiers:

- `JWKSTokenVerifier` fetches the IdP's public keys over HTTPS, caches
  them, and validates RS256 signatures. Production.
- `StaticKeyTokenVerifier` validates against a caller-supplied public
  key. Tests mint a token with the matching private key via
  `sign_test_token()`.

Both raise `AuthenticationError` (→ HTTP 401) on signature/issuer/aud/exp
failures and `AuthorizationError` (→ HTTP 403) when the token is
signed and valid but missing the tenant claim. The two-error split
matters because Auth0 refuses to mint a token without the tenant
claim for a misconfigured user, but a malicious client could forge one
with a wrong key; we want different HTTP semantics for each.
"""
from __future__ import annotations

import abc
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt


class AuthenticationError(Exception):
    """Token is missing, malformed, or fails signature / iss / aud / exp checks.

    HTTP maps to 401.
    """


class AuthorizationError(Exception):
    """Token is valid but lacks the required tenant claim.

    HTTP maps to 403.
    """


@dataclass(frozen=True)
class _Claims:
    """Claims we specifically care about. Everything else is ignored."""

    sub: str
    tenant_id: uuid.UUID


class TokenVerifier(abc.ABC):
    """Contract: verify a bearer token's bytes, return `_Claims`.

    Implementations raise `AuthenticationError` on crypto or standard-
    claim failures, `AuthorizationError` on missing tenant claim.
    """

    def __init__(self, *, tenant_claim: str) -> None:
        self._tenant_claim = tenant_claim

    @abc.abstractmethod
    async def verify(self, token: str) -> _Claims:
        ...

    def _extract_claims(self, payload: dict[str, Any]) -> _Claims:
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise AuthenticationError("token missing 'sub' claim")
        tenant_raw = payload.get(self._tenant_claim)
        if tenant_raw is None:
            raise AuthorizationError(
                f"token missing tenant claim {self._tenant_claim!r}"
            )
        try:
            tenant_id = uuid.UUID(str(tenant_raw))
        except (ValueError, TypeError) as exc:
            raise AuthorizationError(
                f"tenant claim {tenant_raw!r} is not a valid UUID"
            ) from exc
        return _Claims(sub=sub, tenant_id=tenant_id)


class StaticKeyTokenVerifier(TokenVerifier):
    """Verifies tokens against a single static public key.

    Exclusively used by tests. Production uses `JWKSTokenVerifier`.
    """

    def __init__(
        self,
        *,
        public_key: str,
        issuer: str,
        audience: str,
        tenant_claim: str,
        algorithm: str = "RS256",
    ) -> None:
        super().__init__(tenant_claim=tenant_claim)
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience
        self._algorithm = algorithm

    async def verify(self, token: str) -> _Claims:
        try:
            payload = jwt.decode(
                token,
                self._public_key,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"token verification failed: {exc}") from exc
        return self._extract_claims(payload)


class JWKSTokenVerifier(TokenVerifier):
    """Fetches JWKS from the IdP and verifies RS256 signatures.

    JWKS is cached for `jwks_cache_ttl` seconds so we don't refetch per
    request. Cache invalidation happens implicitly on expiry; a new
    `kid` in a token triggers a refetch even if the cache is warm.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenant_claim: str,
        algorithm: str = "RS256",
        jwks_cache_ttl: float = 300.0,
    ) -> None:
        super().__init__(tenant_claim=tenant_claim)
        if not (issuer and audience and jwks_url):
            raise ValueError(
                "JWKSTokenVerifier requires issuer + audience + jwks_url"
            )
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._algorithm = algorithm
        self._jwks_client = jwt.PyJWKClient(
            jwks_url, cache_keys=True, lifespan=int(jwks_cache_ttl)
        )
        self._lock = threading.Lock()
        self._last_refresh: float = 0.0
        self._cache_ttl = jwks_cache_ttl

    async def verify(self, token: str) -> _Claims:
        try:
            signing_key = self._get_signing_key(token)
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"token verification failed: {exc}") from exc
        return self._extract_claims(payload)

    def _get_signing_key(self, token: str) -> str:
        # PyJWKClient handles cache + refresh internally; we wrap only
        # to ensure the lookup surfaces as AuthenticationError for
        # unknown `kid`s rather than a raw PyJWKClientError.
        try:
            key = self._jwks_client.get_signing_key_from_jwt(token)
            self._last_refresh = time.monotonic()
            # `key.key` is PyJWK-typed Any; the signing-key bytes are the
            # value jwt.decode() accepts, so assert-then-return keeps
            # mypy's `-> str` annotation honest at the boundary.
            return str(key.key)
        except jwt.PyJWKClientError as exc:
            raise AuthenticationError(f"JWKS lookup failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Test helpers (imported by tests, not routed through __init__)
# ---------------------------------------------------------------------------


def sign_test_token(
    *,
    private_key: str,
    issuer: str,
    audience: str,
    subject: str,
    tenant_id: uuid.UUID,
    tenant_claim: str,
    expires_in: float = 3600.0,
    algorithm: str = "RS256",
) -> str:
    """Mint a JWT for tests. Matching `StaticKeyTokenVerifier`'s contract."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": now,
        "exp": now + int(expires_in),
        tenant_claim: str(tenant_id),
    }
    return jwt.encode(payload, private_key, algorithm=algorithm)
