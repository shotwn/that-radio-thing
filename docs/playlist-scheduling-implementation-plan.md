# Playlist scheduling and radio administration implementation plan

Status: core v1 implemented; optional Google Calendar/native duudey-admin adapters deferred  
Target project: `thatradiothing`  
Related, but deliberately independent project: `duudey-admin`

Delivered in this repository: SQLite persistence/migrations, Spotify catalog
normalization with cached last-known-good tracks, lock-protected AutoDJ
controls, RFC 5545 schedule resolution and overlap precedence, a versioned
CSRF-protected admin API, standalone React/FullCalendar assets, admin
Socket.IO status, readiness probes, deployment volume wiring, and an online
SQLite backup helper. Google Calendar remains an optional one-way adapter and
`duudey-admin` remains an API consumer rather than a runtime dependency.
The release gate now also includes strict Ruff formatting/linting, ESLint,
Python and recurrence-form tests, transactional schema upgrades, and a
production admin bundle build.

## 1. Outcome

`thatradiothing` will keep using Spotify playlists, but playlist choice will become data-driven:

- A default playlist stored in SQLite plays whenever no schedule is active.
- Authorized radio administrators can create one-time and recurring playlist events in a standalone React admin UI.
- The service evaluates schedules locally and switches AutoDJ at event boundaries.
- Administrators can skip the current AutoDJ track or replace its queued next track.
- Human DJ takeover continues to work exactly as it does now. The schedule keeps advancing in the background and the correct playlist resumes when AutoDJ becomes master again.
- `thatradiothing` remains deployable and usable without `duudey-admin`.
- A later `duudey-admin` radio page will consume `thatradiothing`'s authenticated API or embed its standalone UI. It will not read the radio SQLite file or duplicate scheduling rules.

The recommended first release uses SQLite plus RFC 5545 recurrence rules. Google Calendar is an optional second-stage, one-way schedule source, not a dependency of the playback loop.

## 2. Why this architecture fits the existing code

The current service is one Python 3.12 `aiohttp` process with four relevant runtime objects:

- `ThatRadioThing` constructs the web server, `Master`, and `AutoDJ`.
- `Master.beat()` runs every 0.4 seconds and synchronizes listeners to `master_user`.
- `AutoDJ` acts as an in-memory virtual master, tracks elapsed playback time, and randomly chooses `now` and `next` tracks.
- `WebServer` owns shared JWT authentication, HTTP routes, and the Socket.IO gateway.

Before this implementation, `TRT_PLAYLISTS` was parsed from JSON in the environment and there was no persistent schedule store or automated test suite. It is now a one-time bootstrap input; SQLite is authoritative after the first import.

The existing SSO boundary is useful and should remain unchanged. `thatradiothing`, `duudey.com`, and `duudey-admin` verify the same domain-scoped `duudey_auth` JWT. The new admin permission can therefore be a second Spotify-ID allowlist without creating another login system.

The radio is already shipped as an independent GitLab image. SQLite and the standalone admin assets belong in that image. The shared deployment only needs to attach a durable data volume and pass configuration.

## 3. Decisions

### 3.1 Local scheduling is authoritative in version 1

Use:

- SQLite for playlists, schedule series, recurrence exceptions, settings, and audit records.
- `aiosqlite` for non-blocking database access from the existing asyncio process.
- `python-dateutil` for RFC 5545 `RRULE`, `RDATE`, and `EXDATE` evaluation.
- Python `zoneinfo` and IANA time-zone names for wall-clock scheduling and daylight-saving behavior.

Do not use a general job scheduler as the source of truth. APScheduler-style jobs are good at waking code, but they do not by themselves model event duration, overlapping shows, edited occurrences, exclusions, or the question "which playlist should be active now after a restart?" The database plus a pure schedule resolver answers that question at any instant. A small asyncio coordinator only handles timely wake-ups.

