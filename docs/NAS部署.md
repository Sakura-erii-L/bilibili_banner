# NAS 部署

NAS 版本与 GitHub Pages 共用 `backend/`、`frontend/`、`data/` 和 `scripts/`。NAS 负责抓取和构建，Nginx/Caddy 只提供 `_site/` 静态文件。

## 1. 推荐目录

```text
/volume1/bilibili-banner/
├─ app/
│  ├─ backend/
│  ├─ frontend/
│  ├─ scripts/
│  ├─ requirements.txt
│  ├─ data/
│  └─ .venv/
├─ site/
└─ logs/
```

`app/data/` 是当前代码默认读取和构建的位置。如果程序目录本身就是 `/volume1/bilibili-banner`，则将 `app/` 替换为项目根目录即可。

## 2. 运行环境

推荐：

- Python 3.10 或更高版本
- 可运行的 Chromium、Google Chrome 或 Microsoft Edge
- Playwright Python 包
- Nginx 或 Caddy
- cron 或 NAS 自带任务计划

创建虚拟环境并安装依赖：

```bash
cd /volume1/bilibili-banner/app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如果 NAS 没有系统 Chromium，安装 Playwright 管理的浏览器：

```bash
python -m playwright install chromium
```

如果 NAS 的浏览器不在程序自动探测路径，设置：

```bash
export BROWSER_EXE=/usr/bin/chromium
```

抓取始终以 headless 模式运行，不需要图形桌面，也不会弹出 Bilibili 页面。

## 3. 手工抓取和构建

使用项目默认数据目录时（推荐）：

```bash
cd /volume1/bilibili-banner/app
.venv/bin/python backend/capture.py
.venv/bin/python scripts/build_site.py --output /volume1/bilibili-banner/site
```

如果需要把抓取数据放到项目外部目录：

```bash
cd /volume1/bilibili-banner/app
export BANNER_DATA_DIR=/volume1/bilibili-banner/data
.venv/bin/python backend/capture.py
```

然后在构建前将外部数据同步到项目根目录的 `data/`。当前 `build_site.py` 固定读取项目根目录 `data/`，不会读取 `BANNER_DATA_DIR`。

如果程序和数据都在项目默认位置：

```bash
cd /volume1/bilibili-banner/app
.venv/bin/python backend/capture.py
.venv/bin/python scripts/build_site.py
```

常用维护命令：

```bash
.venv/bin/python backend/capture.py --rebuild-index
.venv/bin/python scripts/serve.py --directory /volume1/bilibili-banner/site --port 8765
```

`--rebuild-index` 只扫描已有 archive，不访问 Bilibili，也不改变 `current/`。

## 4. 定时任务

示例 cron（假设 NAS 本地时区为 `Asia/Shanghai`，即北京时间每 3 小时的第 17 分钟）：

```cron
17 */3 * * * cd /volume1/bilibili-banner/app && BANNER_TIME_SLOT_MINUTES=180 /volume1/bilibili-banner/app/.venv/bin/python backend/capture.py >> /volume1/bilibili-banner/logs/capture.log 2>&1 && /volume1/bilibili-banner/app/.venv/bin/python scripts/build_site.py --output /volume1/bilibili-banner/site >> /volume1/bilibili-banner/logs/build.log 2>&1
```

更推荐使用小脚本或 NAS 任务计划分别记录抓取和构建日志，确保构建只在抓取成功后执行。当前程序没有跨进程任务锁，同一 `BANNER_DATA_DIR` 不应同时运行多个抓取任务。

若 NAS 系统时区不是 `Asia/Shanghai`，可显式设置：

```bash
export BANNER_TIMEZONE=Asia/Shanghai
```

## 5. Nginx/Caddy 静态发布

Nginx 只需将站点根目录指向 `_site/` 或自定义输出目录。例如：

```nginx
server {
    listen 80;
    server_name banner.example.com;
    root /volume1/bilibili-banner/site;
    index index.html;
}
```

前端必须能够访问：

```text
/index.html
/app.js
/style.css
/data/index.json
/data/archive/<id>/banner.json
/data/archive/<id>/<asset>
```

不要只发布 `frontend/`，否则页面会找不到数据和素材。也不要用 `file://` 双击 HTML 调试，使用 Web 服务器提供静态文件。

## 6. 数据迁移和备份

从 GitHub 迁移到 NAS，将仓库中的 `data/` 复制到项目根目录（上例为 `app/data/`）：

```text
data/
```

然后确保要发布的数据已经位于项目根目录的 `data/`，再重新构建 `_site/`：

```bash
.venv/bin/python scripts/build_site.py \
  --output /volume1/bilibili-banner/site
```

注意：`BANNER_DATA_DIR` 只会被 `backend/capture.py` 使用；当前 `scripts/build_site.py` 固定从项目根目录 `data/` 读取，因此外部数据目录必须先同步到该位置，或调整构建脚本。

如果历史 manifest 来自旧版本或索引缺失：

```bash
.venv/bin/python backend/capture.py --rebuild-index
```

备份重点是 `data/archive/` 和 `data/index.json`；`_site/` 可以重新生成，`.runtime/browser-profile` 不属于历史归档。

## 7. 自定义配置

主要环境变量：

| 变量 | 用途 |
| --- | --- |
| `BANNER_DATA_DIR` | 将数据放到独立卷 |
| `BANNER_SOURCE_URL` | 修改抓取页面 |
| `BANNER_TIMEZONE` | 控制时间和季节 |
| `BANNER_VIEWPORT_WIDTH` / `BANNER_VIEWPORT_HEIGHT` | 修改抓取视口 |
| `BROWSER_EXE` | 指定浏览器可执行文件 |
| `BANNER_PROFILE_DIR` | 指定本地 profile |
| `BANNER_PROFILE_MODE=temporary` | 每次使用临时 profile |

修改 `BANNER_VIEWPORT_*` 会改变抓取到的几何布局；不要在未确认展示效果前频繁切换视口。

## 8. NAS 限制

- NAS 的出口网络可能与 GitHub Runner 或个人电脑不同，Banner 可能不同。
- Bilibili DOM 变化会影响选择器和分层读取，需要查看 `data/diagnostic.json`。
- 历史图片/视频会持续占用存储，程序不会自动清理。
- 没有内置数据库、锁或增量对象存储；请用任务计划保证同一时间只有一个抓取过程。
- 若使用独立 `BANNER_DATA_DIR`，构建脚本不会自动从该目录读取数据，必须将数据同步回项目默认目录，或在构建前按项目需求调整数据布局；当前 `build_site.py` 的 `DATA` 常量固定为项目根目录 `data/`。
