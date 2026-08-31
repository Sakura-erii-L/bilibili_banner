import json
import tempfile
import unittest
from pathlib import Path

from backend import capture
from scripts.audit_archives import audit


class ArchiveIntegrityTests(unittest.TestCase):
    def write_archive(self, root: Path, name: str, manifest: dict, files: dict[str, bytes]) -> Path:
        folder = root / "archive" / name
        folder.mkdir(parents=True)
        for filename, content in files.items():
            path = folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest["hashes"] = capture.calculate_manifest_hashes(folder, manifest)
        manifest["contentHash"] = manifest["hashes"]["contentHash"]
        (folder / "banner.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return folder

    def test_static_banner_remains_one_original_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {
                "mode": "static",
                "type": ["static"],
                "static": {"file": "static.png", "tag": "img", "assetType": "image"},
                "layers": [],
                "interaction": {"model": "none", "effects": []},
                "structureEvidence": {"layerCount": 0, "visibleMediaCount": 1, "signals": {}},
            }
            self.write_archive(root, "static", manifest, {"static.png": b"real-static"})
            self.assertEqual(audit(root)["issues"], [])

    def test_layered_parallax_keeps_each_image_and_interaction_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {
                "mode": "split",
                "type": ["layered", "interactive"],
                "static": None,
                "layers": [
                    {"index": 0, "file": "layer0.webp", "tag": "img", "assetType": "image", "motion": {"matrixDelta": [[0, 0, 0, 0, 1, 0]]}},
                    {"index": 1, "file": "layer1.webp", "tag": "img", "assetType": "image", "motion": {"matrixDelta": [[0, 0, 0, 0, -1, 0]]}},
                ],
                "interaction": {"model": "bilibili-sampled-horizontal-v1", "effects": ["matrix"]},
                "structureEvidence": {"layerCount": 2, "visibleMediaCount": 2, "signals": {"hasInteraction": True}},
            }
            folder = self.write_archive(
                root,
                "layered",
                manifest,
                {
                    "layer0.webp": b"layer-zero",
                    "layer1.webp": b"layer-one",
                },
            )
            first = capture.calculate_manifest_hashes(folder, manifest)
            manifest["interaction"]["effects"].append("opacity")
            second = capture.calculate_manifest_hashes(folder, manifest)
            self.assertNotEqual(first["interactionHash"], second["interactionHash"])
            self.assertNotEqual(first["contentHash"], second["contentHash"])
            manifest["hashes"] = second
            manifest["contentHash"] = second["contentHash"]
            (folder / "banner.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(audit(root)["issues"], [])

    def test_unknown_interaction_uses_source_script_to_avoid_false_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "source").mkdir()
            script = folder / "source/script.js"
            script.write_bytes(b"layer.style.transform = firstCurve(x)")
            manifest = {
                "mode": "split",
                "type": ["layered", "interactive"],
                "layers": [],
                "static": None,
                "interaction": {"model": "none", "effects": []},
                "sourceFiles": {"scripts": "source/script.js"},
                "structureEvidence": {"signals": {"hasInteraction": True}},
            }
            first = capture.calculate_manifest_hashes(folder, manifest)
            script.write_bytes(b"layer.style.transform = secondCurve(x)")
            second = capture.calculate_manifest_hashes(folder, manifest)
            self.assertNotEqual(first["interactionHash"], second["interactionHash"])
            self.assertNotEqual(first["contentHash"], second["contentHash"])

    def test_source_script_hash_is_stable_across_line_endings(self) -> None:
        manifest = {
            "mode": "split",
            "type": ["layered", "interactive"],
            "layers": [],
            "interaction": {"model": "unknown", "effects": []},
            "structureEvidence": {"signals": {"hasInteraction": True}},
            "sourceFiles": {"scripts": "source/script.js"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "script.js"
            source.parent.mkdir()
            source.write_bytes(b"line1\nline2\n")
            lf_hash = capture.calculate_manifest_hashes(root, manifest)
            source.write_bytes(b"line1\r\nline2\r\n")
            crlf_hash = capture.calculate_manifest_hashes(root, manifest)
        self.assertEqual(lf_hash, crlf_hash)

    def test_layered_video_interactive_cannot_be_flattened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {
                "mode": "static",
                "type": ["layered", "video", "interactive"],
                "static": {"file": "fallback.png", "tag": "img", "assetType": "image"},
                "layers": [],
                "interaction": {"model": "none", "effects": []},
                "structureEvidence": {
                    "layerCount": 2,
                    "visibleMediaCount": 2,
                    "signals": {"hasVideo": True, "hasInteraction": True},
                },
            }
            self.write_archive(root, "flattened", manifest, {"fallback.png": b"one-frame"})
            reasons = {item["reason"] for item in audit(root)["issues"]}
            self.assertIn("structured-banner-flattened-to-static", reasons)
            self.assertIn("video-banner-flattened-to-static", reasons)


if __name__ == "__main__":
    unittest.main()
