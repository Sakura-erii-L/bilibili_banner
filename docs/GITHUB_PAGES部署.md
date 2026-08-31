# GitHub Pages 部署

## v11 自动化变化（2026-08-31）

`Daily Banner Update` 默认只走 Header API，因此日常 workflow 不再安装 Playwright Chromium，抓取后仍按原逻辑提交 `data/` 并部署 Pages。`Import Historical Banners` 仍保留 Chromium，因为 Wayback API 缺失时需要 DOM fallback。Pages 前端只读取仓库内本地资源，不回源 Bilibili CDN/API。

本项目发布的是 `scripts/build_site.py` 生成的纯静态目录 `_site/`。GitHub Pages 只负责托管前端和仓库内的归档数据，不运行 Python 后端，也不让用户浏览器回源 Bilibili。

## 1. 仓库准备

将项目内容放在 GitHub 仓库根目录，至少保留：

```text
.github/workflows/
backend/
frontend/
scripts/
data/
requirements.txt
```

首次部署建议确认仓库中已经有 `data/index.json` 和至少一个可用归档；空索引也可以部署，但页面不会有 Banner。

### 1.1 小白上传步骤

1. 打开 <https://github.com/new>，填写仓库名，例如 `bilibili-banner-archive`。
2. 建议选择 `Public`，不要勾选初始化 README、`.gitignore` 或 License；本项目已经有这些文件。
3. 解压本项目 ZIP，进入 `D:\Projects\LittleTry\bilibili_banner`，上传的是这个目录里面的内容，不是外面再套一层 `bilibili_banner` 的文件夹。
4. 上传并保留 `.github/`、`backend/`、`frontend/`、`data/`、`scripts/`、`docs/`、`README.md`、`requirements.txt` 和 `.gitignore`。
5. 不要上传 `.runtime/`、`_site/`、`.venv/`、`venv/`、`__pycache__/`、`*.pyc`；如果本地有 `data/diagnostic.json`，也不要上传。它们已被 `.gitignore` 排除。
6. 提交后，仓库首页第一层应直接看到 `.github`、`backend`、`frontend`、`data`、`scripts`；点击 `backend` 后应直接看到 `capture.py`。如果首页先看到一个 `bilibili_banner` 文件夹，说明多套了一层目录，需要重新上传里面的内容。

浏览器上传大量二进制素材失败时，可使用 GitHub Desktop；仍然只把本目录内容放在仓库根目录，不能把 `_site/` 当作源仓库上传。

## 2. 启用 Pages

进入：

```text
Repository → Settings → Pages
→ Build and deployment → Source → GitHub Actions
```

不要选择 `/docs` 目录，因为工作流会上传 `_site/` artifact。

首次运行前再进入：

```text
Repository → Settings → Actions → General
→ Workflow permissions → Read and write permissions → Save
```

这里需要 `Read and write permissions`，因为 `daily-update.yml` 只有在发现新数据时才会提交 `data/`。不需要打开“Allow GitHub Actions to create and approve pull requests”。

## 3. 每日自动更新工作流

`.github/workflows/daily-update.yml` 当前流程：

```text
北京时间每 3 小时的第 17 分钟（每天 8 次），或手工 workflow_dispatch
        ↓
Checkout
        ↓
Python 3.12 + pip cache
        ↓
安装 requirements.txt
        ↓
安装 Playwright Chromium 及系统依赖
        ↓
CI=true、BANNER_PROFILE_MODE=temporary 的 headless capture.py
        ↓
比较 data/ 是否变化
        ↓
有变化才 commit/push data/
        ↓
build_site.py
        ↓
上传 Pages artifact 并 deploy-pages
```

更新 job 使用 `contents: write` 和 `pages: write`；部署 job 使用 `pages: write` 和 `id-token: write`。首次运行前确认仓库 Actions 权限允许工作流写入内容。顶层权限按最小权限配置，不给整个工作流额外的 `id-token`。

默认 schedule：

```yaml
schedule:
  - cron: "17 6 * * *"
    timezone: "Asia/Shanghai"
```

可在：

```text
Actions → Daily Banner Update → Run workflow
```

手工触发。GitHub Runner 使用临时 profile，不会保留本地 Cookie 或浏览器状态。

## 4. 只部署静态页面的工作流

`.github/workflows/pages.yml` 在以下情况运行：

- 手工 `workflow_dispatch`
- `main` 分支的 `frontend/**` 变化
- `data/**` 变化
- `scripts/build_site.py` 变化
- `.github/workflows/pages.yml` 变化

