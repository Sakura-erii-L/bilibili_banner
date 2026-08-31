from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field
import gzip
import html
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

try:
    from . import capture as core
    from .providers import bilibili_header_api as header_api
except ImportError:
    import capture as core
    from providers import bilibili_header_api as header_api


ORIGINAL_PAGE = "https://www.bilibili.com/"
AVAILABILITY_API = os.environ.get(
    "WAYBACK_AVAILABILITY_API",
    "https://archive.org/wayback/available",
)
REPLAY_BASE = os.environ.get(
    "WAYBACK_REPLAY_BASE",
    "https://web.archive.org/web",
)
CDX_API = os.environ.get(
    "WAYBACK_CDX_API",
    "https://web.archive.org/cdx/search/cdx",
)
REQUEST_DELAY_SECONDS = float(os.environ.get("WAYBACK_REQUEST_DELAY", "0.2"))
RETRY_BASE_SECONDS = float(os.environ.get("WAYBACK_RETRY_BASE_SECONDS", "1.0"))
HEADER_API_MAX_DELTA_SECONDS = int(
    os.environ.get("WAYBACK_HEADER_API_MAX_DELTA_SECONDS", str(7 * 24 * 60 * 60))
)
MIN_BACKFILL_DATE = dt.date(2019, 8, 1)

BANNER_CLASS_NAMES = {
    "animated-banner",
    "bili-banner",
    "bili-header__banner",
    "head-banner",
    "header-banner",
    "banner",
    "banner_link",
    "banner-link",
}
VOID_HTML_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
CSS_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<url>.*?)(?P=quote)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
CSS_RULE_RE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)


def run_checkpoint(
    script: str,
    *,
    processed: int,
    succeeded: int,
    changed: int,
    final: bool,
) -> None:
    script_path = Path(script).resolve()
    if not script_path.is_file():
        raise RuntimeError(f"checkpoint script does not exist: {script_path}")

    env = os.environ.copy()
    env.update(
        {
            "WAYBACK_CHECKPOINT_PROCESSED": str(processed),
            "WAYBACK_CHECKPOINT_SUCCEEDED": str(succeeded),
            "WAYBACK_CHECKPOINT_CHANGED": str(changed),
            "WAYBACK_CHECKPOINT_FINAL": "1" if final else "0",
        }
    )
    subprocess.run([sys.executable, str(script_path)], check=True, env=env)


def parse_date(value: str, *, end: bool = False) -> dt.date:
    value = value.strip()
    if len(value) == 4 and value.isdigit():
        year = int(value)
        return dt.date(year, 12 if end else 1, 31 if end else 1)
    return dt.date.fromisoformat(value)


def validate_backfill_range(start: dt.date, end: dt.date) -> None:
    if start < MIN_BACKFILL_DATE:
        raise ValueError(
            f"--from-date must be on or after {MIN_BACKFILL_DATE.isoformat()}"
        )
    if end < MIN_BACKFILL_DATE:
        raise ValueError(
            f"--to-date must be on or after {MIN_BACKFILL_DATE.isoformat()}"
        )
    if start > end:
        raise ValueError("--from-date must not be after --to-date")


def validate_snapshot_timestamp(value: str) -> str:
    if len(value) != 14 or not value.isdigit():
        raise ValueError("--snapshot must use YYYYMMDDhhmmss")
    captured_date = dt.datetime.strptime(value, "%Y%m%d%H%M%S").date()
    if captured_date < MIN_BACKFILL_DATE:
        raise ValueError(
            f"--snapshot must be on or after {MIN_BACKFILL_DATE.isoformat()}"
        )
    return value


def target_dates(start: dt.date, end: dt.date, cadence: str) -> Iterable[dt.date]:
    if cadence == "monthly":
        current = start.replace(day=1)
        while current <= end:
            candidate = max(current, start)
            if candidate <= end:
                yield candidate
            current = (
                current.replace(year=current.year + 1, month=1)
                if current.month == 12
                else current.replace(month=current.month + 1)
            )
        return

    step = dt.timedelta(days=1 if cadence == "daily" else 7)
    current = start
    while current <= end:
        yield current
        current += step


def _decode_http_body(body: bytes, content_encoding: str = "") -> bytes:
    if "gzip" in content_encoding.lower() or body.startswith(b"\x1f\x8b"):
        return gzip.decompress(body)
    return body


def read_bytes(
    url: str,
    *,
    timeout: int = 45,
    attempts: int = 3,
    accept: str = "*/*",
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Accept-Encoding": "identity",
            "User-Agent": core.USER_AGENT,
        },
    )
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                content_encoding = str(
                    response.headers.get("Content-Encoding") or ""
                )
                return _decode_http_body(body, content_encoding)
        except Exception as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(RETRY_BASE_SECONDS * (2**attempt))
    raise RuntimeError(f"Wayback HTTP request failed after {attempts} attempts: {error}")


def read_text(
    url: str,
    *,
    timeout: int = 45,
    attempts: int = 3,
    accept: str = "text/html, text/css, */*",
) -> str:
    return read_bytes(
        url,
        timeout=timeout,
        attempts=attempts,
        accept=accept,
    ).decode("utf-8-sig")


