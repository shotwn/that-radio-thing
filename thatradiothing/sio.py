"""Socket.IO gateway for real-time ``status`` push.

This module owns everything Socket.IO-specific so that :mod:`thatradiothing.server`
can stay focused on the classical HTTP surface.

Design
------
* A single :class:`SocketIOGateway` wraps the :class:`socketio.AsyncServer` and
  attaches it to the aiohttp application owned by ``WebServer``.
* Authentication reuses the HTTP auth path. The browser sends the HttpOnly
  JWT cookie on the WebSocket upgrade (and on long-polling fallback) and we
  resolve the user via ``WebServer.logged_in_user`` using the aiohttp request
  exposed by python-socketio in ``environ['aiohttp.request']``. The JWT is
  never exposed to client JavaScript.
* A background task (:meth:`SocketIOGateway.push_loop`) ticks once per second.
  For each connected socket it rebuilds the per-user status payload and emits
  ``status`` only if the payload differs from what the client last received.
  This turns N-clients x poll-rate load into N-clients x change-rate load.
* Stale authentication on long-lived sockets is handled defensively: every
  tick we call ``user.auth_headers()`` (which refreshes Spotify tokens as
  needed). If that fails we disconnect the socket so the client reconnects
  and re-authenticates via the cookie handshake.
"""

import asyncio
import time
from typing import Any

import socketio
from logzero import logger

STATUS_EVENT = "status"
ADMIN_STATUS_EVENT = "admin_status"

# How often the push loop wakes to look at per-socket state. Kept at 1 s
# so real state changes (track flip, master change, device list, enable
# toggle) are visible to clients within a second.
PUSH_INTERVAL_SECONDS = 1.0

# Heartbeat cadence when nothing else has changed. Without this we used
# to emit every tick because ``master_user.progress_ms`` ticks by ~1000
# each second — a strictly identical payload shape that still tripped
# the diff. Clients only use progress_ms for a UI progress bar, so a
# 5 s correction is plenty; the actual playback-sync loop lives in
# master.py and does not depend on this event.
HEARTBEAT_INTERVAL_SECONDS = 5.0


