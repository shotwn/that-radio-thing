"""Load and validate ThatRadioThing configuration from environment variables.

Configuration errors fail at process startup with the name of the offending
variable. Silent coercion is dangerous for authentication, persistence, and
time-zone scheduling, so explicitly supplied invalid values are never replaced
with unrelated defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a runtime dependency
    load_dotenv = None

if load_dotenv is not None:
    # Loading here keeps ``python main.py`` convenient while real deployments
    # can continue injecting the exact same variables through Docker/CI.
    load_dotenv()


def _split_csv(value: str | None, default: list[str] | None = None) -> list[str]:
    """Split a comma-separated variable, trimming and removing empty entries."""

    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool(value: str | None, default: bool, *, name: str) -> bool:
    """Parse a conventional boolean string or raise for an ambiguous value."""

    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true/false, yes/no, on/off, or 1/0")


def _parse_samesite(value: str | None, default: str = "Lax") -> str:
    """Normalize a cookie SameSite value to the spelling aiohttp expects."""

    normalized = (value or default).strip().lower()
    choices = {"lax": "Lax", "strict": "Strict", "none": "None"}
    try:
        return choices[normalized]
    except KeyError as exc:
        raise RuntimeError("AUTH_COOKIE_SAMESITE must be Lax, Strict, or None") from exc


def _parse_int(
    value: str | None,
    default: int,
    *,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse an integer variable and enforce optional inclusive boundaries."""

    if value is None or not value.strip():
        parsed = default
    else:
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}")
    return parsed


def _require(name: str) -> str:
    """Return one required non-empty environment variable."""

    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "Copy .env.example to .env and fill it in, or export the value in your shell."
        )
    return value.strip()


def _parse_secret(name: str) -> str:
    """Require enough entropy capacity for an HS256 shared secret."""

    secret = _require(name)
    if len(secret.encode()) < 32:
        raise RuntimeError(f"{name} must be at least 32 bytes long")
    return secret


def _parse_playlists(value: str | None) -> list[dict[str, Any]]:
    """Decode the legacy bootstrap playlist array with structural validation."""

    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TRT_PLAYLISTS must be valid JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise RuntimeError("TRT_PLAYLISTS must be a JSON array of objects")
    return parsed


def _parse_origins(value: str | None, *, name: str) -> list[str]:
    """Validate browser origins and return canonical values without trailing slash."""

    origins: list[str] = []
    for raw in _split_csv(value):
        origin = raw.rstrip("/")
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"{name} contains an invalid origin: {raw}")
        origins.append(origin)
    return origins


def _public_url(value: str | None) -> str:
    """Validate and normalize the public base URL used for OAuth callbacks."""

    url = (value or "http://localhost:33408/").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("TRT_URL must be an absolute HTTP(S) URL")
    return url if url.endswith("/") else f"{url}/"


def _timezone(value: str | None) -> str:
    """Validate the default IANA timezone at startup."""

    name = (value or "Europe/Istanbul").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(f"TRT_SCHEDULE_TIMEZONE is not an IANA timezone: {name}") from exc
    return name


def build_config() -> dict[str, Any]:
    """Build the complete validated application configuration dictionary."""

    cookie_secure = _parse_bool(
        os.getenv("AUTH_COOKIE_SECURE"),
        True,
        name="AUTH_COOKIE_SECURE",
    )
    cookie_samesite = _parse_samesite(os.getenv("AUTH_COOKIE_SAMESITE"))
    if cookie_samesite == "None" and not cookie_secure:
        raise RuntimeError("AUTH_COOKIE_SAMESITE=None requires AUTH_COOKIE_SECURE=true")

    return {
        "url": _public_url(os.getenv("TRT_URL")),
        "port": _parse_int(
            os.getenv("TRT_PORT"),
            33408,
            name="TRT_PORT",
            minimum=1,
            maximum=65535,
        ),
        "client_id": _require("SPOTIFY_CLIENT_ID"),
        "client_secret": _require("SPOTIFY_CLIENT_SECRET"),
        "masters_list": _split_csv(os.getenv("TRT_MASTERS_LIST")),
        # Administrators are deliberately separate from live-DJ permission.
        "admin_ids": _split_csv(os.getenv("TRT_ADMIN_IDS")),
        "scopes": _split_csv(
            os.getenv("TRT_SCOPES"),
            default=["user-modify-playback-state", "user-read-playback-state"],
        ),
        "realtime_tolerance_ms": _parse_int(
            os.getenv("TRT_REALTIME_TOLERANCE_MS"),
            1000,
            name="TRT_REALTIME_TOLERANCE_MS",
            minimum=0,
        ),
        "playlists": _parse_playlists(os.getenv("TRT_PLAYLISTS")),
        "database_path": os.getenv(
            "TRT_DATABASE_PATH",
            str(Path(os.getenv("TRT_DATA_DIR", "./data")) / "thatradiothing.sqlite3"),
        ),
        "schedule_timezone": _timezone(os.getenv("TRT_SCHEDULE_TIMEZONE")),
        "catalog_refresh_interval_seconds": _parse_int(
            os.getenv("TRT_CATALOG_REFRESH_INTERVAL_SECONDS"),
            21_600,
            name="TRT_CATALOG_REFRESH_INTERVAL_SECONDS",
            minimum=300,
            maximum=604_800,
        ),
        "catalog_refresh_token": (os.getenv("TRT_CATALOG_REFRESH_TOKEN") or "").strip() or None,
        "auth_cookie_name": os.getenv("AUTH_COOKIE_NAME", "duudey_auth"),
        "logged_in_cookie_name": os.getenv(
            "LOGGED_IN_COOKIE_NAME",
            "duudey_logged_in",
        ),
        "auth_cookie_domain": (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None,
        "auth_cookie_secure": cookie_secure,
        "auth_cookie_samesite": cookie_samesite,
        "auth_cookie_max_age_seconds": _parse_int(
            os.getenv("AUTH_TOKEN_TTL_SECONDS"),
            2_592_000,
            name="AUTH_TOKEN_TTL_SECONDS",
            minimum=60,
        ),
        "auth_jwt_issuer": os.getenv("AUTH_JWT_ISSUER", "duudey-auth"),
        "auth_shared_jwt_secret": _parse_secret("AUTH_SHARED_JWT_SECRET"),
        "cors_allowed_origins": _parse_origins(
            os.getenv("CORS_ALLOWED_ORIGINS"),
            name="CORS_ALLOWED_ORIGINS",
        ),
        "cors_allow_credentials": _parse_bool(
            os.getenv("CORS_ALLOW_CREDENTIALS"),
            True,
            name="CORS_ALLOW_CREDENTIALS",
        ),
        "admin_origins": _parse_origins(
            os.getenv("TRT_ADMIN_ORIGINS"),
            name="TRT_ADMIN_ORIGINS",
        ),
    }


CONFIG = build_config()
