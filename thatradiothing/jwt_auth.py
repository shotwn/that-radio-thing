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
    now = int(time.time())
    claims = {
        "iss": issuer,
        "iat": now,
        "exp": now + max(1, ttl_seconds),
        **payload,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_auth_token(*, token: str, secret: str, issuer: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], issuer=issuer)
    except jwt.PyJWTError:
        return None

    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("provider"), str):
        return None
    if not isinstance(payload.get("providerUserId"), str):
        return None

    return payload
