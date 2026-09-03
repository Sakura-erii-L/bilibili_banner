from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

try:
    from . import capture as core
    from .providers import bilibili_header_api as header_api
    from .providers.history import HistoricalResult
    from .providers.palxiao_history import (
        PROVIDER_NAME as PALXIAO_PROVIDER_NAME,
        PalxiaoHistoryProvider,
    )
    from .providers.mikufan039_reference import (
        DEFAULT_COMMIT as MIKUFAN039_REFERENCE_COMMIT,
        ReferenceRepository,
    )
except ImportError:
    import capture as core
    from providers import bilibili_header_api as header_api
    from providers.history import HistoricalResult
    from providers.palxiao_history import (
        PROVIDER_NAME as PALXIAO_PROVIDER_NAME,
        PalxiaoHistoryProvider,
    )
    from providers.mikufan039_reference import (
        DEFAULT_COMMIT as MIKUFAN039_REFERENCE_COMMIT,
        ReferenceRepository,
    )


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
CDX_DISCOVERY_CHUNK_DAYS = max(
    1,
    int(os.environ.get("WAYBACK_CDX_DISCOVERY_CHUNK_DAYS", "31")),
)
REQUEST_DELAY_SECONDS = float(os.environ.get("WAYBACK_REQUEST_DELAY", "0.6"))
RETRY_BASE_SECONDS = float(os.environ.get("WAYBACK_RETRY_BASE_SECONDS", "1.0"))
DEFAULT_CAPTURE_WORKERS = max(
    1,
    int(os.environ.get("WAYBACK_WORKERS", "3")),
)
HEADER_API_MAX_DELTA_SECONDS = int(
    os.environ.get("WAYBACK_HEADER_API_MAX_DELTA_SECONDS", str(7 * 24 * 60 * 60))
)
MIN_BACKFILL_DATE = dt.date(2019, 1, 1)
WAYBACK_MATCH_CACHE_VERSION = 1
WAYBACK_MATCH_CACHE_NAME = "wayback-slot-matches.json"

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
BANNER_ID_PRIORITIES = {
    "banner_link": 180,
    "banner-link": 180,
}
BANNER_CLASS_PRIORITIES = {
    "bili-header__banner": 165,
    "head-banner": 155,
    "header-banner": 145,
    "bili-banner": 140,
    "animated-banner": 135,
    "banner_link": 125,
    "banner-link": 125,
    "banner": 75,
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


@dataclass(frozen=True)
class CdxQueryStatus:
    """Classify whether CDX returned no rows or could not be queried."""

    status: str
    candidates: tuple[dict[str, Any], ...] = ()
    error: str = ""


_rate_limit_lock = threading.Lock()
_next_wayback_request_at = 0.0


def wait_for_wayback_request() -> None:
    """Apply one process-wide delay to every Wayback/CDN recovery request."""
    global _next_wayback_request_at
    interval = max(0.0, REQUEST_DELAY_SECONDS)
    with _rate_limit_lock:
        now = time.monotonic()
        wait = max(0.0, _next_wayback_request_at - now)
        _next_wayback_request_at = max(now, _next_wayback_request_at) + interval
    if wait:
        time.sleep(wait)


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

    with core.ARCHIVE_WRITE_LOCK:
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


def target_slots(start: dt.date, end: dt.date) -> Iterable[tuple[dt.date, int]]:
    current = start
    while current <= end:
        for slot in range(core.SLOT_COUNT):
            yield current, slot
        current += dt.timedelta(days=1)


def _empty_wayback_match_cache() -> dict[str, Any]:
    return {
        "version": WAYBACK_MATCH_CACHE_VERSION,
        "page": ORIGINAL_PAGE,
        "timeZone": core.TIMEZONE,
        "slotMinutes": core.SLOT_MINUTES,
        "cadence": "3h",
        "matches": {},
    }


def _wayback_match_cache_path() -> Path:
    return core.DATA_DIR / "cache" / WAYBACK_MATCH_CACHE_NAME


def _valid_wayback_timestamp(value: Any) -> str | None:
    timestamp = str(value or "")
    if len(timestamp) != 14 or not timestamp.isdigit():
        return None
    try:
        snapshot_moment(timestamp)
    except ValueError:
        return None
    return timestamp


def load_wayback_match_cache() -> dict[str, Any]:
    path = _wayback_match_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_wayback_match_cache()
    if not isinstance(payload, dict):
        return _empty_wayback_match_cache()
    if (
        payload.get("version") != WAYBACK_MATCH_CACHE_VERSION
        or payload.get("page") != ORIGINAL_PAGE
        or payload.get("timeZone") != core.TIMEZONE
        or int(payload.get("slotMinutes") or 0) != core.SLOT_MINUTES
        or payload.get("cadence") != "3h"
    ):
        return _empty_wayback_match_cache()

    normalized = _empty_wayback_match_cache()
    matches = payload.get("matches")
    if not isinstance(matches, dict):
        return normalized
    for date_text, slots in matches.items():
        try:
            dt.date.fromisoformat(str(date_text))
        except ValueError:
            continue
        if not isinstance(slots, dict):
            continue
        for slot_text, value in slots.items():
            try:
                slot = int(slot_text)
            except (TypeError, ValueError):
                continue
            if not 0 <= slot < core.SLOT_COUNT or not isinstance(value, dict):
                continue
            timestamp = _valid_wayback_timestamp(value.get("timestamp"))
            if not timestamp:
                continue
            entry = {
                "timestamp": timestamp,
                "source": str(value.get("source") or "cache"),
            }
            for field in ("distanceSeconds", "cdxUrl"):
                if field in value:
                    entry[field] = value[field]
            normalized["matches"].setdefault(str(date_text), {})[str(slot)] = entry
    return normalized


def save_wayback_match_cache(cache: dict[str, Any]) -> bool:
    payload = _empty_wayback_match_cache()
    matches = cache.get("matches") if isinstance(cache, dict) else None
    if isinstance(matches, dict):
        payload["matches"] = matches
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path = _wayback_match_cache_path()
    try:
        if path.read_text(encoding="utf-8") == serialized:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)
    return True


def date_range_fully_covered(
    start: dt.date,
    end: dt.date,
    covered_dates: set[dt.date],
) -> bool:
    current = start
    while current <= end:
        if current not in covered_dates:
            return False
        current += dt.timedelta(days=1)
    return True


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
            wait_for_wayback_request()
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


