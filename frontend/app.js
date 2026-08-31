import { sampleCurve, selectTimedVariant } from "./interaction.js";

const gallery = document.getElementById("gallery");
const yearSelect = document.getElementById("year");
const monthSelect = document.getElementById("month");
const seasonSelect = document.getElementById("season");
const resetButton = document.getElementById("reset");
const summary = document.getElementById("summary");
const empty = document.getElementById("empty");

let indexData = null;
let observer = null;

const seasonNames = {
  spring: "春",
  summer: "夏",
  autumn: "秋",
  winter: "冬",
};

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function matrixCss(m) {
  return `matrix(${m.map(v => Number(v).toFixed(8)).join(",")})`;
}

function formatDate(iso) {
  const [y, m, d] = iso.split("-");
  return `${y}年${m}月${d}日`;
}

function resolveAsset(manifestUrl, file) {
  return new URL(file, new URL(manifestUrl, location.href)).href;
}

function applyAnimation(element, animation) {
  if (!animation?.name || animation.name === "none") return;
  element.style.animationName = animation.name;
  element.style.animationDuration = animation.duration || "initial";
  element.style.animationDelay = animation.delay || "0s";
  element.style.animationIterationCount = animation.iterationCount || "1";
  element.style.animationTimingFunction = animation.timingFunction || "ease";
  element.style.animationDirection = animation.direction || "normal";
  element.style.animationFillMode = animation.fillMode || "none";
  element.style.animationPlayState = animation.playState || "running";
  element.style.transitionProperty = animation.transitionProperty || "";
  element.style.transitionDuration = animation.transitionDuration || "";
  element.style.transitionTimingFunction = animation.transitionTimingFunction || "";
}

