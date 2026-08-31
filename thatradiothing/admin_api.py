"""Versioned radio administration HTTP API and static admin entrypoint."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web
from logzero import logger

from thatradiothing.db import VersionConflict, playlist_id_from_uri
from thatradiothing.jwt_auth import verify_auth_token
from thatradiothing.recurrence import (
    RecurrenceError,
    build_rruleset,
    is_original_occurrence_start,
    iso_utc,
    occurrence_from_start,
    occurrences_between,
    parse_local,
    parse_utc,
    truncate_rrule,
    validate_series,
)
from thatradiothing.spotify_catalog import CatalogError

MAX_REQUEST_BYTES = 1024 * 1024
MAX_SCHEDULE_RANGE = timedelta(days=366)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SERIES_FIELDS = {
    "title",
    "playlist_id",
    "dtstart_local",
    "timezone",
    "duration_seconds",
    "rrule",
    "priority",
    "transition_policy",
    "enabled",
    "rdates",
    "exdates",
}
REQUEST_ID_KEY = web.RequestKey("admin_request_id", str)


class AdminAPI:
    """HTTP adapter for the standalone admin client and future consumers."""

    def __init__(self, web_server):
        """Bind API handlers to the existing aiohttp application services."""

        self.web_server = web_server
        self.trt = web_server.trt
        self._recent_mutation_ids: dict[str, float] = {}
        self._mutation_id_ttl = 120.0

    def register(self) -> None:
        """Register static control-room and versioned JSON routes exactly once."""

        app = self.web_server
        if hasattr(app, "middlewares"):
            app.middlewares.append(self._error_middleware())
        app.router.add_get("/admin", self.admin_page)
        app.router.add_get("/admin/", self.admin_page)
        app.router.add_get("/admin/{tail:.*}", self.admin_asset)

        prefix = "/api/admin/v1"
        app.router.add_get(f"{prefix}/session", self.session)
        app.router.add_get(f"{prefix}/status", self.status)
        app.router.add_get(f"{prefix}/audit", self.audit)
        app.router.add_get(f"{prefix}/settings", self.settings)
        app.router.add_patch(f"{prefix}/settings", self.update_settings)
        app.router.add_get(f"{prefix}/playlists", self.playlists)
        app.router.add_post(f"{prefix}/playlists", self.create_playlist)
        app.router.add_get(f"{prefix}/playlists/{{playlist_id}}", self.playlist)
        app.router.add_patch(f"{prefix}/playlists/{{playlist_id}}", self.update_playlist)
        app.router.add_delete(f"{prefix}/playlists/{{playlist_id}}", self.delete_playlist)
        app.router.add_post(f"{prefix}/playlists/{{playlist_id}}/refresh", self.refresh_playlist)
        app.router.add_get(f"{prefix}/schedule", self.schedule)
        app.router.add_post(f"{prefix}/schedule/preview", self.preview_schedule)
        app.router.add_post(f"{prefix}/schedule/series", self.create_series)
        app.router.add_get(f"{prefix}/schedule/series/{{series_id}}", self.get_series)
        app.router.add_patch(f"{prefix}/schedule/series/{{series_id}}", self.update_series)
        app.router.add_delete(f"{prefix}/schedule/series/{{series_id}}", self.delete_series)
        app.router.add_post(
            f"{prefix}/schedule/series/{{series_id}}/exceptions", self.add_exception
        )
        app.router.add_post(f"{prefix}/schedule/series/{{series_id}}/split", self.split_series)
        app.router.add_post(f"{prefix}/playback/skip-current", self.skip_current)
        app.router.add_post(f"{prefix}/playback/skip-next", self.skip_next)
        app.router.add_post(f"{prefix}/playback/reload-playlist", self.reload_playlist)

    def _error_middleware(self):
        """Keep admin API failures machine-readable and request-correlated."""

        @web.middleware
        async def middleware(request: web.Request, handler):
            """Translate admin failures and attach one stable request ID."""

            request[REQUEST_ID_KEY] = self._new_request_id(request)
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                if not request.path.startswith("/api/admin/"):
                    raise
                code = {
                    400: "bad_request",
                    401: "unauthorized",
                    403: "forbidden",
                    404: "not_found",
                    409: "conflict",
                    413: "request_too_large",
                    415: "unsupported_media_type",
                    422: "invalid_request",
                    428: "precondition_required",
                }.get(exc.status, "admin_api_error")
                response = self._error_response(
                    code,
                    exc.reason or "Admin API request failed",
                    exc.status,
                    self._request_id(request),
                )
            except Exception:
                if not request.path.startswith("/api/admin/"):
                    raise
                logger.exception(
                    "Unhandled admin API error (request_id=%s)",
                    self._request_id(request),
                )
                response = self._error_response(
                    "internal_error",
                    "The admin request could not be completed",
                    500,
                    self._request_id(request),
                )
            response.headers["X-Request-ID"] = self._request_id(request)
            return response

        return middleware

    async def admin_page(self, request: web.Request) -> web.StreamResponse:
        """Serve the authenticated standalone admin application shell."""

        _identity, error = self._identity(request)
        if error:
            if error.status == 401:
                raise web.HTTPTemporaryRedirect("/auth?returnTo=/admin")
            raise error
        response = await self._static_admin_file("index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    async def admin_asset(self, request: web.Request) -> web.StreamResponse:
        """Serve an authenticated built asset or the SPA shell fallback."""

        _identity, error = self._identity(request)
        if error:
            raise error
        tail = request.match_info.get("tail") or "index.html"
        if ".." in Path(tail).parts:
            raise web.HTTPNotFound()
        return await self._static_admin_file(tail)

    async def _static_admin_file(self, relative: str) -> web.StreamResponse:
        """Resolve one file below ``static/admin`` without path traversal."""

        root = Path("static/admin").resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise web.HTTPNotFound()
        if target.is_file():
            return self._decorate_admin_response(web.FileResponse(target))

        # Extensionless paths are client-side routes; real missing assets stay 404.
        spa_shell = root / "index.html"
        if not Path(relative).suffix and spa_shell.is_file():
            return self._decorate_admin_response(web.FileResponse(spa_shell))

        if relative == "index.html":
            fallback = Path("static/admin.htm")
            if fallback.is_file():
                response = web.FileResponse(fallback)
                return self._decorate_admin_response(response)
        raise web.HTTPNotFound(text="Admin UI asset was not found")

    def _decorate_admin_response(self, response: web.StreamResponse) -> web.StreamResponse:
        """Apply a restrictive CSP while allowing configured first-party frames."""

        ancestors = [
            "'self'",
            *[origin for origin in getattr(self.trt, "admin_origins", []) if origin],
        ]
        connect_sources = ["'self'", *self.trt.admin_origins]
        response.headers["Content-Security-Policy"] = "; ".join(
            (
                "default-src 'self'",
                "base-uri 'self'",
                "object-src 'none'",
                "form-action 'self'",
                "img-src 'self' data: https://i.scdn.co https://mosaic.scdn.co",
                "style-src 'self' 'unsafe-inline'",
                f"connect-src {' '.join(connect_sources)}",
                f"frame-ancestors {' '.join(ancestors)}",
            )
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _identity(
        self, request: web.Request, mutation: bool = False
    ) -> tuple[dict[str, Any] | None, web.HTTPException | None]:
        """Verify the shared Spotify JWT, allowlist, origin, and CSRF policy."""

        token = self.web_server._extract_auth_token(request)
        if not token:
            return None, web.HTTPUnauthorized(reason="Not logged in")
        claims = verify_auth_token(
            token=token, secret=self.trt.auth_shared_jwt_secret, issuer=self.trt.auth_jwt_issuer
        )
        if not claims:
            return None, web.HTTPUnauthorized(reason="Invalid session")
        if claims.get("provider") != "spotify":
            return None, web.HTTPForbidden(reason="A Spotify session is required")
        spotify_id = claims.get("providerUserId")
        if spotify_id not in set(self.trt.admin_ids):
            return None, web.HTTPForbidden(reason="Radio administrator permission required")
        if mutation:
            origin_error = self._check_origin(request)
            if origin_error:
                return None, origin_error
            csrf_error = self._check_csrf(request)
            if csrf_error:
                return None, csrf_error
        return {
            "providerUserId": str(spotify_id),
            "displayName": claims.get("displayName"),
        }, None

    def _check_origin(self, request: web.Request) -> web.HTTPException | None:
        """Allow mutations only from configured origins or non-browser bearer calls."""

        origin = (request.headers.get("Origin") or "").rstrip("/")
        if not origin:
            # Non-browser bearer callers are safe from cookie CSRF. Cookie
            # callers must provide a browser Origin for mutating requests.
            if request.headers.get("Authorization", "").lower().startswith("bearer "):
                return None
            return web.HTTPForbidden(reason="Origin header required")
        allowed = set(self.trt.admin_origins or self.trt.cors_allowed_origins)
        public_url = urlsplit(str(getattr(self.trt, "url", "")))
        if public_url.scheme and public_url.netloc:
            allowed.add(f"{public_url.scheme}://{public_url.netloc}")
        if origin not in {item.rstrip("/") for item in allowed}:
            return web.HTTPForbidden(reason="Origin is not allowed")
        return None

    def _check_csrf(self, request: web.Request) -> web.HTTPException | None:
        """Enforce double-submit CSRF for cookie-authenticated mutations."""

        if request.headers.get("Authorization", "").lower().startswith("bearer "):
            return None
        cookie = request.cookies.get("trt_csrf")
        header = request.headers.get("X-CSRF-Token")
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            return web.HTTPForbidden(reason="CSRF token is missing or invalid")
        return None

    async def _json(self, request: web.Request) -> dict[str, Any]:
        """Read a size-bounded JSON object request body."""

        if request.content_length and request.content_length > MAX_REQUEST_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                MAX_REQUEST_BYTES,
                request.content_length,
            )
        if request.content_type != "application/json" and not request.content_type.endswith(
            "+json"
        ):
            raise web.HTTPUnsupportedMediaType(reason="Content-Type must be application/json")
        try:
            value = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(reason="Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(reason="Request body must be a JSON object")
        return value

    def _request_id(self, request: web.Request) -> str:
        """Return the request ID assigned by middleware or create one for tests."""

        existing = request.get(REQUEST_ID_KEY)
        if isinstance(existing, str):
            return existing
        request_id = self._new_request_id(request)
        request[REQUEST_ID_KEY] = request_id
        return request_id

    @staticmethod
    def _new_request_id(request: web.Request) -> str:
        """Accept a conservative caller ID or generate a UUID for correlation."""

        supplied = request.headers.get("X-Request-ID", "")
        return supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())

    def _reserve_mutation_id(self, request: web.Request) -> str | None:
        """Reject repeated control commands during a short retry window."""

        request_id = self._request_id(request)
        now = time.monotonic()
        self._recent_mutation_ids = {
            key: expires for key, expires in self._recent_mutation_ids.items() if expires > now
        }
        if request_id in self._recent_mutation_ids:
            return None
        self._recent_mutation_ids[request_id] = now + self._mutation_id_ttl
        return request_id

    def _json_response(self, value: Any, status: int = 200) -> web.Response:
        """Create a non-cacheable JSON response."""

        return web.json_response(value, status=status, headers={"Cache-Control": "no-store"})

    def _error_response(
        self, code: str, message: str, status: int, request_id: str, details: Any = None
    ) -> web.Response:
        """Create the stable machine-readable error envelope."""

        return self._json_response(
            {"code": code, "message": message, "details": details, "request_id": request_id}, status
        )

    async def _audit(
        self,
        actor: dict[str, Any],
        request: web.Request,
        action: str,
        entity_type: str,
        entity_id: str | None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        """Append a redacted audit record for an accepted mutation."""

        await self.trt.db.add_audit(
            actor_id=actor["providerUserId"],
            actor_display_name=actor.get("displayName"),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=self._request_id(request),
            before=self._audit_value(before),
            after=self._audit_value(after),
        )

    @staticmethod
    def _audit_value(value: Any) -> Any:
        """Remove cached track catalogs before copying entities to audit JSON."""

        if not isinstance(value, dict):
            return value
        redacted = dict(value)
        catalog = redacted.pop("catalog", None)
        if isinstance(catalog, dict):
            tracks = catalog.get("tracks")
            redacted["catalog"] = {
                "revision": catalog.get("revision"),
                "track_count": len(tracks) if isinstance(tracks, list) else 0,
            }
        return redacted

    async def session(self, request: web.Request) -> web.Response:
        """Return administrator identity and bootstrap a double-submit token."""

        actor, error = self._identity(request)
        if error:
            raise error
        csrf = request.cookies.get("trt_csrf") or secrets.token_urlsafe(32)
        response = self._json_response(
            {
                "user": actor,
                "permissions": {"admin": True},
                "csrf_token": csrf,
                "settings": await self.trt.db.get_settings(),
            }
        )
        cookie_kwargs = {
            "httponly": False,
            "secure": self.trt.auth_cookie_secure,
            "samesite": self.trt.auth_cookie_samesite,
            "path": "/",
            "max_age": self.trt.auth_cookie_max_age_seconds,
        }
        domain = self.web_server._cookie_domain()
        if domain:
            cookie_kwargs["domain"] = domain
        response.set_cookie("trt_csrf", csrf, **cookie_kwargs)
        return response

    async def status(self, request: web.Request) -> web.Response:
        """Return the current operational control-room snapshot."""

        _actor, error = self._identity(request)
        if error:
            raise error
        return self._json_response(await self.status_payload())

    async def status_payload(self) -> dict[str, Any]:
        """Build the admin status payload shared by HTTP and Socket.IO."""

        master = self.trt.master.master_user
        profile = master.spotify_profile if master and master.spotify_profile else None
        settings = await self.trt.db.get_settings()
        return {
            "autodj": self.trt.autodj.snapshot(),
            "master": {
                "is_autodj": master is self.trt.autodj,
                "display_name": profile.get("display_name") if profile else None,
                "spotify_id": profile.get("id") if profile else None,
            },
            "listeners": self.trt.master.last_listener_count,
            "schedule": self.trt.schedule_coordinator.status(),
            "playlists": await self.trt.db.list_playlist_summaries(),
            "settings": settings,
            "default_playlist_id": settings.get("default_playlist_id", {}).get("value")
            if isinstance(settings.get("default_playlist_id"), dict)
            else None,
            "ready": bool(
                self.trt.autodj.selected_playlist
                and self.trt.schedule_coordinator.last_error is None
            ),
            "degraded": self.trt.schedule_coordinator.status().get("degraded", False),
        }

    async def audit(self, request: web.Request) -> web.Response:
        """Return a bounded page of recent administrative audit entries."""

        _actor, error = self._identity(request)
        if error:
            raise error
        try:
            limit = max(1, min(500, int(request.query.get("limit", "100"))))
        except ValueError:
            limit = 100
        return self._json_response({"items": await self.trt.db.recent_audit(limit)})

    async def settings(self, request: web.Request) -> web.Response:
        """Return all versioned radio settings."""

        _actor, error = self._identity(request)
        if error:
            raise error
        return self._json_response(await self.trt.db.get_settings())

    async def update_settings(self, request: web.Request) -> web.Response:
        """Validate and atomically update supported versioned settings."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        body = await self._json(request)
        changed: dict[str, Any] = {}
        before = await self.trt.db.get_settings()
        for key in ("default_playlist_id", "default_timezone", "catalog_refresh_interval_seconds"):
            if key not in body:
                continue
            if key == "default_playlist_id":
                playlist = await self.trt.db.get_playlist(str(body[key]))
                if not playlist or not playlist.get("enabled", True):
                    return self._error_response(
                        "playlist_unavailable",
                        "Default playlist is missing or disabled",
                        422,
                        self._request_id(request),
                    )
            if key == "default_timezone" and not isinstance(body[key], str):
                return self._error_response(
                    "invalid_timezone",
                    "default_timezone must be an IANA timezone string",
                    422,
                    self._request_id(request),
                )
            if key == "default_timezone":
                try:
                    ZoneInfo(body[key])
                except (ZoneInfoNotFoundError, ValueError):
                    return self._error_response(
                        "invalid_timezone",
                        "default_timezone must be an IANA timezone string",
                        422,
                        self._request_id(request),
                    )
            if key == "catalog_refresh_interval_seconds":
                try:
                    interval = int(body[key])
                except (TypeError, ValueError):
                    return self._error_response(
                        "invalid_refresh_interval",
                        "catalog_refresh_interval_seconds must be an integer",
                        422,
                        self._request_id(request),
                    )
                if interval < 300 or interval > 7 * 24 * 60 * 60:
                    return self._error_response(
                        "invalid_refresh_interval",
                        "catalog_refresh_interval_seconds must be between 300 and 604800",
                        422,
                        self._request_id(request),
                    )
                changed[key] = interval
            else:
                changed[key] = body[key]
        unknown = set(body) - {
            "default_playlist_id",
            "default_timezone",
            "catalog_refresh_interval_seconds",
            "versions",
        }
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported setting fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        if not changed:
            return self._json_response(before)

        versions = body.get("versions")
        if not isinstance(versions, dict):
            return self._error_response(
                "precondition_required",
                "versions is required for every setting update",
                428,
                self._request_id(request),
            )
        missing_versions = [
            key for key in changed if key in before and not isinstance(versions.get(key), int)
        ]
        if missing_versions:
            return self._error_response(
                "precondition_required",
                f"Missing setting versions: {', '.join(missing_versions)}",
                428,
                self._request_id(request),
            )
        expected_versions = {
            key: versions.get(key) for key in changed if isinstance(versions.get(key), int)
        }
        try:
            await self.trt.db.set_settings(
                changed,
                actor["providerUserId"],
                expected_versions,
            )
        except VersionConflict as exc:
            return self._error_response(
                "version_conflict",
                str(exc),
                409,
                self._request_id(request),
                {"expected": exc.expected, "actual": exc.actual},
            )
        if changed:
            await self._audit(
                actor,
                request,
                "settings.update",
                "settings",
                None,
                before,
                await self.trt.db.get_settings(),
            )
            self.trt.schedule_coordinator.wake()
        return self._json_response(await self.trt.db.get_settings())

    async def playlists(self, request: web.Request) -> web.Response:
        """List compact playlist metadata for administration views."""

        _actor, error = self._identity(request)
        if error:
            raise error
        return self._json_response({"items": await self.trt.db.list_playlist_summaries()})

    async def playlist(self, request: web.Request) -> web.Response:
        """Return one playlist record, including its normalized catalog."""

        _actor, error = self._identity(request)
        if error:
            raise error
        row = await self.trt.db.get_playlist(request.match_info["playlist_id"])
        if not row:
            raise web.HTTPNotFound()
        return self._json_response(row)

    async def create_playlist(self, request: web.Request) -> web.Response:
        """Validate a Spotify URI and register its complete playable catalog."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        body = await self._json(request)
        uri = str(body.get("spotify_uri") or body.get("uri") or "")
        if not playlist_id_from_uri(uri):
            return self._error_response(
                "invalid_playlist",
                "A Spotify playlist URI or URL is required",
                422,
                self._request_id(request),
            )
        existing = await self.trt.db.get_playlist_by_uri(
            f"spotify:playlist:{playlist_id_from_uri(uri)}"
        )
        try:
            catalog = await self.trt.autodj.catalog_client.fetch(uri)
        except CatalogError as exc:
            return self._error_response(
                "playlist_unavailable", str(exc), 422, self._request_id(request)
            )
        row = await self.trt.db.upsert_playlist(
            {
                "spotify_uri": catalog.spotify_uri,
                "name": catalog.name,
                "external_url": catalog.external_url,
                "image_url": catalog.image_url,
                "catalog": catalog.as_json(),
                "catalog_revision": catalog.revision,
            },
            actor["providerUserId"],
        )
        await self._audit(
            actor,
            request,
            "playlist.refresh" if existing else "playlist.create",
            "playlist",
            row.get("id"),
            existing,
            row,
        )
        return self._json_response(row, 200 if existing else 201)

    async def update_playlist(self, request: web.Request) -> web.Response:
        """Edit a playlist label or enabled state without trusting catalog input."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        playlist_id = request.match_info["playlist_id"]
        before = await self.trt.db.get_playlist(playlist_id)
        if not before:
            raise web.HTTPNotFound()
        body = await self._json(request)
        unknown = set(body) - {"name", "enabled"}
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported playlist fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        changes: dict[str, Any] = {}
        if "name" in body:
            if (
                not isinstance(body["name"], str)
                or not body["name"].strip()
                or len(body["name"].strip()) > 200
            ):
                return self._error_response(
                    "invalid_playlist_name",
                    "name must contain between 1 and 200 characters",
                    422,
                    self._request_id(request),
                )
            changes["name"] = body["name"].strip()
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                return self._error_response(
                    "invalid_enabled",
                    "enabled must be a boolean",
                    422,
                    self._request_id(request),
                )
            if not body["enabled"]:
                default_id = await self.trt.db.get_setting("default_playlist_id")
                references = await self.trt.db.playlist_reference_count(playlist_id)
                if default_id == playlist_id or references:
                    return self._error_response(
                        "playlist_referenced",
                        "A default or scheduled playlist cannot be disabled",
                        409,
                        self._request_id(request),
                        {"schedule_count": references},
                    )
            changes["enabled"] = body["enabled"]
        after = await self.trt.db.patch_playlist(
            playlist_id,
            changes,
            actor["providerUserId"],
        )
        await self._audit(actor, request, "playlist.update", "playlist", playlist_id, before, after)
        self.trt.schedule_coordinator.wake()
        return self._json_response(after)

    async def delete_playlist(self, request: web.Request) -> web.Response:
        """Delete a playlist only when no setting, series, or override uses it."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        playlist_id = request.match_info["playlist_id"]
        row = await self.trt.db.get_playlist(playlist_id)
        if not row:
            raise web.HTTPNotFound()
        default_id = await self.trt.db.get_setting("default_playlist_id")
        references = await self.trt.db.playlist_reference_count(playlist_id)
        if default_id == playlist_id or references:
            return self._error_response(
                "playlist_referenced",
                "Playlist is the default or used by a schedule",
                409,
                self._request_id(request),
                {"schedule_count": references},
            )
        await self.trt.db.delete_playlist(playlist_id)
        await self._audit(actor, request, "playlist.delete", "playlist", playlist_id, row, None)
        return self._json_response({"deleted": True})

    async def refresh_playlist(self, request: web.Request) -> web.Response:
        """Refresh one catalog while retaining its last-known-good copy on failure."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        playlist_id = request.match_info["playlist_id"]
        row = await self.trt.db.get_playlist(playlist_id)
        if not row:
            raise web.HTTPNotFound()
        before = dict(row)
        try:
            after = await self.trt.autodj.refresh_playlist_record(
                row,
                actor["providerUserId"],
            )
        except CatalogError as exc:
            await self.trt.db.patch_playlist(
                playlist_id, {"validation_error": str(exc)}, actor["providerUserId"]
            )
            return self._error_response(
                "playlist_unavailable", str(exc), 422, self._request_id(request)
            )
        self.trt.schedule_coordinator.catalog_errors.pop(playlist_id, None)
        await self._audit(
            actor, request, "playlist.refresh", "playlist", playlist_id, before, after
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response(after)

    async def schedule(self, request: web.Request) -> web.Response:
        """Expand a bounded UTC range for the calendar interface."""

        _actor, error = self._identity(request)
        if error:
            raise error
        now = datetime.now(UTC)
        try:
            start = (
                parse_utc(request.query["from"])
                if request.query.get("from")
                else now - timedelta(days=7)
            )
            end = (
                parse_utc(request.query["to"])
                if request.query.get("to")
                else now + timedelta(days=90)
            )
            limit = max(1, min(1000, int(request.query.get("limit", "500"))))
        except (RecurrenceError, ValueError) as exc:
            raise web.HTTPBadRequest(reason="from/to must be ISO-8601 timestamps") from exc
        if end <= start or end - start > MAX_SCHEDULE_RANGE:
            raise web.HTTPBadRequest(
                reason="schedule range must be positive and no longer than 366 days"
            )
        return self._json_response(
            {"items": await self.trt.schedule_coordinator.occurrences(start, end, limit)}
        )

    async def preview_schedule(self, request: web.Request) -> web.Response:
        """Validate a complete draft and return its next bounded occurrences."""

        _actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        body = await self._json(request)
        unknown = set(body) - SERIES_FIELDS
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported schedule fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        draft = self._series_values(body, defaults=True)
        draft["id"] = str(uuid.uuid4())
        try:
            validate_series(draft)
            playlist = await self.trt.db.get_playlist(str(draft.get("playlist_id") or ""))
            if not playlist or not playlist.get("enabled", True):
                return self._error_response(
                    "playlist_unavailable",
                    "Schedule playlist is missing or disabled",
                    422,
                    self._request_id(request),
                )
            start = datetime.now(UTC) - timedelta(days=1)
            end = start + timedelta(days=180)
            occurrences = occurrences_between(draft, start, end, 100)
        except (RecurrenceError, ValueError) as exc:
            return self._error_response(
                "invalid_schedule", str(exc), 422, self._request_id(request)
            )
        return self._json_response({"items": [item.as_dict() for item in occurrences]})

    def _series_values(
        self,
        body: dict[str, Any],
        *,
        defaults: bool,
    ) -> dict[str, Any]:
        """Copy only locally editable series fields and optionally add defaults."""

        values = {key: body[key] for key in SERIES_FIELDS if key in body}
        if isinstance(values.get("title"), str):
            values["title"] = values["title"].strip()
        if defaults:
            values.setdefault("title", "Untitled show")
            values.setdefault("timezone", self.trt.schedule_timezone)
            values.setdefault("duration_seconds", 3600)
            values.setdefault("priority", 0)
            values.setdefault("transition_policy", "immediate")
            values.setdefault("enabled", True)
            values["source"] = "local"
        return values

    async def create_series(self, request: web.Request) -> web.Response:
        """Create a validated local schedule series for an enabled playlist."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        body = await self._json(request)
        unknown = set(body) - SERIES_FIELDS
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported schedule fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        values = self._series_values(body, defaults=True)
        if "timezone" not in body:
            values["timezone"] = await self.trt.db.get_setting(
                "default_timezone", self.trt.schedule_timezone
            )
        playlist = await self.trt.db.get_playlist(str(values.get("playlist_id") or ""))
        if not playlist or not playlist.get("enabled", True):
            return self._error_response(
                "playlist_unavailable",
                "Schedule playlist is missing or disabled",
                422,
                self._request_id(request),
            )
        values["id"] = str(uuid.uuid4())
        try:
            validate_series(values)
            row = await self.trt.db.create_schedule_series(values, actor["providerUserId"])
        except (RecurrenceError, ValueError) as exc:
            return self._error_response(
                "invalid_schedule", str(exc), 422, self._request_id(request)
            )
        await self._audit(
            actor, request, "schedule.create", "schedule_series", row["id"], None, row
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response(row, 201)

    async def get_series(self, request: web.Request) -> web.Response:
        """Return one complete schedule series for an editor."""

        _actor, error = self._identity(request)
        if error:
            raise error
        row = await self.trt.db.get_schedule_series(request.match_info["series_id"])
        if not row:
            raise web.HTTPNotFound()
        return self._json_response(row)

    async def update_series(self, request: web.Request) -> web.Response:
        """Patch one series without changing omitted fields or losing edits."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        series_id = request.match_info["series_id"]
        before = await self.trt.db.get_schedule_series(series_id)
        if not before:
            raise web.HTTPNotFound()
        body = await self._json(request)
        unknown = set(body) - SERIES_FIELDS - {"version"}
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported schedule fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        if not isinstance(body.get("version"), int):
            return self._error_response(
                "precondition_required",
                "The current integer version is required",
                428,
                self._request_id(request),
            )
        patch_values = self._series_values(body, defaults=False)
        values = {**before, **patch_values}
        values["id"] = series_id
        playlist = await self.trt.db.get_playlist(str(values.get("playlist_id") or ""))
        if not playlist or not playlist.get("enabled", True):
            return self._error_response(
                "playlist_unavailable",
                "Schedule playlist is missing or disabled",
                422,
                self._request_id(request),
            )
        try:
            validate_series(values)
            after = await self.trt.db.update_schedule_series(
                series_id,
                patch_values,
                actor["providerUserId"],
                body["version"],
            )
        except VersionConflict as exc:
            return self._error_response(
                "version_conflict",
                str(exc),
                409,
                self._request_id(request),
                {"expected": exc.expected, "actual": exc.actual},
            )
        except (RecurrenceError, ValueError) as exc:
            return self._error_response(
                "invalid_schedule", str(exc), 422, self._request_id(request)
            )
        await self._audit(
            actor, request, "schedule.update", "schedule_series", series_id, before, after
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response(after)

    async def delete_series(self, request: web.Request) -> web.Response:
        """Delete a series only when the caller presents its current version."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        series_id = request.match_info["series_id"]
        before = await self.trt.db.get_schedule_series(series_id)
        if not before:
            raise web.HTTPNotFound()
        try:
            expected_version = int(request.query.get("version", ""))
        except ValueError:
            return self._error_response(
                "precondition_required",
                "The current version query parameter is required",
                428,
                self._request_id(request),
            )
        try:
            await self.trt.db.delete_schedule_series(
                series_id,
                actor["providerUserId"],
                expected_version,
            )
        except VersionConflict as exc:
            return self._error_response(
                "version_conflict",
                str(exc),
                409,
                self._request_id(request),
                {"expected": exc.expected, "actual": exc.actual},
            )
        await self._audit(
            actor, request, "schedule.delete", "schedule_series", series_id, before, None
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response({"deleted": True})

    async def add_exception(self, request: web.Request) -> web.Response:
        """Cancel or override exactly one included occurrence of a series."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        series_id = request.match_info["series_id"]
        series = await self.trt.db.get_schedule_series(series_id)
        if not series:
            raise web.HTTPNotFound()
        body = await self._json(request)
        allowed_fields = {
            "action",
            "original_start_utc",
            "title",
            "playlist_id",
            "start_local",
            "timezone",
            "duration_seconds",
            "priority",
            "version",
        }
        unknown = set(body) - allowed_fields
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported exception fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        if body.get("action") not in {"cancel", "override"} or not body.get("original_start_utc"):
            raise web.HTTPBadRequest(reason="action and original_start_utc are required")
        if not isinstance(body.get("version"), int):
            return self._error_response(
                "precondition_required",
                "The current integer version is required",
                428,
                self._request_id(request),
            )
        try:
            original_start = parse_utc(str(body["original_start_utc"]))
            if not is_original_occurrence_start(series, original_start):
                raise RecurrenceError(
                    "original_start_utc is not an included occurrence of this series"
                )
            body["original_start_utc"] = iso_utc(original_start)
            if body["action"] == "override":
                playlist_id = body.get("playlist_id")
                if playlist_id:
                    playlist = await self.trt.db.get_playlist(str(playlist_id))
                    if not playlist or not playlist.get("enabled", True):
                        return self._error_response(
                            "playlist_unavailable",
                            "Override playlist is missing or disabled",
                            422,
                            self._request_id(request),
                        )
                # Reuse the domain resolver to validate timezone, start, and
                # duration fields before writing an override that could make
                # the coordinator ignore an otherwise valid series.
                candidate = original_start.astimezone(ZoneInfo(str(series["timezone"])))
                occurrence_from_start({**series, "overrides": [{**body}]}, candidate)
        except (
            RecurrenceError,
            ValueError,
            TypeError,
            OverflowError,
            ZoneInfoNotFoundError,
        ) as exc:
            return self._error_response(
                "invalid_schedule", str(exc), 422, self._request_id(request)
            )
        override_values = {key: body[key] for key in allowed_fields - {"version"} if key in body}
        try:
            row = await self.trt.db.add_override(
                series_id,
                override_values,
                actor["providerUserId"],
                body["version"],
            )
        except VersionConflict as exc:
            return self._error_response(
                "version_conflict",
                str(exc),
                409,
                self._request_id(request),
                {"expected": exc.expected, "actual": exc.actual},
            )
        except sqlite3.IntegrityError:
            return self._error_response(
                "override_exists",
                "An exception already exists for this occurrence",
                409,
                self._request_id(request),
            )
        await self._audit(
            actor, request, "schedule.exception", "schedule_override", row.get("id"), None, row
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response(row, 201)

    async def split_series(self, request: web.Request) -> web.Response:
        """Create a future series while ending the original at a boundary."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        series_id = request.match_info["series_id"]
        original = await self.trt.db.get_schedule_series(series_id)
        if not original:
            raise web.HTTPNotFound()
        body = await self._json(request)
        if not isinstance(body.get("version"), int):
            return self._error_response(
                "precondition_required",
                "The current integer version is required",
                428,
                self._request_id(request),
            )
        raw_future_values = body.get("values") or {}
        if not isinstance(raw_future_values, dict):
            raise web.HTTPBadRequest(reason="values must be a JSON object")
        unknown = set(raw_future_values) - SERIES_FIELDS
        if unknown:
            return self._error_response(
                "unknown_fields",
                f"Unsupported schedule fields: {', '.join(sorted(unknown))}",
                400,
                self._request_id(request),
            )
        future = {
            **original,
            **self._series_values(raw_future_values, defaults=False),
        }
        future["source"] = "local"
        future["id"] = str(uuid.uuid4())
        if not body.get("effective_dtstart_local"):
            raise web.HTTPBadRequest(reason="effective_dtstart_local is required")
        future["dtstart_local"] = body["effective_dtstart_local"]
        try:
            # End the old series immediately before the future start. For an
            # unbounded RRULE, an UNTIL in UTC is the least lossy portable
            # representation when the source series has an aware DTSTART.
            effective_local = parse_local(str(future["dtstart_local"]), str(future["timezone"]))
            effective_utc = effective_local.astimezone(UTC)
            if not original.get("rrule"):
                raise RecurrenceError("A one-time event cannot be split")
            if not is_original_occurrence_start(original, effective_utc):
                raise RecurrenceError(
                    "effective_dtstart_local must identify an occurrence of the original series"
                )
            old_rules, old_zone = build_rruleset(original)
            previous = old_rules.before(effective_local.astimezone(old_zone), inc=False)
            old_rrule = original.get("rrule")
            if old_rrule and previous:
                old_rrule = truncate_rrule(str(old_rrule), previous.astimezone(UTC))
            elif old_rrule and not previous:
                old_rrule = None

            old_rdates, future_rdates = self._partition_dates(
                original.get("rdates") or [],
                effective_utc,
            )
            old_exdates, future_exdates = self._partition_dates(
                original.get("exdates") or [],
                effective_utc,
            )
            future["rdates"] = future_rdates
            future["exdates"] = future_exdates
            future_override_ids = [
                str(override["id"])
                for override in original.get("overrides") or []
                if parse_utc(str(override["original_start_utc"])) >= effective_utc
            ]
            validate_series(future)
            playlist = await self.trt.db.get_playlist(str(future.get("playlist_id") or ""))
            if not playlist or not playlist.get("enabled", True):
                return self._error_response(
                    "playlist_unavailable",
                    "Schedule playlist is missing or disabled",
                    422,
                    self._request_id(request),
                )
            updated, created = await self.trt.db.split_schedule_series(
                series_id,
                {
                    "rrule": old_rrule,
                    "enabled": bool(previous or old_rdates),
                    "rdates": old_rdates,
                    "exdates": old_exdates,
                    "_future_override_ids": future_override_ids,
                },
                future,
                actor["providerUserId"],
                body["version"],
            )
            if not updated or not created:
                raise web.HTTPNotFound()
        except (RecurrenceError, ValueError, VersionConflict) as exc:
            if isinstance(exc, VersionConflict):
                return self._error_response(
                    "version_conflict",
                    str(exc),
                    409,
                    self._request_id(request),
                    {"expected": exc.expected, "actual": exc.actual},
                )
            return self._error_response(
                "invalid_schedule", str(exc), 422, self._request_id(request)
            )
        await self._audit(
            actor, request, "schedule.split", "schedule_series", series_id, original, created
        )
        self.trt.schedule_coordinator.wake()
        return self._json_response({"original": updated, "future": created}, 201)

    @staticmethod
    def _partition_dates(
        values: list[str],
        boundary_utc: datetime,
    ) -> tuple[list[str], list[str]]:
        """Split canonical recurrence dates around a UTC series boundary."""

        before: list[str] = []
        after: list[str] = []
        for value in values:
            target = after if parse_utc(value) >= boundary_utc else before
            target.append(iso_utc(parse_utc(value)))
        return before, after

    async def skip_current(self, request: web.Request) -> web.Response:
        """Skip the track currently supplied by AutoDJ."""

        return await self._playback_action(request, "skip-current")

    async def skip_next(self, request: web.Request) -> web.Response:
        """Replace AutoDJ's queued next track without changing the current one."""

        return await self._playback_action(request, "skip-next")

    async def reload_playlist(self, request: web.Request) -> web.Response:
        """Fetch the active catalog again and keep the current track playing."""

        return await self._playback_action(request, "reload-playlist")

    async def _playback_action(self, request: web.Request, action: str) -> web.Response:
        """Authorize, serialize, execute, and audit one AutoDJ control command."""

        actor, error = self._identity(request, mutation=True)
        if error:
            raise error
        if self.trt.master.master_user is not self.trt.autodj:
            await self._audit(
                actor,
                request,
                f"playback.{action}.rejected",
                "autodj",
                "AUTODJ",
                None,
                {"reason": "human_master_active"},
            )
            return self._error_response(
                "human_master_active",
                "AutoDJ controls are unavailable while a human DJ is master",
                409,
                self._request_id(request),
            )
        if self._reserve_mutation_id(request) is None:
            return self._error_response(
                "duplicate_request",
                "This control request was already accepted",
                409,
                self._request_id(request),
            )
        before = self.trt.autodj.snapshot()
        try:
            if action == "skip-current":
                after = await self.trt.autodj.skip_current()
            elif action == "skip-next":
                after = await self.trt.autodj.replace_next_track()
            else:
                playlist_id = (self.trt.autodj.selected_playlist or {}).get("id")
                row = await self.trt.db.get_playlist(str(playlist_id)) if playlist_id else None
                if not row:
                    raise RuntimeError("active playlist is unavailable")
                await self.trt.autodj.refresh_playlist_record(
                    row,
                    actor["providerUserId"],
                )
                after = self.trt.autodj.snapshot()
        except (CatalogError, RuntimeError) as exc:
            await self._audit(
                actor,
                request,
                f"playback.{action}.failed",
                "autodj",
                "AUTODJ",
                before,
                {"error": str(exc)},
            )
            return self._error_response("playback_failed", str(exc), 422, self._request_id(request))
        await self._audit(actor, request, f"playback.{action}", "autodj", "AUTODJ", before, after)
        return self._json_response(after)