class SocketIOGateway:
    """Real-time ``status`` gateway backed by python-socketio."""

    def __init__(self, web_server):
        """Create the Socket.IO server and attach it to the aiohttp app.

        ``web_server`` is the :class:`thatradiothing.server.WebServer`
        instance; we use it for auth (``logged_in_user``), payload building
        (``build_status_payload``), and user lookup (``trt.find_user``).
        """
        self.web_server = web_server
        self.trt = web_server.trt

        # Per-sid cache of the last payload we emitted. Used for diffing.
        self._last_payloads = {}
        # Per-sid monotonic timestamp of the last emit, so we can ratchet
        # the progress-bar heartbeat independently of the diff.
        self._last_emit_monotonic = {}
        self._push_task = None

        cors_origins = self._resolve_cors_origins()
        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=cors_origins,
            cors_credentials=bool(self.trt.cors_allow_credentials),
        )
        self.sio.attach(web_server)
        self._register_handlers()

    def _resolve_cors_origins(self):
        """Mirror the HTTP CORS policy so credentialed handshakes work.

        Browsers reject ``Access-Control-Allow-Origin: *`` combined with
        credentials, and this handshake is always credentialed. ``config``
        guarantees an explicit list -- ``_parse_origins`` rejects ``'*'`` at
        startup -- so the configured origins pass through verbatim.
        """
        return list(self.trt.cors_allowed_origins)

    async def start(self):
        """Start the background push loop. Call once after the site is up."""
        if self._push_task is None:
            self._push_task = asyncio.create_task(self.push_loop())

    # --- handlers ---------------------------------------------------------

    def _register_handlers(self):
        """Register authenticated connect and disconnect event callbacks."""

        sio = self.sio

        @sio.event
        async def connect(sid, environ, auth):
            """Authenticate the socket via the HTTP cookie on the handshake."""
            request = environ.get("aiohttp.request")
            if request is None:
                return False

            try:
                user = await self.web_server.logged_in_user(request)
            except Exception:  # noqa: BLE001 - handshake boundary rejects safely
                logger.exception("socket.io connect auth failed")
                return False
            if not user:
                return False

            # Store only the user's session_id; we re-resolve the User
            # object each tick so a logged-out/removed user is caught.
            async with sio.session(sid) as session:
                session["session_id"] = str(user.session_id)
                session["is_admin"] = bool(
                    user.spotify_profile
                    and str(user.spotify_profile.get("id")) in self.trt.admin_ids
                )

            # Send an initial snapshot so the client doesn't wait a tick.
            try:
                async with sio.session(sid) as session:
                    is_admin = bool(session.get("is_admin"))
                payload = (
                    await self.web_server.admin_api.status_payload()
                    if is_admin
                    else await self.web_server.build_status_payload(user)
                )
                self._last_payloads[sid] = payload
                self._last_emit_monotonic[sid] = time.monotonic()
                await sio.emit(ADMIN_STATUS_EVENT if is_admin else STATUS_EVENT, payload, to=sid)
            except Exception:  # noqa: BLE001 - connection survives emit failures
                logger.exception("initial status emit failed")

        @sio.event
        async def disconnect(sid):
            """Discard cached status when a socket disconnects."""

            self._last_payloads.pop(sid, None)
            self._last_emit_monotonic.pop(sid, None)

    # --- push loop --------------------------------------------------------

    async def push_loop(self):
        """Forever: diff and emit per-socket status at a fixed cadence."""
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - background loop must stay alive
                logger.exception("status push loop error")
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)

    async def _tick(self):
        """Emit one status update to every currently tracked socket."""

        # The admin payload carries no per-user data, so build it at most once
        # per tick and share it across admin sockets. Building it per socket
        # cost two SQLite round trips per admin socket per second. ``tick_cache``
        # is scoped to this tick, so the data stays as fresh as it was before.
        tick_cache: dict[str, Any] = {}
        # Snapshot sids so mutation during iteration is safe.
        for sid in list(self._last_payloads.keys()):
            await self._emit_for_sid(sid, tick_cache)

    async def _shared_admin_payload(self, tick_cache):
        """Build the admin status payload once per tick, then reuse it."""

        if "admin" not in tick_cache:
            tick_cache["admin"] = await self.web_server.admin_api.status_payload()
        return tick_cache["admin"]

    async def _emit_for_sid(self, sid, tick_cache=None):
        """Authenticate and conditionally emit the latest state to one socket."""

        try:
            async with self.sio.session(sid) as session:
                session_id = session.get("session_id")
                is_admin = bool(session.get("is_admin"))
        except KeyError:
            # Socket already gone; drop the last-payload slot.
            self._last_payloads.pop(sid, None)
            return

        if not session_id:
            return

        user = await self.trt.find_user(session_id=session_id)
        if not user:
            await self._drop(sid)
            return

        # Token freshness check; refreshes if needed, disconnects if dead.
        if not await user.auth_headers():
            await self._drop(sid)
            return

        if is_admin:
            payload = await self._shared_admin_payload(tick_cache if tick_cache is not None else {})
        else:
            payload = await self.web_server.build_status_payload(user)
        last_payload = self._last_payloads.get(sid)
        content_changed = last_payload is None or self._diff_key(payload) != self._diff_key(
            last_payload
        )

        now = time.monotonic()
        last_emit = self._last_emit_monotonic.get(sid, 0.0)
        heartbeat_due = (now - last_emit) >= HEARTBEAT_INTERVAL_SECONDS

        if content_changed or heartbeat_due:
            self._last_payloads[sid] = payload
            self._last_emit_monotonic[sid] = now
            await self.sio.emit(ADMIN_STATUS_EVENT if is_admin else STATUS_EVENT, payload, to=sid)

    def _diff_key(self, payload):
        """Return a payload view that ignores the per-second progress tick.

        ``master_user.progress_ms`` increments by ~1000 every push tick
        while a master is playing. Left in, it tripped the naive
        ``payload != last`` check every single second even when nothing
        else about the state had changed. Strip it for the change-detect
        comparison; the real value still goes out on the wire whenever
        we do decide to emit (immediate on real change, or once per
        heartbeat interval for UI drift correction).
        """
        if not isinstance(payload, dict):
            return payload
        master = payload.get("master_user")
        if isinstance(master, dict) and "progress_ms" in master:
            masked_master = {**master, "progress_ms": None}
            return {**payload, "master_user": masked_master}
        return payload

    async def _drop(self, sid):
        """Disconnect *sid* and remove all cached state associated with it."""

        await self.sio.disconnect(sid)
        self._last_payloads.pop(sid, None)
        self._last_emit_monotonic.pop(sid, None)
