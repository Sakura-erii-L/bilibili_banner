from __future__ import annotations

import copy
import datetime as dt
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:
    from . import bilibili_header_api as header_api
except ImportError:
    from providers import bilibili_header_api as header_api


DEFAULT_COMMIT = "45b760bfa52166727123d775925dea887ac7d71a"
KNOWN_GAME_EXTENSIONS = {
    "springGame2022",
    "summer2022",
    "autumn2022",
}
SHARED_REFERENCE_DIRECTORIES = {
    # The winter Banner reuses the video stored by the fixed reference commit
    # under the spring game directory.
    "2022winter": ("2022spring",),
}
DATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class GalleryEntry:
    reference_id: str
    title: str
    cover: str
    category: str
    group: str
    start_date: dt.date
    end_date: dt.date | None
    original_start_date: str
    original_end_date: str | None

    @property
    def family_key(self) -> str:
        end = self.end_date.isoformat() if self.end_date else "open"
        return f"{self.start_date.isoformat()}_{end}_{self.group or self.category}"


def _parse_reference_date(value: Any) -> tuple[dt.date, str, str | None]:
    original = str(value or "")
    match = DATE_PATTERN.fullmatch(original)
    if not match:
        raise ValueError(f"invalid reference date: {original}")
    year, month, day = (int(item) for item in match.groups())
    first = dt.date(year, month, 1)
    next_month = (
        dt.date(year + 1, 1, 1)
        if month == 12
        else dt.date(year, month + 1, 1)
    )
    last_day = (next_month - dt.timedelta(days=1)).day
    corrected_day = min(day, last_day)
    corrected = dt.date(year, month, corrected_day)
    warning = corrected.isoformat() if corrected.isoformat() != original else None
    return corrected, original, warning


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_reference_tree(source: Path, target: Path) -> list[str]:
    copied: list[str] = []
    for path in source.rglob("*"):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(source).as_posix()
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(relative)
    return sorted(copied)


def _source_path_candidates(source: str) -> list[str]:
    value = str(source or "")
    if not value:
        return []
    candidates = [value.lstrip("/")]
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        path = parsed.path.lstrip("/")
        if path and path not in candidates:
            candidates.append(path)
    return candidates


def _local_reference_file(
    root: Path,
    source: str,
    fallback_roots: Iterable[Path] = (),
) -> str | None:
    for candidate in _source_path_candidates(source):
        for base in (root, *fallback_roots):
            if (base / candidate).is_file():
                return candidate
    return None


def _entry_reference_file(root: Path, reference_id: str, source: str) -> str | None:
    local = _local_reference_file(root, source)
    if local:
        return local
    prefix = f"res/bilibanner/{reference_id}/"
    value = str(source or "")
    if value.startswith(prefix):
        relative = value[len(prefix):]
        return relative if (root / relative).is_file() else None
    return None


def _rewrite_src_fields(
    node: Any,
    root: Path,
    fallback_roots: Iterable[Path] = (),
) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "src" and isinstance(value, str):
                local = _local_reference_file(root, value, fallback_roots)
                if local:
                    node[key] = local
            else:
                _rewrite_src_fields(value, root, fallback_roots)
    elif isinstance(node, list):
        for value in node:
            _rewrite_src_fields(value, root, fallback_roots)


def _parse_reference_split_layer(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.strip():
        value = json.loads(value)
    if isinstance(value, list):
        layers = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            layer = {
                key: copy.deepcopy(item_value)
                for key, item_value in item.items()
                if key != "images"
            }
            images = item.get("images")
            layer["id"] = index
            layer["resources"] = copy.deepcopy(images) if isinstance(images, list) else []
            layers.append(layer)
        return {"version": "legacy-array", "layers": layers}
    return header_api.parse_split_layer(value)


def _time_slots(extensions: dict[str, Any]) -> list[int]:
    time_map = extensions.get("time")
    if not isinstance(time_map, dict) or not time_map:
        return list(range(8))
    keys = sorted(
        int(key)
        for key in time_map
        if str(key).isdigit()
    )
    if not keys:
        return list(range(8))
    slots: list[int] = []
    for slot in range(8):
        seconds = slot * 3 * 60 * 60
        if any(key <= seconds for key in keys):
            slots.append(slot)
    return slots or [0]


def _season_for_date(date: dt.date) -> str:
    if date.month in {3, 4, 5}:
        return "spring"
    if date.month in {6, 7, 8}:
        return "summer"
    if date.month in {9, 10, 11}:
        return "autumn"
    return "winter"


def _reference_layers(
    split_layer: dict[str, Any],
    root: Path,
    fallback_roots: Iterable[Path] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    layers: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, layer in enumerate(split_layer.get("layers") or []):
        if not isinstance(layer, dict):
            continue
        resources: list[dict[str, Any]] = []
        for resource_index, resource in enumerate(layer.get("resources") or []):
            if not isinstance(resource, dict):
                continue
            source = str(resource.get("src") or "")
            local = _local_reference_file(root, source, fallback_roots)
            if not local:
                missing.append(f"layer_{index:03d}_resource_{resource_index:03d}: {source}")
                continue
            resources.append(
                {
                    **copy.deepcopy(resource),
                    "src": source,
                    "file": local,
                    "resourceIndex": resource_index,
                    "tag": header_api.infer_tag(source),
                    "assetType": "video" if header_api.infer_tag(source) == "video" else "image",
                }
            )
        if not resources:
            continue
        first = resources[0]
        layers.append(
            {
                **{
                    key: copy.deepcopy(value)
                    for key, value in layer.items()
                    if key != "resources"
                },
                "index": int(layer.get("id", index) or index),
                "tag": first["tag"],
                "assetType": first["assetType"],
                "src": first["src"],
                "file": first["file"],
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
                "captureTargetTag": first["tag"],
            }
        )
    return layers, missing


def _iter_src_values(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "src" and isinstance(value, str):
                yield value
            else:
                yield from _iter_src_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_src_values(value)


class ReferenceRepository:
    def __init__(
        self,
        root: Path,
        *,
        commit: str = DEFAULT_COMMIT,
        time_zone: str = "Asia/Shanghai",
    ) -> None:
        self.root = root.resolve()
        self.commit = commit
        self.time_zone = time_zone
        self.banner_root = self.root / "res" / "bilibanner"
        self.warnings: list[dict[str, str]] = []

    def entries(self) -> list[GalleryEntry]:
        gallery_path = self.banner_root / "gallery.json"
        gallery = _read_json(gallery_path)
        entries: list[GalleryEntry] = []
        for category in gallery.get("categories") or []:
            if not isinstance(category, dict):
                continue
            category_id = str(category.get("id") or "")
            category_file = self.banner_root / str(category.get("file") or "")
            if not category_file.is_file():
                continue
            rows = _read_json(category_file)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                try:
                    start, original_start, corrected_start = _parse_reference_date(row.get("startDate"))
                    end = None
                    original_end = None
                    corrected_end = None
                    if row.get("endDate"):
                        end, original_end, corrected_end = _parse_reference_date(row.get("endDate"))
                    if corrected_start:
                        self.warnings.append(
                            {
                                "referenceId": str(row["id"]),
                                "originalDate": original_start,
                                "correctedDate": corrected_start,
                            }
                        )
                    if corrected_end:
                        self.warnings.append(
                            {
                                "referenceId": str(row["id"]),
                                "originalDate": original_end or "",
                                "correctedDate": corrected_end,
                            }
                        )
                    if end and end < start:
                        continue
                except ValueError:
                    continue
                entries.append(
                    GalleryEntry(
                        reference_id=str(row["id"]),
                        title=str(row.get("title") or ""),
                        cover=str(row.get("cover") or ""),
                        category=category_id,
                        group=str(row.get("group") or category_id),
                        start_date=start,
                        end_date=end,
                        original_start_date=original_start,
                        original_end_date=original_end,
                    )
                )
        return entries

    def covered_entries(self, start: dt.date, end: dt.date) -> list[GalleryEntry]:
        """Return reference entries with an explicit historical end date.

        An open-ended gallery entry identifies the current Banner, but does not
        prove that the same Banner was displayed throughout its past interval.
        Those dates must be recovered from Wayback instead.
        """
        result = []
        for entry in self.entries():
            if entry.end_date is None:
                continue
            entry_end = entry.end_date
            if entry.start_date <= end and entry_end >= start:
                result.append(entry)
        return result

    def _manifest_payload(self, entry: GalleryEntry) -> tuple[dict[str, Any], Path]:
        entry_root = self.banner_root / entry.reference_id
        manifest_path = entry_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"reference manifest not found: {manifest_path}")
        payload = _read_json(manifest_path)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"reference manifest has no data: {manifest_path}")
        split_layer = _parse_reference_split_layer(data.get("split_layer"))
        shared_roots = tuple(
            self.banner_root / directory
            for directory in SHARED_REFERENCE_DIRECTORIES.get(
                entry.reference_id,
                (),
            )
        )
        _rewrite_src_fields(split_layer, entry_root, shared_roots)
        extensions = copy.deepcopy(split_layer.get("extensions") or {})
        layers, missing = _reference_layers(
            split_layer,
            entry_root,
            shared_roots,
        )
        shared_files: list[tuple[Path, str]] = []
        for source in _iter_src_values(split_layer):
            if _local_reference_file(entry_root, source):
                continue
            for shared_root in shared_roots:
                local = _local_reference_file(shared_root, source)
                if local:
                    shared_files.append((shared_root / local, local))
                    break
        pic = str(data.get("pic") or "")
        litpic = str(data.get("litpic") or "")
        static_source = pic or litpic
        static_file = _local_reference_file(entry_root, static_source)
        if static_source and not static_file:
            missing.append(f"fallback: {static_source}")
        cover_file = _entry_reference_file(entry_root, entry.reference_id, entry.cover)
        if entry.cover and not cover_file:
            if static_file:
                cover_file = static_file
            else:
                missing.append(f"cover: {entry.cover}")
        is_game = bool(set(extensions) & KNOWN_GAME_EXTENSIONS)
        model = "mikufan-reference-v1"
        mode = "split" if bool(data.get("is_split_layer")) or layers else "static"
        if not layers and static_file:
            mode = "static"
        source_manifest = copy.deepcopy(payload)
        source_data = source_manifest.get("data")
        if isinstance(source_data, dict):
            source_data["split_layer"] = json.dumps(
                split_layer,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if entry.reference_id == "2022winter" and layers:
                source_data["is_split_layer"] = 1
        _rewrite_src_fields(source_manifest, entry_root, shared_roots)
        return {
            "sourcePayload": source_manifest,
            "data": data,
            "splitLayer": split_layer,
            "extensions": extensions,
            "layers": layers,
            "missing": missing,
            "pic": pic,
            "litpic": litpic,
            "staticFile": static_file,
            "coverFile": cover_file,
            "mode": mode,
            "model": model,
            "referenceMode": "interactive" if is_game else "normal",
            "slots": _time_slots(extensions),
            "sharedFiles": shared_files,
        }, entry_root

    def build_manifest(
        self,
        entry: GalleryEntry,
        date: dt.date,
        temp: Path,
    ) -> dict[str, Any]:
        payload, entry_root = self._manifest_payload(entry)
        copied_files = _copy_reference_tree(entry_root, temp)
        for source, relative in payload["sharedFiles"]:
            destination = temp / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if relative not in copied_files:
                copied_files.append(relative)
        copied_files.sort()
        source_file = temp / "reference-manifest.json"
        source_file.write_text(
            json.dumps(payload["sourcePayload"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        source_files = {"json": "reference-manifest.json"}
        auxiliary = [
            {
                "role": "reference",
                "file": file,
                "src": file,
                "assetType": "other",
            }
            for file in copied_files
        ]
        static = None
        if payload["staticFile"]:
            static = {
                "src": payload["pic"] or payload["litpic"],
                "file": payload["staticFile"],
                "tag": header_api.infer_tag(payload["staticFile"]),
                "assetType": "image",
                "sourceKind": "reference",
            }
        evidence = {
            "root": {"source": "mikufan039", "referenceId": entry.reference_id},
            "layerCount": len(payload["layers"]),
            "visibleMediaCount": len(payload["layers"]),
            "resourceUrls": [],
            "media": [],
            "signals": {
                "isSplitLayer": payload["mode"] == "split",
                "hasInteraction": payload["referenceMode"] == "interactive",
                "hasVideo": any(item.get("assetType") == "video" for item in payload["layers"]),
            },
        }
        captured_at = dt.datetime.combine(
            date,
            dt.time(0, 0),
            tzinfo=ZoneInfo(self.time_zone),
        ).isoformat(timespec="seconds")
        manifest: dict[str, Any] = {
            "version": 11.0,
            "capturedAt": captured_at,
            "date": date.isoformat(),
            "season": _season_for_date(date),
            "source": {
                "page": "https://www.bilibili.com/",
                "captureMethod": "mikufan039-reference",
                "provider": "mikufan039-reference",
                "referenceId": entry.reference_id,
                "referenceCommit": self.commit,
                "referenceTitle": entry.title,
                "referenceCover": entry.cover,
                "referenceCategory": entry.category,
                "coverageStartDate": entry.start_date.isoformat(),
                "coverageEndDate": entry.end_date.isoformat() if entry.end_date else None,
                "originalStartDate": entry.original_start_date,
                "originalEndDate": entry.original_end_date,
            },
            "reference": {
                "id": entry.reference_id,
                "commit": self.commit,
                "manifest": "reference-manifest.json",
                "cover": payload["coverFile"],
            },
            "provenance": {
                "primaryProvider": "mikufan039-reference",
                "supportingProviders": [],
                "confidence": "high",
                "agreement": {},
                "conflicts": [],
            },
            "viewport": {"width": 1650, "height": 800},
            "banner": {"referenceHeight": 155},
            "mode": payload["mode"],
            "static": static,
            "layers": payload["layers"],
            "auxiliaryAssets": auxiliary,
            "interaction": {"model": payload["model"], "effects": ["reference"]},
            "referenceMode": payload["referenceMode"],
            "preview_image": payload["coverFile"],
            "sourceFiles": source_files,
            "api": {
                "id": payload["data"].get("id"),
                "name": payload["data"].get("name") or "",
                "isSplitLayer": bool(payload["data"].get("is_split_layer")),
                "extensions": payload["extensions"],
            },
            "extensions": payload["extensions"],
            "timeZone": self.time_zone,
            "lastObservedAt": captured_at,
            "slots": payload["slots"],
        }
        # Preserve explicit date corrections without adding a second date model.
        for warning in self.warnings:
            if warning.get("referenceId") == entry.reference_id:
                manifest["source"]["originalDate"] = warning["originalDate"]
                manifest["source"]["correctedDate"] = warning["correctedDate"]
        try:
            from .. import capture
        except ImportError:
            import capture

        capture.enrich_manifest_metadata(
            manifest,
            evidence,
            missing_assets=payload["missing"],
        )
        return manifest

    def iter_dates(self, entry: GalleryEntry, start: dt.date, end: dt.date) -> Iterable[dt.date]:
        current = max(start, entry.start_date)
        last = min(end, entry.end_date or end)
        while current <= last:
            yield current
            current += dt.timedelta(days=1)
