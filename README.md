# Bilibili Banner Archive v11.0

一个保存并重放 Bilibili 首页 Banner 原始构成的归档项目。目标是保留图片、分层、视频、动画和交互参数，而不是制作历史截图相册。

新抓取生成 `v11.0` manifest/index；现有 `v9.2/v10.1` 归档仍可直接回放。当前 Banner 默认直接读取 Bilibili Header API 的 `is_split_layer + split_layer.layers[].resources[]`，按官方参数保存并重放原始图层；Playwright DOM 实测已降级为显式校验/遗留诊断模式。历史导入按 API-first 优先级支持 Wayback HTTP 和独立的 palxiao structured provider。前端只读取仓库内的 `data/`，不会在用户浏览器中回源 Bilibili。

## 当前实现

- 默认 `python backend/capture.py` 不启动浏览器，直接请求 Header API：`/x/web-show/page/header/v2?resource_id=142`，旧 `/header` 作为兼容端点。
- `split_layer` 是当前动态 Banner 的权威结构来源；逐层保存 `resources[]`，并完整保留 `scale/rotate/translate/blur/opacity` 的 `initial/offset/offsetCurve/wrap` 等原始字段。
- 一个 layer 中存在多份带 `duration` 的资源时按毫秒循环切帧；WebM/MP4 继续以视频保存，不截帧、不合成。
- `pic` 在分层 Banner 中只作为 `fallback_image`，不能成为主 Banner；资源缺失时标记 `partial/missing_assets`，不会压扁成单图。
- 前端 `bilibili-header-api-v1` renderer 按 BiliDynBanner/Bilibili 参数模型计算：水平位移 `(clientX-enterX)/containerWidth`，高度基准 `155`，离场约 `200ms` 线性回位，并支持 cubic-bezier `offsetCurve`。
- `python backend/capture.py --verify-dom` 才会启动隐藏浏览器，只比较 API 与当前 DOM 资源，不再进行 9 点运动采样。
- `python backend/capture.py --legacy-dom-capture` 保留旧 v10.1 的完整 DOM 采样路径，仅用于诊断 API 无法描述的特殊实现，不是默认抓取流程。
- Wayback 历史导入优先通过 CDX 找到实际 Header API snapshot；失败后依次尝试精确日期的 palxiao structured data，再使用直接 HTTP HTML/CSS 推断。默认不启动 Playwright，只有 `--verify-dom` 才校验回放页。
- API、palxiao、HTML fallback 都保存原始资源和证据；后端不生成截图，前端也不使用截图作为分层失败回退。`<video>` 优先保存真实视频源，poster 只作 preview fallback。
- 继续保留兼容的 `contentHash`（manifest 字段仍是 `type`），并额外计算 provider-independent `canonicalContentHash` 与区分来源/交互模型的 `sourceFingerprint`。
- 同日、布局结构相同但实际素材不同的抓取结果继续使用 `familyId`/时段变体机制；旧 sampled renderer 继续兼容现有历史 archive。

历史数据已重置，只保留 `2026-08-30` 的真实分层 archive 作为基线；其余日期由 GitHub Actions 从 `2019-08-01` 起重新采集。回填会持续改变记录数，应以 `data/index.json` 和 `python scripts/audit_archives.py` 的结果为准。

