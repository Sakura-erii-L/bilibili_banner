# Codex 交接说明：API-first Banner v11

## 1. 当前目标与结论

本版本已经把“当前 Banner 默认抓取”改为：

```text
Header API -> split_layer -> 原始 resources -> v11 manifest -> 本地 renderer
```

不再默认打开 Bilibili 首页并做 9 点鼠标实测。

模式现在是：

```text
当前 Banner：API-first
历史 Banner：Wayback archived Header API first -> archived DOM fallback
DOM：verify-only / legacy diagnostic
```

参考了 `MikuFan039/BiliDynBanner` 的 Header API 和参数重放思路，但没有引入 Vue/Webpack，也没有整体复制该项目。

## 2. 本次主要代码变化

重点文件：

```text
backend/providers/__init__.py
backend/providers/bilibili_header_api.py
backend/capture.py
backend/wayback_import.py
frontend/app.js
frontend/interaction.js
scripts/audit_archives.py
tests/test_header_api_provider.py
tests/test_capture_interaction.py
tests/test_wayback_import.py
tests/interaction.test.mjs
.github/workflows/daily-update.yml
README.md
docs/API_FIRST_ARCHITECTURE.md
CODEX_HANDOFF.md
```

关键行为：

- `python backend/capture.py`：只请求 Header API，不启动浏览器。
- `--verify-dom`：隐藏浏览器轻量核对 API/DOM，不做运动采样。
- `--legacy-dom-capture`：旧 v10.1 DOM 采样，仅诊断。
- Wayback 每个快照先找归档 Header API；失败才懒启动 Playwright。
- 分层 Banner 的 `pic` 只能作为 fallback。
- `resources[]` 全部独立下载；视频不转图片，多帧不合并。
- 前端直接解释 `scale/rotate/translate/blur/opacity/offsetCurve/wrap`。
- 旧 v9/v10 renderer 和 archive 保持兼容。

## 3. 已完成的验证

在交付环境中已执行：

```text
python -m unittest discover -s tests
-> 28 tests OK

node tests/interaction.test.mjs
-> OK

python scripts/audit_archives.py
-> 0 issues
-> 2 legacy warnings（旧基线缺 structure hash/type，不是新错误）

python scripts/build_site.py
-> 成功生成 _site
```

还应在最终本机/CI 运行：

```bash
python -m compileall backend scripts tests
python -m unittest discover -s tests
node tests/interaction.test.mjs
python scripts/audit_archives.py
python scripts/build_site.py
```

交付环境无法直接解析 Bilibili 域名，因此**没有完成真实线上 Header API 的端到端下载测试**。在目标网络环境中必须额外运行：

```bash
python backend/capture.py
```

然后检查：

```text
data/current/banner.json
source/api.json
layers[].resources[]
interaction.model == bilibili-header-api-v1（分层 Banner）
type / completeness / missing_assets
```

可选再执行：

```bash
python backend/capture.py --verify-dom
```

用于确认当前 API 与真实页面 DOM 没有明显分流。

## 4. 接手时先保护现有本地修改

用户提供给本次修改的源码本身已经是一个 dirty Git 工作树。接手 Codex **不得假设本地 main 是干净的**，也不得为“方便合并”执行破坏性清理。

开始前必须执行：

```bash
git status --short
git branch --show-current
git remote -v
git log -5 --oneline
```

禁止直接执行：

```text
git reset --hard
git clean -fd
git checkout -- .
```

如果本地还有未提交的重要修改，先识别其来源。必要时创建 checkpoint commit 或另建分支，不要丢弃。

## 5. 将本交付包合并到用户实际本地仓库

推荐不要整目录覆盖 `.git`、`data/` 或用户后来新增的文件。优先只合并“第 2 节”列出的源码/文档文件。

若用户实际本地仓库与本次上传版本完全一致，可把交付包中的对应文件覆盖到工作树；随后：

```bash
git diff -- backend frontend scripts tests .github README.md CODEX_HANDOFF.md docs/API_FIRST_ARCHITECTURE.md
git status --short
```

重点确认：

- 没有误删历史 `data/archive`；
- 没有提交 `_site/`；
- 没有把本地账号、token、浏览器 profile 加入 Git；
- Unicode 中文 docs 文件名没有因解压工具变成 `#Uxxxx` 名称；
- 本次 refactor 之外的用户修改仍存在。

Windows 解压/复制时优先使用能正确保留 UTF-8 文件名的工具；如果看到 `docs/#U67...`，不要提交这些伪文件名，先恢复正确中文文件名。

