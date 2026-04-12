import asyncio
import aiohttp
from logzero import logger
# from pprint import pformat
import time
import math
import json
# import math
import thatradiothing.exceptions as exceptions


class Master:
    def __init__(self, thatradiothing):
        self.trt = thatradiothing
        self.master_user = None
        self.mode = 0
        self.old_mode = 0
        self.modes = {
            'MASTER_PLAYER': 0
        }
        self.now_playing_track = None
        self.now_playing = None
        self.next_track = None
        self.last_listener_count = 0
        self.last_beat_duration = 0

    async def beat(self):
        # Do async preparations here.
        await self.trt.autodj.populate()

        while True:
            # logger.info('heartbeat')
            beat_start = time.time()
            try:
                await self.router()
            except aiohttp.client_exceptions.ClientOSError:
                pass

            await asyncio.sleep(0.4)
            self.last_beat_duration = time.time() - beat_start

    async def router(self):
        if not self.master_user:
            if self.trt.autodj:
                await self.trt.autodj.populate_track()
                self.master_user = self.trt.autodj

        if self.mode == self.modes["MASTER_PLAYER"]:
            await self.sync_to_master_user()

    async def start_playback_to_user(self, user, uri, position_ms):
        uris = [uri]
        if self.next_playing:
            uris.append(self.next_playing['uri'])
        logger.debug(f"""SENDING PLAY COMMAND: {json.dumps(uris)}
POS: {position_ms}""")

        await user.play(uris=uris, position_ms=position_ms)  # Start playback

    async def sync_to_master_user(self):
        """One sync cycle across all users.

        Spotify calls are minimized by fetching ``master.currently_playing``
        *once* per cycle and sharing it across every listener that needs a
        sync this tick. Latency compensation is unaffected: we stamp the
        fetch with ``request_age`` (monotonic wall time) and each listener's
        ``sync_to_master_user_single`` re-computes ``request_delta =
        now - request_age`` at its own send time, so the progress offset
        stays accurate regardless of how long the shared fetch took or how
        many listeners run in parallel.
        """
        if not self.master_user:
            return

        # First pass: classify users; no Spotify calls yet.
        active_listeners = []
        listeners = 0
        for user in self.trt.users:
            if not user.access_token:  # Not logged in.
                continue

            if user == self.master_user:
                continue  # Meta refresh for master is handled below.

            if not user.enabled:
                continue

            if user.paused_cycles > 10:  # Paused too long; disable.
                user.enabled = False
                user.paused_cycles = 0
                logger.debug(f"Disable user: {user.spotify_profile['display_name']}, paused for more than 10 cycles.")
                continue

            listeners += 1

            if user.pass_sync_for_cycles > 0:  # Skip sync for this listener this tick.
                user.pass_sync_for_cycles -= 1
                continue

            active_listeners.append(user)

        self.last_listener_count = listeners

        # If nobody needs a sync this tick, only refresh master meta when
        # the master is alone (so the UI's now_playing stays current).
        if not active_listeners:
            if listeners == 0:
                master_user_playing = await self.master_user.currently_playing(get_next_from_context=True)
                if master_user_playing:
                    self.now_playing_track = master_user_playing['item']
                    self.now_playing = master_user_playing
                    self.next_playing = master_user_playing['next_track']
            return

        # Single master fetch for this tick, shared across all active listeners.
        master_user_playing = await self.master_user.currently_playing(get_next_from_context=True)
        request_age = time.time()
        if not master_user_playing:
            return

        self.now_playing_track = master_user_playing['item']
        self.now_playing = master_user_playing
        self.next_playing = master_user_playing['next_track']

        coroutines = [
            self.sync_to_master_user_single(master_user_playing, request_age, user)
            for user in active_listeners
        ]
        for result in await asyncio.gather(*coroutines, return_exceptions=True):
            if isinstance(result, Exception):
                logger.error(result)

    async def sync_to_master_user_single(self, master_user_playing, request_age, user):
        try:
            master_uri = master_user_playing['item']['uri']
            master_is_playing = master_user_playing['is_playing']
            master_progress = master_user_playing['progress_ms']
            # master_fetched_at = master_user_playing['timestamp']
        except TypeError as error:
            logger.error(error)
            return

        if not master_is_playing:
            await user.pause()
            user.play_if_paused = True  # It is paused because master is. Will play once master starts.
            return

        # Get user info
        select_device = await user.selected_device()  # This will also try to select.
        if not select_device:
            user.enabled = False
            user.message = "Please open spotify from one of your devices."
            return

        try:
            user_playing = await user.currently_playing(raise_exception=True)

        # No playback states. Pause or No Content
        except (exceptions.NoContent, exceptions.PlaybackPaused) as exc:
            if isinstance(exc, exceptions.PlaybackPaused):  # Paused
                if user.play_if_paused:  # Hit this after re-enable, prevent pass due pause.
                    user.play_if_paused = False
                elif master_is_playing and master_progress > 4000:  # TODO: Sketchy, User paused it, play if paused was not triggered. TODO:this is sketchy
                    user.paused_cycles += 1
                    return  # Pass.
            """
            request_delta = int((time.time() - request_age)*1000)
            await self.start_playback_to_user(user, master_uri, master_progress + request_delta)
            return # We are done here.
            """
            logger.debug('User is not playing, setting flag to try to play.')
            user_playing = None  # This will trigger playback.

        # User was not paused.
        user.paused_cycles = 0

        # From here on we will sync stuff.
        # Calculate required times.
        request_delta = int((time.time() - request_age) * 1000)
        # timestamp_delta = math.floor((user_playing['timestamp'] - master_fetched_at)/100)
        fine_progress_ms = master_progress + request_delta

        # User is not playing at all or not playing same thing as master. Start playback. (play)
        if not user_playing or user_playing['item']['uri'] != master_uri:
            logger.debug(user.spotify_profile['display_name'])
            if not user_playing:
                logger.debug('User not playing anything, play.')
            else:
                logger.debug(f"User not playing correct URI: {user_playing['item']['name']}, play: {master_user_playing['item']['name']}")

            try:
                await self.start_playback_to_user(user, master_uri, fine_progress_ms)
                return
            except exceptions.NoActiveDevice:
                logger.debug('Device not found, trying to select the first device.')
                selected = await user.select_device(0, first_one=True)
                if not selected:
                    user.enabled = False
                    user.message = "Please open spotify from one of your devices."
                return

        # User's deltas are outside tolerances. Do time sync. (seek)
        if user_playing['progress_ms'] > master_progress + self.trt.realtime_tolerance_ms or user_playing['progress_ms'] < master_progress - self.trt.realtime_tolerance_ms:

            logger.debug((
                "User time sync.\n"
                f"{user.spotify_profile['display_name']}\n---\n"
                f"> master: { master_progress }\n"
                f"> user:   { user_playing['progress_ms'] }\n"
                f"> delta:  {(master_progress - user_playing['progress_ms'])/1000} seconds\n"
                f"| Master Progress | {master_progress}\n"
                # f"| TimeStamp Delta | {timestamp_delta}\n"
                f"|  Request Delta  | {request_delta}\n"
                f"|  Fine Progress  | {fine_progress_ms}\n"))

            await user.seek(fine_progress_ms)
            return

        # User is in sync and everything is OK. (there was no continue trigger.)
        user.message = ''

        remaining = int((user_playing['item']['duration_ms'] - user_playing['progress_ms']) / 1000)
        # logger.debug(remaining)
        # logger.debug(self.last_beat_duration * 8)

        if remaining < math.ceil(self.last_beat_duration * 8):
            user.pass_sync_for_cycles = int(math.floor(remaining / self.last_beat_duration))
            logger.debug(
                f"""Less than {math.ceil(self.last_beat_duration * 8)} cycles before track end.
                Setting new pass amount: {user.pass_sync_for_cycles}""")
        else:
            user.pass_sync_for_cycles = 8  # will not do user check for X cycles