def read_json(url: str, *, timeout: int = 45, attempts: int = 3) -> Any:
    return json.loads(
        read_text(
            url,
            timeout=timeout,
            attempts=attempts,
            accept="application/json, text/plain, */*",
        )
    )


def availability_url(target: dt.date, api_url: str) -> str:
    query = urllib.parse.urlencode(
        {
            "url": ORIGINAL_PAGE,
            "timestamp": target.strftime("%Y%m%d120000"),
        }
    )
    return f"{api_url}?{query}"


def discover_snapshots(
    start: dt.date,
    end: dt.date,
    *,
    cadence: str,
    api_url: str,
) -> list[dict[str, str]]:
    snapshots: dict[str, dict[str, str]] = {}
    for target in target_dates(start, end, cadence):
        try:
            payload = read_json(availability_url(target, api_url))
        except Exception as exc:
            print(f"Wayback discovery failed near {target.isoformat()}: {exc}")
            continue
        closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
        timestamp = str(closest.get("timestamp") or "")
        if not closest.get("available") or len(timestamp) != 14:
            print(f"No snapshot near {target.isoformat()}")
            continue

        captured_date = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").date()
        if not start <= captured_date <= end:
            print(
                f"Skipped out-of-range snapshot {timestamp} "
                f"for target {target.isoformat()}"
            )
            continue

        snapshots[timestamp] = {
            "timestamp": timestamp,
            "original": ORIGINAL_PAGE,
            "availabilityUrl": availability_url(target, api_url),
        }
        print(f"Discovered {timestamp} near {target.isoformat()}")
        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    return [snapshots[key] for key in sorted(snapshots)]


def imported_wayback_timestamps() -> set[str]:
    timestamps: set[str] = set()
    for _, manifest in core.iter_archive_manifests():
        for observation in core.manifest_observations(manifest):
            source = observation.get("source") or {}
            timestamp = str(source.get("waybackTimestamp") or "")
            if len(timestamp) == 14 and timestamp.isdigit():
                timestamps.add(timestamp)
    return timestamps


def snapshot_moment(timestamp: str) -> dt.datetime:
    utc = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=dt.timezone.utc
    )
    return utc.astimezone(ZoneInfo(core.TIMEZONE))


def has_saved_primary_assets(
    folder: Path,
    *,
    mode: str,
    static: dict[str, Any] | None,
    layers: list[dict[str, Any]],
) -> bool:
    entries = layers if mode == "split" else [static or {}]
    for entry in entries:
        relative = str(entry.get("file") or "")
        path = folder / relative if relative else None
        if path and path.is_file() and path.stat().st_size > 0:
            return True
    return False


def replay_url(timestamp: str, original: str, replay_base: str) -> str:
    return f"{replay_base.rstrip('/')}/{timestamp}/{original}"


def raw_replay_url(timestamp: str, original: str, replay_base: str) -> str:
    return f"{replay_base.rstrip('/')}/{timestamp}id_/{original}"


def page_replay_candidates(
    timestamp: str,
    original: str,
    replay_base: str,
) -> list[str]:
    return list(
        dict.fromkeys(
            [
                raw_replay_url(timestamp, original, replay_base),
                replay_url(timestamp, original, replay_base),
            ]
        )
    )


def archived_asset_url(timestamp: str, src: str, replay_base: str) -> str:
    if src.startswith(("blob:", "data:")):
        return src
    absolute = urllib.parse.urljoin(ORIGINAL_PAGE, src)
    host = (urllib.parse.urlparse(absolute).hostname or "").lower()
    if host in {"web.archive.org", "wayback.archive-it.org"}:
        return absolute
    return f"{replay_base.rstrip('/')}/{timestamp}id_/{absolute}"


def original_url_from_replay(src: str) -> str:
    parsed = urllib.parse.urlparse(src)
    if (parsed.hostname or "").lower() not in {"web.archive.org", "wayback.archive-it.org"}:
        return src
    match = re.search(r"/web/\d+(?:[a-z_]+)?/(https?://.+)$", src, re.I)
    return urllib.parse.unquote(match.group(1)) if match else src


def archived_asset_candidates(timestamp: str, src: str, replay_base: str) -> list[str]:
    original = original_url_from_replay(src)
    candidates = [archived_asset_url(timestamp, original, replay_base)]
    host = (urllib.parse.urlparse(original).hostname or "").lower()
    if host.endswith((".hdslb.com", ".bilibili.com", ".bilivideo.com")):
        candidates.append(original)
    return list(dict.fromkeys(candidates))


def is_direct_bilibili_request(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "hdslb.com"
        or host.endswith(".hdslb.com")
        or host == "bilivideo.com"
        or host.endswith(".bilivideo.com")
    )


def _timestamp_seconds(timestamp: str) -> int:
    return int(
        dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        .replace(tzinfo=dt.timezone.utc)
        .timestamp()
    )


