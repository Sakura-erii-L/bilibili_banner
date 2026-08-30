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
const lerp = (a, b, t) => a + (b - a) * t;

function matrixCss(m) {
  return `matrix(${m.map(v => Number(v).toFixed(8)).join(",")})`;
}

function formatDate(iso) {
  const [y, m, d] = iso.split("-");
  return `${y}年${m}月${d}日`;
}

function maxAbs(list) {
  const n = Math.max(...list.map(v => Math.abs(Number(v) || 0)), 0);
  return n || 1;
}

function resolveAsset(manifestUrl, file) {
  return new URL(file, new URL(manifestUrl, location.href)).href;
}

function createInteractiveBanner(shell, manifest, manifestUrl) {
  const stage = shell.querySelector(".stage");
  stage.innerHTML = "";

  if (manifest.mode !== "split" || !manifest.layers?.length) {
    const file = manifest.static?.file;
    if (file) {
      const img = document.createElement("img");
      img.className = "static-image";
      img.src = resolveAsset(manifestUrl, file);
      img.alt = "";
      stage.appendChild(img);
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
  const nodes = [];

  for (const item of manifest.layers) {
    const layer = document.createElement("div");
    layer.className = "layer";

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
    layer.style.transform = matrixCss(initial);

    if (Array.isArray(item.opacity)) {
      layer.style.opacity = String(item.opacity[0] ?? 1);
      media.style.opacity = String(item.opacity[1] ?? 1);
    }

    layer.appendChild(media);
    stage.appendChild(layer);

    nodes.push({
      layer,
      initial,
      a: Number(item.a) || 0,
    });
  }

  let initX = 0;
  let moveX = 0;
  let startTime = 0;
  let homeRaf = 0;

  function applyMove(currentMoveX, homingProgress = null) {
    const isHoming = typeof homingProgress === "number";

    for (const node of nodes) {
      const m = [...node.initial];

      const movedX = currentMoveX * node.a;
      m[4] = isHoming
        ? lerp(node.initial[4] + movedX, node.initial[4], homingProgress)
        : node.initial[4] + movedX;

      // Strictly horizontal interaction:
      // m[5] is always the original Y component.
      layerSetTransform(node.layer, m);
    }
  }

  function layerSetTransform(layer, matrix) {
    layer.style.transform = matrixCss(matrix);
  }

  function homing(timestamp) {
    if (!startTime) startTime = timestamp;

    const elapsed = timestamp - startTime;
    const progress = Math.min(elapsed / returnDuration, 1);

    applyMove(moveX, progress);

    if (progress < 1) {
      homeRaf = requestAnimationFrame(homing);
    } else {
      moveX = 0;
      applyMove(0);
    }
  }

  shell.addEventListener("mouseenter", e => {
    cancelAnimationFrame(homeRaf);
    initX = e.pageX;
  });

  shell.addEventListener("mousemove", e => {
    moveX = e.pageX - initX;
    requestAnimationFrame(() => applyMove(moveX));
  });

  shell.addEventListener("mouseleave", () => {
    cancelAnimationFrame(homeRaf);
    startTime = 0;
    homeRaf = requestAnimationFrame(homing);
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

function createEntry(record) {
  const article = document.createElement("article");
  article.className = "entry";
  article.dataset.manifest = record.manifest;
  const head = document.createElement("div");
  head.className = "entry-head";

  const date = document.createElement("div");
  date.className = "entry-date";
  date.textContent = formatDate(record.date);

  const meta = document.createElement("div");
  meta.className = "entry-meta";
  meta.textContent =
    `${seasonNames[record.season] || ""} · ` +
    (record.mode === "split" ? `${record.layerCount} 个图层` : "静态 Banner");

  const banner = document.createElement("div");
  banner.className = "banner";
  banner.innerHTML = '<div class="stage"><div class="loading">载入中……</div></div>';

  head.append(date, meta);
  article.append(head, banner);
  return article;
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
} catch (error) {
  summary.innerHTML = `<span class="error">${error.message}</span>`;
}
