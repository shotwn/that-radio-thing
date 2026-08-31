-- Bring databases created by the pre-release in-code v1 migration up to the
-- same query-index baseline as new installations. Every statement is
-- idempotent so fresh databases may already have these indexes from v1.

CREATE INDEX IF NOT EXISTS idx_schedule_enabled
    ON schedule_series(enabled, dtstart_local);
CREATE INDEX IF NOT EXISTS idx_schedule_playlist
    ON schedule_series(playlist_id);
CREATE INDEX IF NOT EXISTS idx_schedule_external
    ON schedule_series(source, external_calendar_id, external_event_id);
CREATE INDEX IF NOT EXISTS idx_override_playlist
    ON schedule_overrides(playlist_id);
CREATE INDEX IF NOT EXISTS idx_audit_occurred
    ON audit_log(occurred_at DESC);
