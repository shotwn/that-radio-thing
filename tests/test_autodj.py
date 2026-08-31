"""State-machine tests for AutoDJ playlist and skip operations."""

import unittest

from thatradiothing.autodj import AutoDJ
from thatradiothing.spotify_catalog import PlaylistCatalog


def track(identifier: str, duration_ms: int = 100_000) -> dict:
    """Build the minimum normalized track shape AutoDJ requires."""

    return {
        "type": "track",
        "uri": f"spotify:track:{identifier}",
        "name": identifier,
        "duration_ms": duration_ms,
    }


class FakeDatabase:
    """Persist refreshed playlist dictionaries for focused AutoDJ tests."""

    def __init__(self):
        """Initialize an empty record map."""

        self.records = {}

    async def upsert_playlist(self, values, actor):
        """Return a deterministic local playlist record."""

        del actor
        record = {"id": values.get("id") or values["spotify_uri"], **values}
        self.records[record["id"]] = record
        return record


class FakeCatalogClient:
    """Return one in-memory playlist catalog without network access."""

    async def fetch(self, spotify_uri):
        """Build a catalog whose identity follows the requested URI."""

        identifier = spotify_uri.rsplit(":", 1)[-1]
        return PlaylistCatalog(
            spotify_id=identifier,
            spotify_uri=spotify_uri,
            name=f"Playlist {identifier}",
            external_url=f"https://open.spotify.com/playlist/{identifier}",
            image_url=None,
            tracks=[track("refreshed-1"), track("refreshed-2")],
        )


class FakeTRT:
    """Provide only application attributes AutoDJ touches."""

    def __init__(self):
        """Initialize users, scheduler, and fake persistence."""

        self.users = []
        self.schedule_coordinator = None
        self.db = FakeDatabase()


class AutoDJTests(unittest.IsolatedAsyncioTestCase):
    """Verify lock-protected AutoDJ invariants."""

    async def test_skip_operations_keep_state_consistent(self):
        """Always retain a current and queued next track after skip controls."""

        trt = FakeTRT()
        dj = AutoDJ(trt, "AUTODJ", "", "client", "secret")
        await dj.activate_playlist(
            {
                "id": "p1",
                "spotify_uri": "spotify:playlist:3cEYpjA9oz9GiPac4AsH4n",
                "catalog": {"tracks": [track("1"), track("2"), track("3")]},
            }
        )
        before = dj.snapshot()
        await dj.replace_next_track()
        self.assertNotEqual(
            before["next_track"]["uri"],
            dj.snapshot()["next_track"]["uri"],
        )
        old_current = dj.snapshot()["track"]["uri"]
        await dj.skip_current()
        self.assertNotEqual(old_current, dj.snapshot()["track"]["uri"])
        self.assertIsNotNone(dj.snapshot()["next_track"])

    async def test_elapsed_time_can_cross_multiple_short_tracks(self):
        """Catch up after process suspension without returning oversize progress."""

        trt = FakeTRT()
        dj = AutoDJ(trt, "AUTODJ", "", "client", "secret")
        await dj.activate_playlist(
            {
                "id": "short",
                "spotify_uri": "spotify:playlist:3cEYpjA9oz9GiPac4AsH4n",
                "catalog": {
                    "tracks": [
                        track("1", 1_000),
                        track("2", 1_000),
                        track("3", 1_000),
                    ]
                },
            }
        )
        dj._playback_started_monotonic -= 2.5
        playback = await dj.currently_playing()
        self.assertLess(playback["progress_ms"], playback["item"]["duration_ms"])

    async def test_refreshing_inactive_catalog_does_not_switch_playback(self):
        """Keep the selected playlist unchanged when another catalog refreshes."""

        trt = FakeTRT()
        dj = AutoDJ(
            trt,
            "AUTODJ",
            "",
            "client",
            "secret",
            catalog_client=FakeCatalogClient(),
        )
        await dj.activate_playlist(
            {
                "id": "active",
                "spotify_uri": "spotify:playlist:3cEYpjA9oz9GiPac4AsH4n",
                "catalog": {"tracks": [track("1"), track("2")]},
            }
        )
        await dj.refresh_playlist_record(
            {
                "id": "inactive",
                "spotify_uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
            },
            "admin",
        )
        self.assertEqual(dj.snapshot()["playlist"]["id"], "active")


if __name__ == "__main__":
    unittest.main()
