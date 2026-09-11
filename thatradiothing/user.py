"""Represent one Spotify listener and wrap their Web API operations."""

import asyncio
import datetime
import re
import time
from pprint import pformat
from urllib.parse import urlparse

import aiohttp
from logzero import logger

import thatradiothing.exceptions as exceptions


def _normalized_items_page(page):
    """Normalize one Spotify items page to the legacy ``tracks`` shape.

    Spotify's current playlist endpoints return ``items[].item`` while older
    releases returned ``items[].track``; both shapes are accepted here and
    emitted as ``{"items": [{"track": ...}], "next": ...}``, which is what the
    master next-track helper consumes. Entries whose track is missing or not an
    object are dropped rather than propagated as ``None``.

    Keeping this in one place matters: the shape moving underneath us is the
    entire reason the normalization exists, so a future move must be handled in
    exactly one function.
    """

    items = []
    for entry in page.get("items", []) or []:
        if not isinstance(entry, dict):
            continue
        track = entry.get("item") or entry.get("track")
        if isinstance(track, dict):
            items.append({"track": track})
    return {"items": items, "next": page.get("next")}


class User:
    """Store listener session state and perform authenticated Spotify calls."""

    def __init__(self, trt, session_id, redirect_uri, client_id, client_secret):
        """Initialize OAuth, device, playback, and transient UI state."""

        self.trt = trt
        self.api = "https://api.spotify.com"
        self.session_id = session_id
        self.redirect_uri = redirect_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_code = None
        self.access_token = None
        self.token_type = None
        self.scope = None
        self.expires_in = None
        self.refresh_token = None
        self.last_refresh = None
        self.refresh_tokens_after = float("inf")
        self._aiohttp_session = None
        self._token_refresh_lock = asyncio.Lock()
        self._selected_device = None
        self.play_if_paused = True  # Disregard user's pause state and start playback.
        self.enabled = True
        self.paused_cycles = 0

        self.spotify_profile = None
        self.message = ""

        self.pass_sync_for_cycles = 0

        self._devices_cache = None
        self._devices_cache_expires_at = 0.0

        # While a user has just hit "play" but no Spotify device is online
        # yet, we enter a short "waiting for device" window: the devices
        # cache TTL collapses to a fast value so a newly-opened client
        # appears in the list within seconds, and the sync loop suppresses
        # its usual "no device, disable user" fallback until the window
        # expires.
        self.waiting_for_device_until = 0.0

        # Last time the user took an action (enable/disable/master/device
        # select). Used by the idle sweep to clear stale ``message`` text
        # for users who disabled playback and then wandered off — so they
        # don't return a day later and see yesterday's warning.
        self.last_interaction_at = time.time()

        # Optional expiry for ``message``. Short-lived explanatory text
        # (e.g. the "no device available" terminal state) sets this so it
        # disappears from the UI shortly after the user has had a chance
        # to see it. 0 means "no expiry".
        self.message_expires_at = 0.0

    DEVICES_TTL_WITH_DEVICES_SECONDS = 90
    DEVICES_TTL_EMPTY_SECONDS = 15
    DEVICES_TTL_EMPTY_WHILE_WAITING_SECONDS = 2
    WAITING_FOR_DEVICE_WINDOW_SECONDS = 30
    IDLE_MESSAGE_RESET_SECONDS = 30 * 60

    def touch_interaction(self):
        """Record activity so idle-state cleanup does not clear fresh feedback."""

        self.last_interaction_at = time.time()

    def set_transient_message(self, text, ttl_seconds=10):
        """Set ``message`` so it auto-expires after ``ttl_seconds``.

        Use for short-lived explanatory text the UI should drop once the
        user has had time to read it (e.g. terminal error states).
        """
        self.message = text
        self.message_expires_at = time.time() + ttl_seconds

    def expire_message_if_due(self):
        """Clear the listener message after its optional expiry time."""

        if self.message_expires_at and time.time() >= self.message_expires_at:
            self.message = ""
            self.message_expires_at = 0.0

    def is_idle_disabled(self):
        """Return whether this disabled listener has been inactive long enough."""

        return (
            not self.enabled
            and not self.is_waiting_for_device()
            and (time.time() - self.last_interaction_at) > self.IDLE_MESSAGE_RESET_SECONDS
        )

    def is_waiting_for_device(self):
        """Return whether the short device-discovery grace period is active."""

        return time.time() < self.waiting_for_device_until

    def begin_waiting_for_device(self):
        """Start device discovery and invalidate the cached device list."""

        self.waiting_for_device_until = time.time() + self.WAITING_FOR_DEVICE_WINDOW_SECONDS
        # Invalidate the cache so the next list_devices call hits Spotify.
        self._devices_cache = None
        self._devices_cache_expires_at = 0.0

    def end_waiting_for_device(self):
        """End the device-discovery grace period."""

        self.waiting_for_device_until = 0.0

    def _is_selected_device(self, device):
        """Return whether *device* is this listener's chosen playback target.

        An explicit ``is not None`` rather than a falsy check: no device may
        compare equal to "nothing selected", and a sentinel string would match
        a real device that happened to carry that ID.
        """

        return self._selected_device is not None and str(device["id"]) == str(self._selected_device)

    async def aiohttp_session(self):
        """Return this listener's reusable, bounded HTTP client session."""

        if not self._aiohttp_session:
            self._aiohttp_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

        return self._aiohttp_session

    async def request_tokens(self):
        """Exchange the OAuth authorization code and load the Spotify profile."""

        tokens_url = "https://accounts.spotify.com/api/token"
        logger.info(self.redirect_uri)
        payload = {
            "grant_type": "authorization_code",
            "code": self.auth_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        session = await self.aiohttp_session()
        async with session.post(tokens_url, data=payload) as response:
            if response.status != 200:
                logger.error(await response.text())
                return False

            data = await response.json(content_type=None)

            if data.get("error", False):
                logger.error(data["error"])
                logger.error(pformat(data))
                return False

            self.access_token = data["access_token"]
            self.token_type = data["token_type"]
            self.scope = data["scope"]
            self.expires_in = int(data["expires_in"])
            self.refresh_tokens_after = time.time() + self.expires_in - 60
            self.refresh_token = data["refresh_token"]
            self.last_refresh = datetime.datetime.now(datetime.UTC)

            # The profile load is part of the contract, not a nice-to-have:
            # ``spotify_profile`` is what identifies this session's account,
            # and ``server._set_auth_cookie`` refuses to mint a JWT without
            # it. Reporting success here while the profile is still ``None``
            # produced a login that appeared to work and then silently
            # dropped the user back on the login page — and, before the
            # callback was hardened, a 500. Propagate the failure instead.
            if not await self.users_profile():
                logger.error(
                    "Spotify token exchange succeeded but /v1/me did not return a profile; "
                    "treating the login as failed."
                )
                return False

            return True

    async def refresh_tokens(self):
        """Refresh the Spotify access token using this listener's refresh token."""

        tokens_url = "https://accounts.spotify.com/api/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        session = await self.aiohttp_session()
        async with session.post(tokens_url, data=payload) as response:
            if response.status != 200:
                logger.error(await response.text())
                return None

            data = await response.json(content_type=None)

            if data.get("error", False):
                logger.error(data["error"])
                logger.error(pformat(data))
                return False

            self.access_token = data["access_token"]
            self.expires_in = int(data["expires_in"])
            self.refresh_tokens_after = time.time() + self.expires_in - 60
            self.scope = data.get("scope", self.scope)
            self.token_type = data.get("token_type", self.token_type or "Bearer")
            if data.get("refresh_token"):
                self.refresh_token = data["refresh_token"]
            return True

    async def auth_headers(self):
        """Return a fresh authorization header or disable an invalid session."""

        if time.time() > self.refresh_tokens_after:
            # Multiple status/playback calls can discover expiry together. Only
            # one may exchange the refresh token; followers reuse its result.
            async with self._token_refresh_lock:
                if time.time() > self.refresh_tokens_after and not await self.refresh_tokens():
                    logger.error("Spotify authentication refresh failed")
                    self.enabled = False
                    return None
        if not self.access_token:
            return None
        return {"Authorization": "Bearer " + self.access_token}

    async def force_refresh_on_unauthorized(self):
        """Force a token refresh in response to a 401 from Spotify.

        Defence-in-depth against the silent-expiry bug: the proactive check
        in ``auth_headers`` handles the common time-based expiry case, but
        clock skew or a revoked-then-reissued token can still surface as a
        401. One forced refresh + retry keeps the sync loop from breaking.
        Returns True if a fresh access token is now in place.
        """
        async with self._token_refresh_lock:
            refreshed = await self.refresh_tokens()
        if not refreshed:
            return False
        return bool(self.access_token)

    async def play(self, uris=None, position_ms=None):
        """Start the supplied tracks on the selected device and position."""

        play_url = self.api + "/v1/me/player/play"
        selected_device = await self.selected_device()
        if not selected_device:
            return False

        payload = {"uris": uris, "position_ms": position_ms if position_ms else 0}
        session = await self.aiohttp_session()
        for attempt in range(2):
            headers = await self.auth_headers()
            if headers is None:
                return False
            async with session.put(
                play_url,
                params={"device_id": selected_device},
                json=payload,
                headers=headers,
            ) as response:
                if response.status == 401 and attempt == 0:
                    if not await self.force_refresh_on_unauthorized():
                        raise exceptions.OtherError(await response.text())
                    continue
                if response.status == 404:
                    raise exceptions.NoActiveDevice()
                if response.status == 403:
                    raise exceptions.PremiumRequired()
                if response.status != 204:
                    raise exceptions.OtherError(await response.text())
                return True

    async def seek(self, position_ms):
        """Seek the current listener device to *position_ms*."""

        seek_url = self.api + "/v1/me/player/seek"
        session = await self.aiohttp_session()
        for attempt in range(2):
            headers = await self.auth_headers()
            if headers is None:
                return False
            async with session.put(
                seek_url, params={"position_ms": position_ms}, headers=headers
            ) as response:
                if response.status == 401 and attempt == 0:
                    if not await self.force_refresh_on_unauthorized():
                        return False
                    continue
                return response.status == 204

    async def queue(self, uri):
        """Append *uri* to the listener's Spotify playback queue."""

        queue_url = self.api + "/v1/me/player/queue"

        session = await self.aiohttp_session()
        for attempt in range(2):
            headers = await self.auth_headers()
            if headers is None:
                return False
            async with session.post(queue_url, params={"uri": uri}, headers=headers) as response:
                if response.status == 401 and attempt == 0:
                    if not await self.force_refresh_on_unauthorized():
                        return False
                    continue
                return response.status == 204

    async def currently_playing(self, raise_exception=False, get_next_from_context=False):
        """Return current playback, optionally raising user-level empty states."""

        currently_playing_url = self.api + "/v1/me/player/currently-playing"
        session = await self.aiohttp_session()
        response = None
        for attempt in range(2):
            headers = await self.auth_headers()
            if headers is None:
                return None
            async with session.get(currently_playing_url, headers=headers) as resp:
                if resp.status == 401 and attempt == 0:
                    if not await self.force_refresh_on_unauthorized():
                        return None
                    continue
                response = resp
                if response.status == 204:
                    if raise_exception:
                        raise exceptions.NoContent("Nothing is playing")
                    return None

                data = await response.json(content_type=None)
                if not data or not isinstance(data, dict):
                    if raise_exception:
                        raise exceptions.NoActiveDevice()
                    return None

                if raise_exception and not data.get("is_playing"):
                    raise exceptions.PlaybackPaused()

                if get_next_from_context and data and "context" in data:
                    next_track = await self.next_from_context(data["context"], data["item"])
                    data["next_track"] = next_track
                return data

    async def next_from_context(self, context, current_track):
        """Find the track after *current_track* in a supported playback context."""

        if not context or "type" not in context:
            return None

        if context["type"] == "playlist":
            playlist = await self.get_playlist(context["uri"])
            if not playlist or not playlist.get("tracks") or not playlist["tracks"].get("items"):
                return None

            return await self.get_next_track(playlist["tracks"]["items"], current_track)

        return None

    async def get_next_track(self, collection, current_track):
        """Return the item immediately following *current_track* in *collection*."""

        grab_next = False
        for item in collection:
            if grab_next:
                return item["track"]
            if item["track"]["uri"] == current_track["uri"]:
                grab_next = True

        return None

    async def get_playlist(self, uri):
        """Fetch and normalize the Spotify playlist identified by *uri*."""

        playlist_id_r = r"playlist:(.*)"
        match = re.search(playlist_id_r, uri)
        if not match:
            return None
        playlist_id = match.group(1)
        playlist_url = self.api + f"/v1/playlists/{playlist_id}"
        headers = await self.auth_headers()
        if headers is None:
            return None
        session = await self.aiohttp_session()
        async with session.get(playlist_url, headers=headers) as response:
            if response.status != 200:
                return None

            playlist = await response.json(content_type=None)

            # Spotify's current playlist contract exposes contents as
            # ``items.items[].item``; older releases returned
            # ``tracks.items[].track``. Normalize the new shape to the
            # legacy structure used by the master next-track helper.
            if not isinstance(playlist.get("tracks"), dict) and isinstance(
                playlist.get("items"), dict
            ):
                playlist["tracks"] = _normalized_items_page(playlist.pop("items"))

            # If metadata did not include item contents, fetch the dedicated
            # current endpoint. The fallback keeps old public-playlist
            # behavior working for grandfathered Spotify applications.
            tracks_payload = (
                playlist.get("tracks") if isinstance(playlist.get("tracks"), dict) else {}
            )
            if not tracks_payload.get("items"):
                items_url = self.api + f"/v1/playlists/{playlist_id}/items"
                async with session.get(
                    items_url, headers=headers, params={"limit": 50}
                ) as items_response:
                    if items_response.status == 200:
                        current_items = await items_response.json(content_type=None)
                        if isinstance(current_items, dict):
                            playlist["tracks"] = _normalized_items_page(current_items)

            if playlist["tracks"]["next"]:
                more_tracks = await self.get_more_playlist_tracks(
                    playlist["tracks"]["next"], session, headers
                )
                if more_tracks:
                    playlist["tracks"]["items"].extend(more_tracks)

            return playlist

    async def get_more_playlist_tracks(self, url, session, headers):
        """Fetch bounded same-origin pagination pages and normalize their items."""

        items = []
        next_url = url
        visited = set()
        for _page_number in range(500):
            if not self._is_spotify_api_url(next_url) or next_url in visited:
                logger.error("Rejected unsafe or cyclic Spotify pagination URL")
                return items or None
            visited.add(next_url)
            async with session.get(next_url, headers=headers) as response:
                if response.status != 200:
                    return items or None
                pagination = await response.json(content_type=None)
            if not isinstance(pagination, dict):
                return items or None
            items.extend(_normalized_items_page(pagination)["items"])
            next_url = pagination.get("next")
            if not next_url:
                return items
        logger.error("Spotify playlist pagination exceeded 500 pages")
        return items

    @staticmethod
    def _is_spotify_api_url(url):
        """Return whether *url* is a safe HTTPS Spotify Web API endpoint."""

        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.spotify.com"
            and parsed.path.startswith("/v1/")
            and parsed.username is None
            and parsed.password is None
        )

    async def pause(self):
        """Pause playback on the listener's active Spotify device."""

        pause_url = self.api + "/v1/me/player/pause"
        session = await self.aiohttp_session()
        for attempt in range(2):
            headers = await self.auth_headers()
            if headers is None:
                return False
            async with session.put(pause_url, headers=headers) as response:
                if response.status == 401 and attempt == 0:
                    if not await self.force_refresh_on_unauthorized():
                        return False
                    continue
                return response.status == 204

    async def list_devices(self):
        """Return the Spotify devices payload, always shaped ``{"devices": [...]}``.

        Callers rely on indexing ``result["devices"]``; we normalise the
        non-200 / malformed-response cases to an empty-list envelope instead
        of a bare ``[]`` so those sites don't explode.
        """
        now = time.monotonic()
        if self._devices_cache is not None and now < self._devices_cache_expires_at:
            cached = self._devices_cache
            for device in cached.get("devices", []):
                device["selected_device"] = self._is_selected_device(device)
            return cached

        devices_url = self.api + "/v1/me/player/devices"
        headers = await self.auth_headers()
        session = await self.aiohttp_session()

        async with session.get(devices_url, headers=headers) as response:
            if response.status != 200:
                logger.debug("User has no devices")
                logger.debug(pformat(await response.text()))
                empty = {"devices": []}
                self._devices_cache = empty
                self._devices_cache_expires_at = now + self.DEVICES_TTL_EMPTY_SECONDS
                return empty

            body = await response.json(content_type=None)
            device_list = body.get("devices", []) if isinstance(body, dict) else []
            for device in device_list:
                device["selected_device"] = self._is_selected_device(device)

            normalized = {"devices": device_list}
            if device_list:
                ttl = self.DEVICES_TTL_WITH_DEVICES_SECONDS
            elif self.is_waiting_for_device():
                ttl = self.DEVICES_TTL_EMPTY_WHILE_WAITING_SECONDS
            else:
                ttl = self.DEVICES_TTL_EMPTY_SECONDS
            self._devices_cache = normalized
            self._devices_cache_expires_at = now + ttl
            return normalized

    async def select_device(self, dev_id, first_one=False):
        """Select a matching device, or the first device when requested."""

        devices = await self.list_devices()
        currently_playing = await self.currently_playing()
        for device in devices["devices"]:
            if str(device["id"]) == str(dev_id) or first_one:
                self._selected_device = device["id"]
                if currently_playing:
                    await self.transfer_playback(str(device["id"]))
                return device
        return False

    async def selected_device(self):
        """Return the selected device, choosing the first available if needed."""

        if self._selected_device:
            return self._selected_device

        result = await self.select_device(0, True)

        if result:
            return self._selected_device

        return False

    async def users_profile(self):
        """Load the listener's Spotify profile and master eligibility."""

        user_profile_url = self.api + "/v1/me"

        headers = await self.auth_headers()
        if headers is None:
            logger.error("Cannot load the Spotify profile: no usable access token.")
            return False
        session = await self.aiohttp_session()
        async with session.get(user_profile_url, headers=headers) as response:
            if response.status != 200:
                # Log the status and body, because the failure modes here are
                # not interchangeable and the bare ``return False`` made them
                # impossible to tell apart from the logs: 429 is rate
                # limiting, 5xx is Spotify being down, 401 means the token we
                # were just handed is already being rejected.
                #
                # Note what a failure here does *not* mean. An app still in
                # development mode rejects non-allow-listed accounts at the
                # /authorize step, so they never reach the token exchange and
                # never reach this call at all. By the time we are here, a
                # code was issued and successfully traded for tokens.
                logger.error(f"Spotify /v1/me returned {response.status}: {await response.text()}")
                return False

            self.spotify_profile = await response.json(content_type=None)
            self.spotify_profile["can_be_master"] = False
            if self.spotify_profile["id"] in self.trt.masters_list:
                self.spotify_profile["can_be_master"] = True

            return self.spotify_profile

    async def transfer_playback(self, dev_id, play=True):
        """Transfer playback to *dev_id* and optionally begin playing."""

        transfer_playback_url = self.api + "/v1/me/player"

        headers = await self.auth_headers()
        if headers is None:
            return False
        session = await self.aiohttp_session()
        payload = {"device_ids": [dev_id], "play": play}

        async with session.put(transfer_playback_url, json=payload, headers=headers) as response:
            if response.status != 204:
                logger.debug("TRANSFER FAIL")
                logger.debug(pformat(response))
                logger.debug(pformat(await response.text()))
                return False

            return True

    async def summary(self):
        """Return the listener fields exposed to an authorized master."""

        return {
            "session_id": str(self.session_id),
            "selected_device": self._selected_device,
            "play_if_paused": self.play_if_paused,
            "enabled": self.enabled,
            "paused_cycles": self.paused_cycles,
            "spotify_profile": self.spotify_profile,
            "pass_sync_for_cycles": self.pass_sync_for_cycles,
        }
