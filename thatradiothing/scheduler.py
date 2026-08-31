"""Schedule resolution and boundary coordination for AutoDJ."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from logzero import logger

from thatradiothing.config import CATALOG_REFRESH_MIN_SECONDS
from thatradiothing.recurrence import (
    Occurrence,
    RecurrenceError,
    iso_utc,
    occurrences_between,
    parse_utc,
    resolve_active,
    validate_series,
)


class ScheduleCoordinator:
    """Recompute the active schedule and wake AutoDJ at boundaries."""

    def __init__(self, trt: Any) -> None:
        """Bind the coordinator to the application services it orchestrates."""

        self.trt = trt
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.active_occurrence: Occurrence | None = None
        self.next_transition: datetime | None = None
        self.last_error: str | None = None
        self.last_resolved_at: datetime | None = None
        self.catalog_errors: dict[str, str] = {}
        self._last_playlist_id: str | None = None
        # Seeded to "now" so the first catalog refresh happens one full interval
        # after start-up (six hours by default) rather than immediately: boot
        # already loads every catalog through refresh_now(), and re-fetching
        # them right away would double the Spotify cost of every restart.
        self._last_catalog_refresh = time.monotonic()

    async def start(self) -> None:
        """Apply the current schedule and start the boundary loop."""

        if self._task is not None:
            return
        await self.refresh_now()
        self._task = asyncio.create_task(self.run(), name="thatradiothing-schedule")

    async def stop(self) -> None:
        """Cancel and await the boundary loop; repeated calls are harmless."""

        self._stopped = True
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        """Wake the coordinator after an administrative mutation."""

        self._wake_event.set()

    async def run(self) -> None:
        """Re-evaluate at boundaries, edit wakeups, and a 30-second safety tick.

        Exactly one instance of this loop runs (see :meth:`start`), so
        iterations cannot overlap and no reentrancy guard is needed. Each
        iteration sleeps until the earliest of: the next schedule transition, an
        explicit :meth:`wake` from an administrative mutation, or 30 seconds.
        The 30-second ceiling is a safety net for state that changes without
        waking us -- a clock step, or a series edited directly in the database.

        Nothing is queued or replayed. :meth:`refresh_now` resolves the schedule
        from the current wall clock every time, so a tick delayed by a slow
        Spotify call applies the correct playlist late rather than working
        through a backlog of missed boundaries. The 0.2 s sleep floor keeps a
        cluster of near-simultaneous boundaries from spinning the loop.
        """

        consecutive_failures = 0
        while not self._stopped:
            try:
                refresh_interval = await self._catalog_refresh_interval()
                if time.monotonic() - self._last_catalog_refresh >= refresh_interval:
                    await self.refresh_catalogs()
                    self._last_catalog_refresh = time.monotonic()
                await self.refresh_now()
            except Exception as exc:  # noqa: BLE001 - coordinator must survive one bad row
                consecutive_failures += 1
                self.last_error = str(exc)
                logger.exception("schedule coordinator refresh failed")
            else:
                consecutive_failures = 0

            now = datetime.now(UTC)
            delay = 30.0
            if self.next_transition is not None:
                delay = min(30.0, max(0.2, (self.next_transition - now).total_seconds()))
            if consecutive_failures:
                # A failed refresh leaves ``next_transition`` at its previous
                # value, which is usually already in the past. Without a floor
                # the loop would spin at the 0.2s minimum, flooding the log and
                # hammering whatever is already broken. Back off instead.
                delay = max(delay, min(30.0, 2.0 ** min(consecutive_failures, 5)))
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _catalog_refresh_interval(self) -> int:
        """Read the versioned setting so admins can tune refresh cadence."""

        value = await self.trt.db.get_setting(
            "catalog_refresh_interval_seconds",
            self.trt.catalog_refresh_interval_seconds,
        )
        try:
            return max(CATALOG_REFRESH_MIN_SECONDS, int(value))
        except (TypeError, ValueError):
            return max(CATALOG_REFRESH_MIN_SECONDS, int(self.trt.catalog_refresh_interval_seconds))

    async def refresh_catalogs(self) -> None:
        """Refresh enabled catalogs without replacing a good cached copy on error."""

        errors: dict[str, str] = {}
        for playlist in await self.trt.db.list_playlists(include_disabled=False):
            try:
                await self.trt.autodj.refresh_playlist_record(playlist, "system")
            except Exception as exc:  # noqa: BLE001 - one stale playlist must not block the rest
                message = str(exc)
                errors[str(playlist["id"])] = message
                await self.trt.db.patch_playlist(
                    playlist["id"],
                    {"validation_error": message},
                    "system",
                )
        self.catalog_errors = errors

    async def refresh_now(self, now: datetime | None = None) -> dict[str, Any]:
        """Resolve and apply the active occurrence at a wall-clock instant."""

        instant = parse_utc(now or datetime.now(UTC))
        series = await self.trt.db.list_schedule_series(include_disabled=False)
        valid_series: list[dict[str, Any]] = []
        invalid_series: list[str] = []
        for item in series:
            try:
                validate_series(item)
            except RecurrenceError as exc:
                invalid_series.append(f"{item.get('id', 'unknown')}: {exc}")
            else:
                valid_series.append(item)
        winner, next_transition, candidates = resolve_active(valid_series, instant)
        self.active_occurrence = winner
        self.next_transition = next_transition
        self.last_resolved_at = instant
        self.last_error = (
            f"Invalid schedule series: {'; '.join(invalid_series)}" if invalid_series else None
        )

        target_playlist_id = (
            winner.playlist_id if winner else await self.trt.db.get_setting("default_playlist_id")
        )
        await self._apply_target_playlist(target_playlist_id, winner)

        return {
            "active": winner.as_dict() if winner else None,
            "next_transition": iso_utc(self.next_transition) if self.next_transition else None,
            "candidates": [item.as_dict() for item in candidates],
            "last_resolved_at": iso_utc(instant),
            "error": self.last_error,
        }

    async def _apply_target_playlist(
        self, target_playlist_id: Any, winner: Occurrence | None
    ) -> None:
        """Hand the resolved playlist to AutoDJ, recording why if we cannot.

        Every failure path here is non-fatal by design: the coordinator records
        the reason in ``last_error`` and returns, leaving AutoDJ on its last
        known good playlist. Taking the station silent is always worse than
        playing something slightly stale.
        """

        if not target_playlist_id:
            self.last_error = "No default or scheduled playlist is configured"
            return

        playlist = await self.trt.db.get_playlist(str(target_playlist_id))
        if not playlist or not playlist.get("enabled", True):
            self.last_error = f"playlist {target_playlist_id} is unavailable or disabled"
            return

        before_snapshot = self.trt.autodj.snapshot()
        active_playlist_id = (before_snapshot.get("playlist") or {}).get("id")
        # AutoDJ only restarts the audible track when the playlist actually
        # changes; this tick runs every 30 seconds against the same playlist.
        needs_restart = active_playlist_id != str(target_playlist_id)
        try:
            snapshot = await self.trt.autodj.activate_playlist(
                playlist,
                force_restart=needs_restart,
                reason=f"schedule:{winner.series_id}" if winner else "default",
            )
        except Exception as exc:  # noqa: BLE001 - retain last known good playlist
            self.last_error = str(exc)
            logger.exception("unable to activate scheduled playlist %s", target_playlist_id)
            return

        if snapshot.get("error"):
            self.last_error = str(snapshot["error"])
            return

        self._last_playlist_id = str(target_playlist_id)
        if needs_restart:
            await self._audit_transition(before_snapshot, snapshot, winner)

    async def _audit_transition(
        self, before_snapshot: dict[str, Any], snapshot: dict[str, Any], winner: Occurrence | None
    ) -> None:
        """Record a schedule-driven playlist change; never fail the transition."""

        try:
            await self.trt.db.add_audit(
                actor_id="system",
                actor_display_name="Schedule coordinator",
                action="playback.schedule_transition",
                entity_type="autodj",
                entity_id="AUTODJ",
                request_id=None,
                before=before_snapshot,
                after={
                    "playback": snapshot,
                    "occurrence": winner.as_dict() if winner else None,
                },
            )
        except Exception:  # noqa: BLE001 - playback already succeeded
            logger.exception("unable to write schedule transition audit")

    async def occurrences(
        self, start: datetime, end: datetime, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return expanded calendar occurrences with winner annotations."""

        series = await self.trt.db.list_schedule_series(include_disabled=False)
        expanded: list[Occurrence] = []
        for item in series:
            try:
                expanded.extend(
                    occurrences_between(item, start, end, max(1, limit - len(expanded)))
                )
            except RecurrenceError:
                continue
            if len(expanded) >= limit:
                break
        expanded.sort(
            key=lambda occurrence: (
                occurrence.start_utc,
                -occurrence.priority,
                occurrence.series_id,
            )
        )
        result: list[dict[str, Any]] = []
        for occurrence in expanded:
            overlaps = [
                other
                for other in expanded
                if other.series_id != occurrence.series_id
                and other.start_utc < occurrence.end_utc
                and occurrence.start_utc < other.end_utc
            ]
            winner = max(
                [occurrence, *overlaps],
                key=lambda item: (item.priority, item.start_utc, item.series_id),
            )
            payload = occurrence.as_dict()
            payload["suppressed"] = winner.series_id != occurrence.series_id
            payload["suppression_reason"] = (
                f"winner:{winner.series_id}" if payload["suppressed"] else None
            )
            result.append(payload)
        return result

    def status(self) -> dict[str, Any]:
        """Return coordinator health and active schedule state."""

        return {
            "active_occurrence": self.active_occurrence.as_dict()
            if self.active_occurrence
            else None,
            "next_transition": iso_utc(self.next_transition) if self.next_transition else None,
            "last_resolved_at": iso_utc(self.last_resolved_at) if self.last_resolved_at else None,
            "error": self.last_error,
            "catalog_errors": dict(self.catalog_errors),
            "degraded": bool(self.last_error or self.catalog_errors),
        }
