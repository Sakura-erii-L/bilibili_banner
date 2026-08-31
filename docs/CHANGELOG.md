# 变更记录

本文档只记录当前代码能够确认的版本特性；未从仓库历史中推断不存在的功能。

## v10.1

- 新增 `backend/wayback_import.py`，可从 2018 年开始按月、周或日发现 Wayback 首页快照并在 headless Chromium 中导入。
- 历史素材 URL 强制重写为 Wayback 回放地址，浏览器阻止直接访问 `bilibili.com` 和 `hdslb.com`。
- 支持旧版 `.head-banner`、`.header-banner`、`#banner_link`、`.banner_link` 的真实图片和 CSS `background-image`；不使用截图回退。
- 新增 `observations`：同一实际素材跨多个历史日期只保存一个物理 archive，但索引可按多个日期引用。
- 新增手动工作流 `wayback-import.yml`；它与每日抓取共用数据写入并发锁，只提交 `data/`，Pages 仍由 `pages.yml` 发布。
- manifest/index 新生成版本为 `10.1`，旧 v9.2/v10 数据继续兼容。

## v10

- 新 Banner 使用 9 个正负水平输入点采样真实 computed matrix/opacity，不再把所有时期简化为单点线性 `a`。
- 采样鼠标离场后的时间曲线，每个 Banner 保存自己的回位节奏；旧 v9.2 manifest 继续兼容。
- 动态 Y 位移会以 `vertical-motion-detected` 中止，前端回放始终锁定初始 Y。
- 同一套实际素材仍只归档一次；新的时段槽只更新已有 manifest/index。
- 同日且布局结构相同的不同素材通过 `familyId` 归为一个 Banner，前端按 `Asia/Shanghai` 当前时刻选择最近观测变体。
- GitHub Actions 从每天一次提高为每 3 小时采样一次。
- `.animated-banner` 缺失但存在原始 `.bili-header__banner` 图片时可生成非截图的 `static` 记录。
- 新增 `frontend/interaction.js` 和无依赖测试，manifest/index 新生成版本为 `10.0`。

## v9.2

该版本重点是禁止截图掩盖分层失败：

- `backend/capture.py` 不再生成 Banner 截图。
- `data/index.json` 的记录不再包含 `preview` 字段。
- `frontend/app.js` 在 manifest 或分层素材加载失败时直接显示错误。
- `scripts/build_site.py` 发布构建排除旧归档中的 `preview.png`。
- `static.*` 明确表示 Bilibili 原始静态图片，不是浏览器截图。
- manifest、index 的 `version` 为 `9.2`。
- 抓取仍要求 `.animated-banner` 容器存在；结构缺失会写诊断并失败。
- 当前内容或历史中已有内容再次出现时完全无操作，不改写 `current/`、`index.json`，也不新增 archive；`--force` 仍可用于人工创建物理副本。
- 任一分层素材 URL 缺失或下载失败时写诊断并终止，避免归档不完整的真实分层。
- 每日工作流用包含未跟踪文件的状态检查检测新 archive；Pages 构建同时排除诊断和抓取临时文件。

## v9.1

- 所有运行环境固定使用 headless 浏览器，不提供可见窗口切换参数。
- 分层运动对象改为读取 `.layer.firstElementChild` 的 transform。
- 通过真实页面的水平鼠标探测计算每层 `a`。
- 前端使用 `initialX + moveX * a` 回放分层交互。
- 动态交互只修改 X，保留原始 Y。
- 全部测量运动为零时中止抓取，避免归档无交互结果。

## v9.0 / v9 系列基础能力

- 使用 Bilibili 首页真实渲染结果作为主来源。
- 将图层素材保存到本地归档，并使用 manifest 描述布局。
- 使用素材内容指纹进行历史去重。
- `data/index.json` 提供历史列表，前端支持年月和季节筛选。
- 通过静态站点构建脚本发布到 GitHub Pages，也支持 NAS 静态服务器。

## 当前明确未实现

以下内容不属于上述版本的已实现能力：

- 自动删除或压缩旧归档。
- 数据库、对象存储和任务锁。
- 完整复现 Banner 的 Y 轴、旋转、缩放等动态效果。
- 保证 GitHub Runner 与本地网络看到完全相同的 Banner。
