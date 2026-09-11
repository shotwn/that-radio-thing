"""HTTP server for thatradiothing.

This module hosts the classical aiohttp request handlers: OAuth flow, master
election, device/profile/status endpoints, etc.

Real-time ``status`` push is intentionally kept out of here — see
:mod:`thatradiothing.sio` for the Socket.IO gateway. The gateway reuses this
class's :meth:`WebServer.logged_in_user` and :meth:`WebServer.build_status_payload`
so the wire format and auth rules stay consistent across both transports.
"""

import json
import secrets
import time
import uuid
from urllib.parse import urlencode, urlparse

from aiohttp import web

import thatradiothing.user
from thatradiothing.admin_api import AdminAPI
from thatradiothing.jwt_auth import issue_auth_token, verify_auth_token
from thatradiothing.logger import debug
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
        """Configure middleware, routes, Socket.IO, and admin services."""

        super().__init__(**kwargs)

        self.trt = thatradiothing

        self.middlewares.append(self._build_cors_middleware())
        self.middlewares.append(self._build_auth_cookie_rotation_middleware())
        self._register_routes()

        self.runner = web.AppRunner(self)

        # Socket.IO: real-time status push. All wiring lives in the gateway.
        self.sio_gateway = SocketIOGateway(self)
        self.admin_api = AdminAPI(self)
        self.admin_api.register()

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self):
        self.router.add_route("*", "/", self.index)
        self.add_routes(
            [
                web.get("/player", self.player),
                web.get("/logo", self.logo),
                web.get("/auth", self.auth),
                web.get("/auth_return", self.auth_return),
                web.post("/logout", self.logout),
                web.get("/successful_auth", self.successful_auth),
                web.get("/devices", self.devices),
                web.post("/devices", self.set_active_device),
                web.post("/master", self.set_master_user),
                web.post("/resign", self.resign_master_user),
                web.get("/profile", self.profile),
                web.post("/enable", self.enable),
                web.post("/disable", self.disable),
                # DEPRECATED: prefer the Socket.IO ``status`` event (see
                # ``sio.py``). The push channel emits the same payload on
                # state change and on connect, so polling this REST endpoint
                # is no longer necessary. Kept around only as a fallback for
                # transports that cannot speak Socket.IO. Will be removed
                # once no client polls it.
                web.get("/status", self.status),
                web.get("/api/now_playing", self.now_playing),
                web.get("/users", self.users),
                web.get("/health/live", self.health_live),
                web.get("/health/ready", self.health_ready),
            ]
        )

    def _build_cors_middleware(self):
        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == "OPTIONS":
                response = web.Response(status=204)
                return self._apply_cors_headers(request, response)

            try:
                response = await handler(request)
            except web.HTTPException as ex:
                response = ex

            return self._apply_cors_headers(request, response)

        return cors_middleware

    def _build_auth_cookie_rotation_middleware(self):
        """Rotate the auth cookie when its Spotify tokens become stale.

        Handlers read tokens via ``user.auth_headers()``, which proactively
        refreshes near-expiry tokens in memory. Without this middleware the
        refreshed tokens never make it back into the client's cookie, so the
        next request rebuilds the user from a stale JWT — the silent-expiry
        bug we're fixing.

        Skipped when the handler has already written a Set-Cookie for the
        auth cookie (logout, /auth_return) so we never overwrite explicit
        intent — logout stays destructive, login stays authoritative.
        """

        @web.middleware
        async def auth_cookie_rotation_middleware(request, handler):
            try:
                response = await handler(request)
            except web.HTTPException as ex:
                response = ex

            try:
                user = request.get("user")
                if not user:
                    return response

                # Don't fight handlers that set the cookie explicitly.
                cookies = getattr(response, "cookies", None)
                if cookies is not None and self.trt.auth_cookie_name in cookies:
                    return response

                loaded_expires_at = request.get("auth_expires_at_on_load")
                current_expires_at = None
                if isinstance(
                    user.refresh_tokens_after, (int, float)
                ) and user.refresh_tokens_after != float("inf"):
                    current_expires_at = int(user.refresh_tokens_after + 60)

                if current_expires_at is None or loaded_expires_at == current_expires_at:
                    return response

                self._set_auth_cookie(response, user)
            except Exception as exc:  # noqa: BLE001 - response must still be returned
                debug(f"Auth cookie rotation skipped: {exc}")

            return response

        return auth_cookie_rotation_middleware

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

        return trimmed.rstrip("/")

    def _allowed_origin(self, request):
        # config._parse_origins canonicalizes and validates the configured list
        # at startup, so it needs no normalization here -- only the untrusted
        # request header does. A bare "*" cannot appear: _parse_origins rejects
        # any entry without an http(s) scheme and hostname.
        request_origin = self._normalize_origin(request.headers.get("Origin"))
        if request_origin and request_origin in self.trt.cors_allowed_origins:
            return request_origin
        return None

    def _apply_cors_headers(self, request, response):
        allowed_origin = self._allowed_origin(request)
        if not allowed_origin:
            return response

        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, X-CSRF-Token, X-Request-ID"
        )
        response.headers["Access-Control-Max-Age"] = "600"

        vary = response.headers.get("Vary")
        if vary:
            if "Origin" not in vary:
                response.headers["Vary"] = f"{vary}, Origin"
        else:
            response.headers["Vary"] = "Origin"

        if self.trt.cors_allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"

        return response

    def _require_mutation_origin(self, request):
        """Reject browser mutations from origins outside the first-party set."""

        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            return
        origin = self._normalize_origin(request.headers.get("Origin"))
        public = urlparse(self.trt.url)
        own_origin = (
            f"{public.scheme}://{public.netloc}" if public.scheme and public.netloc else None
        )
        allowed = {
            normalized
            for value in [own_origin, *self.trt.cors_allowed_origins]
            if (normalized := self._normalize_origin(value))
        }
        if origin not in allowed:
            raise web.HTTPForbidden(reason="Mutation Origin is missing or not allowed")

    @staticmethod
    async def _json_body(request):
        """Read a small JSON object for legacy listener mutation endpoints."""

        maximum = 64 * 1024
        if request.content_length and request.content_length > maximum:
            raise web.HTTPRequestEntityTooLarge(maximum, request.content_length)
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(reason="Content-Type must be application/json")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(reason="Request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="Request body must be a JSON object")
        return body

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
        if host == "duudey.com" or (isinstance(host, str) and host.endswith(".duudey.com")):
            return ".duudey.com"

        return None

    def _set_auth_cookie(self, response, user):
        if not user or not user.spotify_profile:
            return

        spotify_profile = user.spotify_profile
        spotify_id = spotify_profile.get("id") if isinstance(spotify_profile, dict) else None
        if not spotify_id:
            return

        expires_at = None
        if isinstance(
            user.refresh_tokens_after, (int, float)
        ) and user.refresh_tokens_after != float("inf"):
            expires_at = int(user.refresh_tokens_after + 60)

        payload = {
            "provider": "spotify",
            "providerUserId": spotify_id,
            "displayName": spotify_profile.get("display_name"),
            "email": spotify_profile.get("email"),
            "imageUrl": (
                spotify_profile.get("images", [{}])[0].get("url")
                if isinstance(spotify_profile.get("images"), list) and spotify_profile.get("images")
                else None
            ),
            "spotifyProfileUrl": (
                spotify_profile.get("external_urls", {}).get("spotify")
                if isinstance(spotify_profile.get("external_urls"), dict)
                else None
            ),
            "spotifyAccessToken": user.access_token,
            "spotifyRefreshToken": user.refresh_token,
            "spotifyExpiresAt": expires_at,
            "spotifyScope": user.scope,
        }

        token = issue_auth_token(
            secret=self.trt.auth_shared_jwt_secret,
            issuer=self.trt.auth_jwt_issuer,
            payload=payload,
            ttl_seconds=self.trt.auth_cookie_max_age_seconds,
        )

        cookie_kwargs = {
            "max_age": self.trt.auth_cookie_max_age_seconds,
            "httponly": True,
            "secure": self.trt.auth_cookie_secure,
            "samesite": self.trt.auth_cookie_samesite,
            "path": "/",
        }
        domain = self._cookie_domain()
        if domain:
            cookie_kwargs["domain"] = domain

        response.set_cookie(self.trt.auth_cookie_name, token, **cookie_kwargs)

        # Companion presence flag, JS-readable, no auth material. The
        # duudey.com site reads this via ``document.cookie`` to decide
        # whether to skip its ``/api/auth/me`` probe on first paint;
        # see ``src/lib/auth/jwt.ts::LOGGED_IN_COOKIE_NAME`` on the site
        # side for the full contract. We mirror every other attribute
        # of the JWT cookie so the two cookies route identically and
        # expire together — the only divergence is ``httponly=False``.
        flag_kwargs = dict(cookie_kwargs)
        flag_kwargs["httponly"] = False
        response.set_cookie(self.trt.logged_in_cookie_name, "1", **flag_kwargs)

    def _clear_auth_cookie(self, response):
        # Clear both cookies in lockstep so the site shell never sees
        # the flag without the JWT (or vice versa).
        domain = self._cookie_domain()
        if domain:
            response.del_cookie(self.trt.auth_cookie_name, domain=domain, path="/")
            response.del_cookie(self.trt.logged_in_cookie_name, domain=domain, path="/")
            return
        response.del_cookie(self.trt.auth_cookie_name, path="/")
        response.del_cookie(self.trt.logged_in_cookie_name, path="/")

    def _extract_auth_token(self, request):
        auth_header = request.headers.get("Authorization", "")
        if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
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
            if str(user.spotify_profile.get("id")) == str(spotify_id):
                return user
        return None

    def _apply_claims_to_user(self, user, claims):
        if claims.get("spotifyAccessToken"):
            user.access_token = claims.get("spotifyAccessToken")
        if claims.get("spotifyRefreshToken"):
            user.refresh_token = claims.get("spotifyRefreshToken")
        if claims.get("spotifyScope"):
            user.scope = claims.get("spotifyScope")

        expires_at = claims.get("spotifyExpiresAt")
        if isinstance(expires_at, (int, float)):
            user.refresh_tokens_after = max(float(expires_at) - 60, time.time() + 10)

        profile = user.spotify_profile or {}
        spotify_id = claims.get("providerUserId")
        if spotify_id:
            profile["id"] = spotify_id

        if claims.get("displayName"):
            profile["display_name"] = claims.get("displayName")
        if claims.get("email"):
            profile["email"] = claims.get("email")
        if claims.get("spotifyProfileUrl"):
            profile["external_urls"] = {"spotify": claims.get("spotifyProfileUrl")}
        profile["can_be_master"] = profile.get("id") in self.trt.masters_list
        user.spotify_profile = profile

    async def _user_from_jwt_claims(self, claims):
        spotify_id = claims.get("providerUserId")
        if not spotify_id:
            return None

        existing = await self._find_user_by_spotify_id(spotify_id)
        if existing:
            self._apply_claims_to_user(existing, claims)
            return existing

        if not claims.get("spotifyAccessToken"):
            return None

        user = thatradiothing.user.User(
            self.trt,
            uuid.uuid4(),
            self.trt.url + "auth_return",
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
        # Container deployments intentionally expose the service on all interfaces.
        self.site = web.TCPSite(self.runner, "0.0.0.0", self.trt.port)  # noqa: S104
        await self.site.start()
        await self.sio_gateway.start()

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def index(self, request):
        """Show the login page or redirect authenticated users to the player."""

        user = await self.logged_in_user(request)
        if user:
            return web.HTTPTemporaryRedirect("/player", headers={"Cache-Control": "No-Cache"})
        return web.FileResponse("./static/index.htm", headers={"Cache-Control": "No-Cache"})

    async def player(self, request):
        """Serve the listener player to an authenticated user."""

        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPTemporaryRedirect("/", headers={"Cache-Control": "No-Cache"})
        return web.FileResponse("./static/player.htm", headers={"Cache-Control": "No-Cache"})

    async def logo(self, request):
        """Serve the application logo asset."""

        return web.FileResponse("./static/logo.png")

    async def auth(self, request):
        """Start Spotify OAuth with state bound to a short-lived secure cookie."""

        client_id = self.trt.client_id
        scope = " ".join(self.trt.scopes)
        redirect_uri = self.trt.url + "auth_return"
        state = uuid.uuid4()

        redirect_to = "https://accounts.spotify.com/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "scope": scope,
                "redirect_uri": redirect_uri,
                "state": str(state),
            }
        )

        return_to = request.rel_url.query.get("returnTo", "/successful_auth")
        if (
            not isinstance(return_to, str)
            or len(return_to) > 1024
            or not return_to.startswith("/")
            or return_to.startswith("//")
            or "\\" in return_to
        ):
            return_to = "/successful_auth"

        if request.cookies.get("state"):
            user_exists = await self.trt.find_user(session_id=request.cookies.get("state"))
            if user_exists:
                self.trt.users.remove(user_exists)

        response = web.HTTPFound(redirect_to)
        user = thatradiothing.user.User(
            self.trt, state, redirect_uri, client_id, self.trt.client_secret
        )
        debug(redirect_uri)
        self.trt.users.append(user)
        oauth_cookie = {
            "httponly": True,
            "secure": self.trt.auth_cookie_secure,
            "samesite": "Lax",
            "path": "/",
            "max_age": 600,
        }
        response.set_cookie("state", str(state), **oauth_cookie)
        response.set_cookie("auth_return_to", return_to, **oauth_cookie)
        return response

    async def auth_return(self, request):
        """Finish Spotify OAuth only when callback and cookie state agree."""

        state = request.rel_url.query.get("state")
        code = request.rel_url.query.get("code")
        if (
            not state
            or not code
            or not secrets.compare_digest(str(request.cookies.get("state") or ""), str(state))
        ):
            raise web.HTTPBadRequest(reason="OAuth state or code is missing or invalid")
        debug(state)
        for user in self.trt.users:
            debug(str(user.session_id))
            if str(user.session_id) == str(state):
                # Second step of the auth
                user.auth_code = code
                result = await user.request_tokens()  # also loads user profile
                if result:
                    # Drop any previous session objects for this Spotify
                    # account — a user can log in again from a different
                    # browser / tab, and we want the newest session to own
                    # the User record. Iterate a snapshot (list(...)) so the
                    # remove() calls don't skip entries in the live list.
                    #
                    # Matching is done on Spotify id, which only exists once
                    # request_tokens() has loaded the profile. Two kinds of
                    # entry therefore have to be skipped rather than compared:
                    #
                    # * ``prev_user`` objects still sitting at step one of the
                    #   handshake. ``/auth`` appends a User to ``trt.users``
                    #   *before* redirecting to Spotify, and nothing reaps the
                    #   ones that never come back — an abandoned consent
                    #   screen, a crawler, a probe. Their ``spotify_profile``
                    #   is still ``None`` (see ``user.User.__init__``). They
                    #   own no account, so they can never duplicate the
                    #   account that just logged in.
                    # * A ``user`` whose own profile somehow failed to load,
                    #   where every comparison would be meaningless anyway.
                    #
                    # Subscripting those directly raises ``TypeError``, which
                    # is not a ``KeyError``/``ValueError`` and so escapes the
                    # handler as an HTTP 500. That turned a single abandoned
                    # ``/auth`` hit into a permanent "login is broken" for
                    # everyone, since the poisoned entry never aged out.
                    current_profile = user.spotify_profile
                    current_spotify_id = (
                        current_profile.get("id") if isinstance(current_profile, dict) else None
                    )

                    if current_spotify_id is not None:
                        for prev_user in list(self.trt.users):
                            if prev_user is user:
                                continue

                            prev_profile = prev_user.spotify_profile
                            if not isinstance(prev_profile, dict):
                                continue

                            if prev_profile.get("id") != current_spotify_id:
                                continue

                            try:
                                self.trt.users.remove(prev_user)
                            except ValueError:
                                # Already gone (concurrent login for the same
                                # account); nothing left to do for this entry.
                                continue

                    return_to = request.cookies.get("auth_return_to", "/successful_auth")
                    if (
                        not isinstance(return_to, str)
                        or len(return_to) > 1024
                        or not return_to.startswith("/")
                        or return_to.startswith("//")
                        or "\\" in return_to
                    ):
                        return_to = "/successful_auth"
                    response = web.HTTPFound(return_to)
                    response.del_cookie("auth_return_to", path="/")
                    response.del_cookie("state", path="/")
                    self._set_auth_cookie(response, user)
                    return response
                return web.Response(text="failed to get auth token")
        else:
            return web.Response(text="no auth")

    async def logout(self, request):
        """Remove the current user session and clear both auth cookies."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPTemporaryRedirect("/")

        if self.trt.master.master_user == user:
            await self.resign_master_user(request)

        self.trt.users.remove(user)

        response = web.HTTPTemporaryRedirect("/")
        self._clear_auth_cookie(response)
        return response

    async def successful_auth(self, request):
        """Serve the OAuth completion page."""

        return web.FileResponse("./static/successful-auth.htm")

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
                # Record the expiry we started with so the cookie-rotation
                # middleware can detect when auth_headers() has silently
                # refreshed tokens and push the fresh ones back to the client.
                loaded_expiry = claims.get("spotifyExpiresAt")
                if isinstance(loaded_expiry, (int, float)):
                    request["auth_expires_at_on_load"] = int(loaded_expiry)

                user_from_claims = await self._user_from_jwt_claims(claims)
                if user_from_claims and await user_from_claims.auth_headers():
                    request["user"] = user_from_claims
                    return user_from_claims

        user_session_id = request.cookies.get("state", False)
        if not user_session_id:
            return None

        user = await self.trt.find_user(session_id=user_session_id)
        if not user:
            return False

        if not await user.auth_headers():
            return False

        request["user"] = user
        return user

    async def devices(self, request):
        """Return the current user's available Spotify devices."""

        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized(reason="Not logged in.")

        devices = await user.list_devices()
        return web.Response(body=json.dumps(devices))

    async def set_active_device(self, request):
        """Select a Spotify playback device for the current listener."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized(reason="Not logged in.")

        data = await self._json_body(request)

        if not data.get("device_id"):
            return web.HTTPExpectationFailed(reason="'device_id' not found.")

        device_id = data["device_id"]

        device = await user.select_device(device_id)
        if not device:
            return web.HTTPNotFound()

        user.touch_interaction()
        return web.Response(body=json.dumps({"status": True}))

    async def set_master_user(self, request):
        """Promote an authorized listener to the human master role."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user or not user.spotify_profile:
            return web.HTTPUnauthorized()

        if user.spotify_profile["can_be_master"]:
            # Select Master's first device.
            # Normally 'play' does this automatically but master does not receive play API calls.
            if not await user.selected_device():
                return web.HTTPConflict(reason="No Spotify playback device is available")
            self.trt.master.master_user = user

            debug("NEW MASTER USER")
            debug(user.spotify_profile["display_name"])
            user.touch_interaction()
            return web.Response(body="OK")

        return web.HTTPUnauthorized()

    async def resign_master_user(self, request):
        """Return control from the current human master to AutoDJ."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user or not user.spotify_profile:
            return web.HTTPUnauthorized()

        if user.spotify_profile["can_be_master"] and user == self.trt.master.master_user:
            # The schedule continued changing in the background, but AutoDJ's
            # elapsed clock was not audible. Resume its current schedule from
            # a freshly started track rather than jumping into stale progress.
            await self.trt.autodj.advance_track(reason="human-resign")
            self.trt.master.master_user = None
            user.touch_interaction()
            return web.Response(body="OK")

        return web.HTTPUnauthorized()

    async def profile(self, request):
        """Return the authenticated listener's public radio profile."""

        user = await self.logged_in_user(request)
        if not user or not user.spotify_profile:
            return web.HTTPUnauthorized()

        is_user_master = self.trt.master.master_user == user

        user_profile = {
            "profile": user.spotify_profile,
            "can_be_master": user.spotify_profile["can_be_master"],
            "is_master": is_user_master,
            "message": user.message,
        }

        return web.Response(body=json.dumps(user_profile))

    async def enable(self, request):
        """Enable synchronized playback and begin device discovery."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        user.enabled = True
        user.play_if_paused = True
        user.touch_interaction()
        # Fast-refresh the device list and suppress the "no device" auto-disable
        # for a grace window so a newly-opened Spotify client is picked up.
        user.begin_waiting_for_device()
        user.message = "Waiting for a Spotify device to come online…"
        user.message_expires_at = 0.0  # Persist while the waiting window is active.

        return web.HTTPOk()

    async def disable(self, request):
        """Disable synchronized playback and pause the listener's device."""

        self._require_mutation_origin(request)
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized()

        user.enabled = False
        user.touch_interaction()
        # Cancel any pending "waiting for device" grace window so we don't
        # auto-resume playback if a device comes online after the user
        # explicitly disabled.
        user.end_waiting_for_device()
        user.message = ""
        user.message_expires_at = 0.0
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
        # Drop any transient message whose TTL has passed before building
        # the payload, so clients stop seeing it on the next push/diff.
        user.expire_message_if_due()

        master_user = {"display_name": None, "progress_ms": None, "external_url": None}

        if self.trt.master.master_user:
            master_profile = self.trt.master.master_user.spotify_profile or {}
            master_user["display_name"] = master_profile.get("display_name")
            if self.trt.master.now_playing:
                master_user["progress_ms"] = self.trt.master.now_playing["progress_ms"]
            external_urls = master_profile.get("external_urls") or {}
            master_user["external_url"] = external_urls.get("spotify")

        is_user_master = self.trt.master.master_user == user
        can_be_master = bool(user.spotify_profile and user.spotify_profile.get("can_be_master"))

        payload = {
            "now_playing": self.trt.master.now_playing_track,
            "master_user": master_user,
            "listeners": self.trt.master.last_listener_count,
            "enabled": user.enabled,
            "profile": user.spotify_profile,
            "can_be_master": can_be_master,
            "is_master": is_user_master,
            "message": user.message,
            "devices": await user.list_devices(),
        }

        if can_be_master:
            payload["users"] = [await u.summary() for u in self.trt.users]

        return payload

    async def status(self, request):
        """Return the current radio state for the logged-in user.

        DEPRECATED — clients should subscribe to the Socket.IO ``status``
        event instead of polling this endpoint. The websocket channel
        emits the same payload whenever state changes and replays the
        current state on connect, so polling here is wasted traffic.

        This handler is kept available for two narrow cases:

        * Server-side or scripted callers that cannot open a Socket.IO
          connection.
        * One-shot reads during boot, before the client has wired up
          its socket subscription.

        We advertise the deprecation to clients via two response
        headers, following RFC 8594 (the ``Deprecation`` header) and
        the related ``Sunset`` / ``Link`` conventions:

        * ``Deprecation: true`` — flags every response as deprecated so
          ops dashboards and the browser DevTools console can surface
          it without parsing the body.
        * ``Link: </socket.io/>; rel="successor-version"`` — points
          callers at the replacement transport. The ``rel`` value is
          the IANA-registered successor relation used in deprecation
          announcements.

        We deliberately do *not* set a ``Sunset`` date yet because we
        haven't picked a removal window; once we do, add it here.
        """
        user = await self.logged_in_user(request)
        if not user:
            return web.HTTPUnauthorized(
                headers={
                    "Deprecation": "true",
                    "Link": '</socket.io/>; rel="successor-version"',
                }
            )

        payload = await self.build_status_payload(user)
        return web.Response(
            body=json.dumps(payload),
            headers={
                "Deprecation": "true",
                "Link": '</socket.io/>; rel="successor-version"',
            },
        )

    async def now_playing(self, request):
        """Return the public current-track snapshot."""

        payload = {"now_playing": self.trt.master.now_playing_track}
        return web.Response(body=json.dumps(payload))

    async def health_live(self, request):
        """Liveness probe: the process and HTTP server are running."""

        return web.json_response({"ok": True})

    async def health_ready(self, request):
        """Readiness probe: persistence, catalog, and scheduler are usable."""

        scheduler = getattr(self.trt, "schedule_coordinator", None)
        ready = bool(
            getattr(getattr(self.trt, "db", None), "connection", None)
            and getattr(self.trt.autodj, "selected_playlist", None)
            and scheduler
            and scheduler.last_error is None
        )
        payload = {"ok": ready, "schedule": scheduler.status() if scheduler else None}
        return web.json_response(payload, status=200 if ready else 503)

    async def users(self, request):
        """Return listener summaries to an authorized master account."""

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
