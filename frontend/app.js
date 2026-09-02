import {
  formatSlotRanges,
  minutesInTimeZone,
  sampleCurve,
  selectTimedVariant,
  signedCubicBezier,
  wrapDynamicValue,
} from "./interaction.js";

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

function formatDateRange(record) {
  const start = record.dateStart || record.date;
  const end = record.dateEnd || start;
  return start === end
    ? formatDate(start)
    : `${formatDate(start)}～${formatDate(end)}`;
}

function resolveAsset(manifestUrl, file) {
  return new URL(file, new URL(manifestUrl, location.href)).href;
}

function supportsReferenceInteractive() {
  try {
    const canvas = document.createElement("canvas");
    const hasWebgl2 = Boolean(canvas.getContext("webgl2"));
    const hasPixelated = typeof CSS === "undefined"
      || typeof CSS.supports !== "function"
      || CSS.supports("image-rendering", "pixelated");
    const hasShadowDom = typeof document.createElement("div").attachShadow === "function";
    const enoughMemory = !navigator.deviceMemory || navigator.deviceMemory >= 4;
    const connection = navigator.connection;
    const usableNetwork = !connection || !["slow-2g", "2g"].includes(connection.effectiveType);
    return hasWebgl2 && hasPixelated && hasShadowDom && enoughMemory && usableNetwork;
  } catch (_error) {
    return false;
  }
}

function timeExtension(manifest) {
  return manifest?.extensions?.time || manifest?.api?.extensions?.time || null;
}

function currentTimeConfiguration(manifest) {
  const timeMap = timeExtension(manifest);
  if (!timeMap || typeof timeMap !== "object") return null;
  const now = new Date();
  const seconds = minutesInTimeZone(
    now,
    manifest.timeZone || "Asia/Shanghai",
  ) * 60 + now.getSeconds();
  const keys = Object.keys(timeMap)
    .map(Number)
    .filter(Number.isFinite)
    .sort((left, right) => left - right);
  const key = keys.filter(value => value <= seconds).pop();
  const candidates = key === undefined ? [] : timeMap[String(key)];
  if (!Array.isArray(candidates) || !candidates.length) return null;
  return candidates[Math.floor(Math.random() * candidates.length)] || null;
}

