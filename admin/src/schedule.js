/** Pure helpers used by the recurrence editor and its unit tests. */

export const WEEKDAYS = [
  ["MO", "Mon"],
  ["TU", "Tue"],
  ["WE", "Wed"],
  ["TH", "Thu"],
  ["FR", "Fri"],
  ["SA", "Sat"],
  ["SU", "Sun"],
];

const JS_WEEKDAY_CODES = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

/** Parse a local ``datetime-local`` value without applying the browser zone. */
export function localDateParts(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(
    value || "",
  );
  if (!match) return null;
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
    second: Number(match[6] || 0),
  };
}

/** Build the server-authoritative RFC 5545 rule represented by the form. */
export function buildRRule(form) {
  if (form.repeat === "none") return null;
  const interval = Math.max(1, Number(form.interval) || 1);
  const parts = [`FREQ=${form.repeat.toUpperCase()}`, `INTERVAL=${interval}`];
  const local = localDateParts(form.dtstart_local);

  if (form.repeat === "weekly") {
    parts.push(`BYDAY=${form.byday.length ? form.byday.join(",") : "MO"}`);
  } else if (form.repeat === "monthly") {
    if (form.monthlyMode === "weekday" && local) {
      const weekday = JS_WEEKDAY_CODES[
        new Date(Date.UTC(local.year, local.month - 1, local.day)).getUTCDay()
      ];
      const daysInMonth = new Date(Date.UTC(local.year, local.month, 0)).getUTCDate();
      const position = local.day + 7 > daysInMonth ? -1 : Math.ceil(local.day / 7);
      parts.push(`BYDAY=${weekday}`, `BYSETPOS=${position}`);
    } else if (local) {
      parts.push(`BYMONTHDAY=${local.day}`);
    }
  } else if (form.repeat === "yearly" && local) {
    parts.push(`BYMONTH=${local.month}`, `BYMONTHDAY=${local.day}`);
  }

  if (form.ends === "count") {
    parts.push(`COUNT=${Math.max(1, Number(form.count) || 1)}`);
  } else if (form.ends === "until" && form.until) {
    parts.push(`UNTIL=${form.until.replaceAll("-", "")}T235959`);
  }
  return parts.join(";");
}

/** Convert an RRULE string into the subset represented by the editor. */
export function parseRRule(rule, dtstartLocal) {
  const values = Object.fromEntries(
    String(rule || "")
      .replace(/^RRULE:/i, "")
      .split(";")
      .filter(Boolean)
      .map((part) => part.split("=", 2)),
  );
  const frequency = (values.FREQ || "none").toLowerCase();
  const repeat = ["daily", "weekly", "monthly", "yearly"].includes(frequency)
    ? frequency
    : "none";
  const local = localDateParts(dtstartLocal);
  return {
    repeat,
    interval: Number(values.INTERVAL || 1),
    byday: values.BYDAY?.split(",").filter(Boolean) || ["MO"],
    monthlyMode: values.BYSETPOS ? "weekday" : "monthday",
    ends: values.COUNT ? "count" : values.UNTIL ? "until" : "never",
    count: Number(values.COUNT || 10),
    until: values.UNTIL
      ? `${values.UNTIL.slice(0, 4)}-${values.UNTIL.slice(4, 6)}-${values.UNTIL.slice(6, 8)}`
      : "",
    localDay: local?.day || 1,
  };
}

/** Format a UTC ISO timestamp as an offset-free wall time in an IANA zone. */
export function utcToZonedLocal(isoValue, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(isoValue));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
}
