# API-first Banner 架构（v11）

## 目标

v11 将当前 Banner 的默认权威来源改为 Bilibili Header API。程序直接保存 `split_layer` 的结构和原始资源，不再通过鼠标多点采样反推官方已经给出的运动参数。

默认路径：

```text
Header API
  -> is_split_layer / split_layer
  -> layers[].resources[]
  -> 原始图片 / WebM / MP4
  -> apiConfig(scale/rotate/translate/blur/opacity)
  -> v11 manifest
  -> 静态前端 renderer
```

历史路径：

```text
Wayback snapshot
  -> 先尝试归档 Header API JSON
  -> 成功：复用同一个 parser/renderer
  -> 失败：隐藏 Playwright 解析历史 DOM（fallback）
```

DOM 不再是当前 Banner 的默认数据源。`--verify-dom` 只做轻量一致性校验；`--legacy-dom-capture` 才启用旧 v10.1 的完整 DOM 运动采样。

## Header API Provider

实现位于 `backend/providers/bilibili_header_api.py`。

默认尝试：

```text
https://api.bilibili.com/x/web-show/page/header/v2?resource_id=142
https://api.bilibili.com/x/web-show/page/header?resource_id=142
```

可用 `BANNER_HEADER_API_URL` 手工覆盖。

Provider 负责：

- 获取并校验 JSON；
- 解析字符串或对象形式的 `split_layer`；
- 保留全部 layer 原始字段；
- 枚举每个 `resources[]`；
- 将 `//...` 规范为 HTTPS；
- 只为匹配/去重生成 `normalizedIdentity`，实际下载仍使用原请求 URL；
- 识别图片、SVG、WebM、MP4 等资源。

## v11 分层归档

每个 API layer 保存：

```json
{
  "index": 0,
  "apiIndex": 0,
  "id": 1,
  "name": "...",
  "resources": [
    {
      "file": "api_layer_00_00_xxx.webp",
      "src": "https://...",
      "duration": 100,
      "apiResource": {}
    }
  ],
  "apiConfig": {
    "scale": {},
    "rotate": {},
    "translate": {},
    "blur": {},
    "opacity": {}
  },
  "apiLayer": {}
}
```

`source/api.json` 保存完整原始响应。未知字段不会因为当前 renderer 未使用而被删除。

分层 Banner 中的 `pic` 仅作为 fallback；它不进入 layered Banner 的主资源哈希，也不能使 structured Banner 退化为 static。

## 前端参数模型

`frontend/app.js` 的 `bilibili-header-api-v1` renderer 使用与 BiliDynBanner 相同的核心参数语义：

```text
displace = (clientX - enterX) / containerWidth
containerScale = containerHeight / 155
```

各参数的动态部分：

```text
offsetValue = offset * curve(displace)
```

其中 `offsetCurve` 使用有符号 cubic-bezier。`translate` 再乘 `containerScale` 和 API 的初始缩放因子。鼠标离开后约 200 ms 线性回到 `displace=0`。

一个 layer 中多个 resource 只有在全部 resource 都有正 `duration` 时才按毫秒循环切帧；否则按静态第一帧处理。视频始终以视频资源重放。

## 兼容性

- 新 archive/index：v11.0。
- 旧 v9/v10 archive 不迁移，继续走原 sampled renderer。
- v11 新字段仅在实际存在时参与哈希，避免旧 archive 因空 `api` 字段产生假变更。
- `extensions` 会保留，但 snow/petals 等扩展尚未实现时 archive 标记为 `partial`。

## 验证

标准检查：

```bash
python -m compileall backend scripts tests
python -m unittest discover -s tests
node tests/interaction.test.mjs
python scripts/audit_archives.py
python scripts/build_site.py
```

当前离线测试使用模拟 Header API 资源，覆盖多帧图片、WebM、交互参数、fallback、哈希和 Wayback API URL。实际线上 Header API 仍应在目标网络环境中再运行一次 `python backend/capture.py` 验证。