## 快速开始

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
```

只有使用 `--verify-dom`、`--legacy-dom-capture` 时才需要 Playwright 浏览器。机器没有可用的 Edge/Chrome 时安装 Chromium：

```powershell
python -m playwright install chromium
```

抓取并构建静态站点：

```powershell
python backend\capture.py
python scripts\build_site.py
```

必须通过 HTTP 服务预览，因为前端需要 `fetch()` 读取 JSON：

```powershell
python scripts\serve.py
```

然后打开 <http://127.0.0.1:8765>。

Windows 也可以直接运行：

```text
scripts\update_and_preview_windows.cmd
```

该脚本会检查 Python、按需安装 `requirements.txt`、执行隐藏抓取、构建 `_site/`，再启动预览服务。Linux/NAS 可使用：

```bash
bash scripts/update_linux.sh
```

## 常用命令

```text
python backend/capture.py                  # 默认：Header API 抓取当前 Banner，不启动浏览器
python backend/capture.py --verify-dom     # 可选：隐藏浏览器校验 API 与当前 DOM
python backend/capture.py --legacy-dom-capture # 仅诊断：旧完整 DOM 采样路径
python backend/capture.py --force          # 刷新匹配 archive 的元数据，不复制相同内容
python backend/capture.py --rebuild-index  # 只根据已有 archive 重建 data/index.json
python backend/wayback_import.py --from-date 2019-08-01 --to-date 2020 --cadence monthly
python backend/wayback_import.py --from-date 2019-08-01 --to-date 2026 --discovery-only
python scripts/build_site.py               # 构建到 _site/
python scripts/build_site.py --output PATH # 构建到指定目录
python scripts/serve.py --port 8765        # 服务 _site/
python scripts/serve.py --directory PATH   # 服务指定静态目录
```

`--force` 不会绕过结构化 `contentHash` 去重。资源相同但结构或交互不同属于不同版本，不会被强制合并。

## 目录结构

```text
.
├─ backend/
│  ├─ capture.py                    # API-first 抓取、归档、旧 DOM 验证/诊断
│  ├─ providers/
│  │  ├─ bilibili_header_api.py     # Header API 获取、split_layer 解析、资源规范化
│  │  ├─ history.py                 # Unified HistoricalResult
│  │  └─ palxiao_history.py         # palxiao data.json 精确日期 provider
│  ├─ history_import.py             # 历史导入兼容入口
│  └─ wayback_import.py             # Wayback API/HTTP + provider fallback
├─ frontend/
│  ├─ index.html                    # 页面骨架
│  ├─ app.js                        # 数据加载、筛选、分层重建和交互
│  ├─ interaction.js                # 曲线插值和时段变体选择
│  └─ style.css                     # 页面布局与响应式样式
├─ data/
│  ├─ current/                      # 最近一次被接受的唯一内容工作副本
│  ├─ archive/                      # 唯一历史 Banner 的完整归档
│  ├─ index.json                    # 前端历史入口
│  └─ diagnostic.json               # 失败诊断；默认被忽略
├─ scripts/
│  ├─ build_site.py                 # frontend + data → _site
│  ├─ serve.py                      # 本地静态 HTTP 服务
│  ├─ update_and_preview_windows.cmd
│  └─ update_linux.sh
├─ .github/workflows/
│  ├─ daily-update.yml              # 定时抓取、提交数据、部署 Pages
│  ├─ wayback-import.yml            # 手动分批导入历史快照
│  └─ pages.yml                     # 代码或数据变更后的 Pages 部署
├─ docs/                            # 项目、架构、部署和排障文档
├─ tests/                           # 交互采样、时段聚合和前端曲线测试
├─ requirements.txt
└─ _site/                           # 构建产物，默认被 .gitignore 忽略
```

`data/current/` 是最近一次被接受的唯一内容工作副本；重复素材会完全跳过，不会仅因抓取时间变化而改写它。前端历史列表使用 `data/index.json` 中指向 `data/archive/.../banner.json` 的路径。 `_site/` 是发布前的静态目录，不能替代源数据目录。

## 数据和交互模型

v11 分层 archive 的主要结构为：

```text
banner.json
api_layer_00_00_*.webp / .webm / ...
api_layer_00_01_*...                # 同层多帧资源不会被合并
static.*                            # 分层时仅 fallback
litpic.*                            # 若 API 提供
source/api.json                     # Header API 原始响应
```

每层在 `banner.json` 中同时保留 `resources[]` 和原始 `apiLayer`，renderer 直接使用 `apiConfig`：

```text
displace = (clientX - enterX) / containerWidth
containerScale = containerHeight / 155
value = initial + offset * curve(displace)
translate = value * containerScale * scale.initial
```

`scale/rotate/translate/blur/opacity` 可各自带 `offsetCurve`；`opacity/blur` 的 `wrap` 按 API 语义处理。多帧 layer 仅在所有资源都有正 `duration` 时循环切换。旧 v9/v10 `motion.matrixDelta` / `returnRemaining` 仍由 legacy renderer 回放，不需要迁移旧数据。

## 配置环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `BANNER_DATA_DIR` | 项目根目录 `data/` | 数据、归档和索引的根目录 |
| `BANNER_SOURCE_URL` | `https://www.bilibili.com/` | Referer、DOM 校验/legacy 页面 |
| `BANNER_HEADER_API_URL` | 自动使用 v2/旧 Header API | 手工覆盖当前 Header API 端点 |
| `BANNER_TIMEZONE` | `Asia/Shanghai` | 时间戳和季节计算时区 |
| `BANNER_TIME_SLOT_MINUTES` | `180` | 时段观测槽宽度；默认每 3 小时一槽 |
| `BANNER_VIEWPORT_WIDTH` | `1650` | 仅 DOM 校验/legacy/fallback 的浏览器视口宽度 |
| `BANNER_VIEWPORT_HEIGHT` | `800` | 仅 DOM 校验/legacy/fallback 的浏览器视口高度 |
| `BROWSER_EXE` | 自动探测 | 仅 DOM 模式指定 Edge/Chrome/Chromium |
| `BANNER_PROFILE_DIR` | `.runtime/browser-profile` | 仅 legacy DOM 模式的本地 profile |
| `BANNER_PROFILE_MODE=temporary` | 未设置 | DOM 模式使用一次性 profile；CI 也会自动启用 |

