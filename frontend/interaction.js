export function sampleCurve(inputs, outputs, input, fallback = 0) {
  if (!Array.isArray(inputs) || !Array.isArray(outputs) || inputs.length === 0) {
    return fallback;
  }

  const length = Math.min(inputs.length, outputs.length);
  if (length === 0) return fallback;

  const x = Number(input);
  const firstX = Number(inputs[0]);
  const firstY = Number(outputs[0]);
  if (!Number.isFinite(x) || !Number.isFinite(firstX) || !Number.isFinite(firstY)) {
    return fallback;
  }
  if (x <= firstX || length === 1) return firstY;

  for (let i = 1; i < length; i += 1) {
    const rightX = Number(inputs[i]);
    const rightY = Number(outputs[i]);
    if (!Number.isFinite(rightX) || !Number.isFinite(rightY)) continue;
    if (x <= rightX) {
      const leftX = Number(inputs[i - 1]);
      const leftY = Number(outputs[i - 1]);
      const width = rightX - leftX;
      if (!Number.isFinite(leftX) || !Number.isFinite(leftY) || width <= 0) {
        return rightY;
      }
      const progress = (x - leftX) / width;
      return leftY + (rightY - leftY) * progress;
    }
  }

  const last = Number(outputs[length - 1]);
  return Number.isFinite(last) ? last : fallback;
}

export function minutesInTimeZone(date = new Date(), timeZone = "Asia/Shanghai") {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const hour = Number(parts.find(part => part.type === "hour")?.value || 0);
  const minute = Number(parts.find(part => part.type === "minute")?.value || 0);
  return hour * 60 + minute;
}

export function cyclicMinuteDistance(a, b) {
  const distance = Math.abs(Number(a) - Number(b)) % 1440;
  return Math.min(distance, 1440 - distance);
}

export function selectTimedVariant(
  record,
  date = new Date(),
  timeZone = "Asia/Shanghai",
) {
  const variants = Array.isArray(record?.variants) ? record.variants : [];
  if (!variants.length) return null;
  if (variants.length === 1) return variants[0];

  const currentMinute = minutesInTimeZone(date, timeZone);
  let selected = variants[0];
  let selectedDistance = Number.POSITIVE_INFINITY;

  for (const variant of variants) {
    const slots = Array.isArray(variant.observedSlots) ? variant.observedSlots : [];
    const distance = slots.length
      ? Math.min(...slots.map(slot => cyclicMinuteDistance(currentMinute, slot)))
      : Number.POSITIVE_INFINITY;

    if (
      distance < selectedDistance
      || (
        distance === selectedDistance
        && String(variant.capturedAt || "") > String(selected.capturedAt || "")
      )
    ) {
      selected = variant;
      selectedDistance = distance;
    }
  }

  return selected;
}

function cubicBezierCoordinate(t, p1, p2) {
  const inv = 1 - t;
  return 3 * inv * inv * t * p1 + 3 * inv * t * t * p2 + t * t * t;
}

function cubicBezierDerivative(t, p1, p2) {
  const inv = 1 - t;
  return 3 * inv * inv * p1 + 6 * inv * t * (p2 - p1) + 3 * t * t * (1 - p2);
}

export function cubicBezierValue(curve, input) {
  if (!Array.isArray(curve) || curve.length < 4) return Number(input) || 0;
  const [x1, y1, x2, y2] = curve.map(Number);
  if (![x1, y1, x2, y2].every(Number.isFinite)) return Number(input) || 0;

  const x = Math.max(0, Math.min(1, Number(input) || 0));
  let t = x;
  for (let i = 0; i < 8; i += 1) {
    const currentX = cubicBezierCoordinate(t, x1, x2) - x;
    if (Math.abs(currentX) < 1e-7) break;
    const derivative = cubicBezierDerivative(t, x1, x2);
    if (Math.abs(derivative) < 1e-7) break;
    t = Math.max(0, Math.min(1, t - currentX / derivative));
  }

  let low = 0;
  let high = 1;
  for (let i = 0; i < 12; i += 1) {
    const currentX = cubicBezierCoordinate(t, x1, x2);
    if (Math.abs(currentX - x) < 1e-7) break;
    if (currentX < x) low = t;
    else high = t;
    t = (low + high) / 2;
  }
  return cubicBezierCoordinate(t, y1, y2);
}

export function signedCubicBezier(curve, input) {
  const value = Number(input) || 0;
  if (!Array.isArray(curve) || curve.length < 4) return value;
  return Math.sign(value) * cubicBezierValue(curve, Math.abs(value));
}

export function wrapDynamicValue(value, wrap = "clamp", min = 0, max = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return min;
  if (wrap === "alternate") {
    const width = max - min;
    if (width <= 0) return min;
    const normalized = (number - min) / width;
    const cycle = ((normalized % 2) + 2) % 2;
    const folded = cycle <= 1 ? cycle : 2 - cycle;
    return min + folded * width;
  }
  return Math.max(min, Math.min(max, number));
}