def cdx_query_url(
    endpoint: str,
    homepage_timestamp: str,
    *,
    cdx_api: str = CDX_API,
    max_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> str:
    center = _timestamp_seconds(homepage_timestamp)
    start = dt.datetime.fromtimestamp(
        center - max_delta_seconds,
        tz=dt.timezone.utc,
    ).strftime("%Y%m%d%H%M%S")
    end = dt.datetime.fromtimestamp(
        center + max_delta_seconds,
        tz=dt.timezone.utc,
    ).strftime("%Y%m%d%H%M%S")
    query = urllib.parse.urlencode(
        [
            ("url", endpoint),
            ("from", start),
            ("to", end),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,mimetype,digest"),
            ("filter", "statuscode:200"),
            ("matchType", "exact"),
        ]
    )
    return f"{cdx_api.rstrip('?')}?{query}"


def parse_cdx_rows(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict):
        payload = payload.get("captures") or payload.get("rows") or []
    if not isinstance(payload, list) or not payload:
        return []

    first = payload[0]
    if isinstance(first, list):
        headers = [str(value) for value in first]
        rows = payload[1:]
        parsed: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            parsed.append(
                {
                    headers[index]: str(value or "")
                    for index, value in enumerate(row)
                    if index < len(headers)
                }
            )
        return parsed

    return [
        {str(key): str(value or "") for key, value in row.items()}
        for row in payload
        if isinstance(row, dict)
    ]


def _same_original_url(left: str, right: str) -> bool:
    left_parsed = urllib.parse.urlsplit(left)
    right_parsed = urllib.parse.urlsplit(right)
    return (
        left_parsed.scheme.lower(),
        left_parsed.netloc.lower(),
        left_parsed.path,
        left_parsed.query,
        left_parsed.fragment,
    ) == (
        right_parsed.scheme.lower(),
        right_parsed.netloc.lower(),
        right_parsed.path,
        right_parsed.query,
        right_parsed.fragment,
    )


def query_cdx_snapshots(
    endpoint: str,
    homepage_timestamp: str,
    *,
    cdx_api: str = CDX_API,
    max_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> list[dict[str, Any]]:
    query_url = cdx_query_url(
        endpoint,
        homepage_timestamp,
        cdx_api=cdx_api,
        max_delta_seconds=max_delta_seconds,
    )
    rows = parse_cdx_rows(read_json(query_url))
    homepage_seconds = _timestamp_seconds(homepage_timestamp)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        timestamp = str(row.get("timestamp") or "")
        if len(timestamp) != 14 or not timestamp.isdigit():
            continue
        if str(row.get("statuscode") or "200") != "200":
            continue
        if not _same_original_url(str(row.get("original") or endpoint), endpoint):
            continue
        try:
            delta = abs(_timestamp_seconds(timestamp) - homepage_seconds)
        except ValueError:
            continue
        if delta > max_delta_seconds:
            continue
        candidates.append(
            {
                "timestamp": timestamp,
                "original": endpoint,
                "statuscode": str(row.get("statuscode") or "200"),
                "mimetype": str(row.get("mimetype") or ""),
                "digest": str(row.get("digest") or ""),
                "deltaSeconds": delta,
                "cdxUrl": query_url,
            }
        )
    return sorted(
        candidates,
        key=lambda item: (int(item["deltaSeconds"]), str(item["timestamp"])),
    )


def fetch_archived_header_api(
    timestamp: str,
    replay_base: str,
    *,
    cdx_api: str = CDX_API,
    max_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Find and read the nearest successful archived Header API response."""
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    for endpoint in header_api.DEFAULT_ENDPOINTS:
        try:
            candidates.extend(
                query_cdx_snapshots(
                    endpoint,
                    timestamp,
                    cdx_api=cdx_api,
                    max_delta_seconds=max_delta_seconds,
                )
            )
        except Exception as exc:
            errors.append(f"CDX {endpoint}: {exc}")

    candidates.sort(key=lambda item: (int(item["deltaSeconds"]), item["timestamp"]))
    for candidate in candidates:
        endpoint = str(candidate["original"])
        api_timestamp = str(candidate["timestamp"])
        replay = archived_asset_url(api_timestamp, endpoint, replay_base)
        try:
            payload = read_json(replay, attempts=2)
            if not isinstance(payload, dict):
                raise RuntimeError("archived Header API payload is not an object")
            if payload.get("code") not in (None, 0):
                raise RuntimeError(
                    f"archived Header API returned code={payload.get('code')}"
                )
            if not isinstance(payload.get("data"), dict):
                raise RuntimeError("archived Header API payload has no data object")
            parsed = header_api.parse_header_api(payload, endpoint)
            metadata = {
                "homepageWaybackTimestamp": timestamp,
                "headerApiWaybackTimestamp": api_timestamp,
                "headerApiTimeDeltaSeconds": int(candidate["deltaSeconds"]),
                "headerApiWaybackReplay": replay,
                "headerApiCdxUrl": str(candidate.get("cdxUrl") or ""),
            }
            return parsed, replay, metadata
        except Exception as exc:
            errors.append(f"{endpoint} at {api_timestamp}: {exc}")

    if candidates:
        reason = "; ".join(errors)
        raise RuntimeError(
            "no successful archived Header API snapshot within "
            f"{max_delta_seconds} seconds of homepage {timestamp}"
            + (f": {reason}" if reason else "")
        )
    raise RuntimeError(
        "no archived Header API snapshot within "
        f"{max_delta_seconds} seconds of homepage {timestamp}"
        + (f": {'; '.join(errors)}" if errors else "")
    )


def capture_snapshot_api(
    snapshot: dict[str, str],
    *,
    replay_base: str,
    force: bool,
    cdx_api: str = CDX_API,
    max_header_api_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    moment = snapshot_moment(timestamp)
    api_data, api_replay, api_match = fetch_archived_header_api(
        timestamp,
        replay_base,
        cdx_api=cdx_api,
        max_delta_seconds=max_header_api_delta_seconds,
    )
    print(
        f"Wayback Header API {timestamp}: "
        f"split={api_data.get('is_split_layer')}, "
        f"layers={len(api_data.get('layers') or [])}"
    )
    result = core.capture_header_api_payload(
        api_data,
        moment=moment,
        force=force,
        update_current=False,
        record_observation=True,
        source_extra={
            "captureMethod": "wayback-header-api",
            "waybackTimestamp": timestamp,
            "waybackReplay": api_replay,
            "availabilityUrl": snapshot.get("availabilityUrl"),
            **api_match,
        },
        asset_url_candidates=lambda src: archived_asset_candidates(
            str(api_match["headerApiWaybackTimestamp"]),
            src,
            replay_base,
        ),
        referer=api_replay,
    )
    return {
        "timestamp": timestamp,
        "status": result["status"],
        "contentHash": result["contentHash"],
        "archive": str(result["archive"]),
        "captureMethod": "wayback-header-api",
        **api_match,
    }


@dataclass
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    parent: "_HTMLNode | None" = None
    children: list["_HTMLNode"] = field(default_factory=list)


class _BannerHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = _HTMLNode("#document", {})
        self.stack: list[_HTMLNode] = [self.document]
        self.style_blocks: list[str] = []
        self.stylesheet_urls: list[str] = []

    def _add_node(self, tag: str, attrs: list[tuple[str, str | None]]) -> _HTMLNode:
        normalized = {str(key).lower(): str(value or "") for key, value in attrs}
        node = _HTMLNode(tag.lower(), normalized, self.stack[-1])
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add_node(tag, attrs)
        if node.tag == "style":
            self.style_blocks.append("")
        if node.tag == "link":
            rel = set(node.attrs.get("rel", "").lower().split())
            href = node.attrs.get("href", "").strip()
            if href and "stylesheet" in rel:
                self.stylesheet_urls.append(href)
        if node.tag not in VOID_HTML_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add_node(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if any(node.tag == "style" for node in self.stack) and self.style_blocks:
            self.style_blocks[-1] += data


def _walk_nodes(node: _HTMLNode) -> Iterable[_HTMLNode]:
    for child in node.children:
        yield child
        yield from _walk_nodes(child)


def _class_tokens(node: _HTMLNode) -> set[str]:
    return {item.lower() for item in node.attrs.get("class", "").split() if item}


def _is_banner_node(node: _HTMLNode) -> bool:
    node_id = node.attrs.get("id", "").lower()
    return node_id in {"banner_link", "banner-link"} or bool(
        _class_tokens(node) & BANNER_CLASS_NAMES
    )


def _is_layer_node(node: _HTMLNode) -> bool:
    return "layer" in _class_tokens(node)


def _has_ancestor(node: _HTMLNode, predicate: Callable[[_HTMLNode], bool]) -> bool:
    current = node.parent
    while current is not None:
        if predicate(current):
            return True
        current = current.parent
    return False


def _banner_roots(parser: _BannerHTMLParser) -> list[_HTMLNode]:
    return [
        node
        for node in _walk_nodes(parser.document)
        if _is_banner_node(node) and not _has_ancestor(node, _is_banner_node)
    ]


def _first_srcset(value: str) -> str:
    for item in value.split(","):
        candidate = item.strip().split()
        if candidate:
            return candidate[0]
    return ""


def _resolve_original_url(value: str, base_url: str) -> str:
    value = html.unescape(str(value or "")).strip().strip("'\"")
    if not value or value.startswith(("data:", "blob:", "javascript:", "#")):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    resolved = urllib.parse.urljoin(base_url, value)
    return original_url_from_replay(resolved)


def _record_source(
    value: str,
    *,
    tag: str,
    source_kind: str,
    base_url: str,
) -> dict[str, str] | None:
    src = _resolve_original_url(value, base_url)
    if not src:
        return None
    return {
        "src": src,
        "tag": tag or header_api.infer_tag(src),
        "sourceKind": source_kind,
    }


def _dedupe_source_records(records: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        src = str(record.get("src") or "")
        identity = header_api.normalized_identity(src) if src else ""
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(record)
    return result


def _css_urls(value: str) -> list[str]:
    return [match.group("url").strip() for match in CSS_URL_RE.finditer(value)]


def _selector_has_token(selector: str, token: str) -> bool:
    return re.search(
        rf"(?<![\w-]){re.escape(token.lower())}(?![\w-])",
        selector.lower(),
    ) is not None


def _selector_is_banner_related(selector: str) -> bool:
    return any(_selector_has_token(selector, token) for token in BANNER_CLASS_NAMES) or (
        _selector_has_token(selector, "layer")
    )


def _parse_relevant_css(
    css_text: str,
    *,
    base_url: str,
) -> dict[str, Any]:
    records: list[dict[str, str]] = []
    layer_records: list[dict[str, str]] = []
    animation = False
    for match in CSS_RULE_RE.finditer(css_text):
        selectors = match.group("selectors").strip()
        body = match.group("body")
        if not _selector_is_banner_related(selectors):
            continue
        urls = _css_urls(body)
        if not urls:
            continue
        if re.search(r"(?:animation|transition|@keyframes)", body, re.IGNORECASE):
            animation = True
        target = layer_records if _selector_has_token(selectors, "layer") else records
        for value in urls:
            record = _record_source(
                value,
                tag=header_api.infer_tag(value),
                source_kind="css",
                base_url=base_url,
            )
            if record:
                target.append(record)
    return {
        "records": _dedupe_source_records(records),
        "layerRecords": _dedupe_source_records(layer_records),
        "animation": animation,
    }


def _node_visual_sources(node: _HTMLNode, *, base_url: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    nodes = [node, *_walk_nodes(node)]
    picture_nodes: set[int] = set()
    for candidate in nodes:
        if candidate.tag == "picture":
            picture_nodes.add(id(candidate))
            picture_sources = [
                child
                for child in _walk_nodes(candidate)
                if child.tag in {"source", "img"}
            ]
            for image in picture_sources:
                value = image.attrs.get("src") or _first_srcset(image.attrs.get("srcset", ""))
                record = _record_source(
                    value,
                    tag="img",
                    source_kind="html-picture",
                    base_url=base_url,
                )
                if record:
                    records.append(record)
                    break
        if candidate.tag in {"img", "source"} and _has_ancestor(
            candidate, lambda item: item.tag == "picture"
        ):
            continue
        if candidate.tag == "img":
            value = candidate.attrs.get("src") or _first_srcset(candidate.attrs.get("srcset", ""))
            record = _record_source(
                value,
                tag="img",
                source_kind="html-img",
                base_url=base_url,
            )
            if record:
                records.append(record)
        elif candidate.tag == "source":
            value = candidate.attrs.get("src") or _first_srcset(candidate.attrs.get("srcset", ""))
            record = _record_source(
                value,
                tag=header_api.infer_tag(value),
                source_kind="html-source",
                base_url=base_url,
            )
            if record:
                records.append(record)
        elif candidate.tag == "video":
            value = candidate.attrs.get("poster") or candidate.attrs.get("src")
            record = _record_source(
                value,
                tag="video",
                source_kind="html-video",
                base_url=base_url,
            )
            if record:
                records.append(record)
        inline = candidate.attrs.get("style", "")
        for value in _css_urls(inline):
            record = _record_source(
                value,
                tag=header_api.infer_tag(value),
                source_kind="html-style",
                base_url=base_url,
            )
            if record:
                records.append(record)
    return _dedupe_source_records(records)


def parse_banner_resources(
    html_text: str,
    *,
    page_url: str = ORIGINAL_PAGE,
    css_texts: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    parser = _BannerHTMLParser()
    parser.feed(html_text)
    parser.close()
    roots = _banner_roots(parser)
    if not roots:
        raise RuntimeError("no supported Banner container in archived HTML")

    root = roots[0]
    root_records = _node_visual_sources(root, base_url=page_url)
    css_records: list[dict[str, str]] = []
    css_layer_records: list[dict[str, str]] = []
    css_animation = False
    for css in parser.style_blocks:
        parsed = _parse_relevant_css(css, base_url=page_url)
        css_records.extend(parsed["records"])
        css_layer_records.extend(parsed["layerRecords"])
        css_animation = css_animation or bool(parsed["animation"])
    for stylesheet_url, css in css_texts:
        parsed = _parse_relevant_css(css, base_url=stylesheet_url)
        css_records.extend(parsed["records"])
        css_layer_records.extend(parsed["layerRecords"])
        css_animation = css_animation or bool(parsed["animation"])
    css_records = _dedupe_source_records(css_records)
    css_layer_records = _dedupe_source_records(css_layer_records)

    layer_nodes = [
        node
        for node in [root, *_walk_nodes(root)]
        if _is_layer_node(node) and _has_ancestor(node, _is_banner_node)
    ]
    layer_groups = [
        _node_visual_sources(node, base_url=page_url)
        for node in layer_nodes
    ]
    css_layer_index = 0
    for group in layer_groups:
        if group:
            continue
        if css_layer_index < len(css_layer_records):
            group.append(css_layer_records[css_layer_index])
            css_layer_index += 1
    layer_groups = [_dedupe_source_records(group) for group in layer_groups]
    layered = len(layer_groups) >= 2 and all(layer_groups)

    if layered:
        visual_records = _dedupe_source_records(
            record for group in layer_groups for record in group
        )
        mode = "split"
    else:
        visual_records = _dedupe_source_records([*root_records, *css_records])
        mode = "static" if len(visual_records) == 1 else "ambiguous"

    root_info = {
        "tag": root.tag,
        "id": root.attrs.get("id", ""),
        "className": root.attrs.get("class", ""),
    }
    media = [
        {
            "tag": record.get("tag") or "img",
            "src": record.get("src") or "",
            "sourceKind": record.get("sourceKind") or "html",
        }
        for record in visual_records
    ]
    return {
        "mode": mode,
        "root": root_info,
        "visualRecords": visual_records,
        "layerGroups": layer_groups,
        "stylesheetUrls": [
            _resolve_original_url(value, page_url)
            for value in parser.stylesheet_urls
            if _resolve_original_url(value, page_url)
        ],
        "media": media,
        "evidence": {
            "root": {"source": "wayback-http-html-css", **root_info},
            "layerCount": len(layer_groups) if layered else 0,
            "mediaCount": len(media),
            "visibleMediaCount": len(layer_groups) if layered else (1 if mode == "static" else 0),
            "media": media,
            "resourceUrls": [
                {"name": item["src"], "initiatorType": item["sourceKind"]}
                for item in visual_records
            ],
            "stylesheetUrls": [
                _resolve_original_url(value, page_url)
                for value in parser.stylesheet_urls
                if _resolve_original_url(value, page_url)
            ],
            "scriptUrls": [],
            "animationCss": "" if not css_animation else "archived CSS animation detected",
            "signals": {
                "isSplitLayer": bool(layered),
                "hasVideo": any(item["tag"] == "video" for item in visual_records),
                "hasCanvas": False,
                "hasSvg": any(item["tag"] == "svg" for item in visual_records),
                "hasSvgAnimation": False,
                "hasCssAnimation": css_animation,
                "hasInteraction": False,
                "hasDynamicSource": any(item["tag"] == "video" for item in visual_records),
            },
        },
    }


def fetch_archived_page(
    snapshot: dict[str, str],
    *,
    replay_base: str,
) -> tuple[str, str]:
    timestamp = snapshot["timestamp"]
    original = snapshot.get("original") or ORIGINAL_PAGE
    errors: list[str] = []
    for replay in page_replay_candidates(timestamp, original, replay_base):
        try:
            text = read_text(replay, timeout=60, attempts=3)
            if not text.strip():
                raise RuntimeError("empty archived HTML response")
            return text, replay
        except Exception as exc:
            errors.append(f"{replay}: {exc}")
    raise RuntimeError(
        f"archived HTML unavailable for {timestamp}: " + "; ".join(errors)
    )


def _download_archived_asset(
    src: str,
    folder: Path,
    *,
    timestamp: str,
    replay_base: str,
    stem: str,
    tag: str,
    referer: str,
) -> dict[str, Any]:
    original = _resolve_original_url(src, ORIGINAL_PAGE)
    if not original:
        raise ValueError("empty archived asset URL")
    errors: list[str] = []
    candidates = archived_asset_candidates(timestamp, original, replay_base)
    for attempt in range(3):
        for candidate in candidates:
            try:
                item = core._download_http_asset(
                    candidate,
                    folder,
                    stem,
                    referer=referer,
                )
                item["src"] = original
                item["requestedSrc"] = candidate
                item["assetType"] = core.media_type(
                    tag or item.get("tag") or "img",
                    candidate,
                    str(item.get("contentType") or ""),
                )
                return item
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
        if attempt < 2:
            time.sleep(RETRY_BASE_SECONDS * (2**attempt))
    raise RuntimeError("; ".join(errors))


def _write_http_sources(
    temp: Path,
    html_text: str,
    css_texts: list[tuple[str, str]],
) -> dict[str, str]:
    source_dir = temp / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_files: dict[str, str] = {}
    page_path = source_dir / "page.html"
    page_path.write_text(html_text, encoding="utf-8")
    source_files["page"] = "source/page.html"
    for index, (_url, css) in enumerate(css_texts):
        path = source_dir / f"stylesheet_{index:02d}.css"
        path.write_text(css, encoding="utf-8")
        source_files[f"stylesheet{index:02d}"] = f"source/{path.name}"
    return source_files


def capture_snapshot_http(
    snapshot: dict[str, str],
    *,
    replay_base: str,
    force: bool,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    moment = snapshot_moment(timestamp)
    html_text, page_replay = fetch_archived_page(snapshot, replay_base=replay_base)
    parser = _BannerHTMLParser()
    parser.feed(html_text)
    parser.close()
    css_texts: list[tuple[str, str]] = []
    css_errors: list[str] = []
    for stylesheet in parser.stylesheet_urls:
        original_css = _resolve_original_url(stylesheet, snapshot.get("original") or ORIGINAL_PAGE)
        if not original_css:
            continue
        errors: list[str] = []
        for candidate in archived_asset_candidates(timestamp, original_css, replay_base):
            try:
                css_texts.append(
                    (
                        original_css,
                        read_text(candidate, timeout=45, attempts=3, accept="text/css, */*"),
                    )
                )
                break
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
        else:
            css_errors.append(f"stylesheet {original_css}: {'; '.join(errors)}")

    parsed = parse_banner_resources(
        html_text,
        page_url=snapshot.get("original") or ORIGINAL_PAGE,
        css_texts=css_texts,
    )
    if parsed["mode"] == "ambiguous":
        raise RuntimeError(
            "multiple relevant Banner visual resources without strong layered evidence"
        )

    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".wayback_http_", dir=core.DATA_DIR))
    try:
        source_files = _write_http_sources(temp, html_text, css_texts)
        missing_assets = list(css_errors)
        layers: list[dict[str, Any]] = []
        static: dict[str, Any] | None = None
        if parsed["mode"] == "static":
            record = parsed["visualRecords"][0]
            try:
                static = _download_archived_asset(
                    record["src"],
                    temp,
                    timestamp=timestamp,
                    replay_base=replay_base,
                    stem="static",
                    tag=record.get("tag") or "img",
                    referer=page_replay,
                )
            except Exception as exc:
                missing_assets.append(f"static: {exc}")
        else:
            for index, group in enumerate(parsed["layerGroups"]):
                resources: list[dict[str, Any]] = []
                for resource_index, record in enumerate(group):
                    try:
                        item = _download_archived_asset(
                            record["src"],
                            temp,
                            timestamp=timestamp,
                            replay_base=replay_base,
                            stem=f"layer_{index:02d}_{resource_index:02d}",
                            tag=record.get("tag") or "img",
                            referer=page_replay,
                        )
                        resources.append(
                            {
                                **item,
                                "resourceIndex": resource_index,
                            }
                        )
                    except Exception as exc:
                        missing_assets.append(
                            f"layer_{index:03d}_resource_{resource_index:03d}: {exc}"
                        )
                if not resources:
                    missing_assets.append(f"layer_{index:03d}: no resource saved")
                    continue
                first = resources[0]
                layers.append(
                    {
                        "index": index,
                        "tag": first.get("tag") or "img",
                        "assetType": first.get("assetType") or "image",
                        "src": first.get("src") or "",
                        "file": first.get("file") or "",
                        "contentType": first.get("contentType") or "",
                        "resources": resources,
                        "width": 0,
                        "height": 0,
                        "naturalWidth": 0,
                        "naturalHeight": 0,
                        "objectFit": "fill",
                        "objectPosition": "50% 50%",
                        "transformOrigin": "50% 50%",
                        "transform": [1, 0, 0, 1, 0, 0],
                        "opacity": [1, 1],
                        "position": {},
                        "zIndex": index,
                        "animation": {},
                        "animationTarget": "media",
                        "motion": None,
                        "a": 0,
                        "captureTargetTag": first.get("tag") or "img",
                    }
                )

        mode = "split" if parsed["mode"] == "split" else "static"
        if not has_saved_primary_assets(
            temp,
            mode=mode,
            static=static,
            layers=layers,
        ):
            raise RuntimeError("no downloadable archived Banner asset found")

        captured_at = moment.isoformat(timespec="seconds")
        manifest: dict[str, Any] = {
            "version": 10.1,
            "capturedAt": captured_at,
            "date": moment.strftime("%Y-%m-%d"),
            "season": core.season_of(moment.month),
            "source": {
                "page": ORIGINAL_PAGE,
                "resolvedUrl": page_replay,
                "captureMethod": "wayback-http-html-css",
                "waybackTimestamp": timestamp,
                "homepageWaybackTimestamp": timestamp,
                "waybackReplay": page_replay,
                "availabilityUrl": snapshot.get("availabilityUrl"),
            },
            "viewport": core.VIEWPORT,
            "banner": {},
            "mode": mode,
            "static": static,
            "layers": layers,
            "interaction": {
                "model": "none",
                "positionAxis": "observed",
                "effects": [],
            },
            "sourceFiles": source_files,
            "timeZone": core.TIMEZONE,
            "lastObservedAt": captured_at,
        }
        core.enrich_manifest_metadata(
            manifest,
            parsed["evidence"],
            missing_assets=missing_assets,
        )
        result = core.archive_capture(
            temp,
            manifest,
            moment=moment,
            force=force,
            update_current=False,
            record_observation=True,
        )
        return {
            "timestamp": timestamp,
            "status": result["status"],
            "contentHash": result["contentHash"],
            "archive": str(result["archive"]),
            "captureMethod": "wayback-http-html-css",
        }
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def verify_snapshot_dom(
    snapshot: dict[str, str],
    *,
    replay_base: str,
) -> dict[str, Any]:
    """Optional browser verification. It never performs capture or screenshots."""
    timestamp = snapshot["timestamp"]
    original = snapshot.get("original") or ORIGINAL_PAGE
    errors: list[str] = []
    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        system_browser = core.find_system_browser()
        if system_browser:
            launch_kwargs["executable_path"] = system_browser
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport=core.VIEWPORT,
            user_agent=core.USER_AGENT,
            locale="zh-CN",
        )

        def route_request(route) -> None:
            if is_direct_bilibili_request(route.request.url):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            for url in page_replay_candidates(timestamp, original, replay_base):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    return {
                        "status": "ok",
                        "url": page.url,
                        "bannerCount": page.locator(
                            ".bili-banner, .head-banner, .banner, #banner_link, "
                            ".banner_link, .animated-banner"
                        ).count(),
                    }
                except Exception as exc:
                    errors.append(f"{url}: {exc}")
        finally:
            context.close()
            browser.close()
    raise RuntimeError("Wayback DOM verification failed: " + "; ".join(errors))


def capture_snapshot(
    snapshot: dict[str, str],
    *,
    replay_base: str,
    force: bool,
    cdx_api: str = CDX_API,
    max_header_api_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
    verify_dom: bool = False,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    try:
        result = capture_snapshot_api(
            snapshot,
            replay_base=replay_base,
            force=force,
            cdx_api=cdx_api,
            max_header_api_delta_seconds=max_header_api_delta_seconds,
        )
    except Exception as api_exc:
        print(
            f"Archived Header API unavailable for {timestamp}: {api_exc}. "
            "Falling back to HTTP HTML/CSS recovery."
        )
        result = capture_snapshot_http(
            snapshot,
            replay_base=replay_base,
            force=force,
        )

    if verify_dom:
        try:
            result["domVerification"] = verify_snapshot_dom(
                snapshot,
                replay_base=replay_base,
            )
        except Exception as exc:
            print(f"Wayback DOM verification failed for {timestamp}: {exc}")
            result["domVerification"] = {"status": "failed", "error": str(exc)}
    return result


def main() -> None:
    today = dt.datetime.now(ZoneInfo(core.TIMEZONE)).date()
    parser = argparse.ArgumentParser(
        description=(
            "Import Bilibili Banner assets from Wayback. Archived Header API JSON "
            "is matched through CDX first; HTTP HTML/CSS recovery is the fallback. "
            "Playwright is used only with --verify-dom. No screenshots are created."
        )
    )
    parser.add_argument("--from-date", default=MIN_BACKFILL_DATE.isoformat())
    parser.add_argument("--to-date", default=today.isoformat())
    parser.add_argument(
        "--cadence",
        choices=("monthly", "weekly", "daily"),
        default="monthly",
    )
    parser.add_argument("--snapshot", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="Run --checkpoint-script after this many created/updated records.",
    )
    parser.add_argument(
        "--checkpoint-script",
        help="Python script called at each checkpoint and once at the end.",
    )
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--availability-api", default=AVAILABILITY_API)
    parser.add_argument("--cdx-api", default=CDX_API)
    parser.add_argument("--replay-base", default=REPLAY_BASE)
    parser.add_argument(
        "--header-api-max-delta-seconds",
        type=int,
        default=HEADER_API_MAX_DELTA_SECONDS,
        help="Maximum allowed time difference between homepage and Header API snapshots.",
    )
    parser.add_argument(
        "--verify-dom",
        action="store_true",
        help="Optionally verify the archived page with a browser after HTTP recovery.",
    )
    args = parser.parse_args()

    if args.checkpoint_every < 0:
        parser.error("--checkpoint-every must not be negative")
    if bool(args.checkpoint_every) != bool(args.checkpoint_script):
        parser.error(
            "--checkpoint-every and --checkpoint-script must be used together"
        )
    if args.header_api_max_delta_seconds < 0:
        parser.error("--header-api-max-delta-seconds must not be negative")

    start = parse_date(args.from_date)
    end = parse_date(args.to_date, end=True)
    try:
        validate_backfill_range(start, end)
    except ValueError as exc:
        parser.error(str(exc))

    if args.snapshot:
        try:
            snapshot_values = [
                validate_snapshot_timestamp(value)
                for value in sorted(set(args.snapshot))
            ]
        except ValueError as exc:
            parser.error(str(exc))
        snapshots = [
            {"timestamp": value, "original": ORIGINAL_PAGE, "availabilityUrl": ""}
            for value in snapshot_values
        ]
    else:
        snapshots = discover_snapshots(
            start,
            end,
            cadence=args.cadence,
            api_url=args.availability_api,
        )

    if args.limit > 0:
        snapshots = snapshots[: args.limit]
    skipped_known = 0
    if not args.force:
        known_timestamps = imported_wayback_timestamps()
        skipped_known = sum(
            snapshot["timestamp"] in known_timestamps for snapshot in snapshots
        )
        snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot["timestamp"] not in known_timestamps
        ]
        if skipped_known:
            print(f"Skipped {skipped_known} already imported Wayback snapshots.")
    print(json.dumps({"snapshotCount": len(snapshots)}, ensure_ascii=False))
    if args.discovery_only:
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return
    if not snapshots:
        if skipped_known:
            print("All discovered Wayback snapshots were already imported.")
            return
        raise SystemExit("No Wayback snapshots were discovered in the requested range.")

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    changed_since_checkpoint = 0
    processed = 0
    for processed, snapshot in enumerate(snapshots, start=1):
        try:
            result = capture_snapshot(
                snapshot,
                replay_base=args.replay_base,
                force=args.force,
                cdx_api=args.cdx_api,
                max_header_api_delta_seconds=args.header_api_max_delta_seconds,
                verify_dom=args.verify_dom,
            )
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] in {"created", "updated"}:
                changed_since_checkpoint += 1
        except Exception as exc:
            failure = {
                "timestamp": snapshot["timestamp"],
                "error": str(exc),
            }
            failures.append(failure)
            print(json.dumps(failure, ensure_ascii=False))
            continue

        if (
            args.checkpoint_script
            and changed_since_checkpoint >= args.checkpoint_every
            and processed < len(snapshots)
        ):
            run_checkpoint(
                args.checkpoint_script,
                processed=processed,
                succeeded=len(results),
                changed=changed_since_checkpoint,
                final=False,
            )
            changed_since_checkpoint = 0

    if args.checkpoint_script:
        run_checkpoint(
            args.checkpoint_script,
            processed=processed,
            succeeded=len(results),
            changed=changed_since_checkpoint,
            final=True,
        )

    summary = {
        "requested": len(snapshots),
        "succeeded": len(results),
        "created": sum(item["status"] == "created" for item in results),
        "updated": sum(item["status"] == "updated" for item in results),
        "unchanged": sum(item["status"] == "unchanged" for item in results),
        "failed": len(failures),
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
