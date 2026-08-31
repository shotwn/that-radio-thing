"""Interpret radio schedule recurrence rules without performing I/O.

The scheduler stores local wall-clock start times because a weekly 09:00 show
must remain at 09:00 when daylight-saving offsets change. This module is the
single authority for converting those local rules to bounded UTC occurrences,
applying per-occurrence exceptions, and resolving overlaps deterministically.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rruleset, rrulestr

# Longest permitted show, and -- by construction -- the lookback window used
# when scanning for occurrences. These two roles are coupled: because no
# occurrence may run longer than this, every interval still active at time T
# must have started within ``T - MAX_DURATION_SECONDS``, which is what makes
# that lookback both sufficient and necessary. Raising the cap without widening
# the lookback would silently drop long shows from ``latest_occurrence`` and
# ``occurrences_between`` -- no error, just a missing program.
MAX_DURATION_SECONDS = 7 * 24 * 60 * 60

# Hard ceiling on recurrence candidates examined for one series, and on an
# accepted ``COUNT``. A malicious or fat-fingered rule (``FREQ=DAILY`` with no
# ``UNTIL``, queried across a decade) must fail loudly rather than pin the event
# loop. The bare ``10_000`` in the ``INTERVAL`` check below is a separate,
# unrelated bound -- do not collapse the two.
MAX_OCCURRENCE_SCAN = 10_000
ALLOWED_RRULE_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


class RecurrenceError(ValueError):
    """Report schedule input that cannot be evaluated predictably."""


@dataclass(frozen=True, slots=True)
class Occurrence:
    """Represent one concrete schedule interval in UTC.

    Attributes:
        series_id: Stable identifier of the schedule series that produced it.
        title: Human-readable program name.
        playlist_id: Local playlist record selected for the interval.
        start_utc: Inclusive UTC start of the occurrence.
        end_utc: Exclusive UTC end of the occurrence.
        original_start_utc: Recurrence key before an edit-one move is applied.
        timezone: IANA timezone used to produce the local wall-clock start.
        priority: Explicit overlap priority; larger numbers win.
        source: Owning adapter, currently ``local`` or future ``google``.
        suppressed: Whether another overlapping occurrence wins.
        suppression_reason: Machine-readable explanation when suppressed.

    """

    series_id: str
    title: str
    playlist_id: str
    start_utc: datetime
    end_utc: datetime
    original_start_utc: datetime
    timezone: str
    priority: int
    source: str = "local"
    suppressed: bool = False
    suppression_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the occurrence in the JSON representation used by the API."""

        return {
            "series_id": self.series_id,
            "title": self.title,
            "playlist_id": self.playlist_id,
            "start_utc": iso_utc(self.start_utc),
            "end_utc": iso_utc(self.end_utc),
            "original_start_utc": iso_utc(self.original_start_utc),
            "timezone": self.timezone,
            "priority": self.priority,
            "source": self.source,
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
        }


def iso_utc(value: datetime) -> str:
    """Serialize an aware datetime as a canonical RFC 3339 UTC timestamp."""

    if value.tzinfo is None:
        raise RecurrenceError("UTC timestamps must include a timezone offset")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime) -> datetime:
    """Parse an ISO timestamp and normalize it to an aware UTC datetime.

    Naive datetimes are treated as UTC for compatibility with the original
    internal API. Browser-facing schedule inputs use :func:`parse_local` and
    therefore cannot accidentally take this path.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise RecurrenceError(f"Invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _zone(timezone_name: str) -> ZoneInfo:
    """Load an IANA timezone and translate platform errors for API callers."""

    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RecurrenceError(f"Unknown IANA timezone: {timezone_name}") from exc


def _localize(naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach *zone* while rejecting wall times skipped by a DST transition.

    ``datetime.replace(tzinfo=...)`` silently invents an offset for nonexistent
    times such as 02:30 during a spring-forward transition. A UTC round trip
    detects that case. Ambiguous fall-back times deliberately use ``fold=0``
    (the first occurrence), which is stable and documented rather than relying
    on platform-specific guessing.
    """

    aware = naive.replace(tzinfo=zone, fold=0)
    round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        raise RecurrenceError(
            f"Local time {naive.isoformat()} does not exist in {zone.key} because of a DST transition"
        )
    return aware


