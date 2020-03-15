from aiohttp import web

import uuid
import json
from thatradiothing.logger import debug
# from pprint import pformat
import thatradiothing.user


class WebServer(web.Application):
    def __init__(self, thatradiothing, **kwargs):
        super().__init__(**kwargs)

        self.trt = thatradiothing

        self.router.add_route('*', '/', self.index)
        self.add_routes([
            web.get('/player', self.player),
            web.get('/logo', self.logo),
            web.get('/exit', self.exit),
            web.get('/auth', self.auth),
            web.get('/auth_return', self.auth_return),
            web.get('/logout', self.logout),
            web.get('/successful_auth', self.successful_auth),
            web.get('/devices', self.devices),
            web.post('/devices', self.set_active_device),
            web.get('/master', self.set_master_user),
            web.get('/resign', self.resign_master_user),
            web.get('/profile', self.profile),
            web.get('/enable', self.enable),
            web.get('/disable', self.disable),
            web.get('/status', self.status),
            web.get('/api/now_playing', self.now_playing)
        ])
        # web.static('/', './static')

        self.runner = web.AppRunner(self)

    async def run(self):
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.trt.port)
        return await self.site.start()

    async def index(self, request):
        user = await self.logged_in_user(request)
        if user:
            return web.HTTPTemporaryRedirect('/player', headers={'Cache-Control': 'No-Cache'})
        return web.FileResponse('./static/index.htm', headers={'Cache-Control': 'No-Cache'})

    async def player(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPTemporaryRedirect('/', headers={'Cache-Control': 'No-Cache'})
        return web.FileResponse('./static/player.htm', headers={'Cache-Control': 'No-Cache'})

    async def logo(self, request):
        return web.FileResponse('./static/logo.png')

    async def auth(self, request):
        client_id = self.trt.client_id
        scope = ' '.join(self.trt.scopes)
        redirect_uri = self.trt.url + 'auth_return'
        state = uuid.uuid4()

        redirect_to = (
            f'https://accounts.spotify.com/authorize?'
            f'response_type=code'
            f'&client_id={client_id}'
            f'&scope={scope}'
            f'&redirect_uri={redirect_uri}'
            f'&state={state}'
        )

        if request.cookies.get('state'):
            user_exists = await self.trt.find_user(session_id=request.cookies.get('state'))
            if user_exists:
                self.trt.users.remove(user_exists)

        response = web.HTTPFound(redirect_to)
        user = thatradiothing.user.User(self.trt, state, redirect_uri, client_id, self.trt.client_secret)
        debug(redirect_uri)
        self.trt.users.append(user)
        response.cookies['state'] = state
        return response

    async def auth_return(self, request):
        state = request.rel_url.query['state']
        debug(state)
        for user in self.trt.users:
            debug(str(user.session_id))
            if str(user.session_id) == str(state):
                # Second step of the auth
                user.auth_code = request.rel_url.query['code']
                result = await user.request_tokens()  # also loads user profile
                if result:
                    # await user.play('4uLU6hMCjMI75M1A2tKUQC')
                    # self.trt.master.master_user = user
                    # return web.Response(text="great success")
                    # TODO: Logout other users with same spotify profile info
                    for prev_user in self.trt.users:
                        if prev_user == user:
                            continue

                        try:
                            if prev_user.spotify_profile["id"] == user.spotify_profile["id"]:
                                self.trt.users.remove(prev_user)
                        except KeyError:
                            continue

                    return web.HTTPFound('/successful_auth')
                return web.Response(text="failed to get auth token")
        else:
            return web.Response(text="no auth")

    async def logout(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPTemporaryRedirect('/')

        if self.trt.master.master_user == user:
            await self.resign_master_user(request)

        self.trt.users.remove(user)

        return web.HTTPTemporaryRedirect('/')

    async def successful_auth(self, request):
        return web.FileResponse('./static/successful-auth.htm')

    async def exit(self, request):
        exit()

    async def logged_in_user(self, request):
        user_session_id = request.cookies.get('state', False)
        if not user_session_id:
            return None

        user = await self.trt.find_user(session_id=user_session_id)
        if not user:
            return False

        if not await user.auth_headers():
            return False

        return user

    async def devices(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized(reason="Not logged in.")

        devices = await user.list_devices()
        return web.Response(body=json.dumps(devices))

    async def set_active_device(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized(reason="Not logged in.")

        data = await request.json()

        if not data.get('device_id'):
            return web.HTTPExpectationFailed(reason="'device_id' not found.")

        device_id = data["device_id"]

        device = await user.select_device(device_id)
        if not device:
            return web.HTTPNotFound()

        return web.Response(body=json.dumps({'status': True}))

    async def set_master_user(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        if user.spotify_profile['can_be_master']:
            self.trt.master.master_user = user
            # Select Master's first device.
            # Normally 'play' does this automatically but master does not receive play API calls.
            await self.trt.master.master_user.selected_device()

            debug('NEW MASTER USER')
            debug(user.spotify_profile['display_name'])
            return web.Response(body='OK')

        return web.HTTPUnauthorized()

    async def resign_master_user(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        if user.spotify_profile['can_be_master'] and user == self.trt.master.master_user:
            self.trt.master.master_user = None  # TODO: maybe bot here ?
            return web.Response(body='OK')

        return web.HTTPUnauthorized()

    async def profile(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        is_user_master = False
        is_user_master = self.trt.master.master_user == user

        user_profile = {
            'profile': user.spotify_profile,
            'can_be_master': user.spotify_profile['can_be_master'],
            'is_master': is_user_master,
            'message': user.message
        }

        return web.Response(body=json.dumps(user_profile))

    async def enable(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        user.enabled = True
        user.play_if_paused = True

        return web.HTTPOk()

    async def disable(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        user.enabled = False
        await user.pause()  # Pause user.
        user.play_if_paused = True  # Next time enabled, it will play regardless of pause.

        return web.HTTPOk()

    async def status(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        master_user = {
            'display_name': None,
            'progress_ms': None,
            'external_url': None
        }

        if self.trt.master.master_user:
            master_user['display_name'] = self.trt.master.master_user.spotify_profile["display_name"]
            if self.trt.master.now_playing:
                master_user['progress_ms'] = self.trt.master.now_playing["progress_ms"]
            # TODO: Make it optional
            if self.trt.master.master_user.spotify_profile:
                master_user['external_url'] = self.trt.master.master_user.spotify_profile["external_urls"]["spotify"]

        payload = {
            'now_playing': self.trt.master.now_playing_track,
            'master_user': master_user,
            'listeners': self.trt.master.last_listener_count,
            'enabled': user.enabled
        }

        return web.Response(body=json.dumps(payload))

    async def now_playing(self, request):
        payload = {
            'now_playing': self.trt.master.now_playing_track
        }
        return web.Response(body=json.dumps(payload))
