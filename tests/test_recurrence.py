"""Unit tests for timezone-aware recurrence and overlap resolution."""

import unittest
from datetime import UTC, datetime

from thatradiothing.recurrence import (
    RecurrenceError,
    is_original_occurrence_start,
    next_occurrence_start,
    occurrences_between,
    resolve_active,
    truncate_rrule,
    validate_series,
)


class RecurrenceTests(unittest.TestCase):
    """Exercise recurrence behavior without persistence or wall-clock I/O."""

    def base(self, **changes):
        """Build a valid weekly series and apply test-specific changes."""

        value = {
            "id": "series-1",
            "title": "Show",
            "playlist_id": "playlist-1",
            "dtstart_local": "2026-08-03T09:00:00",
            "timezone": "Europe/Istanbul",
            "duration_seconds": 3600,
            "rrule": "FREQ=WEEKLY;BYDAY=MO,WE",
            "priority": 0,
            "enabled": True,
        }
        value.update(changes)
        return value

    def test_weekly_occurrences_use_local_timezone(self):
        """Keep local show time stable while returning UTC API values."""

        items = occurrences_between(
            self.base(),
            datetime(2026, 8, 3, tzinfo=UTC),
            datetime(2026, 8, 10, tzinfo=UTC),
        )
        self.assertEqual([item.start_utc.hour for item in items], [6, 6])
        self.assertEqual([item.start_utc.weekday() for item in items], [0, 2])

    def test_exact_end_is_not_active(self):
        """Treat event intervals as half-open so adjacent shows do not overlap."""

        series = self.base(rrule=None, dtstart_local="2026-08-03T09:00:00")
        self.assertIsNotNone(
            resolve_active([series], datetime(2026, 8, 3, 6, 59, 59, tzinfo=UTC))[0]
        )
        self.assertIsNone(resolve_active([series], datetime(2026, 8, 3, 7, tzinfo=UTC))[0])

    def test_nth_weekday_and_local_until(self):
        """Support monthly positional rules and local finite boundaries."""

        series = self.base(
            rrule="FREQ=MONTHLY;BYDAY=FR;BYSETPOS=3;UNTIL=20260331T090000",
            dtstart_local="2026-01-01T09:00:00",
        )
        items = occurrences_between(
            series,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 4, 1, tzinfo=UTC),
        )
        self.assertEqual(
            [item.start_utc.date().isoformat() for item in items],
            ["2026-01-16", "2026-02-20", "2026-03-20"],
        )

    def test_priority_resolves_overlap(self):
        """Choose the explicit higher-priority event and mark its competitor."""

        low = self.base(id="low", playlist_id="low", rrule=None, priority=0)
        high = self.base(id="high", playlist_id="high", rrule=None, priority=5)
        winner, _, visible = resolve_active(
            [low, high],
            datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
        )
        self.assertEqual(winner.series_id, "high")
        self.assertTrue(next(item for item in visible if item.series_id == "low").suppressed)

    def test_range_includes_occurrence_that_started_before_window(self):
        """Include long events overlapping the left edge of a calendar range."""

        series = self.base(rrule=None, duration_seconds=7200)
        items = occurrences_between(
            series,
            datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 8, tzinfo=UTC),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].start_utc, datetime(2026, 8, 3, 6, tzinfo=UTC))

    def test_moved_override_is_found_outside_original_query_position(self):
        """Find an edit-one occurrence by its moved start, not only original start."""

        series = self.base(
            rrule=None,
            overrides=[
                {
                    "action": "override",
                    "original_start_utc": "2026-08-03T06:00:00Z",
                    "start_local": "2026-08-05T10:00:00",
                    "timezone": "Europe/Istanbul",
                }
            ],
        )
        items = occurrences_between(
            series,
            datetime(2026, 8, 5, 6, tzinfo=UTC),
            datetime(2026, 8, 5, 9, tzinfo=UTC),
        )
        self.assertEqual([item.start_utc.hour for item in items], [7])
        winner, _, _ = resolve_active(
            [series],
            datetime(2026, 8, 5, 7, 30, tzinfo=UTC),
        )
        self.assertIsNotNone(winner)

    def test_cancelled_next_occurrence_advances_to_following_start(self):
        """Do not lose a series when its next recurrence is cancelled."""

        series = self.base(
            overrides=[
                {
                    "action": "cancel",
                    "original_start_utc": "2026-08-03T06:00:00Z",
                }
            ]
        )
        next_start = next_occurrence_start(
            series,
            datetime(2026, 8, 3, 5, tzinfo=UTC),
        )
        self.assertEqual(next_start, datetime(2026, 8, 5, 6, tzinfo=UTC))
        self.assertTrue(
            is_original_occurrence_start(
                series,
                datetime(2026, 8, 3, 6, tzinfo=UTC),
            )
        )

    def test_nonexistent_dst_instance_is_skipped_without_losing_series(self):
        """Skip only the spring-forward gap and retain later weekly instances."""

        series = self.base(
            dtstart_local="2026-03-22T02:30:00",
            timezone="Europe/Berlin",
            rrule="FREQ=WEEKLY;BYDAY=SU",
        )
        items = occurrences_between(
            series,
            datetime(2026, 3, 21, tzinfo=UTC),
            datetime(2026, 4, 6, tzinfo=UTC),
        )
        self.assertEqual(
            [item.start_utc.date().isoformat() for item in items],
            ["2026-03-22", "2026-04-05"],
        )

    def test_unsupported_high_frequency_and_future_policy_are_rejected(self):
        """Bound recurrence work and expose only behavior implemented in v1."""

        with self.assertRaises(RecurrenceError):
            validate_series(self.base(rrule="FREQ=SECONDLY"))
        with self.assertRaises(RecurrenceError):
            validate_series(self.base(transition_policy="finish_current_track"))
        with self.assertRaises(RecurrenceError):
            validate_series(
                self.base(
                    dtstart_local="2026-03-29T02:30:00",
                    timezone="Europe/Berlin",
                )
            )

    def test_truncate_rrule_replaces_count_with_utc_until(self):
        """Produce a valid finite predecessor during this-and-future edits."""

        result = truncate_rrule(
            "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=20",
            datetime(2026, 8, 31, 6, tzinfo=UTC),
        )
        self.assertEqual(
            result,
            "FREQ=WEEKLY;BYDAY=MO;UNTIL=20260831T060000Z",
        )


if __name__ == "__main__":
    unittest.main()
