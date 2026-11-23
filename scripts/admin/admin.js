const selectedKeys = new Set();
const gallery = document.getElementById("gallery");
const recordsPromise = fetch(`./data/index.json?t=${Date.now()}`, { cache: "no-store" })
  .then(response => {
    if (!response.ok) throw new Error(`读取 index.json 失败：HTTP ${response.status}`);
    return response.json();
  });

function dateLabel(record) {
  const start = record.dateStart || record.date || "未知日期";
  const end = record.dateEnd || start;
  return start === end ? start : `${start} ～ ${end}`;
}

function createPanel() {
  const panel = document.createElement("section");
  panel.className = "local-admin-panel";
  panel.innerHTML = `
    <div class="local-admin-heading">
      <div>
        <strong>本地管理模式</strong>
        <span>只在此本地页面提供管理功能</span>
      </div>
      <span class="local-admin-status" role="status" aria-live="polite">正在读取归档……</span>
    </div>
    <div class="local-admin-actions">
      <label class="local-admin-primary">
        <span>合并主体</span>
        <select aria-label="选择合并主体" disabled>
          <option value="">先选择至少两个 Banner</option>
        </select>
      </label>
      <button type="button" data-action="merge" disabled>合并所选 Banner（0）</button>
      <button type="button" data-action="cleanup" disabled>清理非主体原始档案（0）</button>
      <button type="button" data-action="publish" disabled>同步到 Pages</button>
    </div>
    <p class="local-admin-help">勾选多个 Banner 后合并。所选主体的素材用于合并结果，日期范围合并；主体档案保留。清理按钮只删除当前结果未使用的非主体归档，手动触发，不设时间限制。</p>
  `;
  document.querySelector("main").prepend(panel);
  return panel;
}

function requestJson(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    cache: "no-store",
  }).then(async response => {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  });
}

