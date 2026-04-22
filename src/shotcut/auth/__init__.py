"""Stage 8 auth + tenancy package.

Re-exports for callers that don't need to reach into submodules.
"""
from shotcut.auth.middleware import get_current_user, get_verifier
from shotcut.auth.tenancy import UserContext, scope_to_tenant
from shotcut.auth.tokens import (
    AuthenticationError,
    AuthorizationError,
    JWKSTokenVerifier,
    StaticKeyTokenVerifier,
    TokenVerifier,
)

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "JWKSTokenVerifier",
    "StaticKeyTokenVerifier",
    "TokenVerifier",
    "UserContext",
    "get_current_user",
    "get_verifier",
    "scope_to_tenant",
]
