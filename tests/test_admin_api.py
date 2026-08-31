"""Loopback integration tests for admin authentication and schedule mutations."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from thatradiothing.admin_api import AdminAPI
from thatradiothing.db import RadioDatabase
from thatradiothing.jwt_auth import issue_auth_token

PLAYLIST_URI = "spotify:playlist:3cEYpjA9oz9GiPac4AsH4n"


class FakeScheduler:
    """Provide the coordinator surface used by HTTP handlers."""

    last_error = None

    def status(self):
        """Return a healthy minimal scheduler snapshot."""

        return {"error": None, "degraded": False}

    def wake(self):
        """Accept mutation wakeups without starting a task."""

    async def occurrences(self, start, end, limit):
        """Return no expanded events for status-only tests."""

        del start, end, limit
        return []


class FakeAutoDJ:
    """Provide stable playback state without Spotify network access."""

    def __init__(self):
        """Create isolated playback state for each integration test."""

        self.selected_playlist = {"id": "default"}

    def snapshot(self):
        """Return the minimal admin playback payload."""

        return {"playlist": self.selected_playlist, "track": None, "next_track": None}


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real aiohttp router against temporary SQLite persistence."""

    async def asyncSetUp(self):
        """Build an isolated app, database, tokens, and loopback test server."""

        self.temporary_directory = tempfile.TemporaryDirectory(prefix="trt-api-")
        self.path = str(Path(self.temporary_directory.name) / "radio.sqlite3")
        self.db = RadioDatabase(self.path, [{"uri": PLAYLIST_URI}])
        await self.db.open()
        self.secret = "test-secret-for-admin-api-0123456789"
        self.trt = SimpleNamespace(
            db=self.db,
            admin_ids=["admin"],
            auth_shared_jwt_secret=self.secret,
            auth_jwt_issuer="test",
            admin_origins=["https://admin.example"],
            cors_allowed_origins=[],
            cors_allow_credentials=True,
            auth_cookie_secure=False,
            auth_cookie_samesite="Lax",
            auth_cookie_max_age_seconds=3600,
            schedule_timezone="Europe/Istanbul",
            autodj=FakeAutoDJ(),
            schedule_coordinator=FakeScheduler(),
            master=SimpleNamespace(master_user=None, last_listener_count=0),
        )
        self.app = web.Application()
        self.web_server = SimpleNamespace(
            trt=self.trt,
            router=self.app.router,
            middlewares=self.app.middlewares,
            _cookie_domain=lambda: None,
            _extract_auth_token=lambda request: (
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or request.cookies.get("duudey_auth")
            ),
        )
        self.api = AdminAPI(self.web_server)
        self.api.register()
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.token = self.issue_token("admin")
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    async def asyncTearDown(self):
        """Close loopback sockets and SQLite before deleting temporary files."""

        await self.client.close()
        await self.db.close()
        self.temporary_directory.cleanup()

    def issue_token(self, spotify_id: str) -> str:
        """Issue a valid shared Spotify session for one test identity."""

        return issue_auth_token(
            secret=self.secret,
            issuer="test",
            payload={
                "provider": "spotify",
                "providerUserId": spotify_id,
                "displayName": spotify_id.title(),
            },
            ttl_seconds=3600,
        )

    async def create_series(self) -> dict:
        """Create a representative weekly series through the HTTP API."""

        playlist = (await self.db.list_playlists())[0]
        response = await self.client.post(
            "/api/admin/v1/schedule/series",
            headers=self.auth_headers,
            json={
                "title": "Test show",
                "playlist_id": playlist["id"],
                "dtstart_local": "2026-08-31T09:00:00",
                "timezone": "Europe/Istanbul",
                "duration_seconds": 1800,
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
            },
        )
        self.assertEqual(response.status, 201, await response.text())
        return await response.json()

    async def test_allowlist_bearer_and_cookie_csrf_policies(self):
        """Separate authentication, admin authorization, origin, and CSRF checks."""

        response = await self.client.get("/api/admin/v1/session")
        self.assertEqual(response.status, 401)

        non_admin = self.issue_token("listener")
        response = await self.client.get(
            "/api/admin/v1/session",
            headers={"Authorization": f"Bearer {non_admin}"},
        )
        self.assertEqual(response.status, 403)

        response = await self.client.get(
            "/api/admin/v1/session",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status, 200)
        session = await response.json()
        csrf = session["csrf_token"]

        # A bearer caller is not vulnerable to ambient-cookie CSRF and may use
        # the versioned API without manufacturing a cookie pair.
        response = await self.client.patch(
            "/api/admin/v1/settings",
            headers=self.auth_headers,
            json={"default_timezone": "UTC", "versions": {}},
        )
        self.assertEqual(response.status, 200, await response.text())

        cookie = f"duudey_auth={self.token}; trt_csrf={csrf}"
        response = await self.client.patch(
            "/api/admin/v1/settings",
            headers={"Cookie": cookie, "Origin": "https://admin.example"},
            json={
                "default_timezone": "Europe/Berlin",
                "versions": {"default_timezone": 1},
            },
        )
        self.assertEqual(response.status, 403)

        response = await self.client.patch(
            "/api/admin/v1/settings",
            headers={
                "Cookie": cookie,
                "Origin": "https://admin.example",
                "X-CSRF-Token": csrf,
            },
            json={
                "default_timezone": "Europe/Berlin",
                "versions": {"default_timezone": 1},
            },
        )
        self.assertEqual(response.status, 200, await response.text())

    async def test_partial_series_patch_preserves_omitted_fields_and_requires_version(self):
        """Prevent the original implementation's default-overwrite regression."""

        series = await self.create_series()
        response = await self.client.patch(
            f"/api/admin/v1/schedule/series/{series['id']}",
            headers=self.auth_headers,
            json={"title": "Changed"},
        )
        self.assertEqual(response.status, 428)

        response = await self.client.patch(
            f"/api/admin/v1/schedule/series/{series['id']}",
            headers=self.auth_headers,
            json={"title": "Changed", "version": series["version"]},
        )
        self.assertEqual(response.status, 200, await response.text())
        updated = await response.json()
        self.assertEqual(updated["title"], "Changed")
        self.assertEqual(updated["duration_seconds"], 1800)
        self.assertEqual(updated["timezone"], "Europe/Istanbul")
        self.assertEqual(updated["rrule"], "FREQ=WEEKLY;BYDAY=MO")

    async def test_setting_version_conflict_is_atomic(self):
        """Return conflict without committing an earlier field in the same request."""

        await self.db.set_settings({"one": 1, "two": 2}, "bootstrap")
        response = await self.client.patch(
            "/api/admin/v1/settings",
            headers={**self.auth_headers, "X-Request-ID": "atomic-conflict"},
            json={
                "default_timezone": "UTC",
                "catalog_refresh_interval_seconds": 600,
                "versions": {
                    "default_timezone": 0,
                    "catalog_refresh_interval_seconds": 0,
                },
            },
        )
        # Establish both settings and then make one stale for the atomic check.
        self.assertEqual(response.status, 200)
        records = await self.db.get_settings()
        response = await self.client.patch(
            "/api/admin/v1/settings",
            headers={**self.auth_headers, "X-Request-ID": "atomic-conflict-2"},
            json={
                "default_timezone": "Europe/Berlin",
                "catalog_refresh_interval_seconds": 1200,
                "versions": {
                    "default_timezone": records["default_timezone"]["version"],
                    "catalog_refresh_interval_seconds": 0,
                },
            },
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(await self.db.get_setting("default_timezone"), "UTC")
        self.assertEqual(
            await self.db.get_setting("catalog_refresh_interval_seconds"),
            600,
        )
        payload = await response.json()
        self.assertEqual(payload["request_id"], "atomic-conflict-2")
        self.assertEqual(response.headers["X-Request-ID"], "atomic-conflict-2")

    async def test_exception_requires_real_occurrence_and_advances_version(self):
        """Reject arbitrary timestamps and serialize edit-one changes by version."""

        series = await self.create_series()
        path = f"/api/admin/v1/schedule/series/{series['id']}/exceptions"
        response = await self.client.post(
            path,
            headers=self.auth_headers,
            json={
                "action": "cancel",
                "original_start_utc": "2026-08-31T06:15:00Z",
                "version": series["version"],
            },
        )
        self.assertEqual(response.status, 422)

        payload = {
            "action": "cancel",
            "original_start_utc": "2026-08-31T06:00:00Z",
            "version": series["version"],
        }
        response = await self.client.post(path, headers=self.auth_headers, json=payload)
        self.assertEqual(response.status, 201, await response.text())
        current = await self.db.get_schedule_series(series["id"])
        self.assertEqual(current["version"], series["version"] + 1)

        payload["version"] = current["version"]
        response = await self.client.post(path, headers=self.auth_headers, json=payload)
        self.assertEqual(response.status, 409)


if __name__ == "__main__":
    unittest.main()