## 6. 本地验证顺序

合并后按此顺序：

```bash
python -m pip install -r requirements.txt
python -m compileall backend scripts tests
python -m unittest discover -s tests
node tests/interaction.test.mjs
python scripts/audit_archives.py
python scripts/build_site.py
```

如果 Node 未安装，只记录前端单测未运行，不要因此修改项目架构。

随后做真实抓取：

```bash
python backend/capture.py
```

若是动态 Banner，应至少检查：

```text
api.isSplitLayer == true
api.layerCount > 0
layers.length > 0
每个 layer 的 resources[] 对应文件真实存在
static.file 若存在，只是 fallback_image
interaction.model == bilibili-header-api-v1
```

再打开本地站点：

```bash
python scripts/build_site.py
python scripts/serve.py
```

人工检查：

- 鼠标左右移动时各层视差方向、幅度合理；
- 鼠标离开约 200ms 回位；
- WebM/MP4 正常播放；
- 多帧 resource 按 duration 循环；
- 旧 archive 仍能显示；
- 浏览器 Network 中前端没有回源 `bilibili.com/hdslb.com`。

## 7. 真实 API 若失败时怎么处理

不要立即恢复成 DOM-first。

按顺序检查：

1. `BANNER_HEADER_API_URL` 是否被错误覆盖；
2. v2 endpoint 是否返回 `code=0`；
3. 是否只是地区/UA/临时网络失败；
4. Header API schema 是否变化；
5. 更新 `backend/providers/bilibili_header_api.py` parser；
6. 用 `--verify-dom` 或 `--legacy-dom-capture` 仅用于诊断差异。

只有确认官方 Header API 已经不能表达 Banner 时，再讨论新增 provider/fallback，不要悄悄让默认路径重新变成 9 点采样。

## 8. Git 合并、同步远端与推送

完成测试后建议先在独立分支提交：

```bash
git switch -c api-first-banner-v11
```

如果当前已经在包含用户未提交修改的工作分支，不要强制切分支；先由 `git status` 判断是否适合直接提交。

只暂存本次确认过的文件：

```bash
git add backend frontend scripts tests .github/workflows/daily-update.yml README.md CODEX_HANDOFF.md docs/API_FIRST_ARCHITECTURE.md
```

先检查：

```bash
git diff --cached --stat
git diff --cached
```

确认无误后：

```bash
git commit -m "refactor: archive banners from header API"
```

然后再同步远端，避免覆盖 GitHub Actions 或别人刚推送的提交：

```bash
git fetch origin
```

若目标是 `main`，优先：

```bash
git rebase origin/main
```

遇到冲突时逐文件解决，不得 `--theirs`/`--ours` 批量覆盖 `data/`。rebase 后重新执行核心测试和 audit。

更安全的推送方式：

```bash
git push -u origin api-first-banner-v11
```

然后合并 PR。

如果用户明确要求直接推 main，并且确认远端策略允许：

```bash
git push origin HEAD:main
```

禁止 force push，除非用户另外明确要求且已经确认不会覆盖远端历史。

## 9. 推送后检查 GitHub Actions

至少观察：

```text
Daily Banner Update
Import Historical Banners
Pages deploy
```

日常 workflow 现在不再安装 Chromium，因为默认当前抓取不需要浏览器。

Wayback workflow 仍安装 Chromium，因为归档 Header API 缺失时需要 DOM fallback。

如果 Actions 自动产生新的 `data/` commit，而本地随后还要继续推送，先：

```bash
git fetch origin
git rebase origin/main
```

再继续，避免 non-fast-forward。

## 10. 后续优先事项

后续 Codex 可以继续处理，但不要与本次基础重构混在同一次未经验证的大改中：

1. 在真实网络环境验证当前 Header API 的所有字段类型；
2. 对比 `--verify-dom`，确认不同地区/A-B 流量是否返回同一 Banner；
3. 根据实际 API 数据实现 `extensions.snow/petals/...`；
4. 为 Header API schema 变化增加 fixture 回归测试；
5. 研究 Common Crawl / GitHub 历史库 provider，补 Wayback 缺口；
6. 若历史 API 与首页快照时间偏差明显，增加 Wayback API capture timestamp 一致性约束。

核心原则保持不变：

```text
能获得官方结构数据 -> 直接保存结构数据
不能完整恢复 -> partial / fallback
不要截图、不要 flatten、不要用 DOM 实测替代已存在的官方参数
```
