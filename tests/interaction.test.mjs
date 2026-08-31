import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  cubicBezierValue,
  cyclicMinuteDistance,
  sampleCurve,
  selectTimedVariant,
  signedCubicBezier,
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

const appSource = readFileSync(new URL("../frontend/app.js", import.meta.url), "utf8");
assert.match(appSource, /model === "bilibili-header-api-v1"/);
assert.match(appSource, /model === "palxiao-reconstructed-v1"/);
assert.match(appSource, /palxiao-reconstructed-v1/);

console.log("interaction.test.mjs: OK");
