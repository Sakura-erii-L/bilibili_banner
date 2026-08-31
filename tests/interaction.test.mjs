import assert from "node:assert/strict";

import {
  cyclicMinuteDistance,
  sampleCurve,
  selectTimedVariant,
} from "../frontend/interaction.js";

assert.equal(sampleCurve([0, 100], [0, 50], 50), 25);
assert.equal(sampleCurve([0, 100], [0, 50], -10), 0);
assert.equal(sampleCurve([0, 100], [0, 50], 120), 50);
assert.equal(cyclicMinuteDistance(1430, 10), 20);

const record = {
  variants: [
    { manifest: "morning.json", observedSlots: [360], capturedAt: "2026-08-31T06:17:00+08:00" },
    { manifest: "evening.json", observedSlots: [1080], capturedAt: "2026-08-31T18:17:00+08:00" },
  ],
};

assert.equal(
  selectTimedVariant(record, new Date("2026-08-30T22:30:00Z"), "Asia/Shanghai").manifest,
  "morning.json",
);
assert.equal(
  selectTimedVariant(record, new Date("2026-08-31T11:30:00Z"), "Asia/Shanghai").manifest,
  "evening.json",
);

console.log("interaction.test.mjs: OK");