async function start() {
  if (!gallery) return;
  const panel = createPanel();
  const status = panel.querySelector(".local-admin-status");
  const primarySelect = panel.querySelector("select");
  const mergeButton = panel.querySelector('[data-action="merge"]');
  const cleanupButton = panel.querySelector('[data-action="cleanup"]');
  const publishButton = panel.querySelector('[data-action="publish"]');
  const [index, stateResponse] = await Promise.all([
    recordsPromise,
    fetch("/__admin/api/state", { cache: "no-store" }),
  ]);
  if (!stateResponse.ok) throw new Error(`读取管理状态失败：HTTP ${stateResponse.status}`);
  const state = await stateResponse.json();
  const records = index.records || [];
  if (gallery.dataset.ready !== "1") {
    await Promise.race([
      new Promise(resolve => window.addEventListener("bilibili-banner-index-loaded", resolve, { once: true })),
      new Promise(resolve => window.setTimeout(resolve, 5000)),
    ]);
  }
  const timeControlsToggle = document.getElementById("show-time-controls");
  if (timeControlsToggle && !timeControlsToggle.checked) {
    timeControlsToggle.checked = true;
    timeControlsToggle.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const recordByKey = new Map(
    records.map((record, indexNumber) => [state.recordKeys[indexNumber], record]),
  );

  function selectedRecords() {
    return [...selectedKeys]
      .map(key => recordByKey.get(key))
      .filter(Boolean);
  }

  function updateActions() {
    const selected = selectedRecords();
    const previous = primarySelect.value;
    const preferredKey = selectedKeys.has(previous)
      ? previous
      : (selectedKeys.values().next().value || "");
    primarySelect.replaceChildren();
    for (const key of selectedKeys) {
      const record = recordByKey.get(key);
      if (!record) continue;
      const option = document.createElement("option");
      option.value = key;
      option.textContent = `${dateLabel(record)} · ${record.season || "未知季节"}`;
      primarySelect.appendChild(option);
      if (key === preferredKey) primarySelect.value = key;
    }
    primarySelect.disabled = selected.length < 2;
    if (!primarySelect.options.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "先选择至少两个 Banner";
      primarySelect.appendChild(option);
    }
    mergeButton.disabled = selected.length < 2;
    mergeButton.textContent = `合并所选 Banner（${selected.length}）`;
  }

  function decorateEntries() {
    for (const entry of gallery.querySelectorAll(".entry")) {
      if (entry.dataset.adminBound === "1") continue;
      const recordIndex = Number(entry.dataset.adminRecordIndex);
      const key = state.recordKeys[recordIndex];
      if (!key) continue;
      entry.dataset.adminBound = "1";
      const label = document.createElement("label");
      label.className = "local-admin-entry-select";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedKeys.has(key);
      checkbox.setAttribute("aria-label", `选择 ${dateLabel(records[recordIndex])} Banner`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedKeys.add(key);
        else selectedKeys.delete(key);
        updateActions();
      });
      const text = document.createElement("span");
      text.textContent = "合并选择";
      label.append(checkbox, text);
      entry.querySelector(".entry-head")?.prepend(label);
    }
  }

  mergeButton.addEventListener("click", async () => {
    const selected = selectedRecords();
    const primaryKey = primarySelect.value;
    const primary = recordByKey.get(primaryKey);
    if (selected.length < 2 || !primary) return;
    const dates = selected.map(dateLabel).join("、");
    if (!window.confirm(`将合并 ${selected.length} 个 Banner：${dates}\n\n主体：${dateLabel(primary)}。合并结果使用主体素材，合并所选日期范围。原始档案暂时保留。`)) return;
    mergeButton.disabled = true;
    status.textContent = "正在保存合并规则……";
    try {
      await requestJson("/__admin/api/merge", {
        selectedKeys: [...selectedKeys],
        primaryKey,
      });
      status.textContent = "合并已保存，正在刷新本地预览。";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      mergeButton.disabled = false;
    }
  });

  cleanupButton.disabled = false;
  cleanupButton.textContent = `清理非主体原始档案（${state.cleanupManifests.length}）`;
  cleanupButton.addEventListener("click", async () => {
    const count = state.cleanupManifests.length;
    if (!count) {
      status.textContent = "目前没有可清理的非主体原始档案。";
      return;
    }
    if (!window.confirm(`将从当前项目和网站文件中清理 ${count} 个未被合并结果使用的非主体原始归档。主体素材会保留；此操作会推送后才影响 Pages。Git 提交历史仍保留旧文件。继续吗？`)) return;
    cleanupButton.disabled = true;
    status.textContent = "正在手动清理归档……";
    try {
      const result = await requestJson("/__admin/api/cleanup");
      status.textContent = result.message;
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      cleanupButton.disabled = false;
    }
  });

  const publishUnavailable = state.publisherError || state.branch !== "main";
  if (publishUnavailable) {
    publishButton.disabled = true;
    status.textContent = state.publisherError
      ? `暂不能同步：${state.publisherError}`
      : `当前分支为 ${state.branch || "未知"}；需切换到 main 才能发布。`;
  } else {
    publishButton.disabled = false;
    status.textContent = `当前分支 main · ${state.mergeCount} 条本地合并规则`;
  }
  publishButton.addEventListener("click", async () => {
    if (!window.confirm("将只提交 Banner 管理产生的数据改动，并推送 main 触发 GitHub Pages 更新。继续吗？")) return;
    publishButton.disabled = true;
    status.textContent = "正在提交并推送到 GitHub……";
    try {
      const result = await requestJson("/__admin/api/publish");
      status.textContent = result.message;
      publishButton.disabled = false;
    } catch (error) {
      status.textContent = error.message;
      publishButton.disabled = false;
    }
  });

  decorateEntries();
  updateActions();
  new MutationObserver(decorateEntries).observe(gallery, { childList: true });
}

start().catch(error => {
  const panel = document.querySelector(".local-admin-panel") || createPanel();
  panel.querySelector(".local-admin-status").textContent = error.message;
});
