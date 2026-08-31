/** Standalone React control room for radio programming and AutoDJ operations. */

import "./style.css";

import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import FullCalendar from "@fullcalendar/react";
import timeGridPlugin from "@fullcalendar/timegrid";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import { cookieValue, mutate, request } from "./api.js";
import { buildRRule, parseRRule, utcToZonedLocal, WEEKDAYS } from "./schedule.js";

const DAY_MS = 86_400_000;

/** Build the default calendar range used before FullCalendar reports its view. */
function initialRange() {
  return {
    start: new Date(Date.now() - 7 * DAY_MS).toISOString(),
    end: new Date(Date.now() + 90 * DAY_MS).toISOString(),
  };
}

/** Render the complete authenticated administration application. */
function App() {
  const embedded = new URLSearchParams(window.location.search).get("embed") === "1";
  const [tab, setTab] = useState("dashboard");
  const [csrf, setCsrf] = useState("");
  const [status, setStatus] = useState(null);
  const [playlists, setPlaylists] = useState([]);
  const [events, setEvents] = useState([]);
  const [audit, setAudit] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const rangeRef = useRef(initialRange());

  /** Fetch all control-room resources from one internally consistent moment. */
  const loadAll = useCallback(async () => {
    try {
      const range = rangeRef.current;
      const [session, radioStatus, playlistResponse, eventResponse, auditResponse] =
        await Promise.all([
          request("/session"),
          request("/status"),
          request("/playlists"),
          request(
            `/schedule?from=${encodeURIComponent(range.start)}&to=${encodeURIComponent(range.end)}`,
          ),
          request("/audit?limit=50"),
        ]);
      setCsrf(session.csrf_token || cookieValue("trt_csrf"));
      setStatus(radioStatus);
      setPlaylists(playlistResponse.items || []);
      setEvents(eventResponse.items || []);
      setAudit(auditResponse.items || []);
      setError("");
    } catch (caught) {
      setError(caught.message);
      if (caught.status === 401) {
        window.location.assign("/auth?returnTo=/admin");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadAll();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadAll]);

  /** Execute a mutation with consistent feedback and authoritative reload. */
  async function runMutation(operation, successMessage) {
    try {
      setError("");
      setNotice("");
      await operation();
      setNotice(successMessage);
      await loadAll();
      return true;
    } catch (caught) {
      setError(caught.message);
      return false;
    }
  }

  /** Reload events for the range FullCalendar is currently displaying. */
  async function changeCalendarRange(start, end) {
    rangeRef.current = { start: start.toISOString(), end: end.toISOString() };
    try {
      const response = await request(
        `/schedule?from=${encodeURIComponent(rangeRef.current.start)}&to=${encodeURIComponent(rangeRef.current.end)}`,
      );
      setEvents(response.items || []);
    } catch (caught) {
      setError(caught.message);
    }
  }

  const calendarEvents = useMemo(
    () =>
      events.map((event) => ({
        id: `${event.series_id}-${event.original_start_utc}`,
        title: `${event.suppressed ? "[suppressed] " : ""}${event.title}`,
        start: event.start_utc,
        end: event.end_utc,
        className: event.suppressed ? "suppressed-event" : "active-event",
        extendedProps: event,
      })),
    [events],
  );

  const defaultSetting = status?.settings?.default_playlist_id;

  return (
    <div className={embedded ? "shell embedded" : "shell"}>
      <header className="topbar">
        <div>
          <p className="eyebrow">ThatRadioThing</p>
          <h1>Radio control room</h1>
        </div>
        <div className="health" aria-live="polite">
          <span className={`dot ${status?.ready ? "good" : "bad"}`} />
          {status?.ready ? (status?.degraded ? "Ready, degraded" : "Ready") : "Not ready"}
        </div>
      </header>

      <nav className="tabs" aria-label="Radio administration">
        {[
          ["dashboard", "Dashboard"],
          ["schedule", "Schedule"],
          ["playlists", "Playlists"],
          ["audit", "Audit"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "tab active" : "tab"}
            onClick={() => setTab(key)}
            type="button"
          >
            {label}
          </button>
        ))}
      </nav>

      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && (
        <div className="alert success" role="status">
          {notice}
          <button onClick={() => setNotice("")} type="button">Dismiss</button>
        </div>
      )}
      {loading && <div className="alert">Loading radio state…</div>}

      {tab === "dashboard" && (
        <Dashboard
          status={status}
          onPlayback={(action) =>
            runMutation(
              () => mutate(`/playback/${action}`, "POST", {}, csrf),
              action === "skip-current"
                ? "Current track skipped."
                : action === "skip-next"
                  ? "Queued track replaced."
                  : "Active playlist refreshed.",
            )
          }
        />
      )}
      {tab === "schedule" && (
        <Schedule
          events={calendarEvents}
          playlists={playlists}
          csrf={csrf}
          onChanged={loadAll}
          onError={setError}
          onNotice={setNotice}
          onRange={changeCalendarRange}
        />
      )}
      {tab === "playlists" && (
        <Playlists
          playlists={playlists}
          defaultPlaylistId={status?.default_playlist_id}
          onSave={(spotifyUri) =>
            runMutation(
              () => mutate("/playlists", "POST", { spotify_uri: spotifyUri }, csrf),
              "Playlist validated and saved.",
            )
          }
          onRefresh={(id) =>
            runMutation(
              () => mutate(`/playlists/${id}/refresh`, "POST", {}, csrf),
              "Playlist catalog refreshed.",
            )
          }
          onDefault={(id) =>
            runMutation(
              () =>
                mutate(
                  "/settings",
                  "PATCH",
                  {
                    default_playlist_id: id,
                    versions: { default_playlist_id: defaultSetting?.version || 0 },
                  },
                  csrf,
                ),
              "Default playlist updated.",
            )
          }
          onToggle={(playlist) =>
            runMutation(
              () =>
                mutate(
                  `/playlists/${playlist.id}`,
                  "PATCH",
                  { enabled: !playlist.enabled },
                  csrf,
                ),
              `Playlist ${playlist.enabled ? "disabled" : "enabled"}.`,
            )
          }
          onDelete={(playlist) =>
            runMutation(
              () => mutate(`/playlists/${playlist.id}`, "DELETE", undefined, csrf),
              "Playlist deleted.",
            )
          }
        />
      )}
      {tab === "audit" && <Audit items={audit} />}
    </div>
  );
}

