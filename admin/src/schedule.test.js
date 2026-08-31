/** Unit tests for recurrence form serialization. */

import assert from "node:assert/strict";
import test from "node:test";

import { buildRRule, parseRRule, utcToZonedLocal } from "./schedule.js";

const base = {
  repeat: "weekly",
  interval: 2,
  byday: ["MO", "FR"],
  monthlyMode: "monthday",
  dtstart_local: "2026-08-31T09:00:00",
  ends: "never",
  count: 10,
  until: "",
};

test("serializes weekly intervals and weekdays", () => {
  assert.equal(buildRRule(base), "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR");
});

test("serializes the last weekday of a month", () => {
  assert.equal(
    buildRRule({
      ...base,
      repeat: "monthly",
      monthlyMode: "weekday",
      dtstart_local: "2026-08-31T09:00:00",
      ends: "count",
      count: 5,
    }),
    "FREQ=MONTHLY;INTERVAL=2;BYDAY=MO;BYSETPOS=-1;COUNT=5",
  );
});

test("parses an editable finite rule", () => {
  assert.deepEqual(
    parseRRule("FREQ=WEEKLY;INTERVAL=3;BYDAY=TU,TH;COUNT=8", base.dtstart_local),
    {
      repeat: "weekly",
      interval: 3,
      byday: ["TU", "TH"],
      monthlyMode: "monthday",
      ends: "count",
      count: 8,
      until: "",
      localDay: 31,
    },
  );
});

test("converts UTC to the series wall clock", () => {
  assert.equal(
    utcToZonedLocal("2026-08-31T06:00:00Z", "Europe/Istanbul"),
    "2026-08-31T09:00:00",
  );
});