环境变量在启动时读取；修改视口或数据目录后需要重新运行抓取/构建。
注意：`BANNER_DATA_DIR` 由 `backend/capture.py` 使用；当前 `scripts/build_site.py` 固定从项目根目录的 `data/` 复制数据，使用外部数据目录时需要先同步数据或调整构建脚本。

## 自动化部署

`.github/workflows/daily-update.yml` 默认在北京时间每 3 小时的第 `17` 分钟运行（每天 8 次），也支持 `workflow_dispatch` 手工触发。当前日常任务只安装 Python 依赖并运行 Header API 抓取，不再安装 Chromium；只有 `data/` 发生实际变化时才提交，然后构建并部署 GitHub Pages。

`.github/workflows/pages.yml` 用于手工触发、普通 `main` push，或在 `Import Historical Banners` 成功完成后通过 `workflow_run` 构建并部署 Pages。后者用于解决默认 `GITHUB_TOKEN` 产生的自动提交不会再次触发 `push` workflow 的限制。

`.github/workflows/wayback-import.yml` 通过 `workflow_dispatch` 直接运行 `backend/history_import.py`，支持 `auto`、`wayback-api`、`palxiao`、`wayback-html` 和指定日期范围。后端会拒绝更早的范围和时间戳。每产生一个真正创建或更新的归档，就由 `scripts/checkpoint_wayback.py` 立即提交并推送一次 `data/`、触发一次 Pages。失败、完全重复及没有任何可播放主素材的快照不计入提交。它与 Daily Update 共用 `bilibili-banner-data-writes` 且 `cancel-in-progress: false`，数据写入任务会排队执行。

## Wayback 历史导入

先只查询快照、不写入数据：

```powershell
python backend\wayback_import.py --from-date 2019-08-01 --to-date 2026 --cadence monthly --discovery-only
```

本地也可按年份运行同一个批处理脚本，降低网络失败和超时的影响：

```powershell
python backend\wayback_import.py --from-date 2019-08-01 --to-date 2020 --cadence monthly
```

