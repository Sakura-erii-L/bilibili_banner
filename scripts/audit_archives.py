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
        if (manifest.get("mode") == "split" or "layered" in types) and not layers:
            issues.append({"archive": folder.name, "reason": "layered-without-layers"})
        if "static" in types and ("layered" in types or "video" in types):
            issues.append({"archive": folder.name, "reason": "structured-content-marked-static"})
        if expected_layers > len(layers):
            warnings.append({
                "archive": folder.name,
                "reason": f"missing-layers:{expected_layers - len(layers)}",
            })
        if (expected_layers > 1 or visible_media > 1) and not layers and manifest.get("static"):
            issues.append({
                "archive": folder.name,
                "reason": "structured-banner-flattened-to-static",
            })
        if signals.get("hasVideo") and "video" not in types:
            issues.append({"archive": folder.name, "reason": "video-evidence-without-video-type"})
        saved_video = any(
            (layer.get("assetType") == "video" or layer.get("tag") == "video")
            and layer.get("file")
            for layer in layers
        ) or bool(
            ((manifest.get("static") or {}).get("assetType") == "video"
             or (manifest.get("static") or {}).get("tag") == "video")
            and (manifest.get("static") or {}).get("file")
        )
        if signals.get("hasVideo") and not saved_video and manifest.get("static"):
            issues.append({
                "archive": folder.name,
                "reason": "video-banner-flattened-to-static",
            })
        for layer in layers:
            file = str(layer.get("file") or "")
            if not file or not (folder / file).is_file():
                issues.append({
                    "archive": folder.name,
                    "reason": f"missing-layer-file:{layer.get('index', '?')}",
                })
        static_file = str((manifest.get("static") or {}).get("file") or "")
        if static_file and not (folder / static_file).is_file():
            issues.append({"archive": folder.name, "reason": "missing-static-file"})
        calculated = capture.calculate_manifest_hashes(folder, manifest)
        if manifest.get("hashes") and manifest["hashes"] != calculated:
            issues.append({"archive": folder.name, "reason": "hashes-do-not-match-resources"})
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
