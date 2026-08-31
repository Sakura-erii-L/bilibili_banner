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
