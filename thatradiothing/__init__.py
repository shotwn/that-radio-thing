"""Compose ThatRadioThing's persistence, playback, scheduling, and HTTP services."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, ClassVar

from logzero import logger

import thatradiothing.autodj
import thatradiothing.master
import thatradiothing.server
from thatradiothing.config import CONFIG
from thatradiothing.db import RadioDatabase
from thatradiothing.scheduler import ScheduleCoordinator
from thatradiothing.spotify_catalog import SpotifyCatalogClient


class ThatRadioThing:
    """Own the application graph and coordinate its asynchronous lifecycle."""

    find_user_allowed_keys: ClassVar[tuple[str, ...]] = ("session_id",)

    def __init__(self) -> None:
        """Construct services from validated configuration without starting I/O."""

        self.url = CONFIG["url"]
        self.port = CONFIG["port"]
        self.client_id = CONFIG["client_id"]
        self.client_secret = CONFIG["client_secret"]
        self.scopes = CONFIG["scopes"]
        self.realtime_tolerance_ms = CONFIG["realtime_tolerance_ms"]
        self.masters_list = CONFIG["masters_list"]
        # A frozenset because this is membership-tested on every admin request
        # and on every Socket.IO handshake; the config value never changes after
        # startup, so rebuilding a set per check was pure waste.
        self.admin_ids = frozenset(CONFIG.get("admin_ids", []))
        self.auth_cookie_name = CONFIG["auth_cookie_name"]
        self.logged_in_cookie_name = CONFIG["logged_in_cookie_name"]
        self.auth_cookie_domain = CONFIG["auth_cookie_domain"]
        self.auth_cookie_secure = bool(CONFIG["auth_cookie_secure"])
        self.auth_cookie_samesite = CONFIG["auth_cookie_samesite"]
        self.auth_cookie_max_age_seconds = int(CONFIG["auth_cookie_max_age_seconds"])
        self.auth_jwt_issuer = CONFIG["auth_jwt_issuer"]
        self.auth_shared_jwt_secret = CONFIG["auth_shared_jwt_secret"]
        self.cors_allowed_origins = list(CONFIG.get("cors_allowed_origins", []))
        self.cors_allow_credentials = bool(CONFIG.get("cors_allow_credentials", True))
        self.admin_origins = list(CONFIG.get("admin_origins", []))
        self.schedule_timezone = CONFIG.get("schedule_timezone", "Europe/Istanbul")
        self.catalog_refresh_interval_seconds = int(
            CONFIG.get("catalog_refresh_interval_seconds", 21_600)
        )

        self.users: list[Any] = []
        self.db = RadioDatabase(CONFIG["database_path"], CONFIG.get("playlists", []))
        self.web_server = thatradiothing.server.WebServer(self)
        self.web_server_task: asyncio.Task[None] | None = None
        self.master = thatradiothing.master.Master(self)
        self.master_task: asyncio.Task[None] | None = None
        self.autodj = thatradiothing.autodj.AutoDJ(
            self,
            "AUTODJ",
            "AUTODJ",
            self.client_id,
            self.client_secret,
            playlists=CONFIG["playlists"],
            catalog_client=SpotifyCatalogClient(
                self.client_id,
                self.client_secret,
                CONFIG.get("catalog_refresh_token"),
            ),
        )
        self.schedule_coordinator = ScheduleCoordinator(self)
        self._shutdown_event = asyncio.Event()
        self._closed = False

    def run(self) -> None:
        """Run until shutdown or interruption and always release resources."""

        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            # ``asyncio.run`` cancels the main task; ``_run`` performs cleanup
            # in its ``finally`` block before the interruption reaches here.
            logger.info("ThatRadioThing stopped by operator")

    async def _run(self) -> None:
        """Start services in dependency order and wait for a shutdown request."""

        try:
            await self.db.open()
            if await self.db.get_setting("default_timezone") is None:
                await self.db.set_setting(
                    "default_timezone",
                    self.schedule_timezone,
                    "bootstrap",
                )
            if await self.db.get_setting("catalog_refresh_interval_seconds") is None:
                await self.db.set_setting(
                    "catalog_refresh_interval_seconds",
                    self.catalog_refresh_interval_seconds,
                    "bootstrap",
                )
            await self.autodj.populate()
            await self.schedule_coordinator.start()

            self.web_server_task = asyncio.create_task(
                self.web_server.run(),
                name="thatradiothing-http",
            )
            self.web_server_task.add_done_callback(self.aio_exception_handler)
            self.master_task = asyncio.create_task(
                self.master.beat(),
                name="thatradiothing-master",
            )
            self.master_task.add_done_callback(self.aio_exception_handler)
            await self._shutdown_event.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        """Idempotently stop background tasks and close external resources."""

        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()

        await self.schedule_coordinator.stop()
        push_task = self.web_server.sio_gateway._push_task
        if push_task and not push_task.done():
            push_task.cancel()
            with suppress(asyncio.CancelledError):
                await push_task

        for task in (self.master_task, self.web_server_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        if getattr(self.web_server, "runner", None):
            await self.web_server.runner.cleanup()
        for user in list(self.users):
            session = getattr(user, "_aiohttp_session", None)
            if session and not session.closed:
                await session.close()
        await self.autodj.catalog_client.close()
        await self.db.close()

    def aio_exception_handler(self, future: asyncio.Future[Any]) -> None:
        """Log fatal background failures and wake the main lifecycle task."""

        if future.cancelled():
            return
        exception = future.exception()
        if exception is not None:
            logger.error(
                "Background task failed: %s",
                exception,
                exc_info=exception,
            )
            self._shutdown_event.set()

    async def find_users(self, **kwargs: Any) -> list[Any]:
        """Return users matching every explicitly supported lookup attribute."""

        if not kwargs:
            raise ValueError("At least one user lookup field is required")
        unsupported = set(kwargs) - set(self.find_user_allowed_keys)
        if unsupported:
            raise KeyError(f"Unsupported user lookup fields: {sorted(unsupported)}")

        return [
            user
            for user in self.users
            if all(str(getattr(user, key, None)) == str(value) for key, value in kwargs.items())
        ]

    async def find_user(self, **kwargs: Any) -> Any | None:
        """Return the sole matching user, or ``None`` for zero/ambiguous matches."""

        users = await self.find_users(**kwargs)
        return users[0] if len(users) == 1 else None