`monthly` 只取得每月附近的代表快照；需要更密集的历史可改为 `weekly` 或 `daily`。每个快照先通过 CDX 查找实际 Header API timestamp，再尝试精确日期的 palxiao `assets/YYYY-MM-DD/data.json`，最后直接 HTTP 获取 Wayback raw replay 和 HTML/CSS。Playwright 仅在显式 `--verify-dom` 时校验，不参与默认恢复；任何情况下都不会用截图伪造。palxiao 日期只表示其记录/抓取该 Banner 的 `observedAt`，不推断 `effectiveFrom`。详细操作见 [Wayback 历史导入](docs/Wayback历史导入.md)。

GitHub Runner 的出口 IP、地理位置、Cookie 和 A/B 分流可能导致它看到的 Banner 与个人电脑不同。如果必须以自己的网络环境为准，建议在 Windows/NAS 抓取后提交 `data/`，让 GitHub Pages 只负责展示。

## 明确限制

- 当前每日默认抓取依赖 Bilibili Header API；接口失效时默认任务会失败，而不是悄悄改变当前主流程。历史导入另有 Wayback/palxiao/HTTP fallback；可用 `--legacy-dom-capture` 人工诊断。
- Header API 中的 `extensions` 会完整保存在原始 JSON/manifest；当前 renderer 尚未复现 snow/petals 等扩展，因此存在扩展时会标记 `partial`，不会伪装为完整。
- 分层资源缺失时保存至少一个已恢复图层并标记 `partial/missing_assets`；完全没有可播放主素材时直接失败，不创建空 archive。
- v11 renderer 支持 Header API 的 scale/rotate/translate/blur/opacity、offsetCurve、多帧资源和视频；API 外的 WebGL、特殊脚本/扩展仍可能只能留下证据并标记 partial。旧 DOM sampled archive 继续按原 matrix 曲线回放。
- `contentHash` 继续由 `resourceHash + structureHash + interactionHash` 组合并保持旧归档兼容；新增 `canonicalContentHash` 只比较规范化资源 bytes/结构，`sourceFingerprint` 保留 provider 与 interaction model 差异。
- 自动 `familyId` 依据“同一日期 + 相同布局结构”归并时段变体；若网站在同一天把结构完全相同的 Banner 更换为无关主题，可能需要人工拆分 family。
- 前端是纯静态页面，没有后台 API、用户登录、数据库或在线回源逻辑。
- 历史素材会持续增加 Git 仓库和 Pages 体积；程序不会自动删除旧归档。
- Wayback 并不保证每个快照都保存完整子资源或可执行脚本，因此 `2019-08-01` 至今只能导入“归档中仍可恢复”的 Banner，不代表逐日无缺口。
- 当前历史发现以 Wayback Availability/CDX 为主，并支持 palxiao/bilibili-banner 的精确日期 structured provider；palxiao 的字段不是官方 `split_layer`，缺失参数不补造，未知字段保存在原始 source layer。Common Crawl、archive.today、Memento 和其它 GitHub 项目尚未实现统一自动导入 provider。
- `python scripts/audit_archives.py` 可检查 flatten、缺失文件和结构化哈希。重采结果只有通过这些硬检查后才应保留；`partial` 仍表示至少有一个真实主素材，但部分图层或交互证据不可恢复。

## 文档入口

- [详细项目说明](docs/项目说明.md)
- [架构说明](docs/架构说明.md)
- [数据格式](docs/数据格式.md)
- [GitHub Pages 部署](docs/GITHUB_PAGES部署.md)
- [NAS 部署](docs/NAS部署.md)
- [故障排查](docs/故障排查.md)
- [Wayback 历史导入](docs/Wayback历史导入.md)
- [变更记录](docs/CHANGELOG.md)
- [API-first v11 架构](docs/API_FIRST_ARCHITECTURE.md)
- [Codex 交接说明](CODEX_HANDOFF.md)
