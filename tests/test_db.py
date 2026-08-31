"""Integration tests for SQLite migrations, transactions, and backups."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup_sqlite import backup
from thatradiothing.db import RadioDatabase, VersionConflict

PLAYLIST_URI = "spotify:playlist:3cEYpjA9oz9GiPac4AsH4n"


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Run each repository test against an isolated on-disk SQLite database."""

    async def asyncSetUp(self):
        """Create and migrate a temporary database for one test."""

        self.temporary_directory = tempfile.TemporaryDirectory(prefix="trt-db-")
        self.path = str(Path(self.temporary_directory.name) / "radio.sqlite3")
        self.db = RadioDatabase(self.path, [{"uri": PLAYLIST_URI}])
        await self.db.open()

    async def asyncTearDown(self):
        """Close SQLite before removing its temporary directory and sidecars."""

        await self.db.close()
        self.temporary_directory.cleanup()

    async def test_bootstrap_schema_and_setting_version(self):
        """Migrate an empty file and import the legacy default exactly once."""

        playlists = await self.db.list_playlists()
        self.assertEqual(len(playlists), 1)
        default = await self.db.get_setting("default_playlist_id")
        self.assertEqual(default, playlists[0]["id"])
        version = await self.db.fetch_one("PRAGMA user_version")
        self.assertEqual(next(iter(version.values())), 2)

        await self.db.set_setting("default_timezone", "Europe/Istanbul", "admin")
        await self.db.set_setting("default_timezone", "Europe/Berlin", "admin")
        with self.assertRaises(VersionConflict):
            await self.db.set_setting(
                "default_timezone",
                "UTC",
                "other",
                expected_version=1,
            )

    async def test_multi_setting_conflict_rolls_back_entire_request(self):
        """Commit no field when any optimistic version in a batch is stale."""

        await self.db.set_settings({"one": 1, "two": 2}, "admin")
        with self.assertRaises(VersionConflict):
            await self.db.set_settings(
                {"one": 10, "two": 20},
                "other",
                {"one": 1, "two": 0},
            )
        self.assertEqual(await self.db.get_setting("one"), 1)
        self.assertEqual(await self.db.get_setting("two"), 2)

    async def test_catalog_refresh_preserves_disabled_state(self):
        """Do not re-enable a playlist merely because its metadata refreshed."""

        playlist = (await self.db.list_playlists())[0]
        await self.db.patch_playlist(playlist["id"], {"enabled": False}, "admin")
        refreshed = await self.db.upsert_playlist(
            {
                "spotify_uri": PLAYLIST_URI,
                "name": "Refreshed name",
                "catalog": {"tracks": [{"uri": "spotify:track:1"}]},
            },
            "system",
        )
        self.assertFalse(refreshed["enabled"])

    async def test_online_backup_can_be_restored(self):
        """Include committed WAL data in a consistent online backup."""

        backup_path = str(Path(self.temporary_directory.name) / "backup.sqlite3")
        await self.db.set_setting("backup_marker", {"value": 42}, "admin")
        backup(self.path, backup_path)
        with sqlite3.connect(backup_path) as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = 'backup_marker'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("42", row[0])

    async def test_split_series_moves_future_dates_and_overrides_atomically(self):
        """Keep past exceptions on the predecessor and move future ones forward."""

        playlist = (await self.db.list_playlists())[0]
        original = await self.db.create_schedule_series(
            {
                "id": "series-original",
                "title": "Original",
                "playlist_id": playlist["id"],
                "dtstart_local": "2026-08-03T09:00:00",
                "timezone": "Europe/Istanbul",
                "duration_seconds": 1800,
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
                "rdates": [
                    "2026-08-24T06:00:00Z",
                    "2026-09-14T06:00:00Z",
                ],
            },
            "admin",
        )
        override = await self.db.add_override(
            original["id"],
            {
                "action": "cancel",
                "original_start_utc": "2026-09-14T06:00:00Z",
            },
            "admin",
            expected_version=original["version"],
        )
        updated, successor = await self.db.split_schedule_series(
            original["id"],
            {
                "rrule": "FREQ=WEEKLY;BYDAY=MO;UNTIL=20260831T060000Z",
                "enabled": True,
                "rdates": ["2026-08-24T06:00:00Z"],
                "exdates": [],
                "_future_override_ids": [override["id"]],
            },
            {
                "id": "series-successor",
                "title": "Successor",
                "playlist_id": playlist["id"],
                "dtstart_local": "2026-09-07T09:00:00",
                "timezone": "Europe/Istanbul",
                "duration_seconds": 1800,
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
                "rdates": ["2026-09-14T06:00:00Z"],
                "exdates": [],
            },
            "admin",
            expected_version=2,
        )
        self.assertEqual(
            updated["rrule"],
            "FREQ=WEEKLY;BYDAY=MO;UNTIL=20260831T060000Z",
        )
        self.assertEqual(updated["rdates"], ["2026-08-24T06:00:00Z"])
        self.assertEqual(successor["rdates"], ["2026-09-14T06:00:00Z"])
        self.assertEqual([item["id"] for item in successor["overrides"]], [override["id"]])


if __name__ == "__main__":
    unittest.main()