def parse_local(value: str, timezone_name: str) -> datetime:
    """Parse a timezone-free ISO wall-clock timestamp in an IANA timezone."""

    zone = _zone(timezone_name)
    try:
        local = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("Local start must be a valid ISO 8601 timestamp") from exc
    if local.tzinfo is not None:
        raise RecurrenceError("Local start must not contain a timezone offset")
    return _localize(local, zone)


def _as_local(value: str, zone: ZoneInfo) -> datetime:
    """Convert a stored UTC recurrence date to the schedule's local timezone."""

    try:
        return parse_utc(value).astimezone(zone)
    except (TypeError, ValueError, OverflowError, RecurrenceError) as exc:
        raise RecurrenceError(f"Invalid recurrence date: {value}") from exc


def _canonical_rrule(raw_rule: str, zone: ZoneInfo) -> str:
    """Validate and normalize the supported, single-line RRULE subset.

    The database stores DTSTART, RDATE, and EXDATE separately. Accepting those
    directives inside the RRULE text would create two competing sources of
    truth, so only a single rule body (with an optional ``RRULE:`` prefix) is
    permitted. Frequencies finer than daily are intentionally rejected to
    bound expansion work and match the radio programming UI.
    """

    rule = str(raw_rule).strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule[6:]
    if not rule or any(character in rule for character in "\r\n"):
        raise RecurrenceError("RRULE must be a non-empty single line")

    parameters: dict[str, str] = {}
    for part in rule.split(";"):
        if "=" not in part:
            raise RecurrenceError(f"Invalid RRULE component: {part}")
        key, value = part.split("=", 1)
        key = key.strip().upper()
        if not key or key in parameters:
            raise RecurrenceError(f"Duplicate or empty RRULE component: {key}")
        parameters[key] = value.strip()

    frequency = parameters.get("FREQ", "").upper()
    if frequency not in ALLOWED_RRULE_FREQUENCIES:
        allowed = ", ".join(sorted(ALLOWED_RRULE_FREQUENCIES))
        raise RecurrenceError(f"RRULE FREQ must be one of: {allowed}")
    for numeric_key in ("INTERVAL", "COUNT"):
        if numeric_key not in parameters:
            continue
        try:
            numeric_value = int(parameters[numeric_key])
        except ValueError as exc:
            raise RecurrenceError(f"RRULE {numeric_key} must be an integer") from exc
        maximum = MAX_OCCURRENCE_SCAN if numeric_key == "COUNT" else 10_000
        if numeric_value < 1 or numeric_value > maximum:
            raise RecurrenceError(f"RRULE {numeric_key} must be between 1 and {maximum}")

    # dateutil requires an aware DTSTART to be paired with a UTC UNTIL. The
    # editor may send a local UNTIL, so interpret it in the event's timezone.
    until_pattern = re.compile(r"^(\d{8}(?:T\d{6}Z?)?)$", re.IGNORECASE)
    until = parameters.get("UNTIL")
    if until and until_pattern.match(until) and not until.endswith("Z"):
        try:
            if len(until) == 8:
                # UNTIL without an offset is deliberately parsed as local wall time.
                local_until = datetime.strptime(until, "%Y%m%d").replace(  # noqa: DTZ007
                    hour=23,
                    minute=59,
                    second=59,
                )
            else:
                local_until = datetime.strptime(  # noqa: DTZ007
                    until, "%Y%m%dT%H%M%S"
                )
            parameters["UNTIL"] = (
                _localize(local_until, zone).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
        except ValueError as exc:
            raise RecurrenceError(f"Invalid RRULE UNTIL: {until}") from exc

    return ";".join(f"{key}={value}" for key, value in parameters.items())


def truncate_rrule(raw_rule: str, last_start_utc: datetime) -> str:
    """Return *raw_rule* capped at the supplied final occurrence start.

    ``COUNT`` and ``UNTIL`` are mutually exclusive in RFC 5545. A series split
    therefore removes either existing boundary and writes one UTC ``UNTIL``.
    """

    rule = str(raw_rule).strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule[6:]
    parts = [
        part
        for part in rule.split(";")
        if part and not part.upper().startswith(("UNTIL=", "COUNT="))
    ]
    parts.append(f"UNTIL={parse_utc(last_start_utc).strftime('%Y%m%dT%H%M%SZ')}")
    return ";".join(parts)


def build_rruleset(series: dict[str, Any]) -> tuple[rruleset, ZoneInfo]:
    """Build a recurrence set from one series and its explicit include/exclude dates."""

    timezone_name = str(series.get("timezone") or "")
    zone = _zone(timezone_name)
    dtstart = parse_local(str(series.get("dtstart_local") or ""), timezone_name)
    result = rruleset()
    raw_rule = series.get("rrule")
    if raw_rule:
        try:
            rule = rrulestr(_canonical_rrule(str(raw_rule), zone), dtstart=dtstart)
        except (TypeError, ValueError) as exc:
            raise RecurrenceError(f"Invalid RRULE: {raw_rule}") from exc
        result.rrule(rule)
    else:
        result.rdate(dtstart)

    for raw in series.get("rdates") or []:
        result.rdate(_as_local(str(raw), zone))
    for raw in series.get("exdates") or []:
        result.exdate(_as_local(str(raw), zone))
    for override in series.get("overrides") or []:
        if override.get("action") == "cancel":
            try:
                result.exdate(parse_utc(str(override["original_start_utc"])).astimezone(zone))
            except (KeyError, RecurrenceError):
                continue
    return result, zone


def _duration(series: dict[str, Any]) -> timedelta:
    """Validate and return a series duration as a ``timedelta``."""

    try:
        seconds = int(series.get("duration_seconds"))
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("duration_seconds must be an integer") from exc
    if seconds <= 0 or seconds > MAX_DURATION_SECONDS:
        raise RecurrenceError("duration_seconds must be between 1 second and 7 days")
    return timedelta(seconds=seconds)


def _override_for(series: dict[str, Any], original_start: datetime) -> dict[str, Any] | None:
    """Find the exception keyed to an original recurrence start, if present."""

    target = parse_utc(original_start)
    for override in series.get("overrides") or []:
        try:
            if parse_utc(str(override["original_start_utc"])) == target:
                return override
        except (KeyError, RecurrenceError):
            continue
    return None


def _normalize_candidate(start_local: datetime, zone: ZoneInfo) -> datetime:
    """Normalize a dateutil candidate and reject a generated nonexistent time."""

    naive = start_local.astimezone(zone).replace(tzinfo=None)
    return _localize(naive, zone)


def occurrence_from_start(series: dict[str, Any], start_local: datetime) -> Occurrence | None:
    """Convert an original local start into an interval and apply its exception."""

    zone = _zone(str(series.get("timezone") or ""))
    normalized_start = _normalize_candidate(start_local, zone)
    original_start_utc = normalized_start.astimezone(UTC)
    override = _override_for(series, original_start_utc)
    if override and override.get("action") == "cancel":
        return None

    values = dict(series)
    if override:
        for key in ("title", "playlist_id", "timezone", "duration_seconds", "priority"):
            if override.get(key) is not None:
                values[key] = override[key]
        if override.get("start_local"):
            normalized_start = parse_local(str(override["start_local"]), str(values["timezone"]))

    start_utc = normalized_start.astimezone(UTC)
    end_utc = start_utc + _duration(values)
    return Occurrence(
        series_id=str(values["id"]),
        title=str(values.get("title") or "Untitled show"),
        playlist_id=str(values["playlist_id"]),
        start_utc=start_utc,
        end_utc=end_utc,
        original_start_utc=original_start_utc,
        timezone=str(values["timezone"]),
        priority=int(values.get("priority", 0)),
        source=str(values.get("source", "local")),
    )


def _rule_starts_between(
    rules: rruleset,
    zone: ZoneInfo,
    start_utc: datetime,
    end_utc: datetime,
) -> Iterator[datetime]:
    """Yield bounded recurrence starts without materializing an unbounded list."""

    local_start = start_utc.astimezone(zone)
    local_end = end_utc.astimezone(zone)
    for scanned, candidate in enumerate(rules.xafter(local_start, count=None, inc=True), start=1):
        if candidate > local_end:
            break
        if scanned > MAX_OCCURRENCE_SCAN:
            raise RecurrenceError("Schedule expansion exceeded its safety limit")
        yield candidate


def _moved_override_occurrences(series: dict[str, Any]) -> Iterator[Occurrence]:
    """Yield moved overrides even when their original start is outside a query range."""

    original_zone = _zone(str(series.get("timezone") or ""))
    for override in series.get("overrides") or []:
        if override.get("action") != "override" or not override.get("start_local"):
            continue
        try:
            original = parse_utc(str(override["original_start_utc"])).astimezone(original_zone)
            occurrence = occurrence_from_start(series, original)
        except (KeyError, RecurrenceError, TypeError, ValueError):
            continue
        if occurrence is not None:
            yield occurrence


def _occurrence_key(occurrence: Occurrence) -> tuple[Any, ...]:
    """Build a stable de-duplication key for regular and moved paths."""

    return (
        occurrence.series_id,
        occurrence.start_utc,
        occurrence.end_utc,
        occurrence.original_start_utc,
        occurrence.playlist_id,
        occurrence.title,
    )


def latest_occurrence(series: dict[str, Any], now: datetime) -> Occurrence | None:
    """Return the latest occurrence whose half-open interval contains ``now``."""

    now_utc = parse_utc(now)
    rules, zone = build_rruleset(series)
    candidates: dict[tuple[Any, ...], Occurrence] = {}
    lookback = now_utc - timedelta(seconds=MAX_DURATION_SECONDS)
    for candidate in _rule_starts_between(rules, zone, lookback, now_utc):
        try:
            occurrence = occurrence_from_start(series, candidate)
        except RecurrenceError:
            # A recurring local wall time can be nonexistent on one DST day.
            # Skip that instance without invalidating the entire series.
            continue
        if occurrence and occurrence.start_utc <= now_utc < occurrence.end_utc:
            candidates[_occurrence_key(occurrence)] = occurrence
    for occurrence in _moved_override_occurrences(series):
        if occurrence.start_utc <= now_utc < occurrence.end_utc:
            candidates[_occurrence_key(occurrence)] = occurrence
    return max(candidates.values(), key=lambda item: item.start_utc, default=None)


def next_occurrence_start(series: dict[str, Any], now: datetime) -> datetime | None:
    """Return the earliest effective occurrence start strictly after ``now``."""

    now_utc = parse_utc(now)
    rules, zone = build_rruleset(series)
    starts = [
        occurrence.start_utc
        for occurrence in _moved_override_occurrences(series)
        if occurrence.start_utc > now_utc
    ]
    scanned = 0
    for candidate in rules.xafter(now_utc.astimezone(zone), count=None, inc=False):
        scanned += 1
        if scanned > MAX_OCCURRENCE_SCAN:
            raise RecurrenceError("Finding the next occurrence exceeded its safety limit")
        try:
            occurrence = occurrence_from_start(series, candidate)
        except RecurrenceError:
            continue
        if occurrence and occurrence.start_utc > now_utc:
            starts.append(occurrence.start_utc)
            break
    return min(starts, default=None)


def occurrences_between(
    series: dict[str, Any],
    start: datetime,
    end: datetime,
    limit: int = 500,
) -> list[Occurrence]:
    """Expand occurrences that overlap a bounded half-open UTC range.

    Starts are scanned from seven days before the requested range so a long
    event that began earlier still appears in the calendar. The final result,
    rather than the raw recurrence candidates, is capped by ``limit``.
    """

    if limit <= 0:
        return []
    start_utc, end_utc = parse_utc(start), parse_utc(end)
    if end_utc <= start_utc:
        raise RecurrenceError("Occurrence range end must be after start")
    rules, zone = build_rruleset(series)
    scan_start = start_utc - timedelta(seconds=MAX_DURATION_SECONDS)
    occurrences: dict[tuple[Any, ...], Occurrence] = {}
    for candidate in _rule_starts_between(rules, zone, scan_start, end_utc):
        try:
            occurrence = occurrence_from_start(series, candidate)
        except RecurrenceError:
            continue
        if occurrence and occurrence.end_utc > start_utc and occurrence.start_utc < end_utc:
            occurrences[_occurrence_key(occurrence)] = occurrence
    for occurrence in _moved_override_occurrences(series):
        if occurrence.end_utc > start_utc and occurrence.start_utc < end_utc:
            occurrences[_occurrence_key(occurrence)] = occurrence
    return sorted(
        occurrences.values(),
        key=lambda item: (item.start_utc, -item.priority, item.series_id),
    )[:limit]


def is_original_occurrence_start(series: dict[str, Any], value: datetime) -> bool:
    """Return whether *value* is an included original start in ``series``."""

    target = parse_utc(value)
    # An existing cancellation is itself keyed by an original occurrence.
    # Ignore override-derived exclusions here while preserving explicit EXDATEs.
    rules, zone = build_rruleset({**series, "overrides": []})
    candidate = rules.before(target.astimezone(zone), inc=True)
    return candidate is not None and candidate.astimezone(UTC) == target


def resolve_active(
    series: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[Occurrence | None, datetime | None, list[Occurrence]]:
    """Resolve the winner, next boundary, and all active overlap candidates.

    This is the station's on-air decision: whatever this returns as *winner* is
    what listeners hear.

    Args:
        series: Schedule series rows. Rows with ``enabled`` false are skipped,
            and a row that fails to expand is skipped individually so one
            corrupt program cannot take the whole station off the air.
        now: Instant to resolve at; defaults to the current UTC time. Naive
            values are read as UTC by :func:`parse_utc`.

    Returns:
        ``(winner, next_transition, candidates)``.

        *winner* is the single occurrence that should be on air, or ``None``
        when nothing is scheduled -- the caller then falls back to the default
        playlist. Overlaps are broken by ``(priority, start_utc, series_id)``,
        largest first: higher priority wins; on a tie the *later* start wins, so
        a special layered over a long block takes over rather than being buried
        by it; ``series_id`` is a final tiebreak that exists purely to make the
        result deterministic rather than dependent on input order.

        *next_transition* is the earliest end-of-active or next-start strictly
        after ``now``, or ``None`` when no further change is known. It is a hint
        for scheduling a wakeup, not a promise that state will change then.

        *candidates* holds every occurrence active at ``now``, including the
        winner. Losers are returned as *copies* with ``suppressed`` set and
        ``suppression_reason`` naming the winning series, so the admin calendar
        can explain a conflict instead of hiding it. The originals are not
        mutated.

    """

    now_utc = parse_utc(now or datetime.now(UTC))
    candidates: list[Occurrence] = []
    next_times: list[datetime] = []
    for item in series:
        if not item.get("enabled", True):
            continue
        try:
            active = latest_occurrence(item, now_utc)
            if active:
                candidates.append(active)
                next_times.append(active.end_utc)
            upcoming = next_occurrence_start(item, now_utc)
            if upcoming:
                next_times.append(upcoming)
        except RecurrenceError:
            # Validation normally prevents bad rows. Keeping resolution
            # isolated per series ensures one legacy/corrupt row cannot stop
            # every valid radio program.
            continue

    winner: Occurrence | None = None
    if candidates:
        winner = max(
            candidates,
            key=lambda occurrence: (
                occurrence.priority,
                occurrence.start_utc,
                occurrence.series_id,
            ),
        )
        candidates = [
            candidate
            if candidate.series_id == winner.series_id
            else replace(
                candidate,
                suppressed=True,
                suppression_reason=f"winner:{winner.series_id}",
            )
            for candidate in candidates
        ]
    next_transition = min(
        (value for value in next_times if value > now_utc),
        default=None,
    )
    return winner, next_transition, candidates


def validate_series(values: dict[str, Any]) -> None:
    """Validate a complete schedule draft without touching persistence."""

    required = ("id", "playlist_id", "dtstart_local", "timezone")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RecurrenceError(f"Missing schedule fields: {', '.join(missing)}")

    title = str(values.get("title") or "").strip()
    if not title or len(title) > 200:
        raise RecurrenceError("title must contain between 1 and 200 characters")
    if len(str(values["playlist_id"])) > 100:
        raise RecurrenceError("playlist_id is too long")

    _duration(values)
    try:
        priority = int(values.get("priority", 0))
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("priority must be an integer") from exc
    if priority < -1000 or priority > 1000:
        raise RecurrenceError("priority must be between -1000 and 1000")

    if values.get("transition_policy", "immediate") != "immediate":
        raise RecurrenceError("Only the immediate transition policy is supported")
    if values.get("source", "local") not in {"local", "google"}:
        raise RecurrenceError("Unknown schedule source")
    build_rruleset(values)
