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
      <button type="button" data-action="sync">更新本地镜像</button>
      <label class="local-admin-primary">
        <span>合并主体</span>
        <select aria-label="选择合并主体" disabled>
          <option value="">先选择至少两个 Banner</option>
        </select>
      </label>
      <button type="button" data-action="merge" disabled>合并所选 Banner（0）</button>
      <button type="button" data-action="undo" disabled>撤销上一步</button>
      <button type="button" data-action="cleanup" disabled>清理非主体原始档案（0）</button>
      <button type="button" data-action="publish" disabled>同步到 Pages</button>
    </div>
    <p class="local-admin-help">先更新本地镜像，再勾选多个 Banner 并指定主体。合并结果使用主体素材并合并日期；可撤销最近一步合并或清理。管理和预览只改本地文件，确认后点击“同步到 Pages”才会提交并推送。</p>
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
  const syncButton = panel.querySelector('[data-action="sync"]');
  const primarySelect = panel.querySelector("select");
  const mergeButton = panel.querySelector('[data-action="merge"]');
  const undoButton = panel.querySelector('[data-action="undo"]');
  const cleanupButton = panel.querySelector('[data-action="cleanup"]');
  const publishButton = panel.querySelector('[data-action="publish"]');
  const [index, stateResponse] = await Promise.all([
    recordsPromise,
    fetch("/__admin/api/state", { cache: "no-store" }),
  ]);
  if (!stateResponse.ok) throw new Error(`读取管理状态失败：HTTP ${stateResponse.status}`);
  const state = await stateResponse.json();
  const records = index.records || [];
  const mirrorDate = state.mirrorGeneratedAt
    ? new Date(state.mirrorGeneratedAt).toLocaleString()
    : "未知";
  const mirrorSummary = `本地镜像 ${state.recordCount} 条 · ${mirrorDate}`;
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

  const undoActionLabel = state.undoAction === "cleanup" ? "清理" : "合并";
  undoButton.disabled = !state.undoAvailable;
  undoButton.textContent = state.undoAvailable ? `撤销上一步（${undoActionLabel}）` : "撤销上一步";
  undoButton.title = state.undoHasSnapshot
    ? `恢复最近一次${undoActionLabel}前的本地状态`
    : "现有合并没有操作前快照；将移除规则列表末尾的一条合并";
  undoButton.addEventListener("click", async () => {
    if (!state.undoAvailable) return;
    const details = state.undoHasSnapshot
      ? `将撤销最近一次${undoActionLabel}，并保留此操作之前的合并规则。`
      : "现有合并没有操作前快照，将移除规则列表末尾的一条合并；其他合并规则会保留。";
    if (!window.confirm(`${details}\n\n如需要，原始档案会从本地 Git 历史恢复。此操作只改本地，不会推送。继续吗？`)) return;
    undoButton.disabled = true;
    status.textContent = `正在撤销最近一次${undoActionLabel}……`;
    try {
      const result = await requestJson("/__admin/api/undo");
      status.textContent = result.message;
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      undoButton.disabled = !state.undoAvailable;
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

  syncButton.disabled = Boolean(state.mirrorSyncError) || state.branch !== "main";
  syncButton.title = state.mirrorSyncError || "从 origin/main 抓取 Pages 源数据，并保留本地合并与清理规则；不会推送。";
  syncButton.addEventListener("click", async () => {
    if (!window.confirm("从 Pages 对应的 origin/main 抓取最新归档并更新本地镜像。本地合并规则和清理记录会保留；此操作只更新本地，不会推送。继续吗？")) return;
    syncButton.disabled = true;
    status.textContent = "正在抓取 Pages 数据并更新本地镜像……";
    try {
      const result = await requestJson("/__admin/api/sync");
      status.textContent = result.message;
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      syncButton.disabled = Boolean(state.mirrorSyncError) || state.branch !== "main";
    }
  });

  const hasPublishableManagement = state.managedWorktreeChanges || state.pendingManagedCommits > 0;
  const publishUnavailable = state.publisherError || state.branch !== "main" || !hasPublishableManagement;
  if (publishUnavailable) {
    publishButton.disabled = true;
    status.textContent = state.publisherError
      ? `暂不能同步：${state.publisherError}`
      : state.branch !== "main"
        ? `当前分支为 ${state.branch || "未知"}；需切换到 main 才能发布。`
        : `${mirrorSummary} · 先完成本地管理；发布时会一并推送 ${state.aheadCommits} 个尚未推送的本地提交。`;
  } else {
    publishButton.disabled = false;
    status.textContent = `${mirrorSummary} · ${state.mergeCount} 条本地合并规则 · 将一并推送 ${state.aheadCommits} 个尚未推送的本地提交`;
  }
  publishButton.addEventListener("click", async () => {
    const pending = state.aheadSubjects || [];
    const pendingSummary = pending.length
      ? `\n\n本次还会推送 ${pending.length} 个尚未推送的本地提交：\n${pending.join("\n")}`
      : "";
    if (!window.confirm(`将提交 Banner 管理产生的数据并推送 main，触发 GitHub Pages 更新。${pendingSummary}\n\n确认后才会推送。继续吗？`)) return;
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