function createInteractiveBanner(shell, manifest, manifestUrl) {
  const stage = shell.querySelector(".stage");
  stage.innerHTML = "";
  stage.classList.add("animated-banner");

  if (manifest.animationCss) {
    const style = document.createElement("style");
    style.dataset.bannerAnimation = "1";
    style.textContent = manifest.animationCss;
    stage.appendChild(style);
  }

  const types = Array.isArray(manifest.type) ? manifest.type : [];
  const isLayered = types.includes("layered") || manifest.mode === "split";
  if (!isLayered || !manifest.layers?.length) {
    const staticAsset = manifest.static || {};
    const file = staticAsset.file;
    if (file) {
      const isVideo = staticAsset.assetType === "video"
        || staticAsset.tag === "video"
        || /^video\//i.test(staticAsset.contentType || "")
        || /\.(?:mp4|webm|m3u8)(?:$|\?)/i.test(file);
      const media = document.createElement(isVideo ? "video" : "img");
      media.className = isVideo ? "static-video" : "static-image";
      media.src = resolveAsset(manifestUrl, file);
      media.alt = "";
      media.style.objectFit = staticAsset.objectFit || "cover";
      media.style.objectPosition = staticAsset.objectPosition || "50% 50%";
      applyAnimation(media, staticAsset.animation);
      if (isVideo) {
        media.autoplay = true;
        media.loop = true;
        media.muted = true;
        media.playsInline = true;
      }
      stage.appendChild(media);
    } else {
      stage.innerHTML = '<div class="loading error">该归档缺少可显示素材</div>';
    }
    return;
  }

  const baseWidth = manifest.viewport?.width || 1650;
  const compensate = window.innerWidth > baseWidth
    ? window.innerWidth / baseWidth
    : 1;

  const returnDuration = manifest.interaction?.returnDurationMs ?? 300;
  const inputSamples = Array.isArray(manifest.interaction?.inputSamplesPx)
    ? manifest.interaction.inputSamplesPx.map(value => Number(value) * compensate)
    : [];
  const returnSamples = Array.isArray(manifest.interaction?.returnSamplesMs)
    ? manifest.interaction.returnSamplesMs.map(Number)
    : [];
  const sampledModel =
    manifest.interaction?.model === "bilibili-sampled-horizontal-v1"
    && inputSamples.length > 1;
  const nodes = [];

  const zeroEffect = () => ({
    matrix: [0, 0, 0, 0, 0, 0],
    layerOpacity: 0,
    mediaOpacity: 0,
  });

  const copyEffect = effect => ({
    matrix: [...effect.matrix],
    layerOpacity: effect.layerOpacity,
    mediaOpacity: effect.mediaOpacity,
  });

  const addEffects = (left, right) => ({
    matrix: left.matrix.map((value, index) => value + right.matrix[index]),
    layerOpacity: left.layerOpacity + right.layerOpacity,
    mediaOpacity: left.mediaOpacity + right.mediaOpacity,
  });

  const scaleEffect = (effect, scale) => ({
    matrix: effect.matrix.map(value => value * scale),
    layerOpacity: effect.layerOpacity * scale,
    mediaOpacity: effect.mediaOpacity * scale,
  });

  for (const item of manifest.layers) {
    if (!item.file) continue;
    const layer = document.createElement("div");
    layer.className = "layer";
    layer.style.zIndex = String(Number(item.zIndex) || 0);
    const motion = document.createElement("div");
    motion.className = "motion";

    const isVideo =
      item.tag === "video"
      || /\.(webm|mp4)(?:$|\?)/i.test(item.file || "")
      || /^video\//i.test(item.contentType || "");

    const media = document.createElement(isVideo ? "video" : "img");
    media.src = resolveAsset(manifestUrl, item.file);
    media.style.width = `${Number(item.width || item.cssWidth || 0) * compensate}px`;
    media.style.height = `${Number(item.height || item.cssHeight || 0) * compensate}px`;
    media.style.objectFit = item.objectFit || "fill";
    media.style.objectPosition = item.objectPosition || "50% 50%";
    media.draggable = false;

    const animation = item.animation || {};
    applyAnimation(item.animationTarget === "target" ? motion : media, animation);

    if (media.tagName === "VIDEO") {
      media.autoplay = true;
      media.loop = true;
      media.muted = true;
      media.playsInline = true;
    }

    // Reconstruct the initial Bilibili transform on the layer itself,
    // matching the established reproduction approach.
    const initial = [...(item.transform || [1, 0, 0, 1, 0, 0])];
    if (compensate !== 1) {
      initial[4] *= compensate;
      initial[5] *= compensate;
    }
    motion.style.transform = matrixCss(initial);
    motion.style.transformOrigin = item.transformOrigin || "50% 50%";
    const position = item.position || {};
    if (position.type && position.type !== "static") {
      motion.style.position = position.type;
      for (const side of ["left", "top", "right", "bottom"]) {
        if (position[side] && position[side] !== "auto") {
          motion.style[side] = position[side];
        }
      }
    }

    const initialLayerOpacity = Number(item.opacity?.[0] ?? 1);
    const initialMediaOpacity = Number(item.opacity?.[1] ?? 1);
    layer.style.opacity = String(initialLayerOpacity);
    media.style.opacity = String(initialMediaOpacity);

    motion.appendChild(media);
    layer.appendChild(motion);
    stage.appendChild(layer);

    nodes.push({
      layer,
      transformElement: motion,
      media,
      initial,
      initialLayerOpacity,
      initialMediaOpacity,
      motion: item.motion || null,
      a: Number(item.a) || 0,
      currentEffect: zeroEffect(),
    });
  }

  let initX = 0;
  let moveX = 0;
  let startTime = null;
  let homeRaf = 0;
  let moveRaf = 0;
  let enterBaseEffects = nodes.map(() => zeroEffect());
  let homeStartEffects = nodes.map(() => zeroEffect());

  function sampledEffect(node, currentMoveX) {
    if (!sampledModel || !Array.isArray(node.motion?.matrixDelta)) {
      const effect = zeroEffect();
      effect.matrix[4] = currentMoveX * node.a;
      return effect;
    }

    const effect = zeroEffect();
    for (let component = 0; component < 6; component += 1) {
      const outputs = node.motion.matrixDelta.map(sample => Number(sample?.[component]) || 0);
      const scale = component === 4 ? compensate : 1;
      effect.matrix[component] = sampleCurve(inputSamples, outputs.map(x => x * scale), currentMoveX);
    }
    effect.layerOpacity = sampleCurve(
      inputSamples,
      node.motion.layerOpacityDelta || [],
      currentMoveX,
    );
    effect.mediaOpacity = sampleCurve(
      inputSamples,
      node.motion.mediaOpacityDelta || [],
      currentMoveX,
    );
    return effect;
  }

  function applyEffect(node, effect) {
    const matrix = [...node.initial];
    for (let component = 0; component < 6; component += 1) {
      matrix[component] = node.initial[component] + effect.matrix[component];
    }

    layerSetTransform(node.transformElement, matrix);
    node.layer.style.opacity = String(clamp(
      node.initialLayerOpacity + effect.layerOpacity,
      0,
      1,
    ));
    node.media.style.opacity = String(clamp(
      node.initialMediaOpacity + effect.mediaOpacity,
      0,
      1,
    ));
    node.currentEffect = copyEffect(effect);
  }

  function applyPointerMove(currentMoveX) {
    nodes.forEach((node, index) => {
      const effect = addEffects(
        enterBaseEffects[index],
        sampledEffect(node, currentMoveX),
      );
      applyEffect(node, effect);
    });
  }

  function returnRemaining(node, elapsed) {
    const values = node.motion?.returnRemaining;
    if (sampledModel && returnSamples.length > 1 && Array.isArray(values)) {
      return sampleCurve(returnSamples, values, elapsed, 0);
    }
    if (returnDuration <= 0) return 0;
    return clamp(1 - elapsed / returnDuration, 0, 1);
  }

  function resetAllEffects() {
    nodes.forEach(node => applyEffect(node, zeroEffect()));
  }

  function homing(timestamp) {
    if (startTime === null) startTime = timestamp;
    const elapsed = timestamp - startTime;
    const duration = Math.max(
      Number(returnDuration) || 0,
      returnSamples.at(-1) || 0,
    );

    nodes.forEach((node, index) => {
      applyEffect(
        node,
        scaleEffect(homeStartEffects[index], returnRemaining(node, elapsed)),
      );
    });

    if (elapsed < duration) {
      homeRaf = requestAnimationFrame(homing);
    } else {
      moveX = 0;
      resetAllEffects();
    }
  }

  function layerSetTransform(layer, matrix) {
    layer.style.transform = matrixCss(matrix);
  }

  shell.addEventListener("mouseenter", e => {
    cancelAnimationFrame(homeRaf);
    cancelAnimationFrame(moveRaf);
    initX = e.clientX;
    enterBaseEffects = nodes.map(node => copyEffect(node.currentEffect));
  });

  shell.addEventListener("mousemove", e => {
    moveX = e.clientX - initX;
    if (moveRaf) return;
    moveRaf = requestAnimationFrame(() => {
      moveRaf = 0;
      applyPointerMove(moveX);
    });
  });

  shell.addEventListener("mouseleave", () => {
    cancelAnimationFrame(homeRaf);
    cancelAnimationFrame(moveRaf);
    moveRaf = 0;
    startTime = null;
    homeStartEffects = nodes.map(node => copyEffect(node.currentEffect));
    if (returnDuration <= 0 && returnSamples.length === 0) {
      resetAllEffects();
    } else {
      homeRaf = requestAnimationFrame(homing);
    }
  });
}
async function loadEntry(entryEl) {
  if (entryEl.dataset.loaded === "1") return;
  entryEl.dataset.loaded = "1";

  const manifestUrl = entryEl.dataset.manifest;
  const shell = entryEl.querySelector(".banner");

  try {
    const response = await fetch(`${manifestUrl}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const manifest = await response.json();
    createInteractiveBanner(shell, manifest, manifestUrl);
  } catch (error) {
    const stage = shell.querySelector(".stage");

    // Hard rule: never display preview.png or any screenshot as a Banner.
    stage.innerHTML =
      `<div class="loading error">分层素材载入失败：${error.message}</div>`;
  }
}

function selectedVariant(record) {
  return selectTimedVariant(
    record,
    new Date(),
    record.timeZone || indexData?.timeZone || "Asia/Shanghai",
  );
}

function recordMeta(record, variant) {
  const mode = variant?.mode || record.mode;
  const layerCount = variant?.layerCount ?? record.layerCount;
  const types = Array.isArray(variant?.type)
    ? variant.type
    : Array.isArray(record.type) ? record.type : [];
  const typeLabels = types
    .filter(type => type !== "static")
    .map(type => ({
      layered: "分层",
      video: "视频",
      animated: "动画",
      interactive: "交互",
    }[type] || type));
  const typeText = typeLabels.length
    ? typeLabels.join(" · ")
    : (mode === "split" ? `${layerCount} 个图层` : "静态 Banner");
  const variantText = Number(record.variantCount || record.variants?.length || 0) > 1
    ? ` · ${record.variantCount || record.variants.length} 个时段变体`
    : "";
  return `${seasonNames[record.season] || ""} · `
    + typeText
    + (typeLabels.includes("分层") ? ` (${layerCount} 个图层)` : "")
    + variantText;
}

function createEntry(record) {
  const article = document.createElement("article");
  const variant = selectedVariant(record);
  article.className = "entry";
  article.dataset.recordId = record.id;
  article.dataset.manifest = variant?.manifest || record.manifest;
  const head = document.createElement("div");
  head.className = "entry-head";

  const date = document.createElement("div");
  date.className = "entry-date";
  date.textContent = formatDate(record.date);

  const meta = document.createElement("div");
  meta.className = "entry-meta";
  meta.textContent = recordMeta(record, variant);

  const banner = document.createElement("div");
  banner.className = "banner";
  banner.innerHTML = '<div class="stage"><div class="loading">载入中……</div></div>';

  head.append(date, meta);
  article.append(head, banner);
  return article;
}

function refreshTimedVariants() {
  if (!indexData?.records?.length) return;
  const recordsById = new Map(indexData.records.map(record => [record.id, record]));

  for (const article of gallery.querySelectorAll(".entry")) {
    const record = recordsById.get(article.dataset.recordId);
    if (!record) continue;
    const variant = selectedVariant(record);
    const manifest = variant?.manifest || record.manifest;
    if (!manifest || manifest === article.dataset.manifest) continue;

    article.dataset.manifest = manifest;
    article.dataset.loaded = "0";
    article.querySelector(".entry-meta").textContent = recordMeta(record, variant);
    article.querySelector(".stage").innerHTML = '<div class="loading">切换时段素材……</div>';

    const bounds = article.getBoundingClientRect();
    if (bounds.bottom >= -900 && bounds.top <= window.innerHeight + 900) {
      loadEntry(article);
    } else {
      observer?.observe(article);
    }
  }
}

function setupObserver() {
  observer?.disconnect();

  observer = new IntersectionObserver(
    entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          loadEntry(entry.target);
          observer.unobserve(entry.target);
        }
      }
    },
    { rootMargin: "900px 0px" }
  );

  gallery.querySelectorAll(".entry").forEach(el => observer.observe(el));
}

function populateFilters(records) {
  const years = [...new Set(records.map(x => x.year || x.date.slice(0, 4)))].sort().reverse();

  yearSelect.innerHTML = '<option value="">全部</option>';
  for (const year of years) {
    const option = document.createElement("option");
    option.value = year;
    option.textContent = `${year}年`;
    yearSelect.appendChild(option);
  }

  monthSelect.innerHTML = '<option value="">全部</option>';
  for (let i = 1; i <= 12; i++) {
    const value = String(i).padStart(2, "0");
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${i}月`;
    monthSelect.appendChild(option);
  }
}

function render() {
  const year = yearSelect.value;
  const month = monthSelect.value;
  const season = seasonSelect.value;

  const records = indexData.records.filter(record => {
    const recordYear = record.year || record.date.slice(0, 4);
    const recordMonth = record.month || record.date.slice(5, 7);

    if (year && recordYear !== year) return false;
    if (month && recordMonth !== month) return false;
    if (season && record.season !== season) return false;
    return true;
  });

  gallery.innerHTML = "";
  for (const record of records) {
    gallery.appendChild(createEntry(record));
  }

  empty.hidden = records.length !== 0;

  summary.textContent =
    `共 ${records.length} 个唯一 Banner` +
    (indexData.records.length !== records.length
      ? `（全部 ${indexData.records.length} 个）`
      : "");

  setupObserver();
}

yearSelect.addEventListener("change", render);
monthSelect.addEventListener("change", render);
seasonSelect.addEventListener("change", render);

resetButton.addEventListener("click", () => {
  yearSelect.value = "";
  monthSelect.value = "";
  seasonSelect.value = "";
  render();
});

try {
  const response = await fetch(`./data/index.json?t=${Date.now()}`, { cache: "no-store" });

  if (!response.ok) {
    throw new Error("没有找到 data/index.json；请先运行抓取程序。");
  }

  indexData = await response.json();
  populateFilters(indexData.records || []);
  render();
  window.setInterval(refreshTimedVariants, 60_000);
} catch (error) {
  summary.innerHTML = `<span class="error">${error.message}</span>`;
}
