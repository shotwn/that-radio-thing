# that radio thing

> [!NOTE]
>
> ### Project status — September 2026
>
> **That Radio Thing is being retired as part of duudey and released publicly as a self-hosted project.**
>
> The roots of this project go back to **2012**, when duudey was a small internet radio station streaming music directly through **Icecast**. That model eventually became impractical after our residing country brought internet radio broadcasting under **special licensing**. Rather than keep transmitting audio ourselves, the idea was reinvented around **2019** as *That Radio Thing*: **where every listener plays music through their own Spotify account and device, while this service synchronizes the track and playback position between them.**
>
> That Radio Thing still works, but Spotify's current API access model has now created a different limit: Development Mode applications are restricted to **5 authorized users**. That makes the project unsuitable as a public duudey service, but still perfectly usable as a private synchronized listening room for a small group.
>
> Going out of development mode became somewhat impossible since Spotify requires 250,000 monthly active users to even consider an application. Creating a chicken and egg problem.
>
> Rather than let the project and its history disappear, I'm leaving it here as a self-hosted project for anyone who still finds it useful.
>
> **The existing README below remains the technical documentation for the project.**

---

Real-time Spotify playback sync. One "master" listener's current track
and position is mirrored to every other listener's active Spotify device,
so a group can hear the same song, at the same timestamp, across
different rooms, houses, and accounts.

