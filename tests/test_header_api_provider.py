from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import capture
from backend.providers import bilibili_header_api as header_api
from scripts.audit_archives import audit


class HeaderApiProviderTests(unittest.TestCase):
    def sample_payload(self) -> dict:
        return {
            "code": 0,
            "message": "0",
            "data": {
                "id": 123,
                "name": "api banner",
                "pic": "//i0.hdslb.com/bfs/banner/fallback.webp",
                "litpic": "//i0.hdslb.com/bfs/banner/logo.webp",
                "is_split_layer": 1,
                "split_layer": json.dumps(
                    {
                        "version": "1",
                        "layers": [
                            {
                                "id": 10,
                                "name": "frames",
                                "resources": [
                                    {"id": 1, "src": "//i0.hdslb.com/bfs/banner/frame-a.webp", "duration": 100},
                                    {"id": 2, "src": "//i0.hdslb.com/bfs/banner/frame-b.webp", "duration": 200},
                                ],
                                "scale": {"initial": 1.1, "offset": 0.05},
                                "translate": {"initial": [0, 0], "offset": [18, 0]},
                            },
                            {
                                "id": 11,
                                "name": "video",
                                "resources": [
                                    {"id": 3, "src": "https://i1.hdslb.com/bfs/banner/effect.webm"}
                                ],
                                "opacity": {"initial": 0.8, "offset": 0.2, "wrap": "alternate"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        }

    def test_parse_split_layer_preserves_all_resources_and_parameters(self) -> None:
        parsed = header_api.parse_header_api(
            self.sample_payload(),
            header_api.DEFAULT_ENDPOINTS[0],
        )
        self.assertTrue(parsed["is_split_layer"])
        self.assertEqual(len(parsed["layers"]), 2)
        self.assertEqual(len(parsed["resources"]), 3)
        self.assertEqual(parsed["layers"][0]["translate"]["offset"], [18, 0])
        self.assertEqual(parsed["resources"][2]["src"], "https://i1.hdslb.com/bfs/banner/effect.webm")
        self.assertEqual(parsed["pic"], "https://i0.hdslb.com/bfs/banner/fallback.webp")

    def test_extensions_time_preserves_candidates_and_discovers_nested_resources(self) -> None:
        payload = self.sample_payload()
        split = json.loads(payload["data"]["split_layer"])
        split["extensions"] = {
            "time": {
                "0": [
                    {"layers": [{"resources": [{"src": "https://i0.hdslb.com/time-a.webm"}]}]},
                    {"layers": [{"resources": [{"src": "https://i0.hdslb.com/time-b.webm"}]}]},
                ],
                "57600": [{"layers": [{"resources": [{"src": "https://i0.hdslb.com/time-c.webm"}]}]}],
            },
            "unknown": {"src": "https://i0.hdslb.com/unknown.bin"},
        }
        payload["data"]["split_layer"] = json.dumps(split)
        parsed = header_api.parse_header_api(payload, header_api.DEFAULT_ENDPOINTS[0])
        self.assertEqual(len(parsed["extensions"]["time"]["0"]), 2)
        extension_sources = {
            item["src"] for item in parsed["resources"] if "extensionPath" in item
        }
        self.assertEqual(
            extension_sources,
            {
                "https://i0.hdslb.com/time-a.webm",
                "https://i0.hdslb.com/time-b.webm",
                "https://i0.hdslb.com/time-c.webm",
                "https://i0.hdslb.com/unknown.bin",
            },
        )

    def test_api_capture_downloads_time_extension_resources_recursively(self) -> None:
        payload = self.sample_payload()
        split = json.loads(payload["data"]["split_layer"])
        split["extensions"] = {
            "time": {
                "0": [{"layers": [{"resources": [{"src": "https://i0.hdslb.com/time.webm"}]}]}],
            },
        }
        payload["data"]["split_layer"] = json.dumps(split)
        api_data = header_api.parse_header_api(payload, header_api.DEFAULT_ENDPOINTS[0])
        original = (capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            try:
                capture.DATA_DIR = root
                capture.ARCHIVE_DIR = root / "archive"
                capture.CURRENT_DIR = root / "current"

                def fake_download(src, folder, stem, **_kwargs):
                    filename = stem + ".webm"
                    (folder / filename).write_bytes(src.encode("utf-8"))
                    return {
                        "src": src,
                        "requestedSrc": src,
                        "normalizedIdentity": header_api.normalized_identity(src),
                        "file": filename,
                        "contentType": "video/webm",
                        "tag": "video",
                    }

                with mock.patch("backend.capture._download_http_asset", side_effect=fake_download):
                    result = capture.capture_header_api_payload(
                        api_data,
                        moment=dt.datetime(2026, 8, 31, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                        force=False,
                        update_current=False,
                    )
                manifest = capture.read_manifest(Path(result["archive"]))
                self.assertIsNotNone(manifest)
                extension_resource = manifest["api"]["extensions"]["time"]["0"][0]["layers"][0]["resources"][0]
                self.assertTrue(extension_resource["src"].startswith("extension_"))
                self.assertTrue((Path(result["archive"]) / extension_resource["src"]).is_file())
            finally:
                capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR = original

    def test_api_capture_keeps_layers_video_and_fallback_separate(self) -> None:
        api_data = header_api.parse_header_api(
            self.sample_payload(),
            header_api.DEFAULT_ENDPOINTS[0],
        )
        original = (capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            try:
                capture.DATA_DIR = root
                capture.ARCHIVE_DIR = root / "archive"
                capture.CURRENT_DIR = root / "current"

                def fake_download(src, folder, stem, **_kwargs):
                    ext = ".webm" if ".webm" in src else ".webp"
                    filename = stem + ext
                    (folder / filename).write_bytes(("asset:" + src).encode("utf-8"))
                    tag = "video" if ext == ".webm" else "img"
                    return {
                        "src": src,
                        "requestedSrc": src,
                        "normalizedIdentity": header_api.normalized_identity(src),
                        "file": filename,
                        "contentType": "video/webm" if tag == "video" else "image/webp",
                        "tag": tag,
                    }

                with mock.patch("backend.capture._download_http_asset", side_effect=fake_download):
                    result = capture.capture_header_api_payload(
                        api_data,
                        moment=dt.datetime(2026, 8, 31, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                        force=False,
                        update_current=True,
                    )

                manifest = capture.read_manifest(Path(result["archive"]))
                self.assertIsNotNone(manifest)
                self.assertEqual(manifest["version"], 11.0)
                self.assertEqual(manifest["mode"], "split")
                self.assertEqual(manifest["interaction"]["model"], "bilibili-header-api-v1")
                self.assertEqual(len(manifest["layers"]), 2)
                self.assertEqual(len(manifest["layers"][0]["resources"]), 2)
                self.assertEqual(len(manifest["layers"][1]["resources"]), 1)
                self.assertIn("layered", manifest["type"])
                self.assertIn("video", manifest["type"])
                self.assertIn("interactive", manifest["type"])
                self.assertEqual(manifest["completeness"], "complete")
                self.assertEqual(manifest["fallback_image"], manifest["static"]["file"])
                fallback_assets = [
                    item for item in manifest["assets"]
                    if item.get("local_file") == manifest["static"]["file"]
                ]
                self.assertEqual(fallback_assets[0]["role"], "fallback_image")
                self.assertTrue((Path(result["archive"]) / "source/api.json").is_file())
                self.assertEqual(audit(root)["issues"], [])
            finally:
                capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR = original

    def test_api_config_changes_content_hash_without_changing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "layer.webp").write_bytes(b"same")
            manifest = {
                "mode": "split",
                "type": ["layered", "interactive"],
                "layers": [{
                    "index": 0,
                    "file": "layer.webp",
                    "tag": "img",
                    "assetType": "image",
                    "apiConfig": {"translate": {"initial": [0, 0], "offset": [10, 0]}},
                }],
                "interaction": {"model": "bilibili-header-api-v1", "effects": ["translate"]},
            }
            first = capture.calculate_manifest_hashes(folder, manifest)
            manifest["layers"][0]["apiConfig"]["translate"]["offset"] = [20, 0]
            second = capture.calculate_manifest_hashes(folder, manifest)
            self.assertEqual(first["resourceHash"], second["resourceHash"])
            self.assertNotEqual(first["interactionHash"], second["interactionHash"])
            self.assertNotEqual(first["contentHash"], second["contentHash"])


if __name__ == "__main__":
    unittest.main()