def availability_url(target: dt.date | dt.datetime, api_url: str) -> str:
    if isinstance(target, dt.datetime):
        timestamp = target.astimezone(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    else:
        timestamp = target.strftime("%Y%m%d120000")
    query = urllib.parse.urlencode(
        {
            "url": ORIGINAL_PAGE,
            "timestamp": timestamp,
        }
    )
    return f"{api_url}?{query}"


def cdx_discovery_url(
    start: dt.date,
    end: dt.date,
    *,
    cdx_api: str = CDX_API,
) -> str:
    timezone = ZoneInfo(core.TIMEZONE)
    start_datetime = dt.datetime.combine(
        start,
        dt.time.min,
        tzinfo=timezone,
    ).astimezone(dt.timezone.utc)
    end_datetime = dt.datetime.combine(
        end,
        dt.time(23, 59, 59),
        tzinfo=timezone,
    ).astimezone(dt.timezone.utc)
    query = urllib.parse.urlencode(
        [
            ("url", ORIGINAL_PAGE),
            ("from", start_datetime.strftime("%Y%m%d%H%M%S")),
            ("to", end_datetime.strftime("%Y%m%d%H%M%S")),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,mimetype,digest"),
            ("filter", "statuscode:200"),
            ("matchType", "exact"),
        ]
    )
    return f"{cdx_api.rstrip('?')}?{query}"


def query_cdx_homepage_range(
    start: dt.date,
    end: dt.date,
    *,
    cdx_api: str = CDX_API,
) -> list[dict[str, Any]]:
    query_url = cdx_discovery_url(start, end, cdx_api=cdx_api)
    rows = parse_cdx_rows(read_json(query_url))
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        timestamp = str(row.get("timestamp") or "")
        if len(timestamp) != 14 or not timestamp.isdigit():
            continue
        if str(row.get("statuscode") or "200") != "200":
            continue
        if not _same_original_url(str(row.get("original") or ORIGINAL_PAGE), ORIGINAL_PAGE):
            continue
        try:
            moment = snapshot_moment(timestamp)
        except ValueError:
            continue
        if not start <= moment.date() <= end:
            continue
        candidates.setdefault(
            timestamp,
            {
                "timestamp": timestamp,
                "original": ORIGINAL_PAGE,
                "statuscode": str(row.get("statuscode") or "200"),
                "mimetype": str(row.get("mimetype") or ""),
                "digest": str(row.get("digest") or ""),
                "moment": moment,
                "cdxUrl": query_url,
            },
        )
    return sorted(candidates.values(), key=lambda item: item["timestamp"])


def slot_center(local_date: dt.date, slot: int) -> dt.datetime:
    return dt.datetime.combine(
        local_date,
        dt.time.min,
        tzinfo=ZoneInfo(core.TIMEZONE),
    ) + dt.timedelta(
        minutes=slot * core.SLOT_MINUTES + core.SLOT_MINUTES // 2,
    )


def _cached_match(
    matches: dict[str, Any],
    local_date: dt.date,
    slot: int,
    *,
    start: dt.date,
    end: dt.date,
) -> dict[str, Any] | None:
    date_matches = matches.get(local_date.isoformat())
    if not isinstance(date_matches, dict):
        return None
    value = date_matches.get(str(slot))
    if not isinstance(value, dict):
        return None
    timestamp = _valid_wayback_timestamp(value.get("timestamp"))
    if not timestamp:
        return None
    if not start <= snapshot_moment(timestamp).date() <= end:
        return None
    return {**value, "timestamp": timestamp}


def _cache_match(
    matches: dict[str, Any],
    local_date: dt.date,
    slot: int,
    *,
    timestamp: str,
    distance_seconds: int | None,
    cdx_url: str,
    source: str,
) -> None:
    value: dict[str, Any] = {
        "timestamp": timestamp,
        "source": source,
    }
    if distance_seconds is not None:
        value["distanceSeconds"] = distance_seconds
    if cdx_url:
        value["cdxUrl"] = cdx_url
    matches.setdefault(local_date.isoformat(), {})[str(slot)] = value


def discover_snapshots(
    start: dt.date,
    end: dt.date,
    *,
    cadence: str,
    api_url: str,
    cdx_api: str = CDX_API,
    excluded_dates: set[dt.date] | None = None,
    match_cache: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    cached_matches = match_cache if isinstance(match_cache, dict) else {}
    if cadence == "3h":
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(
                end,
                chunk_start + dt.timedelta(days=CDX_DISCOVERY_CHUNK_DAYS - 1),
            )
            active_dates = [
                local_date
                for local_date in target_dates(chunk_start, chunk_end, "daily")
                if local_date not in (excluded_dates or set())
            ]
            missing_targets = [
                (local_date, slot)
                for local_date in active_dates
                for slot in range(core.SLOT_COUNT)
                if _cached_match(
                    cached_matches,
                    local_date,
                    slot,
                    start=start,
                    end=end,
                )
                is None
            ]
            if missing_targets:
                try:
                    candidates = query_cdx_homepage_range(
                        chunk_start,
                        chunk_end,
                        cdx_api=cdx_api,
                    )
                except Exception as exc:
                    print(
                        f"Wayback CDX discovery failed for "
                        f"{chunk_start.isoformat()}..{chunk_end.isoformat()}: {exc}"
                    )
                    candidates = []
            else:
                candidates = []
            if active_dates:
                for local_date in active_dates:
                    for slot in range(core.SLOT_COUNT):
                        cached = _cached_match(
                            cached_matches,
                            local_date,
                            slot,
                            start=start,
                            end=end,
                        )
                        candidate = None
                        if cached:
                            timestamp = str(cached["timestamp"])
                            cdx_url = str(cached.get("cdxUrl") or "")
                        else:
                            if not candidates:
                                print(
                                    f"No snapshot near {local_date.isoformat()} "
                                    f"slot={slot}"
                                )
                                continue
                            center = slot_center(local_date, slot)
                            candidate = min(
                                candidates,
                                key=lambda item: abs(item["moment"] - center),
                            )
                            timestamp = str(candidate["timestamp"])
                            cdx_url = str(candidate.get("cdxUrl") or "")
                            _cache_match(
                                cached_matches,
                                local_date,
                                slot,
                                timestamp=timestamp,
                                distance_seconds=int(
                                    abs(candidate["moment"] - center).total_seconds()
                                ),
                                cdx_url=cdx_url,
                                source="cdx",
                            )
                        captured_date = snapshot_moment(timestamp).date()
                        if not start <= captured_date <= end:
                            print(
                                f"Skipped out-of-range snapshot {timestamp} "
                                f"for target {local_date.isoformat()} slot={slot}"
                            )
                            continue
                        snapshot = snapshots.setdefault(
                            timestamp,
                            {
                                "timestamp": timestamp,
                                "original": ORIGINAL_PAGE,
                                "availabilityUrl": "",
                                "cdxUrl": cdx_url,
                                "targetDate": local_date.isoformat(),
                                "targetSlot": slot,
                                "targetSlots": [],
                                "targetMappings": [],
                            },
                        )
                        snapshot.setdefault("targetSlots", []).append(slot)
                        snapshot.setdefault("targetMappings", []).append(
                            {"date": local_date.isoformat(), "slot": slot}
                        )
                        if snapshot["targetSlot"] == slot:
                            source = "cache" if cached else "cdx"
                            print(
                                f"Matched {timestamp} near "
                                f"{local_date.isoformat()} slot={slot} "
                                f"source={source}"
                            )
            chunk_start = chunk_end + dt.timedelta(days=1)
        for snapshot in snapshots.values():
            snapshot["targetSlots"] = sorted(set(snapshot.get("targetSlots") or []))
            snapshot["targetMappings"] = sorted(
                {
                    (str(item.get("date") or ""), int(item.get("slot")))
                    for item in snapshot.get("targetMappings") or []
                    if isinstance(item, dict) and str(item.get("date") or "")
                }
            )
            snapshot["targetMappings"] = [
                {"date": date_text, "slot": slot}
                for date_text, slot in snapshot["targetMappings"]
            ]
        return [snapshots[key] for key in sorted(snapshots)]
    else:
        targets = (
            (target, None, None)
            for target in target_dates(start, end, cadence)
        )
    for target, slot, target_datetime in targets:
        if target in (excluded_dates or set()):
            continue
        lookup = target_datetime or target
        try:
            lookup_url = availability_url(lookup, api_url)
            payload = read_json(lookup_url)
        except Exception as exc:
            print(f"Wayback discovery failed near {target.isoformat()}: {exc}")
            continue
        closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
        timestamp = str(closest.get("timestamp") or "")
        if not closest.get("available") or len(timestamp) != 14:
            print(f"No snapshot near {target.isoformat()}")
            continue

        captured_date = snapshot_moment(timestamp).date()
        if not start <= captured_date <= end:
            print(
                f"Skipped out-of-range snapshot {timestamp} "
                f"for target {target.isoformat()}"
            )
            continue

        snapshot = snapshots.setdefault(
            timestamp,
            {
                "timestamp": timestamp,
                "original": ORIGINAL_PAGE,
                "availabilityUrl": lookup_url,
                "cdxUrl": "",
                "targetDate": target.isoformat(),
                "targetSlot": slot,
                "targetSlots": [],
                "targetMappings": [],
            },
        )
        if slot is not None:
            snapshot.setdefault("targetSlots", []).append(slot)
            snapshot.setdefault("targetMappings", []).append(
                {"date": target.isoformat(), "slot": slot}
            )
        print(f"Discovered {timestamp} near {target.isoformat()}")
    for snapshot in snapshots.values():
        snapshot["targetSlots"] = sorted(set(snapshot.get("targetSlots") or []))
    return [snapshots[key] for key in sorted(snapshots)]


def _missing_asset_is_optional(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("auxiliary_") or text.startswith("video poster preview")


def _interaction_is_unconfirmed(manifest: dict[str, Any]) -> bool:
    interaction = manifest.get("interaction") or {}
    effects = interaction.get("effects")
    signals = (manifest.get("structureEvidence") or {}).get("signals") or {}
    has_interaction = bool(signals.get("hasInteraction")) or bool(
        manifest.get("interactionState") == "interactive"
    ) or "interactive" in core.manifest_types(manifest) or bool(effects)
    return has_interaction and (
        not isinstance(effects, list) or not effects
    )


def archive_is_reusable(folder: Path, manifest: dict[str, Any]) -> bool:
    # Keep the same primary-image/layer safety gate as the derived index.
    if not core._index_visual_merge_safe(manifest):
        return False
    missing = manifest.get("missing_assets") or []
    if any(not _missing_asset_is_optional(item) for item in missing):
        return False
    if _interaction_is_unconfirmed(manifest):
        return False
    assets = manifest.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if str(asset.get("role") or "").lower() == "auxiliary":
                continue
            local_file = str(asset.get("local_file") or "")
            if local_file and not (folder / local_file).is_file():
                return False
    return True


def imported_wayback_matches(
    *,
    reusable_only: bool = False,
) -> dict[tuple[str, int], set[str]]:
    matches: dict[tuple[str, int], set[str]] = {}
    for folder, manifest in core.iter_archive_manifests():
        if reusable_only and not archive_is_reusable(folder, manifest):
            continue
        for observation in core.manifest_observations(manifest):
            date_text = str(observation.get("date") or "")
            try:
                dt.date.fromisoformat(date_text)
            except ValueError:
                continue
            source = observation.get("source") or {}
            timestamp = _valid_wayback_timestamp(
                source.get("waybackTimestamp")
                or source.get("homepageWaybackTimestamp")
            )
            if not timestamp:
                continue
            for slot in core.manifest_slots(observation):
                matches.setdefault((date_text, slot), set()).add(timestamp)
    return matches


def imported_wayback_slots(
    *,
    reusable_only: bool = False,
) -> dict[str, set[int]]:
    timestamps: dict[str, set[int]] = {}
    for folder, manifest in core.iter_archive_manifests():
        if reusable_only and not archive_is_reusable(folder, manifest):
            continue
        for observation in core.manifest_observations(manifest):
            source = observation.get("source") or {}
            timestamp = str(source.get("waybackTimestamp") or "")
            if len(timestamp) == 14 and timestamp.isdigit():
                timestamps.setdefault(timestamp, set()).update(
                    core.manifest_slots(observation)
                )
    return timestamps


def imported_wayback_timestamps() -> set[str]:
    return set(imported_wayback_slots())


def snapshot_target_slots(snapshot: dict[str, Any]) -> list[int]:
    values = snapshot.get("targetSlots")
    if isinstance(values, list):
        normalized = core.normalize_slots(values)
        if normalized:
            return normalized
    single = core.normalize_slots([snapshot.get("targetSlot")])
    if single:
        return single
    return [core.slot_index(snapshot_moment(snapshot["timestamp"]))]


def snapshot_target_mappings(snapshot: dict[str, Any]) -> list[tuple[str, int]]:
    mappings: list[tuple[str, int]] = []
    raw_mappings = snapshot.get("targetMappings")
    if isinstance(raw_mappings, list):
        for item in raw_mappings:
            if not isinstance(item, dict):
                continue
            date_text = str(item.get("date") or "")
            try:
                dt.date.fromisoformat(date_text)
                slot = core.normalize_slots([item.get("slot")])
            except ValueError:
                continue
            if slot:
                mappings.append((date_text, slot[0]))
    if mappings:
        return sorted(set(mappings))
    date_text = snapshot_target_date(snapshot)
    if date_text:
        return [(date_text, slot) for slot in snapshot_target_slots(snapshot)]
    timestamp = str(snapshot.get("timestamp") or "")
    return [(snapshot_moment(timestamp).date().isoformat(), slot)
            for slot in snapshot_target_slots(snapshot)]


def snapshot_observation_groups(
    snapshot: dict[str, Any],
) -> list[tuple[str, list[int]]]:
    grouped: dict[str, list[int]] = {}
    for date_text, slot in snapshot_target_mappings(snapshot):
        grouped.setdefault(date_text, []).append(slot)
    return [
        (date_text, sorted(set(slots)))
        for date_text, slots in sorted(grouped.items())
    ]


def snapshot_target_date(snapshot: dict[str, Any]) -> str | None:
    value = str(snapshot.get("targetDate") or "")
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


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


def query_cdx_snapshots_status(
    endpoint: str,
    homepage_timestamp: str,
    *,
    cdx_api: str = CDX_API,
    max_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> CdxQueryStatus:
    try:
        candidates = query_cdx_snapshots(
            endpoint,
            homepage_timestamp,
            cdx_api=cdx_api,
            max_delta_seconds=max_delta_seconds,
        )
    except Exception as exc:
        return CdxQueryStatus(
            status="network-error",
            error=str(exc),
        )
    return CdxQueryStatus(
        status="success-with-snapshots" if candidates else "success-empty",
        candidates=tuple(candidates),
    )


def query_cdx_snapshots(
    endpoint: str,
    homepage_timestamp: str,
    *,
    cdx_api: str = CDX_API,
    max_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
) -> list[dict[str, Any]]:
    """Return successful CDX candidates, retaining the legacy list API."""
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
    cdx_statuses: list[CdxQueryStatus] = []
    for endpoint in header_api.DEFAULT_ENDPOINTS:
        status = query_cdx_snapshots_status(
            endpoint,
            timestamp,
            cdx_api=cdx_api,
            max_delta_seconds=max_delta_seconds,
        )
        cdx_statuses.append(status)
        candidates.extend(status.candidates)
        if status.status == "network-error":
            errors.append(f"CDX {endpoint} network failure: {status.error}")

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
    network_failures = [
        status for status in cdx_statuses if status.status == "network-error"
    ]
    successful_queries = [
        status
        for status in cdx_statuses
        if status.status in {"success-empty", "success-with-snapshots"}
    ]
    if network_failures and not successful_queries:
        prefix = "CDX query network failure prevented confirming an archived Header API snapshot"
    elif network_failures:
        prefix = "CDX query returned no matching snapshot; some endpoint queries failed"
    else:
        prefix = "no archived Header API snapshot within; CDX query succeeded but returned no matching snapshot"
    raise RuntimeError(
        f"{prefix} within {max_delta_seconds} seconds of homepage {timestamp}"
        + (f": {'; '.join(errors)}" if errors else "")
    )


def capture_snapshot_api(
    snapshot: dict[str, Any],
    *,
    replay_base: str,
    force: bool,
    cdx_api: str = CDX_API,
    max_header_api_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
    provenance_extra: dict[str, Any] | None = None,
    palxiao_result: HistoricalResult | None = None,
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
    provenance = {
        "primaryProvider": "wayback-header-api",
        "supportingProviders": [],
        "confidence": "high",
        "agreement": {},
        "conflicts": [],
    }
    if provenance_extra:
        provenance.update(provenance_extra)
    if palxiao_result:
        provenance = compare_api_with_palxiao(api_data, palxiao_result)
    result = core.capture_header_api_payload(
        api_data,
        moment=moment,
        force=force,
        update_current=False,
        record_observation=True,
        slots=snapshot_target_slots(snapshot),
        observation_date=snapshot_target_date(snapshot),
        source_extra={
            "captureMethod": "wayback-header-api",
            "waybackTimestamp": timestamp,
            "waybackReplay": api_replay,
            "availabilityUrl": snapshot.get("availabilityUrl"),
            "cdxUrl": snapshot.get("cdxUrl"),
            "provider": "wayback-header-api",
            "provenance": provenance,
            **api_match,
        },
        asset_url_candidates=lambda src: archived_asset_candidates(
            str(api_match["headerApiWaybackTimestamp"]),
            src,
            replay_base,
        ),
        referer=api_replay,
        before_request=wait_for_wayback_request,
        observation_groups=(
            snapshot_observation_groups(snapshot)
            if isinstance(snapshot.get("targetMappings"), list)
            else None
        ),
    )
    return {
        "timestamp": timestamp,
        "status": result["status"],
        "contentHash": result["contentHash"],
        "archive": str(result["archive"]),
        "captureMethod": "wayback-header-api",
        "provenance": provenance,
        **api_match,
    }


def compare_api_with_palxiao(
    api_data: dict[str, Any],
    palxiao_result: HistoricalResult,
) -> dict[str, Any]:
    """Compare two descriptions without combining their layers."""
    api_ids = {
        str(
            item.get("normalizedIdentity")
            or header_api.normalized_identity(str(item.get("src") or ""))
        )
        for item in api_data.get("resources") or []
        if isinstance(item, dict) and item.get("src")
    }
    palxiao_ids = {
        header_api.normalized_identity(str(item.get("src") or ""))
        for item in palxiao_result.layers
        if item.get("src")
    }
    agreement = {
        "apiLayerCount": len(api_data.get("layers") or []),
        "palxiaoLayerCount": len(palxiao_result.layers),
        "sharedResourceCount": len(api_ids & palxiao_ids),
    }
    conflicts: list[str] = []
    if agreement["apiLayerCount"] != agreement["palxiaoLayerCount"]:
        conflicts.append("layer count differs")
    if api_ids and palxiao_ids and api_ids != palxiao_ids:
        conflicts.append("normalized resource identities differ")
    return {
        "primaryProvider": "wayback-header-api",
        "supportingProviders": [PALXIAO_PROVIDER_NAME],
        "confidence": "high" if not conflicts else "conflict",
        "agreement": agreement,
        "conflicts": conflicts,
    }


def _write_provider_source(temp: Path, payload: Any) -> str:
    source_dir = temp / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / "palxiao-data.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "source/palxiao-data.json"


def _exact_palxiao_date(
    snapshot: dict[str, str],
    provider: PalxiaoHistoryProvider,
) -> str | None:
    timestamp = snapshot["timestamp"]
    expected = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").date().isoformat()
    explicit = snapshot.get("palxiaoObservedAt") or snapshot.get("palxiaoDate")
    if explicit:
        if explicit != expected:
            raise ValueError(
                f"palxiao observedAt {explicit} does not exactly match snapshot date {expected}"
            )
        return explicit
    return provider.date_for_timestamp(timestamp)


def capture_snapshot_palxiao(
    snapshot: dict[str, Any],
    *,
    provider: PalxiaoHistoryProvider,
    force: bool,
    historical: HistoricalResult | None = None,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    palxiao_date = _exact_palxiao_date(snapshot, provider)
    if not palxiao_date:
        raise LookupError(
            f"palxiao has no Banner data for the exact observed date of {timestamp}"
        )
    historical = historical or provider.load(palxiao_date)
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".palxiao_history_", dir=core.DATA_DIR))
    try:
        source_file = _write_provider_source(temp, historical.raw_payload)
        layers: list[dict[str, Any]] = []
        missing_assets = list(historical.missing_assets)
        for index, source_layer in enumerate(historical.layers):
            try:
                item = core._download_http_asset(
                    str(source_layer["assetUrl"]),
                    temp,
                    f"palxiao_layer_{index:02d}",
                    referer=historical.source_url,
                )
                item["src"] = source_layer["src"]
                item["sourceSrc"] = source_layer.get("sourceSrc") or ""
                item["sourceProvider"] = PALXIAO_PROVIDER_NAME
                item["assetType"] = core.media_type(
                    str(source_layer.get("tag") or "img"),
                    str(source_layer["assetUrl"]),
                    str(item.get("contentType") or ""),
                )
            except Exception as exc:
                missing_assets.append(f"layer_{index:03d}: {exc}")
                continue
            layers.append(
                {
                    "index": index,
                    "sourceProvider": PALXIAO_PROVIDER_NAME,
                    "sourceLayer": source_layer.get("sourceLayer") or {},
                    "palxiao": source_layer.get("palxiao") or {},
                    "tag": item.get("tag") or source_layer.get("tag") or "img",
                    "assetType": item.get("assetType") or "image",
                    "src": item.get("src") or "",
                    "file": item.get("file") or "",
                    "contentType": item.get("contentType") or "",
                    "resources": [{**item, "resourceIndex": 0}],
                    "width": source_layer.get("width", 0),
                    "height": source_layer.get("height", 0),
                    "naturalWidth": 0,
                    "naturalHeight": 0,
                    "objectFit": "fill",
                    "objectPosition": "50% 50%",
                    "transformOrigin": "50% 50%",
                    "transform": source_layer.get("transform") or [1, 0, 0, 1, 0, 0],
                    "opacity": source_layer.get("opacity") or [1, 1],
                    "position": {},
                    "zIndex": index,
                    "animation": {},
                    "animationTarget": "media",
                    "motion": None,
                    "captureTargetTag": item.get("tag") or "img",
                }
            )
            for optional_key in ("a", "g", "f", "deg", "blur"):
                if optional_key in source_layer:
                    layers[-1][optional_key] = source_layer[optional_key]

        if not has_saved_primary_assets(
            temp,
            mode="split",
            static=None,
            layers=layers,
        ):
            raise RuntimeError("no downloadable palxiao Banner asset found")

        captured_at = snapshot_moment(timestamp).isoformat(timespec="seconds")
        evidence = {
            "root": {
                "source": PALXIAO_PROVIDER_NAME,
                "url": historical.source_url,
                "date": palxiao_date,
            },
            "layerCount": len(historical.layers),
            "visibleMediaCount": len(historical.layers),
            "mediaCount": len(historical.layers),
            "media": [
                {
                    "tag": item.get("tag") or "img",
                    "src": item.get("src") or "",
                    "sourceKind": "palxiao-data.json",
                }
                for item in historical.layers
            ],
            "resourceUrls": [
                {"name": item.get("src") or "", "initiatorType": "palxiao-data.json"}
                for item in historical.layers
            ],
            "stylesheetUrls": [],
            "scriptUrls": [],
            "animationCss": "",
            "signals": {
                "isSplitLayer": False,
                "isStructuredLayered": bool(historical.layers),
                "hasVideo": any(item.get("tag") == "video" for item in historical.layers),
                "hasCanvas": False,
                "hasSvg": any(item.get("tag") == "svg" for item in historical.layers),
                "hasSvgAnimation": False,
                "hasCssAnimation": False,
                "hasInteraction": True,
                "hasDynamicSource": any(item.get("tag") == "video" for item in historical.layers),
            },
            "provider": PALXIAO_PROVIDER_NAME,
        }
        manifest: dict[str, Any] = {
            "version": core.MANIFEST_VERSION,
            "capturedAt": captured_at,
            "date": palxiao_date,
            "season": core.season_of(snapshot_moment(timestamp).month),
            "source": {
                "page": ORIGINAL_PAGE,
                "resolvedUrl": historical.source_url,
                "captureMethod": "palxiao-history",
                "provider": PALXIAO_PROVIDER_NAME,
                "palxiaoDate": palxiao_date,
                "observedAt": historical.observed_at,
                "dateSemantics": "observedAt-only; effectiveFrom is not inferred",
                "parameterSource": (
                    "palxiao data.json fields when present; a/g/f/deg are reproducer "
                    "parameters and unknown fields remain in sourceLayer"
                ),
                "palxiaoDataUrl": historical.source_url,
                "requestedTimestamp": timestamp,
                "structureSemantics": "palxiao layer list; not official Bilibili split_layer",
            },
            "provenance": {
                "primaryProvider": PALXIAO_PROVIDER_NAME,
                "supportingProviders": [],
                "confidence": historical.confidence,
                "agreement": {},
                "conflicts": [],
            },
            "viewport": core.VIEWPORT,
            "banner": {"referenceHeight": core.HEADER_REFERENCE_HEIGHT},
            "mode": "split",
            "static": None,
            "layers": layers,
            "auxiliaryAssets": [],
            "interaction": {
                "model": "palxiao-reconstructed-v1",
                "positionAxis": "horizontal-pixel",
                "inputMode": "relative-from-pointer-enter",
                "effects": [
                    "translateX", "translateY", "scale", "rotate", "opacity",
                    *(["blur"] if any("blur" in item for item in layers) else []),
                ],
                "parameterSource": "palxiao-reproducer; not Bilibili Header API",
            },
            "sourceFiles": {"json": source_file, "palxiaoData": source_file},
            "sourceEvidence": historical.raw_metadata,
            "timeZone": core.TIMEZONE,
            "lastObservedAt": captured_at,
        }
        core.enrich_manifest_metadata(manifest, evidence, missing_assets=missing_assets)
        result = core.archive_capture(
            temp,
            manifest,
            moment=snapshot_moment(timestamp),
            force=force,
            update_current=False,
            record_observation=True,
            slots=(
                snapshot_target_slots(snapshot)
                if snapshot.get("targetSlots") is not None
                else list(range(core.SLOT_COUNT))
            ),
            observation_date=snapshot_target_date(snapshot) or palxiao_date,
        )
        return {
            "timestamp": timestamp,
            "palxiaoDate": palxiao_date,
            "status": result["status"],
            "contentHash": result["contentHash"],
            "archive": str(result["archive"]),
            "captureMethod": "palxiao-history",
        }
    finally:
        shutil.rmtree(temp, ignore_errors=True)


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
        if _is_banner_node(node)
    ]


def _banner_root_score(node: _HTMLNode) -> tuple[int, list[str]]:
    node_id = node.attrs.get("id", "").lower()
    classes = _class_tokens(node)
    score = 0
    reasons: list[str] = []
    if node_id in BANNER_ID_PRIORITIES:
        score = max(score, BANNER_ID_PRIORITIES[node_id])
        reasons.append(f"exact id #{node_id}")
    for name, priority in BANNER_CLASS_PRIORITIES.items():
        if name in classes:
            score = max(score, priority)
            reasons.append(f"class .{name}")
    return score, reasons


def _node_depth(node: _HTMLNode, root: _HTMLNode) -> int:
    depth = 0
    current = node
    while current is not root and current.parent is not None:
        depth += 1
        current = current.parent
    return depth


def _node_penalty(node: _HTMLNode) -> tuple[int, list[str]]:
    tokens = _class_tokens(node)
    value = " ".join(
        [node.attrs.get("id", "").lower(), node.attrs.get("class", "").lower()]
    )
    if any(
        token in tokens
        or re.search(rf"(?:^|[-_]){token}(?:$|[-_])", value)
        for token in ("logo", "icon", "nav", "avatar", "badge")
    ):
        return -70, ["auxiliary UI-like node"]
    return 0, []


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
    origin: dict[str, Any] | None = None,
    score: int = 0,
    reasons: Iterable[str] = (),
) -> dict[str, Any] | None:
    src = _resolve_original_url(value, base_url)
    if not src:
        return None
    return {
        "src": src,
        "tag": tag or header_api.infer_tag(src),
        "sourceKind": source_kind,
        "normalizedIdentity": header_api.normalized_identity(src),
        "role": "candidate",
        "origin": origin or {},
        "score": score,
        "reasons": list(dict.fromkeys(str(item) for item in reasons)),
    }


def _dedupe_source_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        src = str(record.get("src") or "")
        identity = header_api.normalized_identity(src) if src else ""
        if not identity:
            continue
        record = dict(record)
        record["normalizedIdentity"] = identity
        if identity not in seen:
            seen[identity] = record
            result.append(record)
            continue
        existing = seen[identity]
        old_score = int(existing.get("score") or 0)
        new_score = int(record.get("score") or 0)
        existing["score"] = max(old_score, new_score)
        existing["reasons"] = list(dict.fromkeys(
            [*existing.get("reasons", []), *record.get("reasons", [])]
        ))
        source_candidates = list(dict.fromkeys(
            [
                *(existing.get("sourceCandidates") or []),
                *(record.get("sourceCandidates") or []),
            ]
        ))
        if source_candidates:
            existing["sourceCandidates"] = source_candidates
        for key in ("poster", "posterRole", "isPoster", "videoSourceMissing"):
            if key in record and key not in existing:
                existing[key] = record[key]
        if new_score > old_score:
            for key in (
                "src", "tag", "sourceKind", "origin", "role",
                "poster", "posterRole", "isPoster", "videoSourceMissing",
            ):
                if key in record:
                    existing[key] = record[key]
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


def _selector_root_score(selector: str, root: _HTMLNode) -> tuple[int, list[str]]:
    """Return a score only when a selector explicitly relates to ``root``."""
    node_id = root.attrs.get("id", "").lower()
    classes = _class_tokens(root)
    best = 0
    best_reasons: list[str] = []
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        score = 0
        reasons: list[str] = []
        if node_id and re.search(
            rf"#{re.escape(node_id)}(?:$|[^\w-])", part.lower()
        ):
            score = BANNER_ID_PRIORITIES.get(node_id, 150)
            reasons.append(f"exact root selector #{node_id}")
        for class_name in classes:
            if not re.search(
                rf"\.{re.escape(class_name)}(?:$|[^\w-])", part.lower()
            ):
                continue
            class_score = BANNER_CLASS_PRIORITIES.get(class_name, 45)
            if class_score > score:
                score = class_score
                reasons = [f"exact root selector .{class_name}"]
        if _selector_has_token(part, "layer"):
            score = max(score, 35)
            reasons.append("explicit .layer selector")
        if "::before" in part.lower() or "::after" in part.lower():
            score = max(0, score - 20)
            reasons.append("pseudo-element")
        if score and any(char in part for char in (" ", ">", "+", "~")):
            score = max(0, score - 10)
            reasons.append("descendant selector")
        if score > best:
            best = score
            best_reasons = reasons
    return best, best_reasons


def _parse_relevant_css(
    css_text: str,
    *,
    base_url: str,
    root: _HTMLNode,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    layer_records: list[dict[str, Any]] = []
    animation = False
    for match in CSS_RULE_RE.finditer(css_text):
        selectors = match.group("selectors").strip()
        body = match.group("body")
        relation_score, relation_reasons = _selector_root_score(selectors, root)
        if not relation_score:
            continue
        urls = _css_urls(body)
        if not urls:
            continue
        if re.search(r"(?:animation|transition|@keyframes)", body, re.IGNORECASE):
            animation = True
        target = layer_records if _selector_has_token(selectors, "layer") else records
        for index, value in enumerate(urls):
            score = relation_score
            reasons = [*relation_reasons]
            if index == 0:
                score += 10
                reasons.append("first CSS visual resource")
            else:
                score -= 10
                reasons.append("additional CSS visual resource")
            if "::before" in selectors.lower() or "::after" in selectors.lower():
                score = min(score, 90)
                reasons.append("pseudo-element treated as auxiliary candidate")
            record = _record_source(
                value,
                tag=header_api.infer_tag(value),
                source_kind="css",
                base_url=base_url,
                origin={
                    "nodeTag": root.tag,
                    "nodeId": root.attrs.get("id", ""),
                    "nodeClass": root.attrs.get("class", ""),
                    "selector": selectors,
                    "depthFromRoot": 0,
                    "pseudoElement": (
                        "before" if "::before" in selectors.lower()
                        else "after" if "::after" in selectors.lower()
                        else ""
                    ),
                },
                score=score,
                reasons=reasons,
            )
            if record:
                target.append(record)
    return {
        "records": _dedupe_source_records(records),
        "layerRecords": _dedupe_source_records(layer_records),
        "animation": animation,
    }


def _node_visual_sources(
    node: _HTMLNode,
    *,
    base_url: str,
    root: _HTMLNode | None = None,
) -> list[dict[str, Any]]:
    root = root or node
    records: list[dict[str, Any]] = []
    nodes = [node, *_walk_nodes(node)]
    for candidate in nodes:
        depth = _node_depth(candidate, root)
        penalty, penalty_reasons = _node_penalty(candidate)
        origin = {
            "nodeTag": candidate.tag,
            "nodeId": candidate.attrs.get("id", ""),
            "nodeClass": candidate.attrs.get("class", ""),
            "selector": "",
            "depthFromRoot": depth,
            "pseudoElement": "",
        }
        base_score = max(20, 85 - depth * 10) + penalty
        if candidate is root:
            base_score = 100 + penalty
        if candidate.tag == "picture":
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
                    origin=origin,
                    score=base_score + 5,
                    reasons=["picture source", *penalty_reasons],
                )
                if record:
                    records.append(record)
                    break
        if candidate.tag in {"img", "source"} and (
            _has_ancestor(candidate, lambda item: item.tag == "picture")
            or _has_ancestor(candidate, lambda item: item.tag == "video")
        ):
            continue
        if candidate.tag == "img":
            value = candidate.attrs.get("src") or _first_srcset(candidate.attrs.get("srcset", ""))
            record = _record_source(
                value,
                tag="img",
                source_kind="html-img",
                base_url=base_url,
                origin=origin,
                score=base_score,
                reasons=["img inside selected root", *penalty_reasons],
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
                origin=origin,
                score=base_score,
                reasons=["picture/source media", *penalty_reasons],
            )
            if record:
                records.append(record)
        elif candidate.tag == "video":
            raw_sources = [candidate.attrs.get("src", "")]
            raw_sources.extend(
                child.attrs.get("src") or child.attrs.get("data-src", "")
                for child in _walk_nodes(candidate)
                if child.tag == "source"
            )
            video_sources = []
            for value in raw_sources:
                resolved = _resolve_original_url(value, base_url)
                if resolved and resolved not in video_sources:
                    video_sources.append(resolved)
            if video_sources:
                record = _record_source(
                    video_sources[0],
                    tag="video",
                    source_kind="html-video",
                    base_url=base_url,
                    origin=origin,
                    score=base_score + 10,
                    reasons=[
                        "video.src/source src real video resource",
                        *penalty_reasons,
                    ],
                )
                if record:
                    record["sourceCandidates"] = video_sources
                    poster = _resolve_original_url(
                        candidate.attrs.get("poster", ""),
                        base_url,
                    )
                    if poster:
                        record["poster"] = poster
                        record["posterRole"] = "preview-fallback"
                    records.append(record)
            else:
                poster = _resolve_original_url(
                    candidate.attrs.get("poster", ""),
                    base_url,
                )
                if poster:
                    record = _record_source(
                        poster,
                        tag="img",
                        source_kind="html-video-poster",
                        base_url=base_url,
                        origin=origin,
                        score=base_score - 20,
                        reasons=[
                            "poster retained as preview only",
                            "video source unavailable",
                            *penalty_reasons,
                        ],
                    )
                    if record:
                        record["role"] = "preview"
                        record["isPoster"] = True
                        record["videoSourceMissing"] = True
                        records.append(record)
        inline = candidate.attrs.get("style", "")
        for value in _css_urls(inline):
            record = _record_source(
                value,
                tag=header_api.infer_tag(value),
                source_kind="html-style",
                base_url=base_url,
                origin=origin,
                score=base_score + (20 if candidate is root else 5),
                reasons=["inline background-image", *penalty_reasons],
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

    css_sources = [(page_url, css) for css in parser.style_blocks]
    css_sources.extend(css_texts)
    evaluated_roots: list[dict[str, Any]] = []
    for candidate_root in roots:
        node_records = _node_visual_sources(
            candidate_root,
            base_url=page_url,
            root=candidate_root,
        )
        css_records: list[dict[str, Any]] = []
        css_layer_records: list[dict[str, Any]] = []
        css_animation = False
        for css_base, css in css_sources:
            parsed_css = _parse_relevant_css(
                css,
                base_url=css_base,
                root=candidate_root,
            )
            css_records.extend(parsed_css["records"])
            css_layer_records.extend(parsed_css["layerRecords"])
            css_animation = css_animation or bool(parsed_css["animation"])
        records = _dedupe_source_records([*node_records, *css_records])
        root_score, root_reasons = _banner_root_score(candidate_root)
        resource_score = max(
            (int(item.get("score") or 0) for item in records),
            default=0,
        )
        evaluated_roots.append(
            {
                "node": candidate_root,
                "records": records,
                "layerRecords": _dedupe_source_records(css_layer_records),
                "cssAnimation": css_animation,
                "rootScore": root_score,
                "resourceScore": resource_score,
                "score": root_score + resource_score,
                "reasons": root_reasons,
            }
        )

    def root_sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(item["score"]),
            int(item["rootScore"]),
            int(item["resourceScore"]),
        )

    evaluated_roots.sort(key=root_sort_key, reverse=True)
    best = max(evaluated_roots, key=root_sort_key)
    second = max(
        (item for item in evaluated_roots if item is not best),
        key=root_sort_key,
        default=None,
    )
    root_tie = bool(
        second
        and best["score"] == second["score"]
        and {
            item.get("normalizedIdentity") for item in best["records"]
        } != {
            item.get("normalizedIdentity") for item in second["records"]
        }
    )
    root = None if root_tie else best["node"]
    root_records = (
        _dedupe_source_records([*best["records"], *second["records"]])
        if root_tie and second
        else list(best["records"])
    )
    css_layer_records = [] if root_tie else list(best["layerRecords"])
    css_animation = any(bool(item["cssAnimation"]) for item in evaluated_roots)

    layer_nodes = [] if root is None else [
        node
        for node in [root, *_walk_nodes(root)]
        if _is_layer_node(node)
    ]
    layer_groups = [
        _node_visual_sources(node, base_url=page_url, root=root or node)
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
    layered_identities = {
        str(group[0].get("normalizedIdentity") or "")
        for group in layer_groups
        if group
    }
    layered = bool(
        len(layer_groups) >= 2
        and all(layer_groups)
        and len(layered_identities) >= 2
    )
    poster_only_video = any(
        bool(record.get("videoSourceMissing")) for record in root_records
    ) and not any(record.get("tag") == "video" for record in root_records)

    primary_record: dict[str, Any] | None = None
    auxiliary_records: list[dict[str, Any]] = []
    if poster_only_video:
        mode = "ambiguous"
        visual_records = sorted(
            root_records,
            key=lambda item: int(item.get("score") or 0),
            reverse=True,
        )
        for record in visual_records:
            record["role"] = "preview" if record.get("isPoster") else "unresolved-candidate"
    elif layered:
        visual_records = _dedupe_source_records(
            record for group in layer_groups for record in group
        )
        mode = "split"
    else:
        ordered_records = sorted(
            root_records,
            key=lambda item: int(item.get("score") or 0),
            reverse=True,
        )
        primary_record = ordered_records[0] if ordered_records else None
        second_score = (
            int(ordered_records[1].get("score") or 0)
            if len(ordered_records) > 1
            else -10**9
        )
        ambiguous_primary = bool(
            root_tie
            or not primary_record
            or (
                len(ordered_records) > 1
                and int(primary_record.get("score") or 0) - second_score < 15
            )
        )
        mode = "ambiguous" if ambiguous_primary else "static"
        if mode == "static" and primary_record:
            primary_record["role"] = "primary"
            auxiliary_records = ordered_records[1:]
            for record in auxiliary_records:
                record["role"] = "auxiliary"
            visual_records = [primary_record, *auxiliary_records]
        else:
            for record in ordered_records:
                record["role"] = "unresolved-candidate"
            visual_records = ordered_records

    selected_root_score, selected_root_reasons = (
        _banner_root_score(root) if root else (0, [])
    )
    root_info = {
        "tag": root.tag if root else "",
        "id": root.attrs.get("id", "") if root else "",
        "className": root.attrs.get("class", "") if root else "",
        "score": selected_root_score,
        "reasons": selected_root_reasons,
    }
    root_diagnostics = [
        {
            "tag": item["node"].tag,
            "id": item["node"].attrs.get("id", ""),
            "className": item["node"].attrs.get("class", ""),
            "score": item["score"],
            "rootScore": item["rootScore"],
            "resourceScore": item["resourceScore"],
            "reasons": item["reasons"],
            "candidateCount": len(item["records"]),
        }
        for item in evaluated_roots
    ]
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
        "selectedRoot": root_info,
        "roots": root_diagnostics,
        "primaryRecord": primary_record if mode == "static" else None,
        "auxiliaryRecords": auxiliary_records if mode == "static" else [],
        "candidates": visual_records,
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
                "hasVideo": any(
                    item["tag"] == "video" or item.get("videoSourceMissing")
                    for item in visual_records
                ),
                "hasCanvas": False,
                "hasSvg": any(item["tag"] == "svg" for item in visual_records),
                "hasSvgAnimation": False,
                "hasCssAnimation": css_animation,
                "hasInteraction": False,
                "hasDynamicSource": any(
                    item["tag"] == "video" or item.get("videoSourceMissing")
                    for item in visual_records
                ),
            },
            "selectedRoot": root_info,
            "roots": root_diagnostics,
            "primary": primary_record if mode == "static" else None,
            "auxiliaryAssets": auxiliary_records if mode == "static" else [],
            "strongLayerEvidence": bool(layered),
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
    source_candidates: Iterable[str] = (),
) -> dict[str, Any]:
    originals: list[str] = []
    for value in [src, *source_candidates]:
        original = _resolve_original_url(value, ORIGINAL_PAGE)
        if original and original not in originals:
            originals.append(original)
    if not originals:
        raise ValueError("empty archived asset URL")
    errors: list[str] = []
    for attempt in range(3):
        for original in originals:
            candidates = archived_asset_candidates(timestamp, original, replay_base)
            for candidate in candidates:
                try:
                    wait_for_wayback_request()
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
    snapshot: dict[str, Any],
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
            "multiple relevant Banner visual resources without strong layered evidence: "
            + json.dumps(
                {
                    "selectedRoot": parsed.get("selectedRoot"),
                    "candidates": parsed.get("candidates", []),
                },
                ensure_ascii=False,
            )
        )

    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".wayback_http_", dir=core.DATA_DIR))
    try:
        source_files = _write_http_sources(temp, html_text, css_texts)
        missing_assets = list(css_errors)
        layers: list[dict[str, Any]] = []
        static: dict[str, Any] | None = None
        preview_image = ""
        auxiliary_assets: list[dict[str, Any]] = []
        if parsed["mode"] == "static":
            record = parsed.get("primaryRecord") or parsed["visualRecords"][0]
            try:
                static = _download_archived_asset(
                    record["src"],
                    temp,
                    timestamp=timestamp,
                    replay_base=replay_base,
                    stem="static",
                    tag=record.get("tag") or "img",
                    referer=page_replay,
                    source_candidates=record.get("sourceCandidates") or [],
                )
                if record.get("sourceCandidates"):
                    static["sourceCandidates"] = record["sourceCandidates"]
            except Exception as exc:
                missing_assets.append(f"static: {exc}")
            poster = str(record.get("poster") or "")
            if poster:
                try:
                    preview = _download_archived_asset(
                        poster,
                        temp,
                        timestamp=timestamp,
                        replay_base=replay_base,
                        stem="preview",
                        tag="img",
                        referer=page_replay,
                    )
                    preview_image = str(preview.get("file") or "")
                    if static is not None:
                        static["poster"] = poster
                        static["posterFile"] = preview_image
                except Exception as exc:
                    missing_assets.append(f"video poster preview: {exc}")
            for auxiliary_index, auxiliary in enumerate(parsed.get("auxiliaryRecords") or []):
                try:
                    item = _download_archived_asset(
                        auxiliary["src"],
                        temp,
                        timestamp=timestamp,
                        replay_base=replay_base,
                        stem=f"auxiliary_{auxiliary_index:02d}",
                        tag=auxiliary.get("tag") or "img",
                        referer=page_replay,
                    )
                    auxiliary_assets.append(
                        {
                            **item,
                            "role": "auxiliary",
                            "origin": auxiliary.get("origin") or {},
                            "score": auxiliary.get("score", 0),
                            "reasons": auxiliary.get("reasons") or [],
                        }
                    )
                except Exception as exc:
                    missing_assets.append(f"auxiliary_{auxiliary_index:03d}: {exc}")
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
            "version": core.MANIFEST_VERSION,
            "capturedAt": captured_at,
            "date": moment.strftime("%Y-%m-%d"),
            "season": core.season_of(moment.month),
            "source": {
                "page": ORIGINAL_PAGE,
                "resolvedUrl": page_replay,
                "captureMethod": "wayback-http-html-css",
                "provider": "wayback-html",
                "waybackTimestamp": timestamp,
                "homepageWaybackTimestamp": timestamp,
                "waybackReplay": page_replay,
                "availabilityUrl": snapshot.get("availabilityUrl"),
                "cdxUrl": snapshot.get("cdxUrl"),
            },
            "provenance": {
                "primaryProvider": "wayback-html",
                "supportingProviders": [],
                "confidence": "high" if parsed.get("primaryRecord") else "medium",
                "agreement": {},
                "conflicts": [],
            },
            "viewport": core.VIEWPORT,
            "banner": {},
            "mode": mode,
            "static": static,
            "preview_image": preview_image or None,
            "layers": layers,
            "auxiliaryAssets": auxiliary_assets,
            "interaction": {
                "model": "none",
                "positionAxis": "observed",
                "effects": [],
            },
            "sourceFiles": source_files,
            "sourceEvidence": parsed["evidence"],
            "timeZone": core.TIMEZONE,
            "lastObservedAt": captured_at,
        }
        core.enrich_manifest_metadata(
            manifest,
            parsed["evidence"],
            missing_assets=missing_assets,
        )
        observation_groups = (
            snapshot_observation_groups(snapshot)
            if isinstance(snapshot.get("targetMappings"), list)
            else [(snapshot_target_date(snapshot), snapshot_target_slots(snapshot))]
        )
        capture_results = []
        for group_date, group_slots in observation_groups:
            manifest.pop("familyId", None)
            capture_results.append(
                core.archive_capture(
                    temp,
                    manifest,
                    moment=moment,
                    force=force,
                    update_current=False,
                    record_observation=True,
                    slots=group_slots,
                    observation_date=group_date,
                )
            )
        if not capture_results:
            raise ValueError("snapshot has no target observation groups")
        result = dict(capture_results[-1])
        statuses = {str(item.get("status") or "") for item in capture_results}
        result["status"] = (
            "created"
            if "created" in statuses
            else "updated"
            if "updated" in statuses
            else "unchanged"
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


def imported_palxiao_dates() -> set[str]:
    dates: set[str] = set()
    for _, manifest in core.iter_archive_manifests():
        for observation in core.manifest_observations(manifest):
            source = observation.get("source") or {}
            if str(source.get("provider") or "") != PALXIAO_PROVIDER_NAME:
                continue
            date = str(source.get("palxiaoDate") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                dates.add(date)
    return dates


def discover_palxiao_snapshots(
    start: dt.date,
    end: dt.date,
    *,
    provider: PalxiaoHistoryProvider,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for date_text in provider.discover_dates():
        try:
            date = dt.date.fromisoformat(date_text)
        except ValueError:
            continue
        if not start <= date <= end:
            continue
        snapshots.append(
            {
                "timestamp": date.strftime("%Y%m%d") + "120000",
                "original": ORIGINAL_PAGE,
                "availabilityUrl": "",
                "palxiaoDate": date.isoformat(),
                "palxiaoObservedAt": date.isoformat(),
                "targetDate": date.isoformat(),
                "targetSlots": list(range(core.SLOT_COUNT)),
            }
        )
    return snapshots


def imported_reference_observations(
    reference_commit: str,
) -> set[tuple[str, dt.date]]:
    imported: set[tuple[str, dt.date]] = set()
    for _, manifest in core.iter_archive_manifests():
        manifest_source = manifest.get("source")
        fallback_source = manifest_source if isinstance(manifest_source, dict) else {}
        for observation in core.manifest_observations(manifest):
            observation_source = observation.get("source")
            source = dict(fallback_source)
            if isinstance(observation_source, dict):
                source.update(observation_source)
            if str(source.get("referenceCommit") or "") != reference_commit:
                continue
            reference_id = str(source.get("referenceId") or "")
            if not reference_id:
                continue
            date_text = str(observation.get("date") or "")
            try:
                date = dt.date.fromisoformat(date_text)
            except ValueError:
                continue
            imported.add((reference_id, date))
    return imported


def import_reference_repository(
    root: Path,
    *,
    start: dt.date,
    end: dt.date,
    commit: str,
    force: bool,
) -> set[dt.date]:
    if not root.is_dir():
        raise FileNotFoundError(f"reference repository not found: {root}")
    resolved_commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if resolved_commit != commit:
        raise ValueError(
            f"reference repository commit mismatch: expected {commit}, got {resolved_commit}"
        )
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    repository = ReferenceRepository(root, commit=commit, time_zone=core.TIMEZONE)
    covered_dates: set[dt.date] = set()
    imported = (
        imported_reference_observations(commit)
        if not force
        else set()
    )
    skipped_existing = 0
    skipped_unchanged = 0
    for entry in repository.covered_entries(start, end):
        dates = []
        for date in repository.iter_dates(entry, start, end):
            covered_dates.add(date)
            if (entry.reference_id, date) in imported:
                skipped_existing += 1
                continue
            dates.append(date)
        if not dates:
            continue
        temp = Path(tempfile.mkdtemp(prefix=".mikufan039_reference_", dir=core.DATA_DIR))
        try:
            base_manifest = repository.build_manifest(entry, dates[0], temp)
            for date in dates:
                manifest = json.loads(json.dumps(base_manifest, ensure_ascii=False))
                captured_at = dt.datetime.combine(
                    date,
                    dt.time(0, 0),
                    tzinfo=ZoneInfo(core.TIMEZONE),
                ).isoformat(timespec="seconds")
                manifest["date"] = date.isoformat()
                manifest["capturedAt"] = captured_at
                manifest["lastObservedAt"] = captured_at
                family_id = f"{date.isoformat()}_{entry.family_key}"
                result = core.archive_capture(
                    temp,
                    manifest,
                    moment=dt.datetime.fromisoformat(captured_at),
                    force=force,
                    update_current=False,
                    record_observation=True,
                    slots=manifest.get("slots"),
                    family_id=family_id,
                )
                if result["status"] == "unchanged":
                    skipped_unchanged += 1
                    continue
                print(
                    json.dumps(
                        {
                            "referenceId": entry.reference_id,
                            "date": date.isoformat(),
                            "status": result["status"],
                            "contentHash": result["contentHash"],
                            "archive": str(result["archive"]),
                        },
                        ensure_ascii=False,
                    )
                )
        finally:
            shutil.rmtree(temp, ignore_errors=True)
    if skipped_existing:
        print(f"Skipped {skipped_existing} already imported reference observations.")
    if skipped_unchanged:
        print(f"Skipped {skipped_unchanged} unchanged reference observations.")
    return covered_dates


def checkpoint_reference_import(
    covered_dates: set[dt.date],
    checkpoint_script: str | None,
) -> bool:
    if not covered_dates:
        return False
    changed = core.rebuild_index()
    if changed:
        print("Rebuilt index after reference repository import.")
    if checkpoint_script:
        print("Reference repository import completed; running checkpoint.")
        run_checkpoint(
            checkpoint_script,
            processed=0,
            succeeded=0,
            changed=int(changed),
            final=False,
        )
    return changed


def capture_snapshot(
    snapshot: dict[str, str],
    *,
    replay_base: str,
    force: bool,
    cdx_api: str = CDX_API,
    max_header_api_delta_seconds: int = HEADER_API_MAX_DELTA_SECONDS,
    verify_dom: bool = False,
    provider: str = "auto",
    palxiao_provider: PalxiaoHistoryProvider | None = None,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    if provider not in {"auto", "wayback-api", "palxiao", "wayback-html"}:
        raise ValueError(f"unsupported historical provider: {provider}")
    if provider == "palxiao" and palxiao_provider is None:
        raise RuntimeError("Palxiao provider is not configured")

    palxiao_result: HistoricalResult | None = None
    if provider == "auto" and palxiao_provider:
        palxiao_date = _exact_palxiao_date(snapshot, palxiao_provider)
        if palxiao_date:
            try:
                palxiao_result = palxiao_provider.load(palxiao_date)
            except LookupError:
                palxiao_result = None
            except Exception as exc:
                print(f"Palxiao supporting lookup unavailable for {timestamp}: {exc}")

    result: dict[str, Any]
    if provider in {"auto", "wayback-api"}:
        try:
            result = capture_snapshot_api(
                snapshot,
                replay_base=replay_base,
                force=force,
                cdx_api=cdx_api,
                max_header_api_delta_seconds=max_header_api_delta_seconds,
                palxiao_result=palxiao_result,
            )
        except Exception as api_exc:
            if provider == "wayback-api":
                raise
            print(
                f"Archived Header API unavailable for {timestamp}: {api_exc}. "
                "Trying Palxiao structured history before HTTP HTML/CSS recovery."
            )
            result = {}
    else:
        result = {}

    if not result and provider in {"auto", "palxiao"} and palxiao_provider:
        try:
            result = capture_snapshot_palxiao(
                snapshot,
                provider=palxiao_provider,
                force=force,
                historical=palxiao_result,
            )
        except Exception as palxiao_exc:
            if provider == "palxiao":
                raise
            print(f"Palxiao history unavailable for {timestamp}: {palxiao_exc}")

    if not result and provider in {"auto", "wayback-html"}:
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


def capture_snapshot_job(
    snapshot: dict[str, Any],
    *,
    replay_base: str,
    force: bool,
    cdx_api: str,
    max_header_api_delta_seconds: int,
    verify_dom: bool,
    provider: str,
    palxiao_provider: PalxiaoHistoryProvider | None,
) -> dict[str, Any]:
    return capture_snapshot(
        snapshot,
        replay_base=replay_base,
        force=force,
        cdx_api=cdx_api,
        max_header_api_delta_seconds=max_header_api_delta_seconds,
        verify_dom=verify_dom,
        provider=provider,
        palxiao_provider=palxiao_provider,
    )


def iter_capture_results(
    snapshots: list[dict[str, Any]],
    *,
    workers: int,
    replay_base: str,
    force: bool,
    cdx_api: str,
    max_header_api_delta_seconds: int,
    verify_dom: bool,
    provider: str,
    palxiao_provider: PalxiaoHistoryProvider | None,
) -> Iterable[tuple[dict[str, Any], dict[str, Any] | None, Exception | None]]:
    """Capture snapshots concurrently, leaving shared writes serialized in core.archive_capture."""
    if workers <= 1:
        for snapshot in snapshots:
            try:
                yield snapshot, capture_snapshot_job(
                    snapshot,
                    replay_base=replay_base,
                    force=force,
                    cdx_api=cdx_api,
                    max_header_api_delta_seconds=max_header_api_delta_seconds,
                    verify_dom=verify_dom,
                    provider=provider,
                    palxiao_provider=palxiao_provider,
                ), None
            except Exception as exc:
                yield snapshot, None, exc
        return

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="wayback-capture",
    ) as executor:
        futures = {
            executor.submit(
                capture_snapshot_job,
                snapshot,
                replay_base=replay_base,
                force=force,
                cdx_api=cdx_api,
                max_header_api_delta_seconds=max_header_api_delta_seconds,
                verify_dom=verify_dom,
                provider=provider,
                palxiao_provider=palxiao_provider,
            ): snapshot
            for snapshot in snapshots
        }
        for future in as_completed(futures):
            snapshot = futures[future]
            try:
                yield snapshot, future.result(), None
            except Exception as exc:
                yield snapshot, None, exc


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
        choices=("monthly", "weekly", "daily", "3h"),
        default="3h",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "wayback-api", "palxiao", "wayback-html"),
        default="auto",
        help="Historical source priority. auto uses Wayback API, Palxiao, then HTML/CSS.",
    )
    parser.add_argument("--snapshot", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_CAPTURE_WORKERS,
        help="Parallel network capture workers; shared archive/index writes remain serialized.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="Run --checkpoint-script after this many completed capture actions.",
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
        "--reference-repo",
        help="Local MikuFan039.github.io checkout used before Wayback fallback.",
    )
    parser.add_argument(
        "--reference-commit",
        default=MIKUFAN039_REFERENCE_COMMIT,
    )
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
    if args.workers < 1:
        parser.error("--workers must be at least 1")
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

    palxiao_provider = None
    if args.provider in {"auto", "palxiao"}:
        palxiao_provider = PalxiaoHistoryProvider(
            cache_path=core.DATA_DIR / "cache" / "providers" / "palxiao-index.json"
        )

    reference_covered_dates: set[dt.date] = set()
    if args.reference_repo:
        try:
            reference_covered_dates = import_reference_repository(
                Path(args.reference_repo),
                start=start,
                end=end,
                commit=args.reference_commit,
                force=args.force,
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
        ) as exc:
            parser.error(str(exc))
    checkpoint_reference_import(
        reference_covered_dates,
        args.checkpoint_script,
    )

    match_cache = load_wayback_match_cache() if args.cadence == "3h" else None
    if match_cache is not None:
        archive_matches = imported_wayback_matches(reusable_only=True)
        cached_matches = match_cache.setdefault("matches", {})
        for (date_text, slot), timestamps in archive_matches.items():
            if not timestamps:
                continue
            date_matches = cached_matches.setdefault(date_text, {})
            if str(slot) not in date_matches:
                date_matches[str(slot)] = {
                    "timestamp": max(timestamps),
                    "source": "archive",
                }

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
    elif args.provider == "palxiao" and date_range_fully_covered(
        start,
        end,
        reference_covered_dates,
    ):
        snapshots = []
    elif args.provider == "palxiao":
        snapshots = discover_palxiao_snapshots(
            start,
            end,
            provider=palxiao_provider,
        )
    else:
        snapshots = discover_snapshots(
            start,
            end,
            cadence=args.cadence,
            api_url=args.availability_api,
            cdx_api=args.cdx_api,
            excluded_dates=reference_covered_dates,
            match_cache=(match_cache or {}).get("matches")
            if match_cache is not None
            else None,
        )
        if (
            args.provider == "auto"
            and palxiao_provider
            and not date_range_fully_covered(start, end, reference_covered_dates)
        ):
            try:
                palxiao_snapshots = discover_palxiao_snapshots(
                    start,
                    end,
                    provider=palxiao_provider,
                )
                by_date = {
                    item["timestamp"][:8]: item
                    for item in palxiao_snapshots
                }
                existing_dates = {item["timestamp"][:8] for item in snapshots}
                for item in snapshots:
                    palxiao_item = by_date.get(item["timestamp"][:8])
                    if palxiao_item:
                        item["palxiaoDate"] = palxiao_item["palxiaoDate"]
                snapshots.extend(
                    item
                    for item in palxiao_snapshots
                    if item["timestamp"][:8] not in existing_dates
                )
                snapshots.sort(key=lambda item: item["timestamp"])
            except Exception as exc:
                print(f"Palxiao date discovery unavailable: {exc}")

    match_cache_changed = False
    if match_cache is not None:
        match_cache_changed = save_wayback_match_cache(match_cache)

    if reference_covered_dates:
        before_reference_filter = len(snapshots)
        snapshots = [
            snapshot
            for snapshot in snapshots
            if (
                (
                    dt.date.fromisoformat(snapshot_target_date(snapshot))
                    if snapshot_target_date(snapshot)
                    else snapshot_moment(snapshot["timestamp"]).date()
                )
                not in reference_covered_dates
            )
        ]
        removed_reference = before_reference_filter - len(snapshots)
        if removed_reference:
            print(
                f"Skipped {removed_reference} snapshots covered by the reference repository."
            )

    if args.limit > 0:
        snapshots = snapshots[: args.limit]
    skipped_known = 0
    if not args.force:
        known_slots = imported_wayback_slots(reusable_only=True)
        known_matches = imported_wayback_matches(reusable_only=True)
        known_palxiao = imported_palxiao_dates()
        def is_known(snapshot: dict[str, Any]) -> bool:
            if snapshot.get("palxiaoDate") in known_palxiao:
                return True
            mappings = snapshot_target_mappings(snapshot)
            if mappings:
                return all(
                    snapshot["timestamp"] in known_matches.get(mapping, set())
                    for mapping in mappings
                )
            existing_slots = known_slots.get(snapshot["timestamp"], set())
            target = set(snapshot_target_slots(snapshot))
            return bool(existing_slots) and target.issubset(existing_slots)

        skipped_known = sum(is_known(snapshot) for snapshot in snapshots)
        snapshots = [
            snapshot
            for snapshot in snapshots
            if not is_known(snapshot)
        ]
        if skipped_known:
            print(f"Skipped {skipped_known} already imported Wayback snapshots.")
    print(json.dumps({"snapshotCount": len(snapshots)}, ensure_ascii=False))
    if args.discovery_only:
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return

    existing_index_changed = core.rebuild_index()
    if existing_index_changed:
        print("Rebuilt index from existing archives before historical capture.")
        if args.checkpoint_script:
            run_checkpoint(
                args.checkpoint_script,
                processed=0,
                succeeded=0,
                changed=1,
                final=False,
            )
    if not snapshots:
        if skipped_known:
            if args.checkpoint_script and match_cache_changed:
                run_checkpoint(
                    args.checkpoint_script,
                    processed=0,
                    succeeded=0,
                    changed=1,
                    final=True,
                )
            print("All discovered Wayback snapshots were already imported.")
            return
        if reference_covered_dates:
            if args.checkpoint_script and match_cache_changed:
                run_checkpoint(
                    args.checkpoint_script,
                    processed=0,
                    succeeded=0,
                    changed=0,
                    final=True,
                )
            print("Reference repository covered the requested range; no Wayback import needed.")
            return
        raise SystemExit("No Wayback snapshots were discovered in the requested range.")

    workers = args.workers
    if args.verify_dom and workers > 1:
        print("--verify-dom requires serialized capture; forcing --workers=1")
        workers = 1

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    unchanged_results = 0
    actions_since_checkpoint = 0
    changed_since_checkpoint = 0
    processed = 0
    completed_results = iter_capture_results(
        snapshots,
        workers=workers,
        replay_base=args.replay_base,
        force=args.force,
        cdx_api=args.cdx_api,
        max_header_api_delta_seconds=args.header_api_max_delta_seconds,
        verify_dom=args.verify_dom,
        provider=args.provider,
        palxiao_provider=palxiao_provider,
    )
    for processed, (snapshot, result, error) in enumerate(completed_results, start=1):
        if error is not None:
            failure = {
                "timestamp": snapshot["timestamp"],
                "error": str(error),
            }
            failures.append(failure)
            print(json.dumps(failure, ensure_ascii=False))
            continue

        try:
            results.append(result)
            status = str(result.get("status") or "")
            if status == "unchanged":
                unchanged_results += 1
                continue
            print(json.dumps(result, ensure_ascii=False))
            actions_since_checkpoint += 1
            if status in {"created", "updated"}:
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
            and actions_since_checkpoint >= args.checkpoint_every
            and processed < len(snapshots)
        ):
            run_checkpoint(
                args.checkpoint_script,
                processed=processed,
                succeeded=len(results),
                changed=changed_since_checkpoint,
                final=False,
            )
            actions_since_checkpoint = 0
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
    if unchanged_results:
        print(f"Skipped {unchanged_results} unchanged Wayback captures.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