`python-dateutil` implements the iCalendar recurrence model, including intervals, day-of-month rules, nth weekdays, and recurrence sets with explicit inclusions and exclusions: [dateutil rrule documentation](https://dateutil.readthedocs.io/en/stable/rrule.html).

### 3.2 React is the admin client

Add a Vite-built React application inside the `thatradiothing` repository and serve its production output under `/admin` from `aiohttp`.

Use FullCalendar Standard with its React and interaction plugins for month/week/day views. Standard plugins are MIT licensed; no Premium resource/timeline feature is required: [FullCalendar license](https://fullcalendar.io/license).

The backend remains authoritative for recurrence parsing and occurrence expansion. A small tested client helper only builds and parses the supported editor subset; the browser never expands the authoritative schedule.

### 3.3 Immediate playlist transitions

Version 1 changes playlist and track at the scheduled boundary. This makes a 09:00 event actually begin at 09:00 instead of at an unpredictable song end.

Internally keep `transition_policy = immediate` in the API/domain model so a future `finish_current_track` option can be added without changing the event contract.

### 3.4 Deterministic overlap handling

Overlaps are allowed because a special show may intentionally cover part of a regular schedule.

The winning active occurrence is chosen by:

1. Highest explicit integer priority.
2. Latest occurrence start time.
3. Stable schedule-series ID as the final tie-breaker.

The UI warns about detected overlaps, displays the winning occurrence, and shows suppressed occurrences. One-time events should default to a higher suggested priority than normal weekly programming, but priority remains visible and editable.

### 3.5 AutoDJ controls do not control a human master

`skip current`, `replace next`, and playlist reload are valid only while `Master.master_user` is AutoDJ. If a human DJ is master, return HTTP `409 Conflict` with a machine-readable reason and do not issue Spotify playback commands to the human.

## 4. Target component model

```text
Standalone React admin (/admin)        Future duudey-admin radio page
                |                                   |
                +------ credentialed HTTP/WS -------+
                                    |
                         /api/admin/* in aiohttp
                                    |
              +---------------------+--------------------+
              |                     |                    |
        Admin authorization   Schedule service     Playback controls
              |                     |                    |
       shared JWT + IDs       SQLite + RRULE       AutoDJ state lock
                                    |                    |
                                    +---- active --------+
                                         playlist
                                             |
                                      Master sync loop
                                             |
                                      Spotify listeners
```

Dependencies point inward to domain services. HTTP handlers, Google import, and React are adapters; none contains recurrence or playlist-selection rules.

## 5. Domain behavior

### 5.1 Playlist registry

A playlist record contains the canonical Spotify playlist URI/ID plus display and validation state. Accept both Spotify URLs and `spotify:playlist:<id>` input, but store one canonical URI.

On create or refresh:

1. Parse and validate the ID locally. Never fetch an arbitrary submitted URL.
2. Fetch playlist metadata and every item page from Spotify.
3. Reject an empty playlist or a response with no playable track items.
4. Ignore unsupported episodes, local-only tracks, null/deleted items, and unavailable tracks; report the number excluded.
5. Atomically replace the in-memory catalog only after a complete successful fetch.
6. Preserve the last-known-good catalog if refresh fails and surface the stale/error status to administrators.

The service should cache loaded catalogs in memory. Persisting the last-known-good playable track list in SQLite is recommended so a service restart during a Spotify outage does not silence AutoDJ. Refresh it on explicit admin request, when a playlist becomes active, and on a bounded background interval. Keep Spotify attribution/link data beside any displayed Spotify art or metadata.

### 5.2 Spotify API compatibility gate

This is a phase-zero release gate, not a later cleanup.

The existing code calls `GET /v1/playlists/{id}` with a client-credentials token and expects `tracks.items[].track`. Spotify's February 2026 migration renamed playlist contents to `items.items[].item` and documents playlist-item access as limited to playlists owned by or collaborative with the current user: [Spotify migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide), [Get Playlist Items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items).

Before building scheduling, test the production Spotify app against every intended playlist. The adapter must understand the current response shape and paginate `/playlists/{id}/items`.

If app-level access is no longer sufficient, use a dedicated radio-library Spotify account:

- Make that account the owner or collaborator of every radio playlist.
- Obtain and securely deploy its refresh token with only required playlist-read scopes.
- Keep this credential server-side and independent from whichever administrator happens to be logged in.
- Do not use an administrator's session token for background playback metadata.

This decision affects only the Spotify catalog adapter; schedule storage and the admin API remain unchanged.

### 5.3 Schedule series and occurrences

A schedule series has:

- A playlist.
- A local start date/time.
- An IANA time zone, defaulting from the service setting.
- A positive duration.
- An optional RFC 5545 RRULE. No RRULE means one time.
- Optional included and excluded occurrence dates.
- Priority and enabled state.
- Audit metadata and optimistic-lock version.

The initial recurrence form supports:

- Does not repeat.
- Daily and every N days.
- Weekly and every N weeks on selected weekdays.
- Monthly on a specific day, including negative days such as last day.
- Monthly on the nth weekday, such as third Friday or last weekday.
- Yearly.
- Ends never, after N occurrences, or on a date.
- Edit this occurrence, this and future occurrences, or the complete series.

Advanced administrators may inspect/copy the generated RRULE, but arbitrary raw input does not need to be the first-release UI.

Time semantics:

- Recurrence is evaluated in the event's named time zone so a weekly 09:00 show remains at 09:00 after a daylight-saving change.
- API responses expose UTC instants and the source time zone/local values.
- Ends are exclusive: an event `[09:00, 10:00)` no longer wins at exactly 10:00.
- Invalid local times created by a DST jump follow RFC/dateutil behavior and are skipped, not silently moved. Month-day rules such as the 31st likewise skip months without that date. The editor previews upcoming occurrences so this is visible before save.

### 5.4 Active schedule resolution

For an instant `now`:

1. Load enabled series.
2. Build an `rruleset` for each series.
3. Find the latest included, non-excluded occurrence start at or before `now`.
4. Treat it as active only if `start <= now < start + duration`.
5. Apply deterministic overlap precedence.
6. Use the configured default playlist if no occurrence is active.
7. Calculate the next possible change from active ends and upcoming starts.

The resolver is a pure service with an injected clock. It does not call Spotify or mutate AutoDJ, making recurrence and overlap behavior fast to test.

### 5.5 Schedule coordinator

Add one `ScheduleCoordinator` task alongside `WebServer.run()` and `Master.beat()`.

It will:

- Resolve the active schedule on startup before AutoDJ becomes master.
- Apply a playlist change through one AutoDJ method protected by an `asyncio.Lock`.
- Sleep until the next computed boundary, with a short maximum safety interval to tolerate clock changes.
- Wake immediately through an `asyncio.Event` after schedule, default, or playlist edits.
- Recompute from wall-clock truth after restart or a delayed loop rather than replaying missed jobs.
- Retry a failed catalog load with backoff while keeping the last-known-good/default playlist.
- Record transition success/failure and expose next transition/freshness in admin status.

Do not evaluate SQLite queries in the existing 0.4-second listener synchronization loop.

### 5.6 AutoDJ state machine

Refactor AutoDJ before adding controls. Replace loosely coupled dictionary mutation with explicit, lock-protected operations:

- `activate_playlist(playlist_catalog, reason, occurrence)`
- `advance_track(reason)` for natural end or `skip current`
- `replace_next_track(reason)` for `skip next`
- `snapshot()` for API/Socket.IO status

Maintain `current_track`, `next_track`, `track_started_at`, `active_playlist`, and a per-playlist shuffle bag. Avoid immediate repeats and do not select the current track as next when at least two playable tracks exist.

On any forced transition or skip, set every listener's `pass_sync_for_cycles` to zero so the next master beat moves listeners immediately instead of waiting up to several skipped cycles.

Playlist choice continues to update while a human is master, but AutoDJ's elapsed track clock should not run offscreen. When the human resigns, start a fresh track from the currently scheduled playlist and make AutoDJ master.

## 6. SQLite design

Use explicit SQL migrations checked into `thatradiothing/migrations/`. Run them transactionally at startup before background tasks start. Configure:

- `PRAGMA foreign_keys = ON`
- WAL journal mode
- a bounded busy timeout
- one application writer process

Proposed tables:

### `settings`

| Column | Purpose |
| --- | --- |
| `key TEXT PRIMARY KEY` | Stable setting name. |
| `value_json TEXT NOT NULL` | Typed JSON value. |
| `version INTEGER NOT NULL` | Optimistic concurrency. |
| `updated_at TEXT NOT NULL` | UTC RFC3339 timestamp. |
| `updated_by TEXT NOT NULL` | Spotify administrator ID. |

Required keys are `default_playlist_id`, `default_timezone`, and `catalog_refresh_interval_seconds`.

### `playlists`

| Column | Purpose |
| --- | --- |
| `id TEXT PRIMARY KEY` | Application UUID. |
| `spotify_id TEXT UNIQUE NOT NULL` | Parsed Spotify ID. |
| `spotify_uri TEXT UNIQUE NOT NULL` | Canonical URI. |
| `name TEXT` | Last validated Spotify name. |
| `external_url TEXT` | Spotify attribution link. |
| `image_url TEXT` | Unmodified Spotify artwork URL, if present. |
| `catalog_json TEXT` | Last-known-good playable items and metadata. |
| `catalog_revision TEXT` | Snapshot/revision when Spotify supplies one. |
| `validated_at TEXT` | Last successful fetch. |
| `validation_error TEXT` | Latest error without discarding good data. |
| `enabled INTEGER NOT NULL` | Soft disable. |
| audit columns | Creator/updater IDs and timestamps. |

### `schedule_series`

| Column | Purpose |
| --- | --- |
| `id TEXT PRIMARY KEY` | Series UUID. |
| `title TEXT NOT NULL` | Administrator-facing label. |
| `playlist_id TEXT NOT NULL` | Foreign key to playlist. |
| `dtstart_local TEXT NOT NULL` | Wall-clock series start without lossy UTC conversion. |
| `timezone TEXT NOT NULL` | IANA zone. |
| `duration_seconds INTEGER NOT NULL` | Positive occurrence duration. |
| `rrule TEXT` | Canonical RRULE value; null for one-time. |
| `priority INTEGER NOT NULL` | Overlap precedence. |
| `transition_policy TEXT NOT NULL` | Initially `immediate`. |
| `enabled INTEGER NOT NULL` | Soft disable. |
| `source TEXT NOT NULL` | `local` initially; later `google`. |
| `external_calendar_id TEXT` | Optional source identity. |
| `external_event_id TEXT` | Optional source identity. |
| `version INTEGER NOT NULL` | Optimistic concurrency. |
| audit columns | Creator/updater IDs and timestamps. |

### `schedule_dates`

Stores explicit `RDATE` and `EXDATE` values for a series. A uniqueness constraint on `(series_id, kind, occurrence_start_utc)` prevents duplicates.

### `schedule_overrides`

Stores an exception keyed by `(series_id, original_start_utc)`. It can cancel one occurrence or override title, playlist, start, duration, and priority. "This and future" editing is implemented by ending the original RRULE before the split and creating a new series in one transaction.

### `audit_log`

Append-only records containing actor Spotify ID/display name, action, entity type/ID, UTC timestamp, request ID, and compact before/after JSON. Include schedule mutations, default changes, playlist refreshes, skips, and failed control attempts.

Indexes should cover enabled schedules, playlist foreign keys, external source IDs, and audit time. Do not materialize an unbounded occurrences table in version 1.

## 7. Admin authorization and request security

Add `TRT_ADMIN_IDS`, a comma-separated Spotify-ID allowlist separate from `TRT_MASTERS_LIST`. No permission implies the other:

- Masters can take over playback.
- Radio administrators can manage schedules and AutoDJ.
- A person may appear in both lists.

Refactor JWT verification into a request identity helper that does not require a working Spotify access token. Scheduling authorization depends on a valid shared JWT and the configured admin ID, not on whether Spotify token refresh happens to be available.

Rules:

- `/admin` requires a valid shared session and redirects unauthenticated users through the existing Spotify login with a validated return path.
- Authenticated but unlisted users receive a clear 403 page.
- Every `/api/admin/*` endpoint enforces the allowlist server-side.
- Mutation endpoints use POST/PATCH/DELETE, never GET.
- Require JSON content types, a custom CSRF header, and an exact allowed `Origin` for browser mutations. A host-scoped double-submit CSRF cookie can support both standalone and future cross-subdomain clients.
- CORS only permits configured first-party origins with credentials. CORS is not treated as authorization.
- Validate request size, date ranges, durations, RRULE complexity, and playlist IDs.
- Use per-record versions/`If-Match` so two administrators do not silently overwrite each other.
- Secrets, refresh tokens, and JWT contents never appear in admin payloads or audit JSON.

## 8. Admin API

Use a versioned JSON surface under `/api/admin/v1` and a consistent error envelope `{code, message, details, request_id}`.

### Session and status

- `GET /api/admin/v1/session`: identity, permissions, CSRF bootstrap, default time zone.
- `GET /api/admin/v1/status`: AutoDJ/human-master state, active and next track, active playlist/occurrence, next transition, schedule health, Spotify catalog freshness.
- Socket.IO `admin_status`: authorized push equivalent of the status payload. Never add private admin fields to the public now-playing response.

### Playlists and settings

- `GET/POST /api/admin/v1/playlists`
- `GET/PATCH/DELETE /api/admin/v1/playlists/{id}`
- `POST /api/admin/v1/playlists/{id}/refresh`
- `GET/PATCH /api/admin/v1/settings`

Prevent deletion of a playlist referenced by the default or a schedule. Return references in the conflict response.

### Schedule

- `GET /api/admin/v1/schedule?from=<utc>&to=<utc>` returns expanded occurrences plus their series and winner/suppressed state.
- `POST /api/admin/v1/schedule/series`
- `GET/PATCH/DELETE /api/admin/v1/schedule/series/{id}`
- `POST /api/admin/v1/schedule/series/{id}/exceptions` for cancel/edit-one.
- `POST /api/admin/v1/schedule/series/{id}/split` for this-and-future edits.
- `POST /api/admin/v1/schedule/preview` validates a draft and returns the next occurrences and overlap warnings without saving.

Bound occurrence expansion by a maximum range and result count.

### Playback controls

- `POST /api/admin/v1/playback/skip-current`
- `POST /api/admin/v1/playback/skip-next`
- `POST /api/admin/v1/playback/reload-playlist`

Each command is serialized by the AutoDJ lock, idempotently rejects duplicate request IDs for a short window, records an audit entry, and returns the resulting snapshot.

## 9. Standalone admin UI

Suggested route structure:

- `/admin`: operational dashboard with master, listeners, now playing, next track, active playlist/event, next schedule change, and skip controls.
- `/admin/schedule`: month/week/day calendar plus agenda list.
- `/admin/schedule/new` and edit drawer/page: playlist, dates, duration, recurrence, priority, preview, and overlap warnings.
- `/admin/playlists`: add by Spotify URL, validation state, track count, refresh, enable/disable, and default selection.
- `/admin/audit`: recent schedule and playback actions.

Version 1 interaction requirements:

- Desktop and mobile layouts.
- All times labeled with the displayed time zone.
- Clicking an occurrence opens explicit edit-one, edit-this-and-future, edit-series, cancel-one, and delete-series actions.
- Destructive series operations require confirmation and explain their scope.
- Optimistic UI is reconciled with server versions; conflicts show the newer server record.
- Control buttons disable while a request is pending and show the resulting track, not merely "success".
- Human-master state visibly disables AutoDJ controls.
- Accessibility includes keyboard operation, focus restoration for dialogs, and non-color overlap/error indicators.

Calendar drag/resize is intentionally deferred until it can use the same
scope-confirmation flow without creating an accidental whole-series edit.

Build output should be reproducible in CI and copied into the Python image. Development can run Vite separately with a proxy to local `aiohttp`; production has no Node process.

## 10. Independent integration with `duudey-admin`

The invariant is: `thatradiothing` owns radio data, recurrence behavior, Spotify access, and playback commands. `duudey-admin` never mounts the SQLite file and never receives Spotify service credentials.

Prepare two integration paths:

1. Fast integration: `/admin?embed=1` renders the same radio UI without its own outer navigation. `duudey-admin` can place it in an iframe. Set a restrictive CSP `frame-ancestors` allowlist for the known admin origin and use the shared cookie.
2. Native integration: keep the React API client and page components behind a small `RadioAdminApp({apiBase})` boundary. They can later be published as a versioned package or rehosted in `duudey-admin`, still calling `/api/admin/v1` over credentialed CORS.

The API contract and an OpenAPI document are the stable seam. The iframe is not the only integration mechanism, and a native integration does not become a second backend.

## 11. Optional Google Calendar source

Do this only after the local scheduler is stable. Local multi-admin scheduling already meets the collaboration requirement without Google credentials, webhook operations, or an extra failure domain.

Recommended Google mode is one-way import into the same local resolver:

- A dedicated shared Google Calendar is authoritative for Google-sourced events.
- Share it with the administrators in Google; the service uses a narrowly scoped service account or stored OAuth grant.
- Administrators put a Spotify playlist URL in a structured `Playlist:` line in the event description so events created in the normal Google Calendar UI remain usable. Events created through an application may additionally use shared extended properties. Google supports application key/value properties on events: [extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties).
- Imported rows use `source=google`, external IDs, and are read-only in the local editor. Local events remain editable.
- Recurring instances and exceptions map into the existing occurrence model; imported events use the same overlap resolver.
- Keep the last-known-good mirror in SQLite. A Google outage must not stop the current schedule.
- Show last successful sync and parse errors in the admin UI.

Start with periodic bounded synchronization for a small dedicated calendar. If near-real-time edits become necessary, add Calendar push notifications and then fetch changes. Google notifications contain no event body, require an HTTPS webhook, and notification channels expire and must be renewed manually: [Google Calendar push notifications](https://developers.google.com/workspace/calendar/api/guides/push). Incremental sync tokens can expire with HTTP 410 and then require a full resync: [Events list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).

Do not implement two-way synchronization initially. Conflict semantics between local recurrence splits, Google series exceptions, and simultaneous edits would add significant risk without improving playback.

## 12. Failure behavior and operations

### Startup

Startup order is database migration, repository/settings load, Spotify token/catalog initialization, active schedule resolution, then web and playback tasks. Readiness is false until a valid default playlist catalog is available.

### Runtime fallbacks

- Invalid active event playlist: log/audit the failure and retain the last-known-good active playlist; if none, use the validated default.
- Spotify catalog refresh failure: keep cached playable items and mark them stale.
- Schedule database read failure: retain the last resolved state briefly, retry with backoff, and mark readiness unhealthy.
- Process downtime across boundaries: recompute what is active now on startup; never replay every missed transition.
- Human takeover: schedule state remains correct but causes no AutoDJ playback commands.
- Empty/corrupt default playlist: readiness fails loudly rather than starting a silent or crash-looping AutoDJ task.

### Deployment changes

- Add `TRT_DATABASE_PATH=/app/data/thatradiothing.sqlite3`.
- Mount a persistent `/app/data` volume in the standalone and shared Compose definitions.
- Keep the service at one replica while SQLite is authoritative.
- Add `.data/`/database sidecars and frontend build artifacts appropriately to `.gitignore`.
- Add `/health/live` and `/health/ready`; keep `/api/now_playing` backward compatible for `duudey.com`.
- Back up SQLite with its online backup API or `sqlite3 .backup`, not by copying only the main file while WAL writes are active.
- Log structured transition, schedule, catalog, and admin-action events with request/series/occurrence IDs.

If future scale requires multiple active radio processes, move the repository interface to Postgres and add leader election before running multiple coordinators. That is explicitly outside version 1.

## 13. Migration and compatibility

On the first database migration:

1. If the database has no playlists, import valid entries from `TRT_PLAYLISTS`.
2. Set the first imported entry as the database default.
3. Log a deprecation warning for `TRT_PLAYLISTS` after successful bootstrap.
4. Once the database has a default, environment playlist changes do not overwrite administrator data.
5. Keep the importer for one release, then replace it with an explicit migration command or `TRT_BOOTSTRAP_PLAYLIST_URI`.

Existing routes, player UI, shared JWT claims, Socket.IO `status`, `/api/now_playing`, master takeover, and listener sync remain compatible. New admin fields are additive and restricted to the admin channel/API.

## 14. Test strategy

### Backend unit tests

- One-time, daily, interval, multi-weekday, nth weekday, last weekday, day 29/30/31, count, until, RDATE, and EXDATE recurrence.
- DST spring gap and autumn ambiguity in at least Istanbul plus a DST-observing zone.
- Exact start/end inclusivity.
- Overlap precedence and stable tie-breaking.
- Edit-one, cancel-one, and split-series behavior.
- Default fallback and disabled playlist/event behavior.
- AutoDJ no-repeat bag, natural advance, skip current, skip next, and playlist switch.
- Human takeover/resign interaction.
- Spotify legacy/current response normalization, pagination, null/local/episode filtering, 401 retry, 403, 429/backoff, and stale catalog fallback.
- Admin allowlist, JWT rejection, CSRF/origin validation, optimistic version conflicts, and audit redaction.
- SQL migrations from empty and previous versions.

Inject a clock and Spotify/catalog interface. Tests must not depend on real time, sleep, or the live Spotify API.

### Integration tests

- `aiohttp` test client exercises the full admin API against a temporary SQLite database.
- Coordinator wakes on database edits and changes AutoDJ exactly once.
- Restart at the middle of a recurring occurrence selects the correct playlist.
- Concurrent skip and schedule transition serialize correctly.
- Socket.IO admin status never leaks to a non-admin socket.
- Docker test verifies the data volume survives container recreation.

### Frontend tests

- Component tests for recurrence form serialization, time zones, conflict/error states, and disabled controls.
- Calendar tests for create, drag, resize, edit-one, and edit-series confirmation.
- End-to-end browser flow for login/forbidden, playlist validation, schedule activation, skip actions, and stale-version conflict.
- Production asset build and `aiohttp` static routing in CI.

## 15. Implementation sequence and gates

### Phase 0: Compatibility spike and characterization

- Add tests around current AutoDJ/master behavior before refactoring.
- Run the Spotify access/response-shape check against intended playlists.
- Decide client credentials versus a dedicated radio-library user token.
- Capture representative sanitized Spotify fixtures.

Gate: every intended playlist can be fully enumerated through a documented credential path.

### Phase 1: Persistence foundation

- Add `aiosqlite`, migrations, repository interfaces, settings, playlists, audit, startup/readiness, and volume configuration.
- Bootstrap the existing first `TRT_PLAYLISTS` entry as default.

Gate: restart preserves settings/catalog data; invalid/default failures are observable and tested.

### Phase 2: AutoDJ refactor and controls

- Introduce the catalog adapter, normalized Spotify shapes, AutoDJ lock/state machine, shuffle bag, skip operations, and listener fast-resync.
- Preserve human master behavior.

Gate: current player behavior passes characterization tests; concurrent controls cannot corrupt now/next state.

### Phase 3: Recurrence and coordinator

- Implement schedule schema, pure resolver, exceptions/splits, overlap rules, coordinator, transition audit, and restart recovery.

Gate: deterministic clock-based tests cover recurrence/DST/overlaps and a live schedule can cross boundaries without manual intervention.

### Phase 4: Secure admin API

- Add admin IDs, identity-only JWT auth, CSRF/origin checks, versioned endpoints, optimistic locking, OpenAPI, and admin Socket.IO status.

Gate: authorization/security integration tests pass; a master-only non-admin cannot access radio administration.

### Phase 5: Standalone React admin

- Build dashboard, playlists, settings, calendar, recurrence editor, occurrence scope flows, audit view, and responsive/accessibility behavior.
- Serve the built application from the radio image.

Gate: an administrator can complete the full workflow using only the independently deployed `thatradiothing` service.

### Phase 6: Production rollout

- Back up configuration, attach persistent data volume, seed default, deploy with scheduling disabled, and validate playlist/catalog health.
- Enable coordinator, create a short canary event, verify boundary switch and listeners, then add real programming.
- Document backup/restore, playlist credential rotation, schedule disable, and rollback.
- Remove the old environment playlist as runtime authority after the compatibility window.

Gate: canary and restart tests pass in production; operators can restore the SQLite backup.

### Phase 7: Optional integrations

- Add iframe/native `duudey-admin` entry using only the versioned API.
- Add one-way Google Calendar import only if native calendar collaboration remains desirable after using the standalone UI.

## 16. Acceptance criteria

The first release is complete when:

- With no active event, the database-configured default playlist drives AutoDJ.
- One-time and recurring events select the correct playlist in their configured time zone across restarts.
- Weekly, every-N-day/week, day-of-month, nth-weekday, finite, exception, and split-series cases work and preview accurately.
- Overlap results are deterministic and visible before and during playback.
- A schedule boundary changes AutoDJ promptly and listener sync follows on the next beat.
- A human DJ can take over and resign without schedule corruption.
- An allowed radio administrator can schedule and use AutoDJ controls; a master-only or ordinary listener cannot.
- Two administrators receive a conflict instead of losing updates silently.
- Skip-current and skip-next cannot race each other or a schedule transition.
- Playlist/API/calendar outages retain a last-known-good/default program and expose degraded status.
- SQLite data survives image/container replacement and has a tested backup/restore path.
- `thatradiothing` works fully without `duudey-admin`, while the future admin project needs only the documented API and shared SSO.

## 17. Product choices to confirm before Phase 3

The plan uses sensible defaults so work can begin, but these choices should be confirmed before recurrence behavior is frozen:

1. Scheduled boundaries cut immediately (recommended) versus finish the current track.
2. Explicit priority with overlap warnings (recommended) versus rejecting all overlaps.
3. Default schedule time zone, likely `Europe/Istanbul`, while retaining per-event zones.
4. Whether one-time events should merely suggest a higher priority or always override recurring events.
5. Whether Google Calendar import is actually needed after the standalone multi-admin calendar ships.
