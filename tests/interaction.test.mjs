import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  cubicBezierValue,
  cyclicMinuteDistance,
  formatSlotRanges,
  formatVariantPeriods,
  sampleCurve,
  selectTimedVariant,
  slotIndexInTimeZone,
  signedCubicBezier,
  timeSegments,
  wrapDynamicValue,
} from "../frontend/interaction.js";

assert.equal(sampleCurve([0, 100], [0, 50], 50), 25);
assert.equal(sampleCurve([0, 100], [0, 50], -10), 0);
assert.equal(sampleCurve([0, 100], [0, 50], 120), 50);
assert.equal(cyclicMinuteDistance(1430, 10), 20);
assert.ok(Math.abs(cubicBezierValue([0, 0, 1, 1], 0.25) - 0.25) < 1e-5);
assert.ok(Math.abs(signedCubicBezier([0, 0, 1, 1], -0.25) + 0.25) < 1e-5);
assert.equal(wrapDynamicValue(1.2, "clamp"), 1);
assert.ok(Math.abs(wrapDynamicValue(1.2, "alternate") - 0.8) < 1e-9);

const record = {
  variants: [
    { manifest: "morning.json", slots: [2], capturedAt: "2026-08-31T06:17:00+08:00" },
    { manifest: "evening.json", slots: [6], capturedAt: "2026-08-31T18:17:00+08:00" },
  ],
};

assert.equal(slotIndexInTimeZone(new Date("2026-08-30T22:30:00Z")), 2);

assert.equal(
  selectTimedVariant(record, new Date("2026-08-30T22:30:00Z"), "Asia/Shanghai").manifest,
  "morning.json",
);
assert.equal(
  selectTimedVariant(record, new Date("2026-08-31T11:30:00Z"), "Asia/Shanghai").manifest,
  "evening.json",
);
assert.equal(
  selectTimedVariant(record, new Date("2026-08-31T01:30:00Z"), "Asia/Shanghai"),
  null,
);
assert.equal(formatSlotRanges([0, 1]), "00:00–06:00");
assert.equal(formatSlotRanges([0, 2, 3]), "00:00–03:00、06:00–12:00");
assert.equal(
  formatVariantPeriods({ variants: [{ slots: [6, 7] }, { slots: [3] }] }),
  "09:00~12:00，18:00~24:00",
);

const segments = timeSegments({
  variants: [{ manifest: "banner.json", slots: [0, 1, 4, 5] }],
});
assert.deepEqual(
  segments.map(segment => [segment.label, segment.slots]),
  [
    ["00:00~06:00", [0, 1]],
    ["12:00~18:00", [4, 5]],
  ],
);

const modes = {
  variants: [
    { manifest: "normal.json", slots: [0], referenceMode: "normal", capturedAt: "1" },
    { manifest: "interactive.json", slots: [0], referenceMode: "interactive", capturedAt: "2" },
  ],
};
assert.equal(
  selectTimedVariant(modes, new Date("2026-08-31T16:00:00Z"), "Asia/Shanghai", { supportsInteractive: true }).manifest,
  "interactive.json",
);
assert.equal(
  selectTimedVariant(modes, new Date("2026-08-31T16:00:00Z"), "Asia/Shanghai", { supportsInteractive: false }).manifest,
  "interactive.json",
);

const appSource = readFileSync(new URL("../frontend/app.js", import.meta.url), "utf8");
const referenceBundle = readFileSync(new URL("../frontend/assets/mikufan-bilibanner.js", import.meta.url), "utf8");
assert.match(appSource, /model === "bilibili-header-api-v1"/);
assert.match(appSource, /model === "palxiao-reconstructed-v1"/);
assert.match(appSource, /palxiao-reconstructed-v1/);
assert.match(appSource, /mikufan-reference-v1/);
assert.match(appSource, /bilibili-banner-expand/);
assert.match(appSource, /event\?\.type===\"banner-expand\"/);
assert.match(appSource, /min-width:0 !important/);
assert.match(appSource, /springGame2022/);
assert.match(appSource, /springCollapsedHeight/);
assert.match(appSource, /querySelector\("\.bili-banner"\)/);
assert.match(appSource, /setupSpringObservers/);
assert.match(appSource, /frame\.addEventListener\("load", setupSpringObservers\)/);
assert.match(appSource, /ResizeObserver/);
assert.match(appSource, /frame\.style\.width = `\$\{width\}px`/);
assert.match(appSource, /dispatchEvent\(new Event\("resize"\)\)/);
assert.match(appSource, /article\.dataset\.manifest = variant\?\.manifest \|\| ""/);
assert.match(appSource, /show-time-controls/);
assert.match(appSource, /entry-time-controls/);
assert.match(appSource, /resetTimeSelections/);
assert.match(appSource, /article\.dataset\.timeSelection = "0"/);
assert.match(appSource, /"Asia\/Shanghai"/);
assert.doesNotMatch(referenceBundle, /mikufan039\.github\.io/);
assert.doesNotMatch(referenceBundle, /unpkg\.com\/detect-gpu/);

console.log("interaction.test.mjs: OK");
