"""Read structured historical Banner data from palxiao/bilibili-banner.

This module deliberately stops at discovery and normalization.  The caller owns
asset downloads, archive deduplication, manifests, and index updates.
"""

from __future__ import annotations

import copy
import datetime as dt
import gzip
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .history import HistoricalResult
from . import bilibili_header_api as header_api


PROVIDER_NAME = "palxiao-bilibili-banner"
REPOSITORY = "palxiao/bilibili-banner"
API_TREE_URL = (
    "https://api.github.com/repos/palxiao/bilibili-banner/git/trees/main?recursive=1"
)
API_ASSETS_URL = "https://api.github.com/repos/palxiao/bilibili-banner/contents/assets"
RAW_BASE_URL = "https://raw.githubusercontent.com/palxiao/bilibili-banner/main"
DATE_RE = re.compile(r"^assets/(?P<date>\d{4}-\d{2}-\d{2})/data\.json$")


def _decode_body(body: bytes, content_encoding: str = "") -> bytes:
    if "gzip" in content_encoding.lower() or body.startswith(b"\x1f\x8b"):
        return gzip.decompress(body)
    return body


def _date_text(value: str | dt.date) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    parsed = dt.date.fromisoformat(str(value).strip())
    return parsed.isoformat()


def _normalise_transform(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)) and len(value) >= 6:
        result: list[float] = []
        for item in value[:6]:
            try:
                result.append(float(item))
            except (TypeError, ValueError):
                return [1, 0, 0, 1, 0, 0]
        return result
    if isinstance(value, str):
        match = re.search(r"matrix\s*\(([^)]+)\)", value, re.I)
        if match:
            return _normalise_transform(
                [part.strip() for part in match.group(1).split(",")]
            )
    return [1, 0, 0, 1, 0, 0]


