# Bilibili Banner Archive v9.2

一个将 Bilibili 首页顶部 Banner 抓取为本地静态归档，并在浏览器中重建分层水平视差交互的项目。

当前程序版本和数据格式版本均为 `9.2`。抓取器读取 Bilibili 首页实际渲染的 DOM，不依赖旧版 Header API；前端只读取仓库内的 `data/`，不会在用户浏览器中访问 Bilibili。

## 当前实现

- 使用 Playwright 启动始终 `headless=True` 的浏览器，Windows、NAS、GitHub Actions 都不会弹出可见的 Bilibili 页面。
- 从 `.animated-banner .layer` 读取真实分层；运动对象是每个 `.layer` 的 `firstElementChild`。
- 下载原始图层素材（图片或视频），保存初始 CSS matrix、尺寸、透明度和水平运动系数。
- 通过一次水平鼠标探测测量每层的 `a`：`a = (移动后 X - 初始 X) / 鼠标移动距离`。
- 前端按 `layerX = initialX + moveX * a` 回放交互，动态交互只修改 X，Y 保留初始值。
- 按下载素材内容计算 `contentHash`，当前 Banner 不变或历史素材再次出现时都完全跳过数据更新，不新增归档、不改写 `data/current/` 和 `data/index.json`。
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
python backend/capture.py --force          # 即使重复也创建新的物理归档目录
python backend/capture.py --rebuild-index  # 只根据已有 archive 重建 data/index.json
python scripts/build_site.py               # 构建到 _site/
python scripts/build_site.py --output PATH # 构建到指定目录
python scripts/serve.py --port 8765        # 服务 _site/
python scripts/serve.py --directory PATH   # 服务指定静态目录
```

`--force` 可能产生相同内容的多个物理目录；`data/index.json` 仍会按 `contentHash` 去重，只展示一个记录。

## 目录结构

```text
.
├─ backend/
│  └─ capture.py                    # Headless 抓取、测量、归档、索引
├─ frontend/
│  ├─ index.html                    # 页面骨架
│  ├─ app.js                        # 数据加载、筛选、分层重建和交互
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
│  └─ pages.yml                     # 代码或数据变更后的 Pages 部署
├─ docs/                            # 项目、架构、部署和排障文档
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

分层归档的 `banner.json` 保存每个图层的初始 matrix 和 `a`。前端保留 matrix 中的原始 Y、缩放等布局信息，只将 X 改为：

```text
moveX = 鼠标当前 pageX - 鼠标进入 Banner 时的 pageX
layerX = initialX + moveX * a
```

鼠标离开后在 `300ms` 内回到初始位置。原始素材自身的视频动画仍可能播放，这与鼠标交互造成的移动是两件事。

## 配置环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `BANNER_DATA_DIR` | 项目根目录 `data/` | 数据、归档和索引的根目录 |
| `BANNER_SOURCE_URL` | `https://www.bilibili.com/` | 抓取页面 |
| `BANNER_TIMEZONE` | `Asia/Shanghai` | 时间戳和季节计算时区 |
| `BANNER_VIEWPORT_WIDTH` | `1650` | Headless 浏览器视口宽度 |
| `BANNER_VIEWPORT_HEIGHT` | `800` | Headless 浏览器视口高度 |
| `BROWSER_EXE` | 自动探测 | 指定现有 Edge/Chrome/Chromium 可执行文件 |
| `BANNER_PROFILE_DIR` | `.runtime/browser-profile` | 本地持久化浏览器 profile |
| `BANNER_PROFILE_MODE=temporary` | 未设置 | 使用一次性 profile；设置 `CI` 时也会自动使用一次性 profile |

环境变量在启动时读取；修改视口或数据目录后需要重新运行抓取/构建。
注意：`BANNER_DATA_DIR` 由 `backend/capture.py` 使用；当前 `scripts/build_site.py` 固定从项目根目录的 `data/` 复制数据，使用外部数据目录时需要先同步数据或调整构建脚本。

## 自动化部署

`.github/workflows/daily-update.yml` 默认在北京时间每天 `06:17` 运行，也支持 `workflow_dispatch` 手工触发。它会安装 Python 3.12、Playwright Chromium，使用临时 profile 抓取，只有 `data/` 发生变化时才提交数据，然后构建并部署 GitHub Pages。

`.github/workflows/pages.yml` 用于手工触发，或在 `main` 分支的 `frontend/**`、`data/**`、`scripts/build_site.py`、工作流文件发生变化时构建并部署 Pages。

GitHub Runner 的出口 IP、地理位置、Cookie 和 A/B 分流可能导致它看到的 Banner 与个人电脑不同。如果必须以自己的网络环境为准，建议在 Windows/NAS 抓取后提交 `data/`，让 GitHub Pages 只负责展示。

## 明确限制

- 抓取依赖 Bilibili 当前的 `.animated-banner`、`.layer` 和静态图片选择器；网站 DOM 改版、验证码或网络拦截可能导致失败。
- `.animated-banner` 不存在时不会静默生成静态结果，会写入 `data/diagnostic.json` 并退出。
- 分层图层缺少素材 URL 或任一素材下载失败时会写入 `data/diagnostic.json` 并中止本次归档，不会保存缺层的 split 结果。
- 运动测量结果全部为零时会中止归档，避免生成“看起来完整但没有交互”的错误历史项。
- 当前交互只复现水平 X 轴视差；不会根据鼠标动态修改 Y、scale、rotate、opacity 或 blur。
- `contentHash` 基于下载素材及其基本媒体信息，不包含日期、来源 URL、初始 matrix 或 `a`；只改变布局/运动参数而不改变素材时不会形成新的唯一历史记录。
- 前端是纯静态页面，没有后台 API、用户登录、数据库或在线回源逻辑。
- 历史素材会持续增加 Git 仓库和 Pages 体积；程序不会自动删除旧归档。

## 文档入口

- [详细项目说明](docs/项目说明.md)
- [架构说明](docs/架构说明.md)
- [数据格式](docs/数据格式.md)
- [GitHub Pages 部署](docs/GITHUB_PAGES部署.md)
- [NAS 部署](docs/NAS部署.md)
- [故障排查](docs/故障排查.md)
- [变更记录](docs/CHANGELOG.md)