Ships as a single `aiohttp` service with a Socket.IO gateway for
real-time status push, the legacy Vue listener, and a standalone React
administration application. Authentication is JWT-based and was originally
designed to integrate with [duudey.com](https://duudey.com) through shared
authentication and Spotify OAuth infrastructure.

## How the sync works

A background "beat" loop runs every ~0.4 s:

1. Fetch the master user's currently-playing state from Spotify once.
2. For each enabled listener, compare `master.uri` / `master.progress_ms`
   to their state. If different, issue `play(uri, position_ms)` or
   `seek(position_ms)` as appropriate.
3. Users with no active Spotify device enter a short "waiting for
   device" grace window so a Spotify client opened seconds after
   hitting *play* still gets picked up automatically.

The tolerance for "same position" is configurable via
`TRT_REALTIME_TOLERANCE_MS` (default 1000 ms).

## Repository layout

```
thatradiothing/
├── main.py                 # Entry point: ThatRadioThing().run()
├── thatradiothing/         # Core package
│   ├── __init__.py         #   ThatRadioThing: wires config + tasks
│   ├── server.py           #   aiohttp routes, OAuth, CORS, cookie auth
│   ├── sio.py              #   Socket.IO gateway (status push)
│   ├── master.py           #   The sync beat loop
│   ├── user.py             #   User model + Spotify API wrappers
│   ├── autodj.py           #   AutoDJ bot (inherits User, sources tracks)
│   ├── scheduler.py        #   schedule boundary coordinator
│   ├── recurrence.py       #   bounded RFC 5545 occurrence resolution
│   ├── spotify_catalog.py  #   Spotify catalog validation and normalization
│   ├── admin_api.py        #   versioned admin API + React asset serving
│   ├── db.py               #   serialized SQLite repository
│   ├── migrations/         #   append-only transactional SQL migrations
│   ├── config.py           #   Env-based config loader (python-dotenv)
│   ├── jwt_auth.py         #   HS256 JWT issue / verify helpers
│   ├── logger.py           #   logzero + rotating file handler
│   └── exceptions.py       #   User-level Spotify API exception types
├── admin/                  # React/Vite control room (built in CI/image)
├── static/                 # Legacy listener and generated admin assets
│   ├── index.htm           #   Login page
│   ├── player.htm          #   Main UI (Vue + socket.io-client)
│   └── successful-auth.htm #   Post-OAuth landing redirect target
├── Dockerfile              # python:3.12-slim, exposes 33408
├── docker-compose.yml      # Bind-mounts ./logs, reads .env
├── tests/                  # Python unit and loopback API integration tests
├── scripts/                # SQLite online-backup helper
├── pyproject.toml          # Ruff lint/format policy
├── .env.example            # Documented template for .env
└── requirements*.txt       # Runtime and development dependencies
```

## Running locally

### Via Docker (recommended)

```bash
cp .env.example .env        # edit values
docker compose up --build
```

The app binds `${TRT_PORT:-33408}`. Rotating logs land at
`./logs/thatradiothing.log` via a bind mount, so `tail -f ./logs/...`
works. Compose also captures stdout, so `docker compose logs -f
thatradiothing` is equivalent.

### Directly with Python

```bash
python3 -m venv env
source env/bin/activate       # (Windows: env\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env          # edit values
python main.py
```

`python-dotenv` auto-loads `.env` on import, so no shell sourcing is
required. Required env vars raise a clear error on startup if missing
(`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `AUTH_SHARED_JWT_SECRET`).

## Duudey integration

That Radio Thing was originally built as part of the broader duudey ecosystem.
The two services supported shared JWT authentication and Spotify OAuth sessions,
allowing users to move between them without signing in again.

The self-hosted project does not depend on duudey; authentication can be
configured independently through `.env`.

### Configuration reference

See `.env.example` for the authoritative list with inline comments.
Most notable:

| Variable                   | Purpose                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `SPOTIFY_CLIENT_ID/SECRET` | Spotify application credentials.                                       |
| `TRT_URL`                  | Public URL (used as OAuth `redirect_uri` base).                        |
| `TRT_PORT`                 | Listen port (default 33408).                                           |
| `TRT_MASTERS_LIST`         | Comma-separated Spotify user IDs allowed to become master.             |
| `TRT_ADMIN_IDS`            | Comma-separated Spotify user IDs allowed to manage schedules/AutoDJ.   |
| `TRT_SCOPES`               | Comma-separated Spotify OAuth scopes.                                  |
| `TRT_PLAYLISTS`            | JSON array of `{"uri": …}` for the AutoDJ bot.                         |
| `TRT_DATABASE_PATH`        | Durable SQLite path (defaults to `./data/thatradiothing.sqlite3`).      |
| `TRT_SCHEDULE_TIMEZONE`    | IANA timezone used as the schedule editor default.                     |
| `AUTH_SHARED_JWT_SECRET`   | Secret used to sign authentication JWTs; can be shared for SSO.        |
| `AUTH_COOKIE_DOMAIN`       | Cookie domain used by the authentication session.                      |
| `CORS_ALLOWED_ORIGINS`     | Comma-separated origins allowed to call the API with credentials.     |
| `LOG_FILE`                 | Absolute path for rotating logs; empty disables file logging.          |

## Endpoints

All endpoints live on the aiohttp app; `*` means cookie auth required:

| Route                         | Method  | Purpose                                         |
| ----------------------------- | ------- | ----------------------------------------------- |
| `/`                           | GET     | Landing; redirects to `/player` if signed in.  |
| `/auth`                       | GET     | Start Spotify OAuth.                            |
| `/auth_return`                | GET     | OAuth callback; sets the JWT cookie.            |
| `/logout` \*                  | POST    | Clear the JWT cookie.                           |
| `/player` \*                  | GET     | Main UI.                                        |
| `/status` \*                  | GET     | Full status payload (profile/devices/master).   |
| `/profile` \*                 | GET     | User profile + master flags.                    |
| `/devices` \*                 | GET/POST | List / select active Spotify device.           |
| `/master` \*                  | POST    | Claim master role.                              |
| `/resign` \*                 | POST    | Relinquish master role.                         |
| `/enable` / `/disable` \*     | POST    | Opt in / out of the sync loop.                  |
| `/api/now_playing`            | GET     | Public: current master track (unauth).         |
| `/users` \*                   | GET     | Master-only: all sessions.                      |
| `/admin` \*                   | GET     | Standalone React radio control room.            |
| `/api/admin/v1/*` \*         | JSON    | Versioned playlist, schedule, audit, and AutoDJ API. |
| `/health/live`                | GET     | Process liveness probe.                         |
| `/health/ready`               | GET     | Database/catalog/scheduler readiness probe.    |
| `/socket.io/…` \*             | WS      | Real-time status diffs (1 Hz).                  |

## Debugging

- **Liveness** from duudey.com: `GET /api/radio/health` on the site
  probes `/api/now_playing` here with a 2 s timeout.
- **Rotating logs** at `./logs/thatradiothing.log` (+ `.1`…`.5`).
  Stdout logs also streamable via `docker compose logs -f`.
- **Log level**: `LOG_LEVEL=DEBUG` in `.env` for verbose output.

## Playlist scheduling and radio administration

The first `TRT_PLAYLISTS` entry is imported into SQLite only when the
database is empty. After bootstrap, the database setting
`default_playlist_id` is authoritative. The admin control room at `/admin`
lets an allowlisted administrator register Spotify playlists, set the default,
create one-time or recurring events, inspect overlaps, and skip AutoDJ tracks.

Schedule times are wall-clock values in an IANA timezone and recurrence is
RFC 5545 compatible. The service keeps a last-known-good Spotify catalog and
does not stop playback when a refresh fails. SQLite lives at
`TRT_DATABASE_PATH` (default `./data/thatradiothing.sqlite3`, which resolves to
`/app/data/thatradiothing.sqlite3` in the image), so production
deployments must persist `/app/data`.

To make a live backup, run the included SQLite online-backup helper from the
host (or an administrative container):

```sh
python scripts/backup_sqlite.py ./data/thatradiothing.sqlite3 \
  ./backups/thatradiothing-$(date +%Y-%m-%d).sqlite3
```

Restore by stopping the service, replacing the database file with a verified
backup, removing any stale `-wal`/`-shm` sidecars, and starting the service
again. The helper writes a temporary file and atomically replaces the target.

The complete API contract and rollout sequence are in
[`docs/playlist-scheduling-implementation-plan.md`](docs/playlist-scheduling-implementation-plan.md)
and [`docs/admin-api-openapi.yaml`](docs/admin-api-openapi.yaml).
The admin bundle defaults to same-origin `/api/admin/v1`; a future host such
as `duudey-admin` can set `window.__TRT_ADMIN_API_BASE__` before loading it and
reuse the same API without sharing SQLite or Spotify credentials.

## Quality gates

The same release checks run locally and in GitLab CI:

```sh
pip install -r requirements-dev.txt
ruff format --check main.py thatradiothing scripts tests
ruff check main.py thatradiothing scripts tests
python -m unittest discover -s tests -t . -v

cd admin
npm ci
npm run check
```

`npm run check` runs ESLint, the recurrence-form unit tests, and a production
Vite build. Database changes must be append-only SQL files registered in
`thatradiothing.db.MIGRATIONS`; startup applies each migration transactionally.
