"""Spotify playlist catalog adapter.

Spotify has changed playlist response shapes over time.  This module keeps
that compatibility concern outside AutoDJ and stores only normalized playable
track objects in the local catalog cache.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from thatradiothing.db import playlist_id_from_uri

# Spotify returns at most 100 items per page, so this ceiling admits playlists
# far larger than any radio rotation while ensuring a malformed or cyclic
# ``next`` link cannot spin the coordinator forever.
MAX_PLAYLIST_PAGES = 100


class CatalogError(RuntimeError):
    """Raised when Spotify cannot provide a usable playlist catalog."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Attach an optional upstream status without exposing response bodies."""

        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class PlaylistCatalog:
    """Normalized metadata and playable tracks for one Spotify playlist."""

    spotify_id: str
    spotify_uri: str
    name: str
    external_url: str
    image_url: str | None
    tracks: list[dict[str, Any]]
    revision: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return a JSON-safe catalog representation for SQLite."""

        return {
            "spotify_id": self.spotify_id,
            "spotify_uri": self.spotify_uri,
            "name": self.name,
            "external_url": self.external_url,
            "image_url": self.image_url,
            "revision": self.revision,
            "tracks": self.tracks,
        }


def normalize_track_item(item: Any) -> dict[str, Any] | None:
    """Normalize legacy/current wrappers to the compact player track shape."""

    if not isinstance(item, dict):
        return None
    track = item.get("track") or item.get("item")
    if not isinstance(track, dict):
        return None
    if track.get("type") != "track" or not track.get("uri"):
        return None
    if track.get("is_local") or track.get("is_playable") is False:
        return None
    if not isinstance(track.get("duration_ms"), (int, float)) or track["duration_ms"] <= 0:
        return None
    # Spotify track payloads include large fields such as available_markets
    # that playback never reads. Persist only the stable fields consumed by
    # the listener UI and Spotify playback commands.
    artists = track.get("artists") if isinstance(track.get("artists"), list) else []
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    return {
        "id": track.get("id"),
        "type": "track",
        "uri": str(track["uri"]),
        "name": str(track.get("name") or "Unknown track"),
        "duration_ms": int(track["duration_ms"]),
        "external_urls": track.get("external_urls") or {},
        "artists": [
            {
                "id": artist.get("id"),
                "name": artist.get("name"),
                "external_urls": artist.get("external_urls") or {},
            }
            for artist in artists
            if isinstance(artist, dict)
        ],
        "album": {
            "id": album.get("id"),
            "name": album.get("name"),
            "images": album.get("images") or [],
            "external_urls": album.get("external_urls") or {},
        },
    }


def normalize_playlist_payload(
    payload: dict[str, Any], item_pages: list[dict[str, Any]] | None = None
) -> PlaylistCatalog:
    """Normalize Spotify metadata from pre-2026 and current responses."""

    if not isinstance(payload, dict):
        raise CatalogError("Spotify returned an invalid playlist object")
    playlist_id = str(payload.get("id") or "").strip()
    if not playlist_id_from_uri(f"spotify:playlist:{playlist_id}"):
        raise CatalogError("Spotify returned a playlist without a valid ID")

    pages: list[dict[str, Any]] = []
    if isinstance(item_pages, list):
        pages.extend(item_pages)
    for key in ("items", "tracks"):
        value = payload.get(key)
        if isinstance(value, dict):
            pages.append(value)

    tracks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            track = normalize_track_item(item)
            if not track or track["uri"] in seen:
                continue
            seen.add(track["uri"])
            tracks.append(track)

    if not tracks:
        raise CatalogError("Spotify playlist contains no playable tracks")
    images = payload.get("images")
    image_url = (
        images[0].get("url")
        if isinstance(images, list) and images and isinstance(images[0], dict)
        else None
    )
    return PlaylistCatalog(
        spotify_id=playlist_id,
        spotify_uri=f"spotify:playlist:{playlist_id}",
        name=str(payload.get("name") or playlist_id),
        external_url=str(
            (payload.get("external_urls") or {}).get("spotify")
            or f"https://open.spotify.com/playlist/{playlist_id}"
        ),
        image_url=image_url,
        tracks=tracks,
        revision=payload.get("snapshot_id") or payload.get("revision"),
    )


class SpotifyCatalogClient:
    """Fetch playlist metadata/items using a dedicated app-level token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str | None = None,
    ) -> None:
        """Configure Spotify credentials and lazy HTTP/token state."""

        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.expires_at = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def session(self) -> aiohttp.ClientSession:
        """Return the owned HTTP session with bounded connect/read timeouts."""

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "thatradiothing/1.0"},
            )
        return self._session

    async def close(self) -> None:
        """Close the owned HTTP session, if one was created."""

        if self._session is not None:
            await self._session.close()
            self._session = None

    async def token(self, force: bool = False) -> str:
        """Return a cached access token or exchange configured credentials."""

        async with self._lock:
            if not force and self.access_token and time.time() < self.expires_at:
                return self.access_token
            session = await self.session()
            if self.refresh_token:
                payload = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
            else:
                payload = {"grant_type": "client_credentials"}
            try:
                async with session.post(
                    "https://accounts.spotify.com/api/token",
                    data=payload,
                    auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
                ) as response:
                    try:
                        data = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise CatalogError(
                            "Spotify token endpoint returned invalid JSON",
                            status=response.status,
                        ) from exc
                    if (
                        response.status != 200
                        or not isinstance(data, dict)
                        or not data.get("access_token")
                    ):
                        raise CatalogError(
                            f"Spotify token request failed ({response.status})",
                            status=response.status,
                        )
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise CatalogError("Spotify token request failed") from exc
            self.access_token = str(data["access_token"])
            self.expires_at = time.time() + max(
                30,
                int(data.get("expires_in", 3600)) - 60,
            )
            return self.access_token

    @staticmethod
    def _validate_api_url(url: str) -> None:
        """Prevent a pagination URL from carrying Spotify credentials off-site."""

        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.spotify.com"
            or not parsed.path.startswith("/v1/")
        ):
            raise CatalogError("Spotify returned an unsafe pagination URL")

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET Spotify JSON with bounded authentication, rate, and server retries."""

        self._validate_api_url(url)
        session = await self.session()
        force_token = False
        auth_retries = 0
        transient_retries = 0
        while True:
            token = await self.token(force=force_token)
            force_token = False
            try:
                async with session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status == 401 and auth_retries < 1:
                        auth_retries += 1
                        force_token = True
                        continue
                    if response.status == 429 and transient_retries < 2:
                        transient_retries += 1
                        try:
                            retry_after = float(response.headers.get("Retry-After", "2"))
                        except ValueError:
                            retry_after = 2.0
                        await asyncio.sleep(min(30.0, max(0.25, retry_after)))
                        continue
                    if response.status >= 500 and transient_retries < 2:
                        transient_retries += 1
                        await asyncio.sleep(0.5 * transient_retries)
                        continue
                    if response.status != 200:
                        raise CatalogError(
                            f"Spotify playlist request failed ({response.status})",
                            status=response.status,
                        )
                    try:
                        body = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise CatalogError(
                            "Spotify returned invalid playlist JSON",
                            status=response.status,
                        ) from exc
                    if not isinstance(body, dict):
                        raise CatalogError("Spotify returned an invalid JSON object")
                    return body
            except (TimeoutError, aiohttp.ClientError) as exc:
                if transient_retries < 2:
                    transient_retries += 1
                    await asyncio.sleep(0.5 * transient_retries)
                    continue
                raise CatalogError("Spotify playlist request failed") from exc

    async def fetch(self, spotify_uri: str) -> PlaylistCatalog:
        """Fetch and normalize all pages for a playlist."""

        playlist_id = playlist_id_from_uri(spotify_uri)
        if not playlist_id:
            raise CatalogError("Invalid Spotify playlist URI")
        base = f"https://api.spotify.com/v1/playlists/{playlist_id}"
        metadata = await self._get_json(base)

        # Current Spotify responses expose playlist items through /items;
        # older responses put them under metadata.tracks.  We try /items
        # first, then preserve compatibility with the older response.
        pages: list[dict[str, Any]] = []
        items_page: dict[str, Any] | None = None
        try:
            items_page = await self._get_json(f"{base}/items", {"limit": 50})
        except CatalogError as exc:
            legacy = metadata.get("tracks")
            if exc.status in {404, 405} and isinstance(legacy, dict):
                items_page = legacy
            else:
                raise
        if items_page:
            pages.append(items_page)
            next_url = items_page.get("next")
            # ``next`` is response-controlled data. Bound the walk by page count
            # and by URLs already visited so neither a cycle nor an unexpectedly
            # huge playlist can stall catalog refresh indefinitely.
            visited = {f"{base}/items"}
            while next_url and len(pages) < MAX_PLAYLIST_PAGES:
                url = str(next_url)
                if url in visited:
                    raise CatalogError("Spotify returned a repeating pagination URL")
                visited.add(url)
                page = await self._get_json(url)
                pages.append(page)
                next_url = page.get("next")
            if next_url:
                raise CatalogError(
                    f"Spotify playlist exceeds the {MAX_PLAYLIST_PAGES}-page catalog limit"
                )

        return normalize_playlist_payload(metadata, pages)