/** Display current playback, listener, and coordinator state. */
function Dashboard({ status, onPlayback }) {
  const radio = status?.autodj;
  const track = radio?.track;
  const next = radio?.next_track;
  const controlsDisabled = status?.master?.is_autodj !== true;
  return (
    <main className="content">
      <section className="hero card">
        <div>
          <p className="eyebrow">Now playing</p>
          <h2>{track?.name || "No track"}</h2>
          <p className="muted">
            {track?.artists?.map((artist) => artist.name).join(", ") ||
              "AutoDJ is waiting for a playlist"}
          </p>
          <p className="muted small">
            {radio?.playlist?.name || "No active playlist"} ·{" "}
            {status?.schedule?.active_occurrence?.title || "Default program"}
          </p>
        </div>
        <div className="controls">
          <button
            className="primary"
            disabled={controlsDisabled}
            onClick={() => onPlayback("skip-current")}
            type="button"
          >
            Skip current
          </button>
          <button
            className="secondary"
            disabled={controlsDisabled}
            onClick={() => onPlayback("skip-next")}
            type="button"
          >
            Replace next
          </button>
          <button
            className="secondary"
            disabled={controlsDisabled}
            onClick={() => onPlayback("reload-playlist")}
            type="button"
          >
            Refresh playlist
          </button>
        </div>
      </section>
      <div className="grid three">
        <Info
          label="Master"
          value={
            status?.master?.is_autodj
              ? "AutoDJ"
              : status?.master?.display_name || "Human DJ"
          }
        />
        <Info label="Listeners" value={status?.listeners ?? 0} />
        <Info
          label="Next transition"
          value={
            status?.schedule?.next_transition
              ? new Date(status.schedule.next_transition).toLocaleString()
              : "None"
          }
        />
      </div>
      <section className="card two-col">
        <div>
          <p className="eyebrow">Queued next</p>
          <h3>{next?.name || "None"}</h3>
          <p className="muted">
            {next?.artists?.map((artist) => artist.name).join(", ")}
          </p>
        </div>
        <div>
          <p className="eyebrow">Schedule status</p>
          <p>{status?.schedule?.active_occurrence?.title || "Default playlist"}</p>
          <p className="muted small">
            {status?.schedule?.error ||
              (status?.schedule?.catalog_errors &&
              Object.keys(status.schedule.catalog_errors).length
                ? `${Object.keys(status.schedule.catalog_errors).length} catalog refresh error(s)`
                : "No scheduler errors")}
          </p>
        </div>
      </section>
    </main>
  );
}

/** Render one compact operational metric card. */
function Info({ label, value }) {
  return (
    <div className="card info">
      <p className="eyebrow">{label}</p>
      <strong>{value}</strong>
    </div>
  );
}

