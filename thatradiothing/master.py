import asyncio
import aiohttp
from logzero import logger
from pprint import pformat
import time
import math
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
        self.now_playing = ''
    
    async def beat(self):
        while True:
            #logger.info('heartbeat')
            try:
                await self.router()
            except aiohttp.client_exceptions.ClientOSError:
                pass

            await asyncio.sleep(0.4)
    
    async def router(self):
        if self.mode == self.modes["MASTER_PLAYER"]:
            await self.sync_to_master_user()

    async def sync_to_master_user(self):
        if not self.master_user:
            return

        for user in self.trt.users: # Iterate all users.
            # Pass states
            if not user.access_token: # Not logged in pass.
                continue
            if user == self.master_user: # Master user. Get now playing then pass.
                if len(self.trt.users) == 1 and user.enabled: # When there is only master.
                    master_user_playing = await self.master_user.currently_playing()
                    self.now_playing = master_user_playing['item']
                continue
            if user.pass_sync_for_cycles > 0: # Pauses user sync for X amount of cycles.
                user.pass_sync_for_cycles += -1
                continue
            if not user.enabled: # User disabled pass.
                continue
            if user.paused_cycles > 10: # Paused for too long. Disable.
                user.enabled = False
                user.paused_cycles = 0
                logger.debug('Disable user, paused for more than 10 cycles.')
                continue

            # Get master info
            
            master_user_playing = await self.master_user.currently_playing()
            request_will_start_at = time.time()
            if not master_user_playing:
                continue
            
            try:
                master_uri = master_user_playing['item']['uri']
                master_is_playing = master_user_playing['is_playing']
                master_progress = master_user_playing['progress_ms']
                master_fetched_at = master_user_playing['timestamp']
                self.now_playing = master_user_playing['item']
            except TypeError:
                continue

            if not master_is_playing:
                await user.pause()
                user.play_if_paused = True # It is paused because master is. Will play once master starts.
                continue


            # Get user info

            try:
                try:
                    user_playing = await user.currently_playing(raise_exception=True)
                
                # No playback states. Pause or No Content
                except (exceptions.NoContent, exceptions.PlaybackPaused) as exc:
                    if isinstance(exc, exceptions.PlaybackPaused): # Paused
                        if user.play_if_paused: # Hit this after re-enable, prevent pass due pause.
                            user.play_if_paused = False
                        elif master_is_playing and master_progress > 4000: # User paused it, play if paused was not triggered. TODO:this is sketchy
                            user.paused_cycles += 1
                            continue # Pass.

                    logger.debug('User is not playing, try to play.')
                    
                    request_delta = int((time.time() - request_will_start_at)*1000)
                    await user.play(uris=[master_uri], position_ms=master_progress + request_delta) # Start playback
                    continue # We are done here.
            except exceptions.NoActiveDevice:
                logger.debug('Device not found, trying to select first device.')

                await user.select_device(0, first_one=True)
                continue
            
            # User is playing. From here we will sync stuff.
            # Calculate required times.
            request_delta = int((time.time() - request_will_start_at)*1000)
            timestamp_delta = math.floor((user_playing['timestamp'] - master_fetched_at)/100)
            fine_progress_ms = master_progress + request_delta
            
            # User is not playing same thing as master. Start playback. (play)
            if user_playing['item']['uri'] != master_uri:
                logger.debug('User not playing correct uri, play.')
                await user.play(uris=[master_uri], position_ms=fine_progress_ms)
                continue
            
            # User's deltas are outside tolerances. Do time sync. (seek)
            if user_playing['progress_ms'] > master_progress + self.trt.realtime_tolerance_ms or user_playing['progress_ms'] < master_progress - self.trt.realtime_tolerance_ms:
                
                logger.debug((
                    "User time sync.\n"
                    f"> master: { master_progress }\n"
                    f"> user:   { user_playing['progress_ms'] }\n"
                    f"> delta:  {(master_progress - user_playing['progress_ms'])/1000} seconds\n"
                    f"| Master Progress | {master_progress}\n"
                    f"| TimeStamp Delta | {timestamp_delta}\n"
                    f"|  Request Delta  | {request_delta}\n"
                    f"|  Fine Progress  | {fine_progress_ms}\n"))
                
                await user.seek(fine_progress_ms)
                continue
            
            # User is in sync and everything is OK. (there was no continue trigger.)
            user.pass_sync_for_cycles = 8 # will not do user check for X cycles TODO: Smart duration