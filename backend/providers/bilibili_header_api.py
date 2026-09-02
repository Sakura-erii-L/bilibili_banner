from __future__ import annotations

import copy
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


DEFAULT_ENDPOINTS = (
    "https://api.bilibili.com/x/web-show/page/header/v2?resource_id=142",
    "https://api.bilibili.com/x/web-show/page/header?resource_id=142",
)


def configured_endpoints() -> list[str]:
    manual = os.environ.get("BANNER_HEADER_API_URL", "").strip()
    if manual:
        return [manual]
    return list(DEFAULT_ENDPOINTS)


def _request(url: str, *, user_agent: str, referer: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": referer,
            "User-Agent": user_agent,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_header_api(
    *,
    user_agent: str,
    referer: str,
    endpoints: list[str] | None = None,
    timeout: int = 30,
    loader: Callable[[str], bytes] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Fetch the first usable Header API response.

    ``loader`` exists mainly for Wayback/history callers and tests. It receives the
    endpoint URL and must return raw JSON bytes.
    """
    errors: list[str] = []
    for endpoint in endpoints or configured_endpoints():
        try:
            raw = loader(endpoint) if loader else _request(
                endpoint,
                user_agent=user_agent,
                referer=referer,
                timeout=timeout,
            )
            payload = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Header API root is not an object")
            if int(payload.get("code", -1)) != 0:
                raise ValueError(
                    f"Header API code={payload.get('code')}: {payload.get('message')}"
                )
            if not isinstance(payload.get("data"), dict):
                raise ValueError("Header API data is missing")
            return endpoint, payload
        except Exception as exc:  # provider should try the next compatible endpoint
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("Header API unavailable; " + " | ".join(errors))


def parse_split_layer(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("split_layer is not an object")
        return parsed
    return {"version": "1", "layers": []}


def absolute_asset_url(src: str) -> str:
    value = str(src or "").strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://"):
        host = (urllib.parse.urlparse(value).hostname or "").lower()
        if host.endswith(("hdslb.com", "bilibili.com", "bilivideo.com")):
            return "https://" + value[len("http://"):]
    return value


def normalized_identity(src: str) -> str:
    """Identity used only for matching/dedup, never as a download URL."""
    value = absolute_asset_url(src)
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path
    # Bilibili image-transcoding suffixes are presentation variants. Keep the
    # requested URL unchanged; strip only for identity comparison.
    path = re.sub(r"@[^/]+$", "", path)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def infer_tag(src: str, content_type: str = "") -> str:
    value = f"{src} {content_type}".lower()
    if any(token in value for token in ("video/", ".webm", ".mp4", ".m3u8")):
        return "video"
    if ".svg" in value or "image/svg" in value:
        return "svg"
    return "img"


def iter_layer_resources(split_layer: dict[str, Any]):
    layers = split_layer.get("layers")
    if not isinstance(layers, list):
        return
    for layer_index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        resources = layer.get("resources")
        if not isinstance(resources, list):
            continue
        for resource_index, resource in enumerate(resources):
            if not isinstance(resource, dict):
                continue
            src = absolute_asset_url(str(resource.get("src") or ""))
            if src:
                yield layer_index, resource_index, layer, resource, src


def iter_extension_resources(extensions: dict[str, Any]):
    """Yield nested extension src fields without changing the source shape."""
    def walk(node: Any, path: tuple[str, ...]):
        if isinstance(node, dict):
            for key, value in node.items():
                current_path = path + (str(key),)
                if key == "src" and isinstance(value, str):
                    src = absolute_asset_url(value)
                    if src:
                        yield current_path, src
                else:
                    yield from walk(value, current_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, path + (str(index),))

    yield from walk(extensions, ())


def parse_header_api(payload: dict[str, Any], endpoint: str) -> dict[str, Any]:
    data = payload.get("data") or {}
    split = parse_split_layer(data.get("split_layer"))
    layers = split.get("layers") if isinstance(split.get("layers"), list) else []
    is_split = bool(int(data.get("is_split_layer") or 0)) or bool(layers)
    resources = [
        {
            "layerIndex": layer_index,
            "resourceIndex": resource_index,
            "src": src,
            "normalizedIdentity": normalized_identity(src),
            "id": resource.get("id"),
            "duration": resource.get("duration"),
        }
        for layer_index, resource_index, _layer, resource, src
        in iter_layer_resources(split)
    ]
    extensions = copy.deepcopy(split.get("extensions") or {})
    resources.extend(
        {
            "extensionPath": ".".join(path),
            "src": src,
            "normalizedIdentity": normalized_identity(src),
            "tag": infer_tag(src),
        }
        for path, src in iter_extension_resources(extensions)
    )
    return {
        "endpoint": endpoint,
        "raw": payload,
        "data": copy.deepcopy(data),
        "id": data.get("id"),
        "name": data.get("name") or "",
        "pic": absolute_asset_url(str(data.get("pic") or "")),
        "litpic": absolute_asset_url(str(data.get("litpic") or "")),
        "url": data.get("url") or "",
        "request_id": data.get("request_id"),
        "is_split_layer": is_split,
        "split_layer": split,
        "layers": copy.deepcopy(layers),
        "extensions": extensions,
        "resources": resources,
    }


def extension_for_url(url: str, content_type: str = "") -> str:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    if suffix and 1 < len(suffix) <= 8:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    return guessed or ".bin"


def interaction_effects(layers: list[dict[str, Any]]) -> list[str]:
    effects: list[str] = []
    for key in ("scale", "rotate", "translate", "blur", "opacity"):
        if any(
            isinstance(layer.get(key), dict)
            and (
                layer[key].get("offset") not in (None, 0, [0, 0])
                or layer[key].get("offsetCurve")
            )
            for layer in layers
            if isinstance(layer, dict)
        ):
            effects.append(key)
    return effects