function manifestWithCurrentTimeConfiguration(manifest) {
  const configuration = currentTimeConfiguration(manifest);
  if (!configuration || !Array.isArray(configuration.layers)) return manifest;
  const layers = configuration.layers.map((layer, layerIndex) => {
    const resources = Array.isArray(layer.resources)
      ? layer.resources.map((resource, resourceIndex) => ({
        ...resource,
        resourceIndex,
        file: resource.file || resource.src || "",
      }))
      : [];
    const first = resources[0] || {};
    return {
      ...layer,
      index: layer.index ?? layer.id ?? layerIndex,
      file: layer.file || first.file || layer.src || "",
      resources,
      tag: layer.tag || first.tag || "img",
      assetType: layer.assetType || first.assetType || "image",
      width: Number(layer.width || 0),
      height: Number(layer.height || 0),
      naturalWidth: Number(layer.naturalWidth || 0),
      naturalHeight: Number(layer.naturalHeight || 0),
      objectFit: layer.objectFit || "fill",
      objectPosition: layer.objectPosition || "50% 50%",
      transform: layer.transform || [1, 0, 0, 1, 0, 0],
      opacity: layer.opacity || [1, 1],
    };
  }).filter(layer => layer.file || layer.resources.length);
  return { ...manifest, mode: "split", layers };
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


function createHeaderApiBanner(shell, manifest, manifestUrl) {
  manifest = manifestWithCurrentTimeConfiguration(manifest);
  const stage = shell.querySelector(".stage");
  stage.innerHTML = "";
  stage.classList.add("animated-banner");

  const referenceHeight = Number(manifest.interaction?.containerReferenceHeight) || 155;
  const returnDuration = Number(manifest.interaction?.returnDurationMs) || 200;
  const nodes = [];

  const configNumber = (config, key, fallback = 0) => {
    const value = Number(config?.[key]);
    return Number.isFinite(value) ? value : fallback;
  };

  const curveValue = (config, displacement) => {
    const curve = config?.offsetCurve;
    return Array.isArray(curve) && curve.length >= 4
      ? signedCubicBezier(curve, displacement)
      : displacement;
  };

  function mediaElement(resource) {
    const isVideo = resource.tag === "video"
      || /^video\//i.test(resource.contentType || "")
      || /\.(?:webm|mp4|m3u8)(?:$|\?)/i.test(resource.file || "");
    const element = document.createElement(isVideo ? "video" : "img");
    element.src = resolveAsset(manifestUrl, resource.file);
    element.draggable = false;
    element.alt = "";
    element.style.display = "block";
    element.style.maxWidth = "none";
    element.style.maxHeight = "none";
    if (isVideo) {
      element.autoplay = true;
      element.loop = true;
      element.muted = true;
      element.playsInline = true;
    }
    return element;
  }

  for (const item of manifest.layers || []) {
    const resources = Array.isArray(item.resources) && item.resources.length
      ? item.resources.filter(resource => resource?.file)
      : (item.file ? [{
          file: item.file,
          tag: item.tag,
          contentType: item.contentType,
          duration: 0,
        }] : []);
    if (!resources.length) continue;

    const layer = document.createElement("div");
    layer.className = "layer api-layer";
    layer.style.zIndex = String(Number(item.zIndex ?? item.index) || 0);

    const mediaHost = document.createElement("div");
    mediaHost.className = "api-layer-media";
    mediaHost.style.display = "flex";
    mediaHost.style.alignItems = "center";
    mediaHost.style.justifyContent = "center";
    mediaHost.style.willChange = "transform, filter, opacity";
    mediaHost.style.transformOrigin = "50% 50%";

    const mediaItems = resources.map(resource => {
      const element = mediaElement(resource);
      element.style.display = "none";
      mediaHost.appendChild(element);
      return {
        resource,
        element,
        naturalWidth: 0,
        naturalHeight: 0,
      };
    });
    mediaItems[0].element.style.display = "block";

    const config = item.apiConfig || {};
    const initialOpacity = configNumber(config.opacity, "initial", 1);
    mediaHost.style.opacity = String(initialOpacity);
    layer.appendChild(mediaHost);
    stage.appendChild(layer);

    const durations = mediaItems.map(entry => Math.max(0, Number(entry.resource.duration) || 0));
    const frameAnimation = mediaItems.length > 1 && durations.every(duration => duration > 0);
    const loopDuration = durations.reduce((sum, value) => sum + value, 0);

    const node = {
      layer,
      mediaHost,
      mediaItems,
      config,
      currentDisplacement: 0,
      frameAnimation,
      durations,
      loopDuration,
      frameRaf: 0,
    };
    nodes.push(node);

    const rememberNaturalSize = entry => {
      const target = entry.element;
      entry.naturalWidth = Number(target.naturalWidth || target.videoWidth || 0);
      entry.naturalHeight = Number(target.naturalHeight || target.videoHeight || 0);
      updateNodeSize(node);
    };
    mediaItems.forEach(entry => {
      entry.element.addEventListener("load", () => rememberNaturalSize(entry));
      entry.element.addEventListener("loadedmetadata", () => rememberNaturalSize(entry));
      if (entry.element.complete || entry.element.readyState >= 1) rememberNaturalSize(entry);
    });
  }

  function updateNodeSize(node) {
    const containerScale = Math.max(stage.clientHeight, 1) / referenceHeight;
    const rawInitialScale = node.config.scale?.initial;
    const initialScale = rawInitialScale === undefined ? 1 : Number(rawInitialScale);
    for (const entry of node.mediaItems) {
      if (entry.naturalWidth > 0) {
        entry.element.style.width = `${entry.naturalWidth * containerScale * initialScale}px`;
      }
      if (entry.naturalHeight > 0) {
        entry.element.style.height = `${entry.naturalHeight * containerScale * initialScale}px`;
      }
    }
    applyApiDisplacement(node, node.currentDisplacement);
  }

  function applyApiDisplacement(node, displacement) {
    node.currentDisplacement = displacement;
    const config = node.config;
    const containerScale = Math.max(stage.clientHeight, 1) / referenceHeight;
    const rawInitialScale = config.scale?.initial;
    const initialScale = rawInitialScale === undefined ? 1 : Number(rawInitialScale);
    const translateScale = Number(rawInitialScale) || 1;

    const scale = 1 + configNumber(config.scale, "offset", 0)
      * curveValue(config.scale, displacement);
    const rotate = configNumber(config.rotate, "initial", 0)
      + configNumber(config.rotate, "offset", 0) * curveValue(config.rotate, displacement);

    const translateInitial = Array.isArray(config.translate?.initial)
      ? config.translate.initial.map(Number)
      : [0, 0];
    const translateOffset = Array.isArray(config.translate?.offset)
      ? config.translate.offset.map(Number)
      : [0, 0];
    const translateCurve = curveValue(config.translate, displacement);
    const translateX = ((translateInitial[0] || 0) + (translateOffset[0] || 0) * translateCurve)
      * containerScale * translateScale;
    const translateY = ((translateInitial[1] || 0) + (translateOffset[1] || 0) * translateCurve)
      * containerScale * translateScale;

    const blurRaw = configNumber(config.blur, "initial", 0)
      + configNumber(config.blur, "offset", 0) * curveValue(config.blur, displacement);
    const blur = config.blur?.wrap === "alternate" ? Math.abs(blurRaw) : Math.max(0, blurRaw);
    const opacityRaw = configNumber(config.opacity, "initial", 1)
      + configNumber(config.opacity, "offset", 0) * curveValue(config.opacity, displacement);
    const opacity = wrapDynamicValue(opacityRaw, config.opacity?.wrap || "clamp", 0, 1);

    node.mediaHost.style.transform = `scale(${scale}) translate(${translateX}px, ${translateY}px) rotate(${rotate}deg)`;
    node.mediaHost.style.filter = blur < 1e-4 ? "" : `blur(${blur}px)`;
    node.mediaHost.style.opacity = String(opacity);
  }

  function animateFrames(node, timestamp) {
    if (!node.frameAnimation || node.loopDuration <= 0) return;
    const elapsed = timestamp % node.loopDuration;
    let cursor = 0;
    let index = node.mediaItems.length - 1;
    for (let i = 0; i < node.durations.length; i += 1) {
      cursor += node.durations[i];
      if (elapsed < cursor) {
        index = i;
        break;
      }
    }
    node.mediaItems.forEach((entry, itemIndex) => {
      entry.element.style.display = itemIndex === index ? "block" : "none";
    });
    node.frameRaf = requestAnimationFrame(next => animateFrames(node, next));
  }

  nodes.forEach(node => {
    updateNodeSize(node);
    if (node.frameAnimation) node.frameRaf = requestAnimationFrame(ts => animateFrames(node, ts));
  });

  let enterX = 0;
  let current = 0;
  let moveRaf = 0;
  let returnRaf = 0;

  shell.addEventListener("mouseenter", event => {
    cancelAnimationFrame(returnRaf);
    enterX = event.clientX;
  });

  shell.addEventListener("mousemove", event => {
    const width = Math.max(stage.clientWidth, 1);
    current = (event.clientX - enterX) / width;
    if (moveRaf) return;
    moveRaf = requestAnimationFrame(() => {
      moveRaf = 0;
      nodes.forEach(node => applyApiDisplacement(node, current));
    });
  });

  shell.addEventListener("mouseleave", () => {
    cancelAnimationFrame(moveRaf);
    cancelAnimationFrame(returnRaf);
    moveRaf = 0;
    const start = performance.now();
    const from = current;
    const returnHome = timestamp => {
      const progress = returnDuration <= 0
        ? 1
        : clamp((timestamp - start) / returnDuration, 0, 1);
      current = from * (1 - progress);
      nodes.forEach(node => applyApiDisplacement(node, current));
      if (progress < 1) returnRaf = requestAnimationFrame(returnHome);
    };
    returnRaf = requestAnimationFrame(returnHome);
  });

  const resizeObserver = typeof ResizeObserver === "function"
    ? new ResizeObserver(() => nodes.forEach(updateNodeSize))
    : null;
  if (resizeObserver) resizeObserver.observe(stage);
}

function createPalxiaoBanner(shell, manifest, manifestUrl) {
  const stage = shell.querySelector(".stage");
  stage.innerHTML = "";
  stage.classList.add("animated-banner");

  let compensate = window.innerWidth > 1650 ? window.innerWidth / 1650 : 1;
  const nodes = [];
  const number = (value, fallback = 0) => {
    const result = Number(value);
    return Number.isFinite(result) ? result : fallback;
  };
  const multiply = (left, right) => [
    left[0] * right[0] + left[2] * right[1],
    left[1] * right[0] + left[3] * right[1],
    left[0] * right[2] + left[2] * right[3],
    left[1] * right[2] + left[3] * right[3],
    left[0] * right[4] + left[2] * right[5] + left[4],
    left[1] * right[4] + left[3] * right[5] + left[5],
  ];

  for (const item of manifest.layers || []) {
    if (!item.file) continue;
    const layer = document.createElement("div");
    layer.className = "layer palxiao-layer";
    layer.style.zIndex = String(Number(item.zIndex ?? item.index) || 0);

    const motion = document.createElement("div");
    motion.className = "motion";
    const isVideo = item.tag === "video"
      || /^video\//i.test(item.contentType || "")
      || /\.(?:webm|mp4|m3u8)(?:$|\?)/i.test(item.file || "");
    const media = document.createElement(isVideo ? "video" : "img");
    media.src = resolveAsset(manifestUrl, item.file);
    media.draggable = false;
    media.style.width = `${number(item.width) * compensate}px`;
    media.style.height = `${number(item.height) * compensate}px`;
    media.style.objectFit = item.objectFit || "fill";
    media.style.objectPosition = item.objectPosition || "50% 50%";
    if (isVideo) {
      media.autoplay = true;
      media.loop = true;
      media.muted = true;
      media.playsInline = true;
    }
    motion.appendChild(media);
    layer.appendChild(motion);
    stage.appendChild(layer);

    const initial = Array.isArray(item.transform) && item.transform.length >= 6
      ? item.transform.slice(0, 6).map(value => number(value))
      : [1, 0, 0, 1, 0, 0];
    initial[4] *= compensate;
    initial[5] *= compensate;
    const opacity = Array.isArray(item.opacity) ? item.opacity : [item.opacity, item.opacity];
    const initialOpacity = number(opacity[0], 1);
    const targetOpacity = number(opacity[1], initialOpacity);
    const blur = number(item.blur, 0);
    nodes.push({
      layer,
      motion,
      media,
      initial,
      a: number(item.a),
      g: number(item.g),
      f: number(item.f),
      deg: number(item.deg),
      initialOpacity,
      targetOpacity,
      blur,
    });
  }

  let current = 0;
  let enterX = 0;
  let moveRaf = 0;
  let returnRaf = 0;
  const returnDuration = Number(manifest.interaction?.returnDurationMs) || 300;

  function apply(node, moveX) {
    const coordinate = moveX / compensate;
    const scale = 1 + node.f * coordinate;
    const angle = node.deg * coordinate;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const effect = [
      scale * cos,
      scale * sin,
      -scale * sin,
      scale * cos,
      node.a * moveX,
      node.g * moveX,
    ];
    node.motion.style.transform = matrixCss(multiply(node.initial, effect));
    node.layer.style.opacity = String(
      node.initialOpacity
      + (node.targetOpacity - node.initialOpacity)
        * clamp((coordinate / Math.max(window.innerWidth / compensate, 1)) * 2, 0, 1),
    );
    node.media.style.filter = node.blur > 0 ? `blur(${node.blur}px)` : "";
  }

  const applyAll = moveX => nodes.forEach(node => apply(node, moveX));
  applyAll(0);

  shell.addEventListener("mouseenter", event => {
    cancelAnimationFrame(returnRaf);
    enterX = event.clientX;
  });
  shell.addEventListener("mousemove", event => {
    current = event.clientX - enterX;
    if (moveRaf) return;
    moveRaf = requestAnimationFrame(() => {
      moveRaf = 0;
      applyAll(current);
    });
  });
  shell.addEventListener("mouseleave", () => {
    cancelAnimationFrame(moveRaf);
    cancelAnimationFrame(returnRaf);
    const from = current;
    const started = performance.now();
    const home = timestamp => {
      const progress = returnDuration <= 0
        ? 1
        : clamp((timestamp - started) / returnDuration, 0, 1);
      current = from * (1 - progress);
      applyAll(current);
      if (progress < 1) returnRaf = requestAnimationFrame(home);
    };
    returnRaf = requestAnimationFrame(home);
  });
  window.addEventListener("resize", () => {
    const nextCompensate = window.innerWidth > 1650 ? window.innerWidth / 1650 : 1;
    if (Math.abs(nextCompensate - compensate) < 1e-6) return;
    nodes.forEach(node => {
      const width = number(node.media.style.width.replace("px", ""));
      const height = number(node.media.style.height.replace("px", ""));
      node.media.style.width = `${width * nextCompensate / compensate}px`;
      node.media.style.height = `${height * nextCompensate / compensate}px`;
      node.initial[4] *= nextCompensate / compensate;
      node.initial[5] *= nextCompensate / compensate;
    });
    compensate = nextCompensate;
    applyAll(current);
  });
}

function showUnsupportedBanner(shell, message) {
  const stage = shell.querySelector(".stage");
  stage.classList.remove("animated-banner");
  stage.innerHTML = `<div class="loading error">${message}</div>`;
}

function createReferenceBanner(shell, manifest, manifestUrl) {
  const referenceManifest = manifest.reference?.manifest;
  if (!referenceManifest) {
    showUnsupportedBanner(shell, "参考 Banner 缺少本地 manifest");
    return;
  }
  const stage = shell.querySelector(".stage");
  stage.innerHTML = "";
  stage.classList.add("animated-banner");
  if (shell._referenceBannerMessageHandler) {
    window.removeEventListener("message", shell._referenceBannerMessageHandler);
  }
  shell._referenceBannerResizeObserver?.disconnect();
  const frame = document.createElement("iframe");
  frame.title = "参考 Banner 本地回放";
  frame.loading = "lazy";
  frame.referrerPolicy = "no-referrer";
  frame.style.display = "block";
  frame.style.width = "100%";
  frame.style.height = "100%";
  frame.style.border = "0";
  frame.style.maxWidth = "100%";
  frame.style.minWidth = "0";
  let expanded = false;
  const updateExpandedSize = () => {
    if (!expanded) return;
    const width = Math.max(shell.getBoundingClientRect().width, 1);
    const height = Math.max(155, width / (16 / 3));
    shell.style.aspectRatio = "auto";
    shell.style.height = `${height}px`;
    shell.style.maxHeight = "none";
    frame.style.width = `${width}px`;
    frame.style.height = `${height}px`;
    try {
      frame.contentWindow?.dispatchEvent(new Event("resize"));
    } catch (_error) {
      // The browser may temporarily expose no contentWindow while navigating srcdoc.
    }
  };
  const onMessage = event => {
    if (event.source !== frame.contentWindow) return;
    if (event.data?.type !== "bilibili-banner-expand") return;
    expanded = Boolean(event.data.expanded);
    if (expanded) {
      updateExpandedSize();
    } else {
      shell.style.removeProperty("aspect-ratio");
      shell.style.removeProperty("height");
      shell.style.removeProperty("max-height");
      frame.style.width = "100%";
      frame.style.height = "100%";
    }
  };
  shell._referenceBannerMessageHandler = onMessage;
  window.addEventListener("message", onMessage);
  shell._referenceBannerResizeObserver = typeof ResizeObserver === "function"
    ? new ResizeObserver(updateExpandedSize)
    : null;
  shell._referenceBannerResizeObserver?.observe(shell);
  const localManifestUrl = resolveAsset(manifestUrl, referenceManifest);
  const bundleUrl = new URL("./assets/mikufan-bilibanner.js", location.href).href;
  frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>
    html,body,#bili-banner{margin:0;width:100%;height:100%;min-width:0 !important;overflow:hidden;background:transparent}
    #bili-banner,.bili-banner,.summer-banner,.autumn-banner{box-sizing:border-box;width:100% !important;max-width:100% !important;min-width:0 !important}
  </style></head><body><div id="bili-banner"></div>
  <script src="${bundleUrl}"></script>
  <script>const originalDispatchEvent=EventTarget.prototype.dispatchEvent;EventTarget.prototype.dispatchEvent=function(event){if(event?.type==="banner-expand")window.parent.postMessage({type:"bilibili-banner-expand",expanded:Boolean(event.detail)},"*");return originalDispatchEvent.call(this,event)};BiliBanner.init(${JSON.stringify(localManifestUrl)});</script>
  </body></html>`;
  stage.appendChild(frame);
}

function createInteractiveBanner(shell, manifest, manifestUrl) {
  const model = manifest.interaction?.model || "none";
  if (model === "mikufan-reference-v1") {
    createReferenceBanner(shell, manifest, manifestUrl);
    return;
  }
  if (model === "bilibili-header-api-v1" && manifest.layers?.length) {
    createHeaderApiBanner(shell, manifest, manifestUrl);
    return;
  }
  if (model === "palxiao-reconstructed-v1" && manifest.layers?.length) {
    createPalxiaoBanner(shell, manifest, manifestUrl);
    return;
  }
  const supportedModels = new Set([
    "none",
    "bilibili-header-api-v1",
    "palxiao-reconstructed-v1",
    "bilibili-sampled-horizontal-v1",
    "bilibili-moveX-times-a",
  ]);
  if (!supportedModels.has(model)) {
    showUnsupportedBanner(shell, `不支持的 Banner 交互模型：${model}`);
    return;
  }
  if (model === "bilibili-header-api-v1" && !manifest.layers?.length) {
    showUnsupportedBanner(shell, "该 API 分层归档暂缺可回放图层（partial）");
    return;
  }

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

  if (!manifestUrl) {
    showUnsupportedBanner(shell, "当前时段未观测");
    return;
  }

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
    { supportsInteractive: supportsReferenceInteractive() },
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
  article.dataset.manifest = variant?.manifest || "";
  const head = document.createElement("div");
  head.className = "entry-head";

  const date = document.createElement("div");
  date.className = "entry-date";
  date.textContent = formatDateRange(record);
  const slots = document.createElement("div");
  slots.className = "entry-slots";
  slots.textContent = `时段：${formatSlotRanges(variant?.slots || []) || "未观测"}`;
  date.appendChild(slots);

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
    const manifest = variant?.manifest || "";
    if (!manifest || manifest === article.dataset.manifest) continue;

    article.dataset.manifest = manifest;
    article.dataset.loaded = "0";
    article.querySelector(".entry-meta").textContent = recordMeta(record, variant);
    article.querySelector(".entry-slots").textContent =
      `时段：${formatSlotRanges(variant?.slots || []) || "未观测"}`;
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
    const start = record.dateStart || record.date;
    const end = record.dateEnd || start;
    const startMonth = Number(start.slice(0, 4)) * 12 + Number(start.slice(5, 7)) - 1;
    const endMonth = Number(end.slice(0, 4)) * 12 + Number(end.slice(5, 7)) - 1;

    if (year && (Number(year) < Number(start.slice(0, 4))
      || Number(year) > Number(end.slice(0, 4)))) return false;
    if (month) {
      const monthNumber = Number(month);
      const touchesMonth = Array.from(
        { length: Math.max(0, endMonth - startMonth + 1) },
        (_value, index) => startMonth + index,
      ).some(value => (value % 12) + 1 === monthNumber
        && (!year || Math.floor(value / 12) === Number(year)));
      if (!touchesMonth) return false;
    }
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
