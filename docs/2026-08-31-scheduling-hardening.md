# Scheduling implementation hardening

## Context

The first scheduling implementation established the right component boundary:
SQLite and recurrence logic stay inside `thatradiothing`, while the standalone
React control room and a future `duudey-admin` page consume a versioned HTTP
API. This pass reviewed that implementation as a release candidate rather than
adding Google Calendar synchronization.

## Corrections made

- Partial series edits now preserve every omitted field instead of replacing
  it with create-form defaults.
- Multi-setting updates validate all optimistic versions before committing, so
  one conflict rolls back the complete request.
- Occurrence exceptions and series splits require the current series version,
  validate a real recurrence start, partition dates, and move future overrides
  in one transaction.
- Recurrence expansion is bounded, uses local IANA wall time, handles DST gaps,
  includes events crossing a query boundary, and resolves overlaps
  deterministically.
- Refreshing an inactive playlist no longer changes playback. Failed refreshes
  retain the last-known-good catalog; Spotify pagination may not send bearer
  tokens outside the official API origin, and the page walk is bounded by both
  a page ceiling and a visited-URL check so a cyclic `next` link cannot stall
  catalog refresh.
- A failed schedule refresh backs off instead of retrying at the 0.2 second
  boundary floor, which previously turned a database or Spotify outage into a
  five-hertz retry loop with one logged traceback per iteration.
- AutoDJ state changes are lock-protected, elapsed time can catch up across
  multiple tracks, and returning from a human DJ begins a fresh scheduled
  track.
- Admin cookie mutations require an exact configured origin plus a
  double-submit CSRF token. Bearer callers are supported independently. Legacy
  listener mutations were changed from GET to origin-checked POST operations.
- The unauthenticated process-exit route was removed. OAuth callbacks now bind
  state to a short-lived HttpOnly cookie and accept only internal return paths.
- Checked-in migrations, optimistic versions, request IDs, compact audit data,
  health status, safe static-file resolution, and consistent JSON error
  envelopes form the operational boundary.

## Quality policy

Python modules, public classes, and functions carry explanatory docstrings;
complex safety choices have local comments. Ruff owns deterministic formatting
and a deliberately strict lint set. The React application uses ESLint, hook
rules, focused recurrence-form tests, and a production Vite build. GitLab runs
both language gates before publishing a commit-addressable image.

The test suite covers fresh migrations, transactional conflicts, online
backups, split-series data movement, bounded recurrence and DST behavior,
Spotify response normalization and pagination safety, AutoDJ transitions, and
loopback admin authentication/mutation policies.

## Deliberately deferred

Google Calendar remains a one-way adapter candidate after the local scheduler
has operated reliably. Calendar drag/resize and a native `duudey-admin` page are
also follow-up adapters; neither is allowed to become a second recurrence
engine or receive direct SQLite/Spotify credential access.