/** Render the calendar and scoped recurrence editor. */
function Schedule({
  events,
  playlists,
  csrf,
  onChanged,
  onError,
  onNotice,
  onRange,
}) {
  const [editor, setEditor] = useState(null);
  const [saving, setSaving] = useState(false);

  /** Fetch the authoritative series when an expanded occurrence is clicked. */
  async function selectOccurrence(clickInfo) {
    try {
      onError("");
      const occurrence = clickInfo.event.extendedProps;
      const series = await request(`/schedule/series/${occurrence.series_id}`);
      setEditor({ series, occurrence });
    } catch (caught) {
      onError(caught.message);
    }
  }

  /** Execute one scoped calendar edit and close/reload on success. */
  async function editSchedule(action, values) {
    setSaving(true);
    onError("");
    try {
      const series = editor?.series;
      const occurrence = editor?.occurrence;
      const { occurrence_start_local: occurrenceStartLocal, ...seriesValues } = values;
      if (action === "create") {
        await mutate("/schedule/series", "POST", seriesValues, csrf);
      } else if (action === "series") {
        await mutate(
          `/schedule/series/${series.id}`,
          "PATCH",
          { ...seriesValues, version: series.version },
          csrf,
        );
      } else if (action === "occurrence") {
        await mutate(
          `/schedule/series/${series.id}/exceptions`,
          "POST",
          {
            action: "override",
            original_start_utc: occurrence.original_start_utc,
            title: values.title,
            playlist_id: values.playlist_id,
            start_local: occurrenceStartLocal,
            timezone: values.timezone,
            duration_seconds: values.duration_seconds,
            priority: values.priority,
            version: series.version,
          },
          csrf,
        );
      } else if (action === "cancel") {
        await mutate(
          `/schedule/series/${series.id}/exceptions`,
          "POST",
          {
            action: "cancel",
            original_start_utc: occurrence.original_start_utc,
            version: series.version,
          },
          csrf,
        );
      } else if (action === "future") {
        await mutate(
          `/schedule/series/${series.id}/split`,
          "POST",
          {
            effective_dtstart_local: utcToZonedLocal(
              occurrence.original_start_utc,
              series.timezone,
            ),
            values: seriesValues,
            version: series.version,
          },
          csrf,
        );
      } else if (action === "delete") {
        await mutate(
          `/schedule/series/${series.id}?version=${series.version}`,
          "DELETE",
          undefined,
          csrf,
        );
      }
      setEditor(null);
      onNotice(
        action === "delete"
          ? "Schedule series deleted."
          : action === "cancel"
            ? "Occurrence cancelled."
            : "Schedule saved.",
      );
      await onChanged();
    } catch (caught) {
      onError(caught.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="content">
      <section className="section-heading">
        <div>
          <p className="eyebrow">Programming</p>
          <h2>Schedule</h2>
          <p className="muted">
            Times follow each event&apos;s IANA timezone. Click an occurrence to edit
            one, this and future, or the whole series.
          </p>
        </div>
        <button
          className="primary"
          onClick={() => setEditor(editor ? null : { series: null, occurrence: null })}
          type="button"
        >
          {editor ? "Close editor" : "Add show"}
        </button>
      </section>
      {editor && (
        <ScheduleForm
          key={`${editor.series?.id || "new"}-${editor.occurrence?.original_start_utc || ""}`}
          playlists={playlists}
          series={editor.series}
          occurrence={editor.occurrence}
          saving={saving}
          onAction={editSchedule}
        />
      )}
      {!editor && (
        <div className="card calendar">
          <FullCalendar
            plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin]}
            initialView="timeGridWeek"
            headerToolbar={{
              left: "prev,next today",
              center: "title",
              right: "dayGridMonth,timeGridWeek,timeGridDay",
            }}
            events={events}
            eventClick={(info) => void selectOccurrence(info)}
            datesSet={(info) => void onRange(info.start, info.end)}
            height="auto"
          />
        </div>
      )}
    </main>
  );
}