def _normalise_opacity(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        values = list(value[:2])
        if len(values) == 1:
            values.append(values[0])
    else:
        values = [value if value is not None else 1]
        values.append(values[0])
    result: list[float] = []
    for item in values:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            result.append(1.0)
    return result


def resolve_asset_source(
    src: str,
    date: str,
    *,
    raw_base_url: str = RAW_BASE_URL,
) -> str:
    """Resolve palxiao's local ``src`` to a raw GitHub asset URL."""
    value = str(src or "").strip().replace("\\", "/")
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    value = value.lstrip("./")
    if value.startswith("assets/"):
        path = value
    else:
        path = f"assets/{date}/{value}"
    return f"{raw_base_url.rstrip('/')}/{path}"


class PalxiaoHistoryProvider:
    """Discover and parse palxiao's checked-in structured history."""

    def __init__(
        self,
        *,
        token: str | None = None,
        cache_path: Path | None = None,
        loader: Callable[[str], bytes] | None = None,
        api_tree_url: str = API_TREE_URL,
        raw_base_url: str = RAW_BASE_URL,
    ) -> None:
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self.cache_path = cache_path
        self.loader = loader
        self.api_tree_url = api_tree_url
        self.raw_base_url = raw_base_url.rstrip("/")
        self._dates: set[str] | None = None
        self._index_metadata: dict[str, Any] = {}

    def _headers(self, *, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "User-Agent": "bilibili-banner-archive/11",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _read_url(self, url: str, *, accept: str = "*/*") -> bytes:
        if self.loader:
            return _decode_body(self.loader(url))
        request = urllib.request.Request(url, headers=self._headers(accept=accept))
        with urllib.request.urlopen(request, timeout=45) as response:
            return _decode_body(
                response.read(),
                str(response.headers.get("Content-Encoding") or ""),
            )

    def _read_json(self, url: str) -> Any:
        return json.loads(self._read_url(url, accept="application/json").decode("utf-8-sig"))

    def _read_cache(self) -> dict[str, Any] | None:
        if not self.cache_path or not self.cache_path.is_file():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            dates = payload.get("dates")
            fetched_at = dt.datetime.fromisoformat(str(payload.get("fetchedAt")))
            age = (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds()
            ttl = int(os.environ.get("PALXIAO_INDEX_CACHE_TTL_SECONDS", "86400"))
            if isinstance(dates, list) and age <= max(0, ttl):
                self._dates = {str(item) for item in dates}
                self._index_metadata = payload
                return payload
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return None

    def _write_cache(self, payload: dict[str, Any]) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def discover_dates(self, *, force: bool = False) -> list[str]:
        if not force and self._dates is not None:
            return sorted(self._dates)
        if not force and self._read_cache():
            return sorted(self._dates or ())

        payload = self._read_json(self.api_tree_url)
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if not isinstance(tree, list):
            raise RuntimeError("palxiao GitHub tree response has no tree list")
        dates: set[str] = set()
        for item in tree:
            if not isinstance(item, dict) or item.get("type") not in (None, "blob"):
                continue
            match = DATE_RE.match(str(item.get("path") or ""))
            if match:
                dates.add(match.group("date"))

        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self._dates = dates
        self._index_metadata = {
            "fetchedAt": fetched_at,
            "commitSha": str(payload.get("sha") or ""),
            "dates": sorted(dates),
            "repository": REPOSITORY,
        }
        self._write_cache(self._index_metadata)
        return sorted(dates)

    def has_date(self, value: str | dt.date) -> bool:
        return _date_text(value) in set(self.discover_dates())

    def date_for_timestamp(self, timestamp: str) -> str | None:
        """Return a date only for an exact UTC calendar-day match.

        The directory date is an observation date in palxiao's repository. It
        is not an effective-from date and is never selected by proximity.
        """
        candidates = [timestamp[:8]]
        try:
            utc = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").date()
            candidates.append(utc.isoformat())
        except ValueError:
            pass
        for candidate in candidates:
            if re.fullmatch(r"\d{8}", candidate):
                candidate = dt.datetime.strptime(candidate, "%Y%m%d").date().isoformat()
            if self.has_date(candidate):
                return candidate
        return None

    def load(self, value: str | dt.date) -> HistoricalResult:
        date = _date_text(value)
        if not self.has_date(date):
            raise LookupError(f"palxiao has no Banner data for {date}")
        data_url = f"{self.raw_base_url}/assets/{date}/data.json"
        payload = self._read_json(data_url)
        raw_layers = payload if isinstance(payload, list) else payload.get("layers", [])
        if not isinstance(raw_layers, list):
            raise RuntimeError(f"palxiao data.json for {date} has no layer list")

        layers: list[dict[str, Any]] = []
        missing: list[str] = []
        for index, raw_layer in enumerate(raw_layers):
            if not isinstance(raw_layer, dict):
                missing.append(f"layer_{index:03d}: invalid source object")
                continue
            raw_src = str(raw_layer.get("src") or "")
            asset_url = resolve_asset_source(
                raw_src,
                date,
                raw_base_url=self.raw_base_url,
            )
            if not asset_url:
                missing.append(f"layer_{index:03d}: source missing")
                continue
            tag = str(raw_layer.get("tagName") or raw_layer.get("tag") or "img").lower()
            asset_type = "video" if header_api.infer_tag(asset_url) == "video" else "image"
            layer = {
                "index": index,
                "sourceProvider": PROVIDER_NAME,
                "sourceSrc": raw_src,
                "sourceLayer": copy.deepcopy(raw_layer),
                "src": asset_url,
                "tag": tag,
                "assetType": asset_type,
                "width": raw_layer.get("width", 0),
                "height": raw_layer.get("height", 0),
                "transform": _normalise_transform(raw_layer.get("transform")),
                "opacity": _normalise_opacity(raw_layer.get("opacity")),
                "palxiao": copy.deepcopy(raw_layer),
                "assetUrl": asset_url,
            }
            # These values are understood by palxiao's reproducer when they
            # exist, but are not guaranteed to be part of every data.json.
            # Keep their absence meaningful and preserve the full raw object
            # above instead of inventing official API defaults.
            for optional_key in ("a", "g", "f", "deg", "blur"):
                if optional_key in raw_layer:
                    layer[optional_key] = raw_layer[optional_key]
            layers.append(layer)

        return HistoricalResult(
            provider=PROVIDER_NAME,
            observed_at=date,
            source_url=data_url,
            confidence="high" if layers else "unverified",
            mode="layered" if layers else "unresolved",
            raw_metadata={
                "repository": REPOSITORY,
                "date": date,
                "dateMeaning": "observedAt: palxiao recorded/fetched this Banner on this date; effectiveFrom is unknown",
                "dataUrl": data_url,
                "index": copy.deepcopy(self._index_metadata),
                "rawLayerCount": len(raw_layers),
            },
            raw_payload=copy.deepcopy(payload),
            layers=layers,
            missing_assets=missing,
        )
