"""SQLite persistence for radio settings, playlists, schedules, and audit data.

The service deliberately keeps this layer small.  SQLite is a good fit while
the radio has one coordinator process, but all callers go through this module
so a future move to PostgreSQL does not leak SQL throughout playback code.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "0001_initial.sql"),
    (2, "0002_hardening_indexes.sql"),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def utc_now() -> str:
    """Return a sortable UTC timestamp in RFC3339 form."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    """Return an application identifier suitable for SQLite text keys."""

    return str(uuid.uuid4())


class RadioDatabase:
    """Async SQLite repository with one connection and serialized writers."""

    def __init__(
        self,
        path: str,
        bootstrap_playlists: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        """Configure the repository without opening a filesystem resource yet.

        Args:
            path: SQLite database file path. Parent directories are created by
                :meth:`open`.
            bootstrap_playlists: Legacy ``TRT_PLAYLISTS`` entries imported only
                when the database does not already contain a playlist.

        """

        self.path = path
        self.bootstrap_playlists = list(bootstrap_playlists or [])
        self.connection: aiosqlite.Connection | None = None
        # A transaction belongs to the connection, not to an asyncio task.
        # Serializing reads as well as writes prevents an unrelated read from
        # accidentally running inside another task's open transaction.
        self._connection_lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database, apply migrations, and import legacy settings."""

        if self.connection is not None:
            return

        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        await self._migrate()
        await self._bootstrap_legacy_playlists()

    async def close(self) -> None:
        """Close the connection, if it was opened."""

        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        """Return the live connection or fail with an actionable lifecycle error."""

        if self.connection is None:
            raise RuntimeError("RadioDatabase.open() must be awaited before use")
        return self.connection

    async def _migrate(self) -> None:
        """Apply every schema migration newer than SQLite's ``user_version``."""

        connection = self._require_connection()
        cursor = await connection.execute("PRAGMA user_version")
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {current} is newer than supported {SCHEMA_VERSION}"
            )

        migration_root = Path(__file__).with_name("migrations")
        for version, filename in MIGRATIONS:
            if version <= current:
                continue
            script = await asyncio.to_thread(
                (migration_root / filename).read_text,
                encoding="utf-8",
            )
            # ``executescript`` otherwise commits implicitly before running.
            # Explicit SQL transaction statements keep each migration and its
            # version marker atomic even if a statement fails in the middle.
            async with self._connection_lock:
                try:
                    await connection.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except Exception:
                    await connection.rollback()
                    raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a serialized transaction and roll it back on any exception."""

        connection = self._require_connection()
        async with self._connection_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def fetch_one(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        """Execute a read query and return its first row as a plain dictionary."""

        connection = self._require_connection()
        async with self._connection_lock:
            cursor = await connection.execute(query, parameters)
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return dict(row) if row else None

    async def fetch_all(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute a read query and return all rows as plain dictionaries."""

        connection = self._require_connection()
        async with self._connection_lock:
            cursor = await connection.execute(query, parameters)
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [dict(row) for row in rows]

    async def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
        """Execute one write statement in a committed transaction."""

        async with self.transaction() as connection:
            await connection.execute(query, parameters)

    async def _bootstrap_legacy_playlists(self) -> None:
        """Import the old TRT_PLAYLISTS JSON only into an empty database."""

        existing = await self.fetch_one("SELECT id FROM playlists LIMIT 1")
        if existing or not self.bootstrap_playlists:
            return

        actor = "bootstrap"
        imported: list[str] = []
        async with self.transaction() as connection:
            for raw in self.bootstrap_playlists:
                if not isinstance(raw, dict):
                    continue
                uri = str(raw.get("uri") or "").strip()
                if not uri:
                    continue
                spotify_id = playlist_id_from_uri(uri)
                if not spotify_id:
                    continue
                playlist_id = new_id()
                now = utc_now()
                await connection.execute(
                    """INSERT OR IGNORE INTO playlists
                       (id, spotify_id, spotify_uri, name, external_url, image_url,
                        catalog_json, catalog_revision, validated_at, validation_error,
                        enabled, created_at, updated_at, created_by, updated_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        playlist_id,
                        spotify_id,
                        f"spotify:playlist:{spotify_id}",
                        raw.get("name"),
                        f"https://open.spotify.com/playlist/{spotify_id}",
                        raw.get("image_url"),
                        json.dumps(raw.get("catalog")) if raw.get("catalog") else None,
                        raw.get("catalog_revision"),
                        raw.get("validated_at"),
                        raw.get("validation_error"),
                        now,
                        now,
                        actor,
                        actor,
                    ),
                )
                row = await connection.execute(
                    "SELECT id FROM playlists WHERE spotify_id = ?", (spotify_id,)
                )
                selected = await row.fetchone()
                if selected:
                    imported.append(str(selected[0]))

            if imported:
                await connection.execute(
                    """INSERT OR IGNORE INTO settings
                       (key, value_json, version, updated_at, updated_by)
                       VALUES ('default_playlist_id', ?, 1, ?, ?)""",
                    (json.dumps(imported[0]), utc_now(), actor),
                )

    async def get_setting(self, key: str, default: Any = None) -> Any:
        """Return one decoded setting value or *default* when it is absent/invalid."""

        row = await self.fetch_one("SELECT value_json FROM settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    async def get_settings(self) -> dict[str, Any]:
        """Return every setting with its optimistic-lock and audit metadata."""

        rows = await self.fetch_all(
            "SELECT key, value_json, version, updated_at, updated_by FROM settings"
        )
        result: dict[str, Any] = {}
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                value = None
            result[row["key"]] = {
                "value": value,
                "version": row["version"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
        return result

    async def set_settings(
        self,
        values: Mapping[str, Any],
        actor: str,
        expected_versions: Mapping[str, int | None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Update multiple settings atomically after checking all versions.

        Version checks happen before any write in the same ``BEGIN IMMEDIATE``
        transaction. A stale field therefore cannot leave the earlier fields
        from the same HTTP request committed.

        Args:
            values: Mapping of setting keys to JSON-serializable values.
            actor: Spotify ID or system label responsible for the update.
            expected_versions: Optional current version for each edited key.

        Returns:
            Updated records keyed by setting name.

        Raises:
            VersionConflict: If any supplied expected version is stale.
            TypeError: If a value cannot be encoded as JSON.

        """

        if not values:
            return {}
        now = utc_now()
        expectations = dict(expected_versions or {})
        current_versions: dict[str, int] = {}
        async with self.transaction() as connection:
            placeholders = ",".join("?" for _ in values)
            cursor = await connection.execute(
                f"SELECT key, version FROM settings WHERE key IN ({placeholders})",
                tuple(values),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
            current_versions = {str(row[0]): int(row[1]) for row in rows}
            for key in values:
                current_version = current_versions.get(key, 0)
                expected = expectations.get(key)
                if expected is not None and current_version != int(expected):
                    raise VersionConflict(key, int(expected), current_version)

            for key, value in values.items():
                next_version = current_versions.get(key, 0) + 1
                encoded = json.dumps(value, separators=(",", ":"))
                await connection.execute(
                    """INSERT INTO settings
                       (key, value_json, version, updated_at, updated_by)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value_json=excluded.value_json,
                         version=excluded.version,
                         updated_at=excluded.updated_at,
                         updated_by=excluded.updated_by""",
                    (key, encoded, next_version, now, actor),
                )

        return {
            key: {
                "key": key,
                "value": value,
                "version": current_versions.get(key, 0) + 1,
                "updated_at": now,
                "updated_by": actor,
            }
            for key, value in values.items()
        }

    async def set_setting(
        self,
        key: str,
        value: Any,
        actor: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Update one setting through the same atomic path as batch updates."""

        updated = await self.set_settings(
            {key: value},
            actor,
            {key: expected_version} if expected_version is not None else None,
        )
        return updated[key]

    async def list_playlists(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Return playlist records, including cached catalogs for playback code."""

        where = "" if include_disabled else "WHERE enabled = 1"
        rows = await self.fetch_all(
            f"SELECT * FROM playlists {where} ORDER BY lower(COALESCE(name, spotify_uri))"
        )
        return [decode_playlist(row) for row in rows]

    async def list_playlist_summaries(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        """Return admin-safe playlist metadata without shipping every track."""

        where = "" if include_disabled else "WHERE enabled = 1"
        rows = await self.fetch_all(
            f"""SELECT id, spotify_id, spotify_uri, name, external_url,
                       image_url, catalog_revision, validated_at,
                       validation_error, enabled, created_at, updated_at,
                       created_by, updated_by,
                       CASE
                         WHEN json_valid(catalog_json)
                         THEN COALESCE(json_array_length(catalog_json, '$.tracks'), 0)
                         ELSE 0
                       END AS track_count
                FROM playlists {where}
                ORDER BY lower(COALESCE(name, spotify_uri))"""
        )
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        return rows

    async def get_playlist(self, playlist_id: str) -> dict[str, Any] | None:
        """Return one playlist by local ID, including its cached track catalog."""

        row = await self.fetch_one("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        return decode_playlist(row) if row else None

    async def get_playlist_by_uri(self, spotify_uri: str) -> dict[str, Any] | None:
        """Return one playlist matching an exact canonical Spotify URI."""

        row = await self.fetch_one("SELECT * FROM playlists WHERE spotify_uri = ?", (spotify_uri,))
        return decode_playlist(row) if row else None

    async def upsert_playlist(self, playlist: dict[str, Any], actor: str) -> dict[str, Any]:
        """Insert a Spotify playlist or refresh its mutable metadata and catalog."""

        now = utc_now()
        spotify_id = playlist_id_from_uri(
            str(playlist.get("spotify_uri") or playlist.get("uri") or "")
        )
        if not spotify_id:
            raise ValueError("spotify_uri must be a Spotify playlist URI or URL")
        spotify_uri = f"spotify:playlist:{spotify_id}"
        catalog = playlist.get("catalog")
        async with self.transaction() as connection:
            existing_cursor = await connection.execute(
                "SELECT id, enabled FROM playlists WHERE spotify_id = ?",
                (spotify_id,),
            )
            existing = await existing_cursor.fetchone()
            playlist_id = str(existing[0]) if existing else new_id()
            enabled = (
                bool(playlist["enabled"])
                if "enabled" in playlist
                else bool(existing[1])
                if existing
                else True
            )
            await connection.execute(
                """INSERT INTO playlists
                   (id, spotify_id, spotify_uri, name, external_url, image_url,
                    catalog_json, catalog_revision, validated_at, validation_error,
                    enabled, created_at, updated_at, created_by, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(spotify_id) DO UPDATE SET spotify_uri=excluded.spotify_uri,
                    name=excluded.name, external_url=excluded.external_url,
                    image_url=excluded.image_url, catalog_json=COALESCE(excluded.catalog_json, playlists.catalog_json),
                    catalog_revision=COALESCE(excluded.catalog_revision, playlists.catalog_revision),
                    validated_at=COALESCE(excluded.validated_at, playlists.validated_at),
                    validation_error=excluded.validation_error, enabled=excluded.enabled,
                    updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (
                    playlist_id,
                    spotify_id,
                    spotify_uri,
                    playlist.get("name"),
                    playlist.get("external_url")
                    or f"https://open.spotify.com/playlist/{spotify_id}",
                    playlist.get("image_url"),
                    json.dumps(catalog, separators=(",", ":")) if catalog is not None else None,
                    playlist.get("catalog_revision"),
                    playlist.get("validated_at") or (now if catalog is not None else None),
                    playlist.get("validation_error"),
                    1 if enabled else 0,
                    playlist.get("created_at") or now,
                    now,
                    playlist.get("created_by") or actor,
                    actor,
                ),
            )
            row_cursor = await connection.execute(
                "SELECT * FROM playlists WHERE spotify_id = ?", (spotify_id,)
            )
            row = await row_cursor.fetchone()
        return decode_playlist(dict(row)) if row else {}

    async def patch_playlist(
        self, playlist_id: str, values: dict[str, Any], actor: str
    ) -> dict[str, Any] | None:
        """Patch repository-controlled playlist fields and return the new record."""

        allowed = {
            "name",
            "enabled",
            "validation_error",
            "catalog_revision",
            "validated_at",
            "catalog",
            "image_url",
            "external_url",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return await self.get_playlist(playlist_id)
        columns: list[str] = []
        parameters: list[Any] = []
        for key, value in values.items():
            column = "catalog_json" if key == "catalog" else key
            columns.append(f"{column} = ?")
            parameters.append(
                json.dumps(value, separators=(",", ":"))
                if key == "catalog"
                else (1 if value is True else 0 if value is False else value)
            )
        columns += ["updated_at = ?", "updated_by = ?"]
        parameters += [utc_now(), actor, playlist_id]
        async with self.transaction() as connection:
            await connection.execute(
                f"UPDATE playlists SET {', '.join(columns)} WHERE id = ?", tuple(parameters)
            )
        return await self.get_playlist(playlist_id)

    async def list_schedule_series(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Return schedule series with their dates and per-occurrence overrides."""

        where = "WHERE enabled = 1" if not include_disabled else ""
        rows = await self.fetch_all(
            f"SELECT * FROM schedule_series {where} ORDER BY dtstart_local, priority DESC"
        )
        for row in rows:
            row["rdates"] = await self._schedule_dates(row["id"], "rdate")
            row["exdates"] = await self._schedule_dates(row["id"], "exdate")
            row["overrides"] = await self.fetch_all(
                "SELECT * FROM schedule_overrides WHERE series_id = ?", (row["id"],)
            )
        return rows

    async def get_schedule_series(self, series_id: str) -> dict[str, Any] | None:
        """Return one complete schedule series or ``None`` when it is absent."""

        row = await self.fetch_one("SELECT * FROM schedule_series WHERE id = ?", (series_id,))
        if not row:
            return None
        row["rdates"] = await self._schedule_dates(series_id, "rdate")
        row["exdates"] = await self._schedule_dates(series_id, "exdate")
        row["overrides"] = await self.fetch_all(
            "SELECT * FROM schedule_overrides WHERE series_id = ?", (series_id,)
        )
        return row

    async def _schedule_dates(self, series_id: str, kind: str) -> list[str]:
        """Load ordered RDATE or EXDATE values for one series."""

        rows = await self.fetch_all(
            "SELECT occurrence_start_utc FROM schedule_dates WHERE series_id = ? AND kind = ? ORDER BY occurrence_start_utc",
            (series_id, kind),
        )
        return [str(row["occurrence_start_utc"]) for row in rows]

    async def create_schedule_series(self, values: dict[str, Any], actor: str) -> dict[str, Any]:
        """Create a version-one schedule series and its explicit dates atomically."""

        series_id = str(values.get("id") or new_id())
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """INSERT INTO schedule_series
                   (id, title, playlist_id, dtstart_local, timezone, duration_seconds,
                    rrule, priority, transition_policy, enabled, source,
                    external_calendar_id, external_event_id, version,
                    created_at, updated_at, created_by, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    series_id,
                    values["title"],
                    values["playlist_id"],
                    values["dtstart_local"],
                    values["timezone"],
                    int(values["duration_seconds"]),
                    values.get("rrule"),
                    int(values.get("priority", 0)),
                    values.get("transition_policy", "immediate"),
                    1 if values.get("enabled", True) else 0,
                    values.get("source", "local"),
                    values.get("external_calendar_id"),
                    values.get("external_event_id"),
                    now,
                    now,
                    actor,
                    actor,
                ),
            )
            await self._replace_dates(connection, series_id, values.get("rdates", []), "rdate")
            await self._replace_dates(connection, series_id, values.get("exdates", []), "exdate")
        created = await self.get_schedule_series(series_id)
        if not created:
            raise RuntimeError("schedule insert did not return a row")
        return created

    async def split_schedule_series(
        self,
        series_id: str,
        old_values: dict[str, Any],
        future_values: dict[str, Any],
        actor: str,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """End one series and insert its successor in one transaction."""

        future_id = str(future_values.get("id") or new_id())
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT version FROM schedule_series WHERE id = ?", (series_id,)
            )
            current = await cursor.fetchone()
            if not current:
                return None, None
            current_version = int(current[0])
            if expected_version is not None and current_version != expected_version:
                raise VersionConflict(series_id, expected_version, current_version)

            fields = {
                key: old_values[key]
                for key in (
                    "title",
                    "playlist_id",
                    "dtstart_local",
                    "timezone",
                    "duration_seconds",
                    "rrule",
                    "priority",
                    "transition_policy",
                    "enabled",
                    "source",
                    "external_calendar_id",
                    "external_event_id",
                )
                if key in old_values
            }
            assignments = [f"{key} = ?" for key in fields]
            parameters: list[Any] = [
                1 if value is True else 0 if value is False else value for value in fields.values()
            ]
            assignments += ["version = ?", "updated_at = ?", "updated_by = ?"]
            parameters += [current_version + 1, now, actor, series_id]
            await connection.execute(
                f"UPDATE schedule_series SET {', '.join(assignments)} WHERE id = ?",
                tuple(parameters),
            )

            await connection.execute(
                """INSERT INTO schedule_series
                   (id, title, playlist_id, dtstart_local, timezone, duration_seconds,
                    rrule, priority, transition_policy, enabled, source,
                    external_calendar_id, external_event_id, version,
                    created_at, updated_at, created_by, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    future_id,
                    future_values["title"],
                    future_values["playlist_id"],
                    future_values["dtstart_local"],
                    future_values["timezone"],
                    int(future_values["duration_seconds"]),
                    future_values.get("rrule"),
                    int(future_values.get("priority", 0)),
                    future_values.get("transition_policy", "immediate"),
                    1 if future_values.get("enabled", True) else 0,
                    future_values.get("source", "local"),
                    future_values.get("external_calendar_id"),
                    future_values.get("external_event_id"),
                    now,
                    now,
                    actor,
                    actor,
                ),
            )
            await self._replace_dates(
                connection, future_id, future_values.get("rdates", []), "rdate"
            )
            await self._replace_dates(
                connection, future_id, future_values.get("exdates", []), "exdate"
            )
            if "rdates" in old_values:
                await self._replace_dates(
                    connection,
                    series_id,
                    old_values["rdates"],
                    "rdate",
                )
            if "exdates" in old_values:
                await self._replace_dates(
                    connection,
                    series_id,
                    old_values["exdates"],
                    "exdate",
                )
            future_override_ids = old_values.get("_future_override_ids", [])
            if future_override_ids:
                placeholders = ",".join("?" for _ in future_override_ids)
                await connection.execute(
                    f"""UPDATE schedule_overrides
                        SET series_id = ?, updated_at = ?, updated_by = ?
                        WHERE series_id = ? AND id IN ({placeholders})""",
                    (
                        future_id,
                        now,
                        actor,
                        series_id,
                        *future_override_ids,
                    ),
                )

        return await self.get_schedule_series(series_id), await self.get_schedule_series(future_id)

    async def update_schedule_series(
        self,
        series_id: str,
        values: dict[str, Any],
        actor: str,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Patch a series under an optional optimistic version precondition."""

        fields = {
            key: values[key]
            for key in (
                "title",
                "playlist_id",
                "dtstart_local",
                "timezone",
                "duration_seconds",
                "rrule",
                "priority",
                "transition_policy",
                "enabled",
                "source",
                "external_calendar_id",
                "external_event_id",
            )
            if key in values
        }
        if fields or "rdates" in values or "exdates" in values:
            assignments = [f"{key} = ?" for key in fields]
            parameters: list[Any] = [
                1 if value is True else 0 if value is False else value for value in fields.values()
            ]
            assignments += ["version = ?", "updated_at = ?", "updated_by = ?"]
            async with self.transaction() as connection:
                cursor = await connection.execute(
                    "SELECT version FROM schedule_series WHERE id = ?", (series_id,)
                )
                current = await cursor.fetchone()
                if not current:
                    return None
                current_version = int(current[0])
                if expected_version is not None and current_version != expected_version:
                    raise VersionConflict(series_id, expected_version, current_version)
                parameters += [current_version + 1, utc_now(), actor, series_id]
                await connection.execute(
                    f"UPDATE schedule_series SET {', '.join(assignments)} WHERE id = ?",
                    tuple(parameters),
                )
                if "rdates" in values:
                    await self._replace_dates(connection, series_id, values["rdates"], "rdate")
                if "exdates" in values:
                    await self._replace_dates(connection, series_id, values["exdates"], "exdate")
        elif expected_version is not None:
            current = await self.get_schedule_series(series_id)
            if not current:
                return None
            if int(current["version"]) != expected_version:
                raise VersionConflict(series_id, expected_version, int(current["version"]))
        return await self.get_schedule_series(series_id)

    async def _replace_dates(
        self, connection: aiosqlite.Connection, series_id: str, dates: Iterable[str], kind: str
    ) -> None:
        """Replace all explicit recurrence dates of one kind within a transaction."""

        await connection.execute(
            "DELETE FROM schedule_dates WHERE series_id = ? AND kind = ?", (series_id, kind)
        )
        for value in dates:
            await connection.execute(
                "INSERT INTO schedule_dates(id, series_id, kind, occurrence_start_utc) VALUES (?, ?, ?, ?)",
                (new_id(), series_id, kind, value),
            )

    async def delete_schedule_series(
        self,
        series_id: str,
        actor: str,
        expected_version: int | None = None,
    ) -> bool:
        """Delete a schedule series after an optional optimistic version check."""

        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT version FROM schedule_series WHERE id = ?",
                (series_id,),
            )
            current = await cursor.fetchone()
            if not current:
                return False
            current_version = int(current[0])
            if expected_version is not None and current_version != expected_version:
                raise VersionConflict(series_id, expected_version, current_version)
            cursor = await connection.execute(
                "DELETE FROM schedule_series WHERE id = ?", (series_id,)
            )
            return cursor.rowcount > 0

    async def add_override(
        self,
        series_id: str,
        values: dict[str, Any],
        actor: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Create one cancellation or edit override keyed by original UTC start."""

        override_id = new_id()
        now = utc_now()
        async with self.transaction() as connection:
            version_cursor = await connection.execute(
                "SELECT version FROM schedule_series WHERE id = ?",
                (series_id,),
            )
            current = await version_cursor.fetchone()
            if not current:
                raise KeyError(series_id)
            current_version = int(current[0])
            if current_version != expected_version:
                raise VersionConflict(series_id, expected_version, current_version)
            await connection.execute(
                """INSERT INTO schedule_overrides
                   (id, series_id, original_start_utc, action, title, playlist_id,
                    start_local, timezone, duration_seconds, priority, created_at,
                    updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    override_id,
                    series_id,
                    values["original_start_utc"],
                    values["action"],
                    values.get("title"),
                    values.get("playlist_id"),
                    values.get("start_local"),
                    values.get("timezone"),
                    values.get("duration_seconds"),
                    values.get("priority"),
                    now,
                    now,
                    actor,
                ),
            )
            await connection.execute(
                """UPDATE schedule_series
                   SET version = ?, updated_at = ?, updated_by = ?
                   WHERE id = ?""",
                (current_version + 1, now, actor, series_id),
            )
        row = await self.fetch_one("SELECT * FROM schedule_overrides WHERE id = ?", (override_id,))
        return row or {}

    async def add_audit(
        self,
        *,
        actor_id: str,
        actor_display_name: str | None,
        action: str,
        entity_type: str,
        entity_id: str | None,
        request_id: str | None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        """Append an immutable administrative audit record."""

        await self.execute(
            """INSERT INTO audit_log
               (id, actor_spotify_id, actor_display_name, action, entity_type,
                entity_id, request_id, occurred_at, before_json, after_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id(),
                actor_id,
                actor_display_name,
                action,
                entity_type,
                entity_id,
                request_id,
                utc_now(),
                json.dumps(before, separators=(",", ":")) if before is not None else None,
                json.dumps(after, separators=(",", ":")) if after is not None else None,
            ),
        )

    async def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest audit records with a defensive upper bound."""

        return await self.fetch_all(
            "SELECT * FROM audit_log ORDER BY occurred_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        )

    async def playlist_reference_count(self, playlist_id: str) -> int:
        """Count series and overrides that prevent safe playlist deletion."""

        row = await self.fetch_one(
            """SELECT
                   (SELECT COUNT(*) FROM schedule_series WHERE playlist_id = ?)
                   +
                   (SELECT COUNT(*) FROM schedule_overrides WHERE playlist_id = ?)
                   AS count""",
            (playlist_id, playlist_id),
        )
        return int(row["count"]) if row else 0

    async def delete_playlist(self, playlist_id: str) -> bool:
        """Delete an unreferenced playlist and report whether a row existed."""

        async with self.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM playlists WHERE id = ?",
                (playlist_id,),
            )
            return cursor.rowcount > 0


class VersionConflict(RuntimeError):
    """Raised when an administrator edits a stale record version."""

    def __init__(self, entity: str, expected: int, actual: int):
        """Record the stale expectation and authoritative database version."""

        super().__init__(f"Version conflict for {entity}: expected {expected}, actual {actual}")
        self.entity = entity
        self.expected = expected
        self.actual = actual


def playlist_id_from_uri(value: str) -> str | None:
    """Extract and validate a Spotify playlist ID from URI or public URL."""

    value = value.strip()
    if value.startswith("spotify:playlist:"):
        candidate = value.split(":", 2)[-1]
    elif "open.spotify.com/playlist/" in value:
        candidate = (
            value.split("open.spotify.com/playlist/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        )
    else:
        return None
    if len(candidate) != 22 or any(
        character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        for character in candidate
    ):
        return None
    return candidate


def decode_playlist(row: dict[str, Any] | None) -> dict[str, Any]:
    """Decode the JSON catalog column while keeping database fields intact."""

    if not row:
        return {}
    result = dict(row)
    raw_catalog = result.pop("catalog_json", None)
    if raw_catalog:
        try:
            result["catalog"] = json.loads(raw_catalog)
        except (TypeError, json.JSONDecodeError):
            result["catalog"] = None
    else:
        result["catalog"] = None
    result["enabled"] = bool(result.get("enabled", 0))
    return result
