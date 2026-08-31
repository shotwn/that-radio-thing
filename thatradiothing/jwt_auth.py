"""Issue and verify the HS256 session shared with the sibling duudey site."""

from __future__ import annotations

import time
from typing import Any

import jwt


def issue_auth_token(
    *,
    secret: str,
    issuer: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> str:
    """Issue a short, explicitly bounded shared-session JWT.

    Args:
        secret: Shared HS256 secret used by both trusted services.
        issuer: Exact issuer value accepted during verification.
        payload: Spotify identity/token claims to embed.
        ttl_seconds: Positive lifetime in seconds.

    Returns:
        Encoded JWT string suitable for the HttpOnly auth cookie.

    """

    now = int(time.time())
    claims = {
        "iss": issuer,
        "iat": now,
        "exp": now + max(1, ttl_seconds),
        **payload,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_auth_token(*, token: str, secret: str, issuer: str) -> dict[str, Any] | None:
    """Verify signature, issuer, lifetime, and required Spotify identity claims."""

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=issuer,
            options={"require": ["exp", "iat", "iss"]},
            leeway=5,
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("provider") != "spotify":
        return None
    if not isinstance(payload.get("providerUserId"), str):
        return None

    return payload
