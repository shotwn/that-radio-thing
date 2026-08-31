"""Unit tests for Spotify playlist response normalization and safety checks."""

import asyncio
import unittest

from thatradiothing.spotify_catalog import (
    MAX_PLAYLIST_PAGES,
    CatalogError,
    SpotifyCatalogClient,
    normalize_playlist_payload,
)

PLAYLIST_ID = "3cEYpjA9oz9GiPac4AsH4n"


def track(identifier: str, **extra) -> dict:
    """Build a representative Spotify track payload."""

    return {
        "type": "track",
        "uri": f"spotify:track:{identifier}",
        "name": identifier,
        "duration_ms": 1000,
        "available_markets": ["TR", "DE"],
        **extra,
    }


class CatalogTests(unittest.TestCase):
    """Verify compatibility with legacy and current Spotify response shapes."""

    def test_normalizes_legacy_tracks_to_compact_shape(self):
        """Read legacy wrappers, deduplicate, filter local, and drop large fields."""

        catalog = normalize_playlist_payload(
            {
                "id": "3cEYpjA9oz9GiPac4AsH4n",
                "name": "Legacy",
                "tracks": {
                    "items": [
                        {"track": track("a")},
                        {"track": track("a")},
                        {"track": track("local", is_local=True)},
                    ]
                },
            }
        )
        self.assertEqual(
            [item["uri"] for item in catalog.tracks],
            ["spotify:track:a"],
        )
        self.assertNotIn("available_markets", catalog.tracks[0])

    def test_normalizes_current_items(self):
        """Read the current ``items[].item`` playlist response shape."""

        catalog = normalize_playlist_payload(
            {"id": "3cEYpjA9oz9GiPac4AsH4n", "name": "Current"},
            [{"items": [{"item": track("a")}, {"item": track("b")}]}],
        )
        self.assertEqual(len(catalog.tracks), 2)

    def test_rejects_empty_or_invalid_playlist(self):
        """Require a valid Spotify identifier and at least one playable track."""

        with self.assertRaises(CatalogError):
            normalize_playlist_payload(
                {
                    "id": "3cEYpjA9oz9GiPac4AsH4n",
                    "tracks": {"items": []},
                }
            )
        with self.assertRaises(CatalogError):
            normalize_playlist_payload(
                {
                    "id": "!!!!!!!!!!!!!!!!!!!!!!",
                    "tracks": {"items": [{"track": track("a")}]},
                }
            )

    def test_rejects_off_site_pagination_url(self):
        """Never send a Spotify bearer token to a response-controlled host."""

        with self.assertRaises(CatalogError):
            SpotifyCatalogClient._validate_api_url("https://evil.example/v1/playlists/page")


class PaginationBoundTests(unittest.TestCase):
    """``next`` is response-controlled, so the walk must terminate on its own."""

    @staticmethod
    def client_returning(pages):
        """Build a client whose HTTP layer replays a scripted page sequence."""

        client = SpotifyCatalogClient("id", "secret")
        calls = []

        async def fake_get_json(url, params=None):
            """Return the scripted page for *url* and record the request."""

            del params
            calls.append(url)
            return pages(url)

        client._get_json = fake_get_json
        return client, calls

    def test_rejects_a_pagination_cycle(self):
        """A ``next`` link pointing at an already-visited page must not loop."""

        base = f"https://api.spotify.com/v1/playlists/{PLAYLIST_ID}"
        loop_url = f"{base}/items?offset=100"

        def pages(url):
            if url == base:
                return {"id": PLAYLIST_ID, "name": "Cyclic"}
            if url == f"{base}/items":
                return {"items": [{"track": track("a")}], "next": loop_url}
            # Point straight back at the page we just came from.
            return {"items": [{"track": track("b")}], "next": loop_url}

        client, calls = self.client_returning(pages)
        with self.assertRaises(CatalogError) as caught:
            asyncio.run(client.fetch(f"spotify:playlist:{PLAYLIST_ID}"))
        self.assertIn("repeating", str(caught.exception))
        # Metadata, the first item page, and exactly one cycle detection.
        self.assertEqual(len(calls), 3)

    def test_stops_after_the_page_ceiling(self):
        """An endlessly advancing playlist stops at the documented ceiling."""

        base = f"https://api.spotify.com/v1/playlists/{PLAYLIST_ID}"

        def pages(url):
            if url == base:
                return {"id": PLAYLIST_ID, "name": "Endless"}
            offset = len(url)
            return {
                "items": [{"track": track(f"t{offset}")}],
                "next": f"{base}/items?offset={'0' * offset}",
            }

        client, calls = self.client_returning(pages)
        with self.assertRaises(CatalogError) as caught:
            asyncio.run(client.fetch(f"spotify:playlist:{PLAYLIST_ID}"))
        self.assertIn(str(MAX_PLAYLIST_PAGES), str(caught.exception))
        # One metadata call plus exactly MAX_PLAYLIST_PAGES item pages.
        self.assertEqual(len(calls), MAX_PLAYLIST_PAGES + 1)


if __name__ == "__main__":
    unittest.main()