/** Render create/edit fields and explicit recurrence edit scopes. */
function ScheduleForm({ playlists, series, occurrence, saving, onAction }) {
  const timezone =
    series?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Istanbul";
  const recurrence = parseRRule(series?.rrule, series?.dtstart_local);
  const [form, setForm] = useState({
    title: series?.title || "",
    playlist_id: series?.playlist_id || playlists[0]?.id || "",
    dtstart_local: series?.dtstart_local || "",
    occurrence_start_local: occurrence
      ? utcToZonedLocal(occurrence.start_utc, timezone)
      : "",
    duration_seconds: series?.duration_seconds || 3600,
    timezone,
    priority: series?.priority || 0,
    ...recurrence,
  });

  /** Update one controlled form field. */
  function field(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
  }

  /** Toggle one weekday while retaining at least one weekly selection. */
  function toggleWeekday(code) {
    setForm((current) => {
      const selected = current.byday.includes(code)
        ? current.byday.filter((item) => item !== code)
        : [...current.byday, code];
      return { ...current, byday: selected.length ? selected : [code] };
    });
  }

  /** Convert controlled values to the backend series contract. */
  function payload() {
    return {
      title: form.title.trim() || "Untitled show",
      playlist_id: form.playlist_id,
      dtstart_local: form.dtstart_local,
      duration_seconds: Number(form.duration_seconds),
      timezone: form.timezone.trim(),
      rrule: buildRRule(form),
      priority: Number(form.priority),
      transition_policy: "immediate",
      occurrence_start_local: form.occurrence_start_local,
    };
  }

  /** Save a new series or apply a whole-series edit on normal submit. */
  function submit(event) {
    event.preventDefault();
    void onAction(series ? "series" : "create", payload());
  }

  return (
    <form className="card form" onSubmit={submit}>
      <div className="form-grid">
        <label>
          Title
          <input
            required
            maxLength="200"
            value={form.title}
            onChange={(event) => field("title", event.target.value)}
            placeholder="Morning program"
          />
        </label>
        <label>
          Playlist
          <select
            required
            value={form.playlist_id}
            onChange={(event) => field("playlist_id", event.target.value)}
          >
            {playlists.filter((playlist) => playlist.enabled).map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name || playlist.spotify_uri}
              </option>
            ))}
          </select>
        </label>
        <label>
          Series starts
          <input
            required
            type="datetime-local"
            step="1"
            value={form.dtstart_local}
            onChange={(event) => field("dtstart_local", event.target.value)}
          />
        </label>
        {occurrence && (
          <label>
            This occurrence starts
            <input
              required
              type="datetime-local"
              step="1"
              value={form.occurrence_start_local}
              onChange={(event) => field("occurrence_start_local", event.target.value)}
            />
          </label>
        )}
        <label>
          Duration (seconds)
          <input
            required
            min="1"
            max="604800"
            type="number"
            value={form.duration_seconds}
            onChange={(event) => field("duration_seconds", event.target.value)}
          />
        </label>
        <label>
          IANA timezone
          <input
            required
            value={form.timezone}
            onChange={(event) => field("timezone", event.target.value)}
            placeholder="Europe/Istanbul"
          />
        </label>
        <label>
          Priority
          <input
            min="-1000"
            max="1000"
            type="number"
            value={form.priority}
            onChange={(event) => field("priority", event.target.value)}
          />
        </label>
        <label>
          Repeats
          <select
            value={form.repeat}
            onChange={(event) => field("repeat", event.target.value)}
          >
            <option value="none">Does not repeat</option>
            <option value="daily">Every N days</option>
            <option value="weekly">Every N weeks</option>
            <option value="monthly">Every N months</option>
            <option value="yearly">Every N years</option>
          </select>
        </label>
        {form.repeat !== "none" && (
          <label>
            Repeat interval
            <input
              min="1"
              max="10000"
              type="number"
              value={form.interval}
              onChange={(event) => field("interval", event.target.value)}
            />
          </label>
        )}
        {form.repeat === "monthly" && (
          <label>
            Monthly pattern
            <select
              value={form.monthlyMode}
              onChange={(event) => field("monthlyMode", event.target.value)}
            >
              <option value="monthday">Same day of month</option>
              <option value="weekday">Same nth weekday</option>
            </select>
          </label>
        )}
        {form.repeat !== "none" && (
          <label>
            Ends
            <select
              value={form.ends}
              onChange={(event) => field("ends", event.target.value)}
            >
              <option value="never">Never</option>
              <option value="until">On date</option>
              <option value="count">After count</option>
            </select>
          </label>
        )}
        {form.ends === "until" && form.repeat !== "none" && (
          <label>
            Last local date
            <input
              required
              type="date"
              value={form.until}
              onChange={(event) => field("until", event.target.value)}
            />
          </label>
        )}
        {form.ends === "count" && form.repeat !== "none" && (
          <label>
            Occurrence count
            <input
              required
              min="1"
              max="10000"
              type="number"
              value={form.count}
              onChange={(event) => field("count", event.target.value)}
            />
          </label>
        )}
      </div>

      {form.repeat === "weekly" && (
        <fieldset className="weekday-fieldset">
          <legend>Weekdays</legend>
          <div className="weekday-options">
            {WEEKDAYS.map(([code, label]) => (
              <label key={code} className="check-label">
                <input
                  checked={form.byday.includes(code)}
                  onChange={() => toggleWeekday(code)}
                  type="checkbox"
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <div className="form-actions">
        <button className="primary" disabled={saving || !playlists.length} type="submit">
          {series ? "Save whole series" : "Save schedule"}
        </button>
        {occurrence && (
          <>
            <button
              className="secondary"
              disabled={saving}
              onClick={() => void onAction("occurrence", payload())}
              type="button"
            >
              Save this occurrence
            </button>
            {series.rrule && (
              <button
                className="secondary"
                disabled={saving}
                onClick={() => void onAction("future", payload())}
                type="button"
              >
                Save this and future
              </button>
            )}
            <button
              className="secondary danger-button"
              disabled={saving}
              onClick={() => void onAction("cancel", payload())}
              type="button"
            >
              Cancel this occurrence
            </button>
            <button
              className="secondary danger-button"
              disabled={saving}
              onClick={() => {
                if (window.confirm("Delete this entire schedule series?")) {
                  void onAction("delete", payload());
                }
              }}
              type="button"
            >
              Delete series
            </button>
          </>
        )}
      </div>
    </form>
  );
}

/** Render playlist registration, default selection, refresh, and lifecycle actions. */
function Playlists({
  playlists,
  defaultPlaylistId,
  onSave,
  onRefresh,
  onDefault,
  onToggle,
  onDelete,
}) {
  const [uri, setUri] = useState("");
  return (
    <main className="content">
      <section className="section-heading">
        <div>
          <p className="eyebrow">Spotify sources</p>
          <h2>Playlists</h2>
          <p className="muted">
            Add a Spotify playlist URL or URI. The service validates and caches
            playable tracks before saving it.
          </p>
        </div>
      </section>
      <form
        className="card inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          void onSave(uri).then((saved) => {
            if (saved) setUri("");
          });
        }}
      >
        <input
          required
          value={uri}
          onChange={(event) => setUri(event.target.value)}
          placeholder="https://open.spotify.com/playlist/..."
        />
        <button className="primary" type="submit">Validate and add</button>
      </form>
      <div className="list">
        {playlists.map((playlist) => {
          const isDefault = defaultPlaylistId === playlist.id;
          return (
            <article className="card row" key={playlist.id}>
              <div>
                <h3>{playlist.name || playlist.spotify_uri}</h3>
                <p className="muted small">
                  {playlist.spotify_uri} · {playlist.track_count || 0} playable tracks
                </p>
                {playlist.validation_error && (
                  <p className="danger small">{playlist.validation_error}</p>
                )}
              </div>
              <div className="row-actions">
                <span className={playlist.enabled ? "pill good" : "pill"}>
                  {playlist.enabled ? "Enabled" : "Disabled"}
                </span>
                {isDefault ? (
                  <span className="pill good">Default</span>
                ) : (
                  <button
                    className="secondary"
                    disabled={!playlist.enabled}
                    onClick={() => void onDefault(playlist.id)}
                    type="button"
                  >
                    Set default
                  </button>
                )}
                <button
                  className="secondary"
                  onClick={() => void onRefresh(playlist.id)}
                  type="button"
                >
                  Refresh
                </button>
                <button
                  className="secondary"
                  disabled={isDefault}
                  onClick={() => void onToggle(playlist)}
                  type="button"
                >
                  {playlist.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  className="secondary danger-button"
                  disabled={isDefault}
                  onClick={() => {
                    if (window.confirm(`Delete ${playlist.name || "this playlist"}?`)) {
                      void onDelete(playlist);
                    }
                  }}
                  type="button"
                >
                  Delete
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </main>
  );
}

/** Render the newest administrative audit entries. */
function Audit({ items }) {
  return (
    <main className="content">
      <section className="section-heading">
        <div>
          <p className="eyebrow">Change history</p>
          <h2>Audit log</h2>
        </div>
      </section>
      <div className="list">
        {items.map((item) => (
          <article className="card audit-row" key={item.id}>
            <div>
              <strong>{item.action}</strong>
              <p className="muted small">
                {item.entity_type} {item.entity_id || ""} ·{" "}
                {item.actor_display_name || item.actor_spotify_id}
              </p>
            </div>
            <time className="muted small">{new Date(item.occurred_at).toLocaleString()}</time>
          </article>
        ))}
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
