"""AutoDJ state machine and Spotify playlist-backed track selection."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp
from logzero import logger

import thatradiothing.user
from thatradiothing.spotify_catalog import CatalogError, SpotifyCatalogClient


class AutoDJ(thatradiothing.user.User):
    """Virtual master that supplies tracks to the existing sync loop."""

    def __init__(
        self,
        *args: Any,
        playlists: list[dict[str, Any]] | None = None,
        catalog_client: SpotifyCatalogClient | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize playback state while retaining legacy playlist bootstrap data."""

        super().__init__(*args)
        self.enabled = False
        self.playlists = list(playlists or [])
        self.selected_playlist: dict[str, Any] | None = None
        self._shuffle_bag: list[dict[str, Any]] = []
        # Catalog network calls are serialized separately so the shorter state
        # lock never remains held during Spotify latency or rate-limit waits.
        self._activation_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._catalog_client = catalog_client
        self.last_error: str | None = None
        self.now_playing: dict[str, Any] = {"track": None}
        self._playback_started_monotonic = time.monotonic()
        self.spotify_profile = {"display_name": "AutoDJ", "external_urls": {"spotify": "#"}}

    @property
    def catalog_client(self) -> SpotifyCatalogClient:
        """Lazily construct the app-level catalog client."""

        if self._catalog_client is None:
            self._catalog_client = SpotifyCatalogClient(self.client_id, self.client_secret)
        return self._catalog_client

    async def populate(self) -> bool:
        """Load the configured database default and register AutoDJ."""

        if self not in self.trt.users:
            self.trt.users.append(self)

        playlist = None
        if getattr(self.trt, "db", None) is not None:
            default_id = await self.trt.db.get_setting("default_playlist_id")
            if default_id:
                playlist = await self.trt.db.get_playlist(str(default_id))
            if playlist is None:
                rows = await self.trt.db.list_playlists(include_disabled=False)
                playlist = rows[0] if rows else None
        if playlist is None and self.playlists:
            playlist = self.playlists[0]
        if playlist is None:
            raise CatalogError("No default AutoDJ playlist has been configured")
        await self.activate_playlist(playlist, force_restart=False, reason="startup")
        return True

    async def request_tokens(self) -> bool:
        """Obtain a client-credentials token for playlist metadata."""

        payload = {"grant_type": "client_credentials"}
        session = await self.aiohttp_session()
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data=payload,
            auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or data.get("error"):
                raise CatalogError("AutoDJ Spotify catalog credentials failed")
            self.access_token = data["access_token"]
            self.expires_in = int(data.get("expires_in", 3600))
            self.refresh_tokens_after = time.time() + self.expires_in - 60
            self.token_type = data.get("token_type")
            return True

    async def refresh_tokens(self) -> bool:
        """Refresh the legacy ``User`` token fields used by inherited methods."""

        return await self.request_tokens()

    async def refresh_playlist_record(
        self, playlist_record: dict[str, Any], actor: str = "system"
    ) -> dict[str, Any]:
        """Fetch a complete catalog and persist it atomically."""

        catalog = await self.catalog_client.fetch(
            str(playlist_record.get("spotify_uri") or playlist_record.get("uri") or "")
        )
        updated = await self.trt.db.upsert_playlist(
            {
                "id": playlist_record.get("id"),
                "spotify_uri": catalog.spotify_uri,
                "name": catalog.name,
                "external_url": catalog.external_url,
                "image_url": catalog.image_url,
                "catalog": catalog.as_json(),
                "catalog_revision": catalog.revision,
                "validated_at": datetime.now(UTC).isoformat(),
                "validation_error": None,
            },
            actor,
        )
        selected = self.selected_playlist or {}
        if selected.get("id") == updated.get("id") or selected.get("spotify_uri") == updated.get(
            "spotify_uri"
        ):
            await self.activate_playlist(
                updated,
                force_restart=False,
                reason="catalog-refresh",
            )
        return updated

    def _tracks_for(self, playlist: dict[str, Any]) -> list[dict[str, Any]]:
        """Read normalized tracks from a database catalog or legacy response."""

        catalog = playlist.get("catalog")
        if isinstance(catalog, dict) and isinstance(catalog.get("tracks"), list):
            return [track for track in catalog["tracks"] if isinstance(track, dict)]
        data = playlist.get("data") or {}
        tracks = data.get("tracks") if isinstance(data, dict) else None
        items = tracks.get("items") if isinstance(tracks, dict) else None
        return [
            item.get("track")
            for item in items or []
            if isinstance(item, dict) and isinstance(item.get("track"), dict)
        ]

    async def activate_playlist(
        self,
        playlist: dict[str, Any],
        *,
        force_restart: bool = True,
        reason: str = "schedule",
    ) -> dict[str, Any]:
        """Activate a playlist atomically, fetching an absent catalog if needed.

        Spotify I/O happens under ``_activation_lock`` but outside
        ``_state_lock``. Playback reads and skip controls therefore remain
        responsive while a cold playlist catalog is loading.
        """

        async with self._activation_lock:
            tracks = self._tracks_for(playlist)
            if not tracks and playlist.get("spotify_uri"):
                try:
                    catalog = await self.catalog_client.fetch(str(playlist["spotify_uri"]))
                except CatalogError as exc:
                    logger.error("AutoDJ catalog load failed: %s", exc)
                    async with self._state_lock:
                        self.last_error = str(exc)
                        if self.selected_playlist and self._tracks_for(self.selected_playlist):
                            return self.snapshot()
                    raise
                playlist = dict(playlist)
                playlist.update(
                    {
                        "name": catalog.name,
                        "external_url": catalog.external_url,
                        "image_url": catalog.image_url,
                        "catalog": catalog.as_json(),
                    }
                )
                tracks = catalog.tracks
            if not tracks:
                raise CatalogError("Selected playlist has no playable tracks")

            async with self._state_lock:
                previous_uri = (self.selected_playlist or {}).get("spotify_uri")
                self.selected_playlist = dict(playlist)
                self._shuffle_bag = []
                should_restart = (
                    force_restart
                    or previous_uri != self.selected_playlist.get("spotify_uri")
                    or not self.now_playing.get("track")
                )
                if should_restart:
                    await self._populate_track_locked()
                self.now_playing["reason"] = reason
                self.last_error = None
                return self.snapshot()

    def _random_track_locked(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """Draw from a shuffle bag, avoiding immediate repeats where possible."""

        excluded = exclude or set()
        if not self._shuffle_bag:
            self._shuffle_bag = list(self._tracks_for(self.selected_playlist or {}))
            random.shuffle(self._shuffle_bag)
        candidates = [track for track in self._shuffle_bag if track.get("uri") not in excluded]
        if not candidates:
            candidates = self._shuffle_bag or self._available_tracks_locked(excluded)
        if not candidates:
            raise CatalogError("Selected playlist has no playable tracks")
        # This is entertainment shuffle state, never a security decision.
        selected = random.choice(candidates)  # noqa: S311
        self._shuffle_bag.remove(selected)
        return selected

    async def _populate_track_locked(
        self,
        has_been_playing_for_ms: int = 0,
        track: dict[str, Any] | None = None,
    ) -> None:
        """Cue a current and next track; caller must hold ``_state_lock``."""

        current = self.now_playing.get("track") or {}
        item = track or current.get("next_track") or self._random_track_locked()
        next_track = self._random_track_locked({str(item.get("uri"))})
        self.now_playing["track"] = {
            "item": item,
            "next_track": next_track,
            "progress_ms": max(0, int(has_been_playing_for_ms)),
            "is_playing": True,
            "context": {
                "type": "playlist",
                "uri": self.selected_playlist.get("spotify_uri")
                if self.selected_playlist
                else None,
            },
        }
        elapsed_seconds = max(0, has_been_playing_for_ms) / 1000
        self.now_playing["playback_started_at"] = time.time() - elapsed_seconds
        self._playback_started_monotonic = time.monotonic() - elapsed_seconds
        logger.debug("AUTODJ: Cue in -> %s", item.get("name"))

    async def populate_track(
        self,
        has_been_playing_for_ms: int = 0,
    ) -> dict[str, Any] | None:
        """Ensure an initial track exists and return the current playback object."""

        async with self._state_lock:
            if not self.selected_playlist:
                return None
            if not self.now_playing.get("track"):
                await self._populate_track_locked(has_been_playing_for_ms)
            return self.now_playing["track"]

    async def currently_playing(
        self,
        raise_exception: bool = False,
        get_next_from_context: bool = False,
    ) -> dict[str, Any] | None:
        """Return current AutoDJ playback, advancing across elapsed track ends."""

        del raise_exception, get_next_from_context
        async with self._state_lock:
            if not self.now_playing.get("track"):
                if not self.selected_playlist:
                    return None
                await self._populate_track_locked()
            else:
                track = self.now_playing["track"]
                elapsed_ms = max(
                    0,
                    int((time.monotonic() - self._playback_started_monotonic) * 1000),
                )
                # A suspended process can wake several songs late. Consume the
                # elapsed duration in one call so listeners never receive a
                # progress value beyond the current track's duration.
                for _ in range(100):
                    duration_ms = max(1, int(track["item"].get("duration_ms", 0)))
                    if elapsed_ms < duration_ms:
                        track["progress_ms"] = elapsed_ms
                        break
                    elapsed_ms -= duration_ms
                    await self._populate_track_locked(
                        elapsed_ms,
                        track=track.get("next_track"),
                    )
                    track = self.now_playing["track"]
                else:
                    # The catalog should never contain zero-length tracks, but
                    # this guard prevents corrupted legacy data from monopolizing
                    # the master loop after a very long process suspension.
                    await self._populate_track_locked()
            return self.now_playing["track"]

    async def advance_track(self, reason: str = "natural-end") -> dict[str, Any]:
        """Advance now to the queued next track."""

        async with self._state_lock:
            await self._populate_track_locked(
                track=(self.now_playing.get("track") or {}).get("next_track")
            )
            self.now_playing["reason"] = reason
            self._wake_scheduler()
            self._fast_resync_listeners()
            return self.snapshot()

    async def replace_next_track(self, reason: str = "skip-next") -> dict[str, Any]:
        """Replace only the queued next track."""

        async with self._state_lock:
            if not self.selected_playlist:
                raise CatalogError("No AutoDJ playlist is active")
            if not self.now_playing.get("track"):
                await self._populate_track_locked()
            track = self.now_playing.get("track") or {}
            current_uri = (track.get("item") or {}).get("uri")
            next_uri = (track.get("next_track") or {}).get("uri")
            self._shuffle_bag = [item for item in self._shuffle_bag if item.get("uri") != next_uri]
            track["next_track"] = self._random_track_locked({str(current_uri), str(next_uri)})
            self.now_playing["reason"] = reason
            self._fast_resync_listeners()
            return self.snapshot()

    async def skip_current(self) -> dict[str, Any]:
        """Skip the current track while preserving the queued-next invariant."""

        return await self.advance_track("skip-current")

    def _fast_resync_listeners(self) -> None:
        """Make enabled listeners compare playback again on the next beat."""

        for user in self.trt.users:
            if user is not self and getattr(user, "enabled", False):
                user.pass_sync_for_cycles = 0

    def _wake_scheduler(self) -> None:
        """Ask the coordinator to recompute wall-clock truth after a control."""

        scheduler = getattr(self.trt, "schedule_coordinator", None)
        if scheduler:
            scheduler.wake()

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe operational snapshot."""

        track = self.now_playing.get("track") or {}
        playlist = self.selected_playlist or {}
        return {
            "playlist": {
                "id": playlist.get("id"),
                "spotify_id": playlist.get("spotify_id"),
                "spotify_uri": playlist.get("spotify_uri"),
                "name": playlist.get("name"),
                "external_url": playlist.get("external_url"),
            },
            "track": track.get("item"),
            "next_track": track.get("next_track"),
            "progress_ms": track.get("progress_ms"),
            "is_playing": track.get("is_playing", False),
            "reason": self.now_playing.get("reason"),
            "track_started_at": self.now_playing.get("playback_started_at"),
            "error": self.last_error,
        }
