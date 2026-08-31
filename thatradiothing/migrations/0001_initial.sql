-- Initial persistent radio-programming schema.
--
-- Migrations are append-only. Never edit a migration after deployment; add a
-- new numbered file and register it in thatradiothing.db.MIGRATIONS instead.

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE playlists (
    id TEXT PRIMARY KEY,
    spotify_id TEXT NOT NULL UNIQUE,
    spotify_uri TEXT NOT NULL UNIQUE,
    name TEXT,
    external_url TEXT,
    image_url TEXT,
    catalog_json TEXT,
    catalog_revision TEXT,
    validated_at TEXT,
    validation_error TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE schedule_series (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    playlist_id TEXT NOT NULL REFERENCES playlists(id),
    dtstart_local TEXT NOT NULL,
    timezone TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL
        CHECK (duration_seconds BETWEEN 1 AND 604800),
    rrule TEXT,
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -1000 AND 1000),
    transition_policy TEXT NOT NULL DEFAULT 'immediate'
        CHECK (transition_policy = 'immediate'),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'google')),
    external_calendar_id TEXT,
    external_event_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE schedule_dates (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES schedule_series(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('rdate', 'exdate')),
    occurrence_start_utc TEXT NOT NULL,
    UNIQUE(series_id, kind, occurrence_start_utc)
);

CREATE TABLE schedule_overrides (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES schedule_series(id) ON DELETE CASCADE,
    original_start_utc TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('cancel', 'override')),
    title TEXT,
    playlist_id TEXT REFERENCES playlists(id),
    start_local TEXT,
    timezone TEXT,
    duration_seconds INTEGER CHECK (
        duration_seconds IS NULL OR duration_seconds BETWEEN 1 AND 604800
    ),
    priority INTEGER CHECK (priority IS NULL OR priority BETWEEN -1000 AND 1000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    UNIQUE(series_id, original_start_utc)
);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    actor_spotify_id TEXT NOT NULL,
    actor_display_name TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    request_id TEXT,
    occurred_at TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);

CREATE INDEX idx_schedule_enabled
    ON schedule_series(enabled, dtstart_local);
CREATE INDEX idx_schedule_playlist
    ON schedule_series(playlist_id);
CREATE INDEX idx_schedule_external
    ON schedule_series(source, external_calendar_id, external_event_id);
CREATE INDEX idx_override_playlist
    ON schedule_overrides(playlist_id);
CREATE INDEX idx_audit_occurred
    ON audit_log(occurred_at DESC);
