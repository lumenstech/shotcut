# 0005 — Auth + Multi-Tenancy

**Status:** Accepted
**Date:** 2026-04-22
**Context:** Stage 8 of BUILD_PLAN.md — production auth and tenant isolation.

## Decisions

### Token verification is a pluggable abstraction, not a hard-coded Auth0 call

`TokenVerifier` is an ABC. Production uses
`JWKSTokenVerifier(issuer, audience, jwks_url)` — resolves Auth0's
public keys, RS256-verifies the signature, validates `iss`/`aud`/`exp`.
Tests inject `StaticKeyTokenVerifier(public_key, issuer, audience)`
paired with a `sign_test_token()` helper — the test mints a valid JWT
with a private key it owns, the verifier validates against the
corresponding public key, no Auth0 traffic.

This is the same shape we used for `EdgarClient` in Stage 7: the wire
call has a well-defined contract; tests substitute at that seam.

### Tenant identity lives on the JWT, not in a sidecar database

The JWT carries a custom claim under the configured namespace (default
`https://shotcut.lumenstech.com/tenant_id`). The middleware extracts
it once per request, attaches it to `UserContext`, and every layer
downstream (routes, orchestrator, audit.record) threads it explicitly.

No tenant lookup table. If Auth0 says you're in tenant X, you're in
tenant X. That keeps the auth critical path to one JWKS fetch +
signature verification, and makes misrouting impossible without
forging a token.

### Defense in depth: application-level filter + Postgres RLS

Tenant isolation is enforced at two layers:

1. **Application layer (always on).** Every query that touches a
   tenant-scoped table filters on `tenant_id = user.tenant_id`. This
   is what SQLite-backed tests verify.
2. **Postgres RLS (production only).** Migration 0006 enables row-
   level security on `sessions`, `actions`, `session_branches`, and
   `financial_facts`. A per-connection `SET LOCAL app.tenant_id = ...`
   makes `USING (tenant_id = current_setting('app.tenant_id')::uuid)`
   filter automatically. If the application layer ever has a bug that
   forgets a filter, RLS catches it.

SQLite has no RLS; the migration no-ops the policy creation on non-pg
dialects so tests run clean. The application-level tests cover the
"tenant A can't read tenant B" acceptance criterion for both backends.

### Virus scanning is pluggable, with a deterministic test backend

`VirusScanner` is an ABC. Three implementations:

- `StubScanner` — legacy, always clean. Default when scanning isn't
  configured, for dev environments that don't want a clamd dependency.
- `PatternScanner` — scans for a configured set of byte-patterns
  including the EICAR antivirus test signature. Used in CI / tests
  (EICAR is the industry-standard test string; real scanners always
  flag it).
- `ClamdScanner` — talks to a real `clamd` daemon via its TCP
  socket. Production. Added as optional dep.

The upload endpoint was already wired to `scan_bytes()` in Stage 2;
Stage 8 swaps that to a `VirusScanner` instance resolved from config.

### user_sub in the audit trail

Every `audit.record()` call threads the JWT `sub` claim onto the
`user_sub` column (already added in the Stage 3 consolidated schema).
That field was nullable and dormant until now; Stage 8 activates it
without a migration.

### Out of scope for Stage 8

- **Encryption at rest.** The storage module already supports a swap
  to S3; SSE-KMS is a deployment-time choice, not a code concern. The
  local-filesystem backend stays plaintext for dev.
- **Auth0-specific admin features** (role assignment, SSO, user
  management). We validate tokens, we don't issue them. Auth0
  management API access is not required for Stage 8.
- **Rate limiting per tenant.** Useful but orthogonal. Can be added
  later as middleware without touching the auth contract.
