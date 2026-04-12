"""HTTP server for thatradiothing.

This module hosts the classical aiohttp request handlers: OAuth flow, master
election, device/profile/status endpoints, etc.

Real-time ``status`` push is intentionally kept out of here — see
:mod:`thatradiothing.sio` for the Socket.IO gateway. The gateway reuses this
class's :meth:`WebServer.logged_in_user` and :meth:`WebServer.build_status_payload`
so the wire format and auth rules stay consistent across both transports.
"""

from aiohttp import web

import uuid
import json
import time
from urllib.parse import urlparse

from thatradiothing.logger import debug
import thatradiothing.user
from thatradiothing.jwt_auth import issue_auth_token, verify_auth_token
from thatradiothing.sio import SocketIOGateway


class WebServer(web.Application):
    """aiohttp application hosting HTTP routes and the Socket.IO gateway.

    The class is intentionally split into clearly-labelled sections so that
    the HTTP surface can be read independently of the cross-cutting helpers
    (CORS, cookies, JWT) and from the Socket.IO wiring, which lives in
    :class:`thatradiothing.sio.SocketIOGateway` and is merely instantiated
    here.
    """

    def __init__(self, thatradiothing, **kwargs):
        super().__init__(**kwargs)

        self.trt = thatradiothing

        self.middlewares.append(self._build_cors_middleware())
        self._register_routes()

        self.runner = web.AppRunner(self)

        # Socket.IO: real-time status push. All wiring lives in the gateway.
        self.sio_gateway = SocketIOGateway(self)

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self):
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
            web.get('/api/now_playing', self.now_playing),
            web.get('/users', self.users),
        ])

    def _build_cors_middleware(self):
        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == 'OPTIONS':
                response = web.Response(status=204)
                return self._apply_cors_headers(request, response)

            try:
                response = await handler(request)
            except web.HTTPException as ex:
                response = ex

            return self._apply_cors_headers(request, response)

        return cors_middleware

    # ------------------------------------------------------------------
    # CORS helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_origin(origin):
        if not isinstance(origin, str):
            return None

        trimmed = origin.strip()
        if not trimmed:
            return None

        return trimmed.rstrip('/')

    def _allowed_origin(self, request):
        request_origin = self._normalize_origin(request.headers.get('Origin'))
        if not request_origin:
            return None

        allowed_origins = [
            self._normalize_origin(origin)
            for origin in self.trt.cors_allowed_origins
        ]
        allowed_origins = [origin for origin in allowed_origins if origin]

        if '*' in allowed_origins:
            return request_origin

        if request_origin in allowed_origins:
            return request_origin

        return None

    def _apply_cors_headers(self, request, response):
        allowed_origin = self._allowed_origin(request)
        if not allowed_origin:
            return response

        response.headers['Access-Control-Allow-Origin'] = allowed_origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response.headers['Access-Control-Max-Age'] = '600'

        vary = response.headers.get('Vary')
        if vary:
            if 'Origin' not in vary:
                response.headers['Vary'] = f'{vary}, Origin'
        else:
            response.headers['Vary'] = 'Origin'

        if self.trt.cors_allow_credentials:
            response.headers['Access-Control-Allow-Credentials'] = 'true'

        return response

    # ------------------------------------------------------------------
    # Auth cookie + JWT helpers
    #
    # Auth is JWT-based. The JWT is stored in an HttpOnly cookie so client
    # JavaScript cannot read it; the browser attaches it automatically on
    # both HTTP requests and Socket.IO handshakes. Cross-origin requests
    # from duudey.com require a ``.duudey.com`` cookie domain and the
    # credentialed CORS headers applied above.
    # ------------------------------------------------------------------

    def _cookie_domain(self):
        domain = self.trt.auth_cookie_domain
        if isinstance(domain, str) and domain.strip():
            return domain.strip()

        host = urlparse(self.trt.url).hostname
        if host == 'duudey.com' or (isinstance(host, str) and host.endswith('.duudey.com')):
            return '.duudey.com'

        return None

    def _set_auth_cookie(self, response, user):
        if not user or not user.spotify_profile:
            return

        spotify_profile = user.spotify_profile
        spotify_id = spotify_profile.get('id') if isinstance(spotify_profile, dict) else None
        if not spotify_id:
            return

        expires_at = None
        if isinstance(user.refresh_tokens_after, (int, float)) and user.refresh_tokens_after != float('inf'):
            expires_at = int(user.refresh_tokens_after + 60)

        payload = {
            'provider': 'spotify',
            'providerUserId': spotify_id,
            'displayName': spotify_profile.get('display_name'),
            'email': spotify_profile.get('email'),
            'imageUrl': (
                spotify_profile.get('images', [{}])[0].get('url')
                if isinstance(spotify_profile.get('images'), list) and spotify_profile.get('images')
                else None
            ),
            'spotifyProfileUrl': (
                spotify_profile.get('external_urls', {}).get('spotify')
                if isinstance(spotify_profile.get('external_urls'), dict)
                else None
            ),
            'spotifyAccessToken': user.access_token,
            'spotifyRefreshToken': user.refresh_token,
            'spotifyExpiresAt': expires_at,
            'spotifyScope': user.scope,
        }

        token = issue_auth_token(
            secret=self.trt.auth_shared_jwt_secret,
            issuer=self.trt.auth_jwt_issuer,
            payload=payload,
            ttl_seconds=self.trt.auth_cookie_max_age_seconds,
        )

        cookie_kwargs = {
            'max_age': self.trt.auth_cookie_max_age_seconds,
            'httponly': True,
            'secure': self.trt.auth_cookie_secure,
            'samesite': self.trt.auth_cookie_samesite,
            'path': '/',
        }
        domain = self._cookie_domain()
        if domain:
            cookie_kwargs['domain'] = domain

        response.set_cookie(self.trt.auth_cookie_name, token, **cookie_kwargs)

    def _clear_auth_cookie(self, response):
        domain = self._cookie_domain()
        if domain:
            response.del_cookie(self.trt.auth_cookie_name, domain=domain, path='/')
            return
        response.del_cookie(self.trt.auth_cookie_name, path='/')

    def _extract_auth_token(self, request):
        auth_header = request.headers.get('Authorization', '')
        if isinstance(auth_header, str) and auth_header.lower().startswith('bearer '):
            token = auth_header.split(' ', 1)[1].strip()
            if token:
                return token

        cookie_token = request.cookies.get(self.trt.auth_cookie_name)
        if isinstance(cookie_token, str) and cookie_token.strip():
            return cookie_token.strip()

        return None

    async def _find_user_by_spotify_id(self, spotify_id):
        for user in self.trt.users:
            if not user.spotify_profile:
                continue
            if str(user.spotify_profile.get('id')) == str(spotify_id):
                return user
        return None

    def _apply_claims_to_user(self, user, claims):
        if claims.get('spotifyAccessToken'):
            user.access_token = claims.get('spotifyAccessToken')
        if claims.get('spotifyRefreshToken'):
            user.refresh_token = claims.get('spotifyRefreshToken')
        if claims.get('spotifyScope'):
            user.scope = claims.get('spotifyScope')

        expires_at = claims.get('spotifyExpiresAt')
        if isinstance(expires_at, (int, float)):
            user.refresh_tokens_after = max(float(expires_at) - 60, time.time() + 10)

        profile = user.spotify_profile or {}
        spotify_id = claims.get('providerUserId')
        if spotify_id:
            profile['id'] = spotify_id

        if claims.get('displayName'):
            profile['display_name'] = claims.get('displayName')
        if claims.get('email'):
            profile['email'] = claims.get('email')
        if claims.get('spotifyProfileUrl'):
            profile['external_urls'] = {'spotify': claims.get('spotifyProfileUrl')}
        profile['can_be_master'] = profile.get('id') in self.trt.masters_list
        user.spotify_profile = profile

    async def _user_from_jwt_claims(self, claims):
        spotify_id = claims.get('providerUserId')
        if not spotify_id:
            return None

        existing = await self._find_user_by_spotify_id(spotify_id)
        if existing:
            self._apply_claims_to_user(existing, claims)
            return existing

        if not claims.get('spotifyAccessToken'):
            return None

        user = thatradiothing.user.User(
            self.trt,
            uuid.uuid4(),
            self.trt.url + 'auth_return',
            self.trt.client_id,
            self.trt.client_secret,
        )
        self._apply_claims_to_user(user, claims)
        self.trt.users.append(user)
        return user

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self):
        """Bind the TCP site and start the Socket.IO push loop."""
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.trt.port)
        await self.site.start()
        await self.sio_gateway.start()

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

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
        response.cookies['state'] = str(state)
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

                    response = web.HTTPFound('/successful_auth')
                    self._set_auth_cookie(response, user)
                    return response
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

        response = web.HTTPTemporaryRedirect('/')
        self._clear_auth_cookie(response)
        return response

    async def successful_auth(self, request):
        return web.FileResponse('./static/successful-auth.htm')

    async def exit(self, request):
        exit()

    async def logged_in_user(self, request):
        """Resolve the currently-authenticated user for an aiohttp request.

        Two mechanisms, tried in order:

        1. **JWT**: ``Authorization: Bearer …`` header or the HttpOnly auth
           cookie. On success, claims are reconciled into an existing
           ``User`` (matched by Spotify id) or used to hydrate a new one.
        2. **Legacy ``state`` cookie** from the OAuth handshake, which maps
           directly to an in-memory ``User.session_id``.

        Returns the ``User`` or a falsy value when unauthenticated or when
        Spotify token refresh fails.

        Also used by the Socket.IO gateway via the aiohttp request exposed
        on the handshake ``environ``.
        """
        token = self._extract_auth_token(request)
        if token:
            claims = verify_auth_token(
                token=token,
                secret=self.trt.auth_shared_jwt_secret,
                issuer=self.trt.auth_jwt_issuer,
            )
            if claims:
                user_from_claims = await self._user_from_jwt_claims(claims)
                if user_from_claims and await user_from_claims.auth_headers():
                    return user_from_claims

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
        if not user or not user.spotify_profile:
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
        if not user or not user.spotify_profile:
            return web.HTTPUnauthorized()

        if user.spotify_profile['can_be_master'] and user == self.trt.master.master_user:
            self.trt.master.master_user = None  # TODO: maybe bot here ?
            return web.Response(body='OK')

        return web.HTTPUnauthorized()

    async def profile(self, request):
        user = await self.logged_in_user(request)
        if not user or not user.spotify_profile:
            return web.HTTPUnauthorized()

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

    # ------------------------------------------------------------------
    # Status payload (shared with Socket.IO gateway)
    # ------------------------------------------------------------------

    async def build_status_payload(self, user):
        """Compute the ``status`` payload for ``user``.

        Shared between the HTTP ``/status`` handler and the Socket.IO push
        loop so both transports stay byte-for-byte compatible; the gateway
        diffs consecutive payloads with ``==`` to decide whether to emit.

        Includes the per-user fields (``profile``, ``enabled``, ``devices``,
        ``is_master``, ``message``) that previously lived on ``/profile``
        and ``/devices``, letting clients drop their polling of those.
        When ``user.can_be_master`` is true the payload also embeds a
        ``users`` summary list (consumed by the listener-count hover UI).
        """
        master_user = {
            'display_name': None,
            'progress_ms': None,
            'external_url': None
        }

        if self.trt.master.master_user:
            master_profile = self.trt.master.master_user.spotify_profile or {}
            master_user['display_name'] = master_profile.get('display_name')
            if self.trt.master.now_playing:
                master_user['progress_ms'] = self.trt.master.now_playing["progress_ms"]
            external_urls = master_profile.get('external_urls') or {}
            master_user['external_url'] = external_urls.get('spotify')

        is_user_master = self.trt.master.master_user == user
        can_be_master = bool(user.spotify_profile and user.spotify_profile.get('can_be_master'))

        payload = {
            'now_playing': self.trt.master.now_playing_track,
            'master_user': master_user,
            'listeners': self.trt.master.last_listener_count,
            'enabled': user.enabled,
            'profile': user.spotify_profile,
            'can_be_master': can_be_master,
            'is_master': is_user_master,
            'message': user.message,
            'devices': await user.list_devices(),
        }

        if can_be_master:
            payload['users'] = [await u.summary() for u in self.trt.users]

        return payload

    async def status(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        payload = await self.build_status_payload(user)
        return web.Response(body=json.dumps(payload))

    async def now_playing(self, request):
        payload = {
            'now_playing': self.trt.master.now_playing_track
        }
        return web.Response(body=json.dumps(payload))

    async def users(self, request):
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        if not user.spotify_profile:
            return web.HTTPUnauthorized()

        if not user.spotify_profile["can_be_master"]:
            return web.HTTPUnauthorized()

        resp_list = []
        for user in self.trt.users:
            resp_list.append(await user.summary())

        return web.json_response(resp_list)