它只安装 Python 3.12 并构建 Pages，不执行 Bilibili 抓取。适合前端样式、数据修复或构建脚本变更后的重新发布。

## 5. 首次验证

按以下顺序操作：

1. 进入 `Actions`，左侧点击 `Daily Banner Update`，点击右侧 `Run workflow`，选择 `main`，再点击绿色的 `Run workflow`。
2. 打开这次运行，先观察 `update-and-build` job：应依次看到 `Checkout repository`、`Set up Python`、`Install Python dependencies`、`Install Playwright Chromium`、`Capture current Bilibili banner`、`Detect data changes`、`Build static Pages site`、`Configure GitHub Pages` 和 `Upload Pages artifact`。
3. 再观察 `deploy` job：它应在 `update-and-build` 成功后运行 `Deploy GitHub Pages`。
4. 成功表现是两个 job 都有绿色对勾。若 Banner 是新素材，仓库会多出 `data/archive/<id>/`，并更新 `data/current/` 与 `data/index.json`；若是重复素材，日志会显示 `No visual change` 或 `Visual already exists in history`，`data/` 不变，也不会产生数据 commit。
5. 失败时点击红色 job，展开失败的 step，复制最底部的错误信息和 `data/diagnostic.json` 相关信息；不要只看 Actions 列表上的标题。
6. 在 `Settings → Pages` 的部署记录或成功的 `Deploy GitHub Pages` job 中点击 `page_url`；也可打开仓库的 `Settings → Pages → Visit site`。
7. 公网首页打开后，按 `Ctrl+F5` 强制刷新。开发者工具的 Network 中应只看到当前 Pages 域名下的 `data/index.json`、manifest 和本地图片/视频，不应看到 `bilibili.com` 请求。
8. 点击年份、月份、季节筛选，确认历史条目可加载；将鼠标在 Banner 内左右移动，确认使用该归档自己的采样效果和回位节奏，且没有动态上下漂移。
9. 第二天若首页 Banner 实际素材相同，检查 Actions 日志的 no-op 文本，并确认没有新增 archive、`data/index.json` 没有新增 record、仓库没有新的数据 commit。

Pages URL 通常为：

```text
https://<用户名>.github.io/<仓库名>/
```

前端 manifest 使用相对路径，因此项目放在仓库子路径下也能按 Pages 的 base URL 解析资源。

## 6. GitHub 与本地结果不同

工作流中的浏览器虽然使用固定的 `1650 × 800` 视口和 `Asia/Shanghai` 时区，但以下条件仍可能不同：

- Runner 出口 IP、地理位置
- Bilibili Cookie 和 A/B 分流
- CDN 网络路径
- 首页动态内容初始化时机

所以 GitHub Actions 记录的是 Runner 实际看到的页面。如果必须归档个人电脑或 NAS 网络看到的 Banner，使用：

```text
Windows/NAS 本地 headless 抓取
        ↓
提交 data/
        ↓
GitHub Pages 只构建和展示
```

## 7. 构建物和截图规则

`build_site.py` 会复制 `frontend/` 与 `data/`，但忽略旧归档中的 `preview.png`、失败诊断 `diagnostic.json`、抓取临时目录 `.capture_*` 和替换备份 `.current_old`。当前版本：

- `backend/capture.py` 不生成截图。
- `data/index.json` 不写 `preview` 字段。
- 前端分层失败时只显示错误，不显示截图。
- `static.*` 代表 Bilibili 原始静态资源，不是浏览器截图。

每日工作流使用仓库的 `GITHUB_TOKEN` 推送数据。GitHub 官方说明，使用该 token 推送的提交不会再次触发新的 workflow，因此不会因自动提交而递归触发 `pages.yml`；人工 push 或手工运行仍可能与每日任务并行，两个 workflow 已分别设置并发组。

## 8. 仓库容量

每个唯一 Banner 可能包含多个高分辨率图片或视频，历史会持续增加 Git 和 Pages artifact 体积。程序不会自动删除旧目录。

建议：

- 定期查看 `data/` 体积。
- 迁移 NAS 时保留完整 `data/`。
- 不要只备份 `_site/`，因为它是可重建产物。
- 在没有另行设计存储策略前，不要随意删除仍被 `data/index.json` 引用的目录。

## 9. Actions 失败排查

先进入：

```text
Actions → Daily Banner Update → 失败运行
```

重点看：

- `Install Playwright Chromium`
- `Capture current Bilibili banner`
- `Detect data changes`
- `Build static Pages site`

若抓取失败，工作流不会在正常路径中删除既有 archive；下载失败、DOM 变化和零运动量的详细排查见 [故障排查](故障排查.md)。
