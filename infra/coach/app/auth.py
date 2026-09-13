"""Shared-secret join guard for the live call surface.

Same model as healthguard's PATIENT_API_TOKEN: a deployment credential, not a
per-user identity. There is no Keycloak/session layer on this overlay. Anyone
who holds COACH_JOIN_SECRET can mint a token for a call_id; the call_id is the
meeting capability behind that secret.

Kept free of FastAPI so infra/coach/run_tests.py can cover it with stdlib only.
"""

from __future__ import annotations

import hmac


def bearer_token(authorization: str | None) -> str:
    """Extract the token from an `Authorization: Bearer …` header."""
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def join_secret_ok(provided: str | None, expected: str) -> bool:
    """Constant-time compare. Missing either side is a deny."""
    if not expected or not provided:
        return False
    left = provided.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
