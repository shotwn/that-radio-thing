import json
import aiohttp
import datetime
import thatradiothing.exceptions as exceptions
from logzero import logger
from pprint import pformat

class User:
    def __init__(self, trt, session_id, redirect_uri, client_id, client_secret):
        self.trt = trt
        self.api = 'https://api.spotify.com'
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
        self._aiohttp_session = None
        self._selected_device = None
        self.play_if_paused = True # Disregard user's pause state and start playback.
        self.enabled = True
        self.paused_cycles = 0

        self.pass_sync_for_cycles = 0

    async def aiohttp_session(self):
        if not self._aiohttp_session:
            self._aiohttp_session = aiohttp.ClientSession()
        
        return self._aiohttp_session
    async def request_tokens(self):
        tokens_url = 'https://accounts.spotify.com/api/token'
        logger.info(self.redirect_uri)
        payload = {
            "grant_type": 'authorization_code',
            "code": self.auth_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        session = await self.aiohttp_session()
        async with session.post(tokens_url, data=payload) as response:
            if response.status != 200:
                logger.error(await response.text())
                return False
            
            
            data = await response.json(content_type = None)
            
            if data.get('error', False):
                logger.error(data['error'])
                logger.error(pformat(data))
                return False
            
            self.access_token = data["access_token"]
            self.token_type = data["token_type"]
            self.scope = data["scope"]
            self.expires_in = int(data["expires_in"])
            self.refresh_token = data["refresh_token"]
            self.last_refresh = datetime.datetime.now()

            await self.users_profile()
            logger.info(pformat(vars(self)))
            return True

    async def auth_headers(self):
        return {'Authorization': 'Bearer '+self.access_token}

    async def play(self, uris=None, position_ms=None):
        play_url = 	self.api+'/v1/me/player/play'
        selected_device = await self.selected_device()
        if not selected_device:
            return
        
        play_url += '?device_id='+selected_device

        payload = {
            'uris': uris,
            'position_ms': position_ms if position_ms else 0
        }
        headers = await self.auth_headers()
        session = await self.aiohttp_session()
        async with session.put(play_url, json=payload, headers=headers) as response:
            if response.status == 404:
                raise exceptions.NoActiveDevice()
            if response.status == 403:
                raise exceptions.PremiumRequired()

            if response.status != 204:
                raise exceptions.OtherError(await response.text())
            return True
    
    async def seek(self, position_ms):
        seek_url = self.api + '/v1/me/player/seek'
        headers = await self.auth_headers()
        session = await self.aiohttp_session()
        async with session.put(seek_url+f'?position_ms={position_ms}', headers=headers) as response:
            if response.status != 204:
                return False
            return True

    async def currently_playing(self, raise_exception = False):
        currently_playing_url = 'https://api.spotify.com/v1/me/player/currently-playing'
        session = await self.aiohttp_session()
        headers = await self.auth_headers()
        async with session.get(currently_playing_url, headers=headers) as response:
            if response.status == 204:
                if raise_exception:
                    raise exceptions.NoContent('Nothing is playing')
                return None

            data = await response.json(content_type = None)
            if not data and raise_exception:
                raise exceptions.NoActiveDevice()
            
            if raise_exception:
                if not data['is_playing']:
                    raise exceptions.PlaybackPaused()

            return data
    
    async def pause(self):
        pause_url = 'https://api.spotify.com/v1/me/player/pause'
        headers = await self.auth_headers()
        session = await self.aiohttp_session()
        async with session.put(pause_url, headers=headers) as response:
            if response.status != 204:
                return False
            return True
    
    async def list_devices(self):
        devices_url = self.api+'/v1/me/player/devices'
        headers = await self.auth_headers()
        session = await self.aiohttp_session()

        async with session.get(devices_url, headers=headers) as response:
            if response.status != 200:
                logger.debug("User has no devices")
                logger.debug(pformat(await response.text()))
                return []
            
            devices = await response.json(content_type=None)
            for device in devices["devices"]:
                device["selected_device"] = (str(device['id']) == str(self._selected_device if self._selected_device else ' NONE '))
            return devices
    
    async def select_device(self, dev_id, first_one = False):
        devices = await self.list_devices()
        currently_playing = await self.currently_playing()
        for device in devices["devices"]:
            if str(device['id']) == str(dev_id) or first_one:
                self._selected_device = device['id']
                if currently_playing:
                    await self.transfer_playback(str(device['id']))
                return device
        else:
            return False

    async def selected_device(self):
        if self._selected_device:
            return self._selected_device
        
        result = await self.select_device(0, True)
    
        if result:
            return self._selected_device

        return False
    
    async def users_profile(self):
        user_profile_url = self.api + '/v1/me'
        
        headers = await self.auth_headers()
        session = await self.aiohttp_session()
        async with session.get(user_profile_url, headers=headers) as response:
            if response.status != 200:
                return False
            
            self.spotify_profile = await response.json(content_type=None)
            self.spotify_profile['can_be_master'] = False
            if self.spotify_profile['id'] in self.trt.masters_list:
                self.spotify_profile['can_be_master'] = True

            return self.spotify_profile
        
    async def transfer_playback(self, dev_id, play=True):
        transfer_playback_url = self.api + '/v1/me/player'

        headers = await self.auth_headers()
        session = await self.aiohttp_session()
        payload = {
            'device_ids': [dev_id],
            'play': play
        }

        async with session.put(transfer_playback_url, data=json.dumps(payload), headers=headers) as response:
            if response.status != 204:
                logger.debug('TRANSFER FAIL')
                logger.debug(pformat(response))
                logger.debug(pformat(await response.text()))
                return False

            return True