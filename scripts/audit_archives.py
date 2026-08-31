from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import capture


def audit(data_dir: Path) -> dict[str, object]:
    archive_dir = data_dir / "archive"
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checked = 0
    for folder in sorted(archive_dir.iterdir() if archive_dir.exists() else []):
        if not folder.is_dir():
            continue
        manifest = capture.read_manifest(folder)
        if not manifest:
            issues.append({"archive": folder.name, "reason": "manifest-missing-or-invalid"})
            continue
        checked += 1
        types = set(capture.manifest_types(manifest))
        layers = manifest.get("layers") or []
        evidence = manifest.get("structureEvidence") or {}
        signals = evidence.get("signals") or {}
        expected_layers = int(evidence.get("layerCount") or 0)
        visible_media = int(evidence.get("visibleMediaCount") or 0)
        api = manifest.get("api") or {}
        api_split = bool(api.get("isSplitLayer"))
        api_expected_layers = int(api.get("layerCount") or 0)
        api_expected_assets = int(api.get("assetCount") or 0)
        if (manifest.get("mode") == "split" or "layered" in types) and not layers:
            target = warnings if manifest.get("completeness") == "partial" else issues
            target.append({"archive": folder.name, "reason": "layered-without-layers"})
        if api_split and not layers:
            target = warnings if manifest.get("completeness") == "partial" else issues
            target.append({"archive": folder.name, "reason": "api-split-layer-without-saved-layers"})
        if api_split and manifest.get("mode") != "split":
            issues.append({"archive": folder.name, "reason": "api-split-layer-marked-non-split"})
        if api_split and (manifest.get("interaction") or {}).get("model") != "bilibili-header-api-v1":
            issues.append({"archive": folder.name, "reason": "api-split-layer-without-api-renderer"})
        if "static" in types and ("layered" in types or "video" in types):
            issues.append({"archive": folder.name, "reason": "structured-content-marked-static"})
        if expected_layers > len(layers):
            warnings.append({
                "archive": folder.name,
                "reason": f"missing-layers:{expected_layers - len(layers)}",
            })
        if api_expected_layers > len(layers):
            target = issues if manifest.get("completeness") == "complete" else warnings
            target.append({
                "archive": folder.name,
                "reason": f"api-missing-layers:{api_expected_layers - len(layers)}",
            })
        if (
            (expected_layers > 1 or visible_media > 1)
            and not layers
            and manifest.get("static")
            and manifest.get("mode") != "split"
        ):
            issues.append({
                "archive": folder.name,
                "reason": "structured-banner-flattened-to-static",
            })
        if signals.get("hasVideo") and "video" not in types:
            issues.append({"archive": folder.name, "reason": "video-evidence-without-video-type"})
        saved_video = False
        saved_api_assets = 0
        for layer in layers:
            resources = layer.get("resources") or []
            if isinstance(resources, list) and resources:
                for resource in resources:
                    if not isinstance(resource, dict):
                        continue
                    file = str(resource.get("file") or "")
                    if file and (folder / file).is_file():
                        saved_api_assets += 1
                        if (
                            resource.get("tag") == "video"
                            or str(resource.get("contentType") or "").lower().startswith("video/")
                            or str(file).lower().endswith((".webm", ".mp4", ".m3u8"))
                        ):
                            saved_video = True
            elif (
                (layer.get("assetType") == "video" or layer.get("tag") == "video")
                and layer.get("file")
            ):
                saved_video = True
        if (
            ((manifest.get("static") or {}).get("assetType") == "video"
             or (manifest.get("static") or {}).get("tag") == "video")
            and (manifest.get("static") or {}).get("file")
        ):
            saved_video = True
        if (
            signals.get("hasVideo")
            and not saved_video
            and not layers
            and manifest.get("static")
            and manifest.get("mode") != "split"
        ):
            issues.append({
                "archive": folder.name,
                "reason": "video-banner-flattened-to-static",
            })
        if api_expected_assets > saved_api_assets:
            target = issues if manifest.get("completeness") == "complete" else warnings
            target.append({
                "archive": folder.name,
                "reason": f"api-missing-assets:{api_expected_assets - saved_api_assets}",
            })
        if api_split:
            for asset in manifest.get("assets") or []:
                if asset.get("role") == "primary" and asset.get("local_file") == (manifest.get("static") or {}).get("file"):
                    issues.append({"archive": folder.name, "reason": "api-pic-used-as-primary"})
                    break
        for layer in layers:
            file = str(layer.get("file") or "")
            if not file or not (folder / file).is_file():
                issues.append({
                    "archive": folder.name,
                    "reason": f"missing-layer-file:{layer.get('index', '?')}",
                })
            resources = layer.get("resources") or []
            if isinstance(resources, list):
                for resource_index, resource in enumerate(resources):
                    if not isinstance(resource, dict):
                        continue
                    resource_file = str(resource.get("file") or "")
                    if not resource_file or not (folder / resource_file).is_file():
                        issues.append({
                            "archive": folder.name,
                            "reason": (
                                f"missing-layer-resource:{layer.get('index', '?')}:"
                                f"{resource.get('resourceIndex', resource_index)}"
                            ),
                        })
        static_file = str((manifest.get("static") or {}).get("file") or "")
        if static_file and not (folder / static_file).is_file():
            issues.append({"archive": folder.name, "reason": "missing-static-file"})
        calculated = capture.calculate_manifest_hashes(folder, manifest)
        stored_hashes = manifest.get("hashes")
        if stored_hashes:
            comparable = {
                key: calculated[key]
                for key in stored_hashes
                if key in calculated
            }
            if stored_hashes != comparable:
                issues.append({"archive": folder.name, "reason": "hashes-do-not-match-resources"})
            if "canonicalContentHash" not in stored_hashes:
                warnings.append({"archive": folder.name, "reason": "legacy-manifest-without-canonical-hash"})
        elif not manifest.get("hashes"):
            warnings.append({"archive": folder.name, "reason": "legacy-manifest-without-structure-hashes"})
        if not manifest.get("type"):
            warnings.append({"archive": folder.name, "reason": "legacy-manifest-without-type"})

    return {"checked": checked, "issues": issues, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Banner manifests without rasterizing them.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    result = audit(Path(args.data_dir).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["issues"] or (args.strict and result["warnings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
