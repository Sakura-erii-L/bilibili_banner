# Bilibili Banner Archive v10.1

一个将 Bilibili 首页顶部 Banner 抓取为本地静态归档，并在浏览器中重建分层水平视差交互的项目。

新抓取生成 `v10.1` manifest/index；现有 `v9.2` 归档仍可直接回放。抓取器读取 Bilibili 首页实际渲染的 DOM，不依赖旧版 Header API；前端只读取仓库内的 `data/`，不会在用户浏览器中访问 Bilibili。

## 当前实现

- 使用 Playwright 启动始终 `headless=True` 的浏览器，Windows、NAS、GitHub Actions 都不会弹出可见的 Bilibili 页面。
- 从 `.animated-banner .layer` 读取真实分层；运动对象是每个 `.layer` 的 `firstElementChild`。
- 下载原始图层素材（图片或视频），保存初始 CSS matrix、尺寸和透明度。
- 在水平输入范围内进行 9 点真实 DOM 采样，保存每层 matrix/透明度响应曲线，并采样鼠标离场后的真实回位曲线。
- 前端按每个 Banner 自己的曲线插值回放；Y 始终使用初始值，旧 v9.2 manifest 自动回退到 `moveX * a`。
- 按下载素材内容计算 `contentHash`；同一套实际素材永远只保留一个 archive。发现新的 3 小时时段槽时只补充观测元数据，重复时段完全 no-op。
- 同日、布局结构相同但实际素材不同的抓取结果使用同一 `familyId`，前端按 `Asia/Shanghai` 当前时刻选择最近观测的真实时段变体。
- 可通过独立的 `wayback_import.py` 从 Wayback 快照导入 2018 年至今仍可恢复的真实图片、视频和分层 DOM；不会下载回放页截图，也会阻止直接回源 Bilibili。
- 同一素材跨多个历史日期出现时只保留一个物理 archive，并通过 `observations` 在多个日期记录中复用。
- 分层不可用但仍能获得 Bilibili 原始静态图时，使用 `static` 模式。
- 后端不生成截图，前端也不使用 `preview.png` 作为分层失败回退；分层载入失败会明确显示错误。
- 历史记录按抓取时间倒序排列，支持年份、月份、春夏秋冬筛选，并使用 `IntersectionObserver` 延迟加载较远记录。

仓库当前样例（2026-08-30）包含 1 个唯一 `split` Banner，28 个图层；实际历史数量以 `data/index.json` 为准。

## 快速开始

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
```

如果机器没有可用的 Edge/Chrome，另外安装 Playwright Chromium：

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
python backend/capture.py                  # 抓取当前 Banner
python backend/capture.py --force          # 重复素材时刷新交互元数据，不复制素材目录
python backend/capture.py --rebuild-index  # 只根据已有 archive 重建 data/index.json
python backend/wayback_import.py --from-date 2018 --to-date 2018 --cadence monthly
python backend/wayback_import.py --from-date 2018 --to-date 2026 --discovery-only
python scripts/build_site.py               # 构建到 _site/
python scripts/build_site.py --output PATH # 构建到指定目录
python scripts/serve.py --port 8765        # 服务 _site/
python scripts/serve.py --directory PATH   # 服务指定静态目录
```

`--force` 不会绕过 `contentHash` 去重；它只允许用本次测量结果刷新已有 archive 的交互元数据。

## 目录结构

```text
.
├─ backend/
│  ├─ capture.py                    # Headless 抓取、测量、归档、索引
│  └─ wayback_import.py             # Wayback 历史快照发现和真实素材导入
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

每个归档目录包含：

```text
banner.json
layer_00_*.webp / .png / .webm / ...
layer_01_...
static.avif / static.*
```

v10.1 分层归档保存每个图层的初始 matrix，以及与 `interaction.inputSamplesPx` 对齐的 `motion.matrixDelta`、透明度和回位数组。前端在相邻采样点之间线性插值；输入只来自鼠标水平差值，matrix 的 Y 分量始终保持初始值。

```text
moveX = 鼠标当前 clientX - 鼠标进入 Banner 时的 clientX
effect = interpolate(inputSamplesPx, layer.motion, moveX)
matrixY = initialMatrixY
```

鼠标离开后按该 Banner 抓取时保存的 `returnSamplesMs + returnRemaining` 回位，不再写死 `300ms`。原始素材自身的视频动画仍可能播放，这与鼠标交互造成的移动是两件事。

## 配置环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `BANNER_DATA_DIR` | 项目根目录 `data/` | 数据、归档和索引的根目录 |
| `BANNER_SOURCE_URL` | `https://www.bilibili.com/` | 抓取页面 |
| `BANNER_TIMEZONE` | `Asia/Shanghai` | 时间戳和季节计算时区 |
| `BANNER_TIME_SLOT_MINUTES` | `180` | 时段观测槽宽度；默认每 3 小时一槽 |
| `BANNER_VIEWPORT_WIDTH` | `1650` | Headless 浏览器视口宽度 |
| `BANNER_VIEWPORT_HEIGHT` | `800` | Headless 浏览器视口高度 |
| `BROWSER_EXE` | 自动探测 | 指定现有 Edge/Chrome/Chromium 可执行文件 |
| `BANNER_PROFILE_DIR` | `.runtime/browser-profile` | 本地持久化浏览器 profile |
| `BANNER_PROFILE_MODE=temporary` | 未设置 | 使用一次性 profile；设置 `CI` 时也会自动使用一次性 profile |

