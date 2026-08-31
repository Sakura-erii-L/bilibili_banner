# Wayback 历史 Banner 导入

## 1. 能导入什么

`backend/wayback_import.py` 使用 Internet Archive 的 Availability API 查找 `https://www.bilibili.com/` 历史快照，再在始终 headless 的 Chromium 中打开 Wayback 回放页。

它从回放页检查以下真实来源和结构证据：

- 回放 DOM 中的 `.animated-banner .layer` 图片或视频；
- 当前及旧版 Header 中的真实 `<img>`；
- `.bili-banner`、`.head-banner`、`.header-banner`、`#banner_link`、`.banner_link`、`.banner-link` 等旧版容器的真实 `background-image`。
- IMG/srcset、PICTURE/SOURCE、VIDEO/SOURCE、SVG、CANVAS、data-*、computed transform/animation、performance 资源 URL、相关内联 CSS/JS/JSON。

原始素材 URL 会优先转换为同一时间戳的 Wayback `id_` 原始响应地址。浏览器回放路由阻止对 `bilibili.com`、`hdslb.com` 和 `bilivideo.com` 的直接请求；若 Wayback 子资源缺失，后端下载器才会受控尝试从重写 URL 还原出的原始 CDN 地址。前端始终只读本地文件。程序不保存截图，也不会将截图当作缺失图层的回退。

## 2. 2018 年至今的发现范围

默认范围就是 `2018-01-01` 至当前上海日期，默认每月查询一次接近月初的可用快照：

```powershell
python backend\wayback_import.py --discovery-only
```

明确指定范围：

```powershell
python backend\wayback_import.py `
  --from-date 2018-01-01 `
  --to-date 2026-08-31 `
  --cadence monthly `
  --discovery-only
```

`--from-date` 和 `--to-date` 也接受单独年份。`--to-date 2018` 会解释为 `2018-12-31`。

可用采样密度：

- `monthly`：适合第一次回填，约每月一个候选快照；
- `weekly`：更容易发现短期主题，但运行时间和仓库体积明显增加；
- `daily`：用于特定短日期范围，不建议一次覆盖八年以上。

Availability API 返回目标日期附近的最近快照。脚本会去除重复时间戳，并丢弃落在请求范围外的结果。

## 3. 本地导入

安装依赖和 Chromium：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

建议按年份运行：

```powershell
python backend\wayback_import.py --from-date 2018 --to-date 2018 --cadence monthly
python backend\wayback_import.py --from-date 2019 --to-date 2019 --cadence monthly
```

只测试一个已知时间戳：

```powershell
python backend\wayback_import.py --snapshot 20180201082457
```

限制本次处理数量：

```powershell
python backend\wayback_import.py --from-date 2018 --to-date 2018 --limit 2
```

完成后运行：

```powershell
python scripts\build_site.py
python scripts\serve.py
```

## 4. GitHub Actions 自动导入

`.github/workflows/wayback-import.yml` 不需要 Codex 或人工逐个执行下载命令：

- `backend/wayback_import.py` 或该 workflow 更新并推送后，自动启动一次 2018 年至今的 `monthly` 回填；
- 每月 2 日北京时间 `02:27` 自动再次扫描并补抓；
- 已写入 observation 的 Wayback 时间戳会被脚本自动跳过；
- 单个日期的 API/回放失败会记录后继续处理其余月份。

部分网络环境无法访问 `web.archive.org` 回放正文，因此自动任务默认运行在 GitHub Runner。需要人工缩小范围复查时仍可：

1. 打开仓库的 **Actions**。
2. 左侧选择 **Import Historical Banners**。
3. 点击 **Run workflow**。
4. `from_date` 和 `to_date` 填写需要复查的范围，例如 `2018`、`2018`。
5. 第一次选择 `monthly`，`limit` 填 `0`。
6. 点击绿色 **Run workflow**。
7. 查看 `Import real assets from Wayback snapshots` 日志。

自动和手工事件都通过脚本执行历史导入及 `data/` 提交。每个 `created`/`updated` 结果完成后，`scripts/checkpoint_wayback.py` 都会立即提交、推送一次并用 `workflow_dispatch` 触发 Pages。失败和 `unchanged` 不提交。历史 workflow 成功结束后，`pages.yml` 的 `workflow_run` 仍会执行最终一致性发布。

每日抓取和历史导入共用 `bilibili-banner-data-writes` 并发组，因此两者不会同时执行 `git push`。

## 5. 去重和跨日期出现

`contentHash` 由真实下载素材、图层结构、动画和交互参数共同计算，不依赖 Wayback URL 或日期。

如果同一素材在多个历史日期出现：

- `data/archive/` 中仍只有一个物理素材目录；
- `banner.json` 的 `observations` 保存各次历史出现；
- `data/index.json` 可以在多个日期 family 中引用同一 manifest；
- 前端不会直接请求 Wayback，发布站点只读取仓库内素材。

重复运行同一时间戳不会新增物理 archive，也不会新增重复 observation。

## 6. 为什么某些快照会失败

常见失败原因：

- Wayback 只保存了首页 HTML，没有保存 Banner 图片或视频；
- 页面依赖的历史 JS bundle 缺失，分层 DOM 没有生成；
- 分层 DOM 存在，但原互动脚本未运行；此时会保留素材并将交互标为 partial，而不是伪造运动参数；
- 回放页返回限流、重定向错误或 Wayback 服务暂时不可用；
- 历史版本使用尚未覆盖的 Header DOM 结构；
- 原效果依赖未归档的外部脚本、3D/CSS filter 或 Canvas/WebGL shader，无法完整序列化。

有可恢复资源时会生成 `completeness: partial` 并列出 `missing_assets`；完全没有支持容器或可判断证据时才进入批处理 `failures`。两种情况都不会生成截图或把 fallback 图片冒充完整 split。

Wayback 的收录本身并不完整，因此“2018 年至今”表示查询范围，不保证每一天或每一个活动 Banner 都存在可恢复快照。

当前自动发现器只实现 Wayback Availability API。Common Crawl、archive.today、Memento、GitHub 历史项目和其它 CDN 可作为人工恢复线索，`structureEvidence` 会保留可见 URL，但当前脚本尚未把这些来源实现为统一自动 provider。不要把来源规划误解为已完成回填能力。