环境变量在启动时读取；修改视口或数据目录后需要重新运行抓取/构建。
注意：`BANNER_DATA_DIR` 由 `backend/capture.py` 使用；当前 `scripts/build_site.py` 固定从项目根目录的 `data/` 复制数据，使用外部数据目录时需要先同步数据或调整构建脚本。

## 自动化部署

`.github/workflows/daily-update.yml` 默认在北京时间每 3 小时的第 `17` 分钟运行（每天 8 次），也支持 `workflow_dispatch` 手工触发。它会安装 Python 3.12、Playwright Chromium，使用临时 profile 抓取；只有新素材、新时段槽或旧交互模型升级导致 `data/` 变化时才提交，然后构建并部署 GitHub Pages。

`.github/workflows/pages.yml` 用于手工触发、普通 `main` push，或在 `Import Historical Banners` 成功完成后通过 `workflow_run` 构建并部署 Pages。后者用于解决默认 `GITHUB_TOKEN` 产生的自动提交不会再次触发 `push` workflow 的限制。

`.github/workflows/wayback-import.yml` 直接运行 `backend/wayback_import.py` 自动下载：脚本或工作流更新推送后启动首次 `2018-01-01` 至今的月度回填，此后每月 2 日北京时间 `02:27` 自动补抓，也保留手工指定范围入口。每累计 10 个真正创建或更新的归档，就由 `scripts/checkpoint_wayback.py` 提交并推送一次 `data/`、触发一次 Pages；末尾不足 10 个也会提交。失败和完全重复记录不计入批次。它与每日抓取共用数据写入锁，核心抓取逻辑仍位于 Python 后端，可迁移到 NAS。

## Wayback 历史导入

先只查询快照、不写入数据：

```powershell
python backend\wayback_import.py --from-date 2018 --to-date 2026 --cadence monthly --discovery-only
```

本地也可按年份运行同一个批处理脚本，降低网络失败和超时的影响：

```powershell
python backend\wayback_import.py --from-date 2018 --to-date 2018 --cadence monthly
```

`monthly` 只取得每月附近的代表快照；需要更密集的历史可改为 `weekly` 或 `daily`。导入器全程 headless，页面和素材均从 Wayback 回放地址获取，并兼容旧版 `.bili-banner`；如果归档没有保存素材、原脚本无法运行、分层互动全为零或检测到动态 Y，该快照会失败并跳过，不会用截图伪造。详细操作见 [Wayback 历史导入](docs/Wayback历史导入.md)。

GitHub Runner 的出口 IP、地理位置、Cookie 和 A/B 分流可能导致它看到的 Banner 与个人电脑不同。如果必须以自己的网络环境为准，建议在 Windows/NAS 抓取后提交 `data/`，让 GitHub Pages 只负责展示。

## 明确限制

- 抓取依赖 Bilibili 当前的 `.animated-banner`、`.layer` 和静态图片选择器；网站 DOM 改版、验证码或网络拦截可能导致失败。
- `.animated-banner` 不存在但页面提供原始 `.bili-header__banner` 图片时会生成 `static` 记录；两者都没有才写诊断并退出。
- 分层图层缺少素材 URL 或任一素材下载失败时会写入 `data/diagnostic.json` 并中止本次归档，不会保存缺层的 split 结果。
- 分层 Banner 的全部采样效果均为零时会中止归档，避免生成“看起来完整但没有交互”的错误历史项。
- v10 可回放采样到的二维 matrix（Y 除外）和透明度变化；动态 Y 一旦被检测到会中止抓取，CSS filter 等未采样属性仍不属于回放协议。
- `contentHash` 基于下载素材及其基本媒体信息，不包含日期、来源 URL或交互曲线；只改变交互元数据不会形成重复 archive。
- 自动 `familyId` 依据“同一日期 + 相同布局结构”归并时段变体；若网站在同一天把结构完全相同的 Banner 更换为无关主题，可能需要人工拆分 family。
- 前端是纯静态页面，没有后台 API、用户登录、数据库或在线回源逻辑。
- 历史素材会持续增加 Git 仓库和 Pages 体积；程序不会自动删除旧归档。
- Wayback 并不保证每个快照都保存完整子资源或可执行脚本，因此 2018 年至今只能导入“归档中仍可恢复”的 Banner，不代表逐日无缺口。

## 文档入口

- [详细项目说明](docs/项目说明.md)
- [架构说明](docs/架构说明.md)
- [数据格式](docs/数据格式.md)
- [GitHub Pages 部署](docs/GITHUB_PAGES部署.md)
- [NAS 部署](docs/NAS部署.md)
- [故障排查](docs/故障排查.md)
- [Wayback 历史导入](docs/Wayback历史导入.md)
- [变更记录](docs/CHANGELOG.md)
