from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import capture, wayback_import
from backend.providers.mikufan039_reference import (
    DEFAULT_COMMIT,
    ReferenceRepository,
    _parse_reference_split_layer,
)


def write_reference_manifest(
    root: Path,
    reference_id: str,
    extension: dict | None = None,
    *,
    is_split_layer: int = 1,
) -> None:
    folder = root / "res" / "bilibanner" / reference_id
    (folder / "bfs").mkdir(parents=True, exist_ok=True)
    (folder / "bfs" / "layer.png").write_bytes(reference_id.encode("utf-8"))
    split = {
        "version": "1",
        "layers": [{
            "id": 0,
            "resources": [{"src": "bfs/layer.png", "id": 0}],
            "translate": {"initial": [0, 0]},
        }],
    }
    if extension:
        split["extensions"] = extension
    payload = {
        "code": 0,
        "data": {
            "id": 1,
            "title": reference_id,
            "litpic": "bfs/layer.png",
            "is_split_layer": is_split_layer,
            "split_layer": json.dumps(split, ensure_ascii=False),
        },
    }
    (folder / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class MikuFan039ReferenceTests(unittest.TestCase):
    def make_repository(self) -> Path:
        root = Path(self.temp.name)
        banner_root = root / "res" / "bilibanner"
        (banner_root / "gallery").mkdir(parents=True)
        categories = [
            {"id": category, "file": f"gallery/{category}.json"}
            for category in ("seasonal", "festival", "activity", "event", "game", "other")
        ]
        (banner_root / "gallery.json").write_text(
            json.dumps({"categories": categories}),
            encoding="utf-8",
        )
        for category in categories:
            rows = []
            if category["id"] == "seasonal":
                rows = [
                    {
                        "id": "2021summer",
                        "title": "2021 夏",
                        "startDate": "2021-06-21",
                        "endDate": "2021-09-31",
                        "group": "summer",
                    },
                    {
                        "id": "2022spring",
                        "title": "2022 春",
                        "startDate": "2022-03-01",
                        "endDate": "2022-03-02",
                        "group": "spring",
                    },
                    {
                        "id": "latest-open",
                        "title": "当前 Banner",
                        "startDate": "2022-01-01",
                        "endDate": None,
                        "group": "spring",
                    },
                ]
            elif category["id"] == "game":
                rows = [{
                    "id": "2022springAdv",
                    "title": "2022 春互动",
                    "startDate": "2022-03-01",
                    "endDate": "2022-03-02",
                    "group": "spring",
                }]
            (banner_root / category["file"]).write_text(
                json.dumps(rows),
                encoding="utf-8",
            )
        write_reference_manifest(
            root,
            "2021summer",
            {"time": {
                "0": [{"layers": [{"resources": [{"src": "bfs/layer.png"}]}]}],
                "21600": [{"layers": [{"resources": [{"src": "bfs/layer.png"}]}]}],
                "57600": [{"layers": [{"resources": [{"src": "bfs/layer.png"}]}]}],
            }},
        )
        write_reference_manifest(root, "2022spring", is_split_layer=0)
        write_reference_manifest(root, "2022springAdv", {"springGame2022": {}})
        return root

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reference_dates_and_time_slots_are_normalized(self) -> None:
        repository = ReferenceRepository(self.make_repository(), commit=DEFAULT_COMMIT)
        entries = repository.entries()
        entry = next(item for item in entries if item.reference_id == "2021summer")
        self.assertEqual(entry.end_date, dt.date(2021, 9, 30))
        self.assertEqual(entry.original_end_date, "2021-09-31")
        with tempfile.TemporaryDirectory() as target:
            manifest = repository.build_manifest(entry, dt.date(2021, 8, 1), Path(target))
        self.assertEqual(manifest["slots"], [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(manifest["season"], "summer")
        self.assertEqual(manifest["source"]["referenceId"], "2021summer")
        self.assertEqual(manifest["source"]["referenceCommit"], DEFAULT_COMMIT)
        self.assertEqual(manifest["source"]["originalDate"], "2021-09-31")
        self.assertEqual(manifest["source"]["correctedDate"], "2021-09-30")

    def test_legacy_array_split_layer_is_converted_to_layers(self) -> None:
        split_layer = _parse_reference_split_layer(
            json.dumps([
                {
                    "images": [{"src": "bfs/one.png", "duration": 5000}],
                    "initial": {"blur": 4},
                }
            ])
        )
        self.assertEqual(split_layer["version"], "legacy-array")
        self.assertEqual(split_layer["layers"][0]["resources"][0]["src"], "bfs/one.png")
        self.assertEqual(split_layer["layers"][0]["initial"], {"blur": 4})

    def test_open_ended_reference_entry_is_not_historical_coverage(self) -> None:
        repository = ReferenceRepository(self.make_repository(), commit=DEFAULT_COMMIT)
        covered = repository.covered_entries(
            dt.date(2022, 1, 1),
            dt.date(2022, 3, 2),
        )
        self.assertNotIn(
            "latest-open",
            {entry.reference_id for entry in covered},
        )
        self.assertIn(
            "2022spring",
            {entry.reference_id for entry in covered},
        )

    def test_same_group_normal_and_interactive_entries_share_daily_record(self) -> None:
        root = self.make_repository()
        original = (capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR)
        with tempfile.TemporaryDirectory() as data_dir:
            data = Path(data_dir)
            capture.DATA_DIR = data
            capture.ARCHIVE_DIR = data / "archive"
            capture.CURRENT_DIR = data / "current"
            try:
                with mock.patch(
                    "backend.wayback_import.subprocess.run",
                    return_value=mock.Mock(stdout=DEFAULT_COMMIT),
                ):
                    covered = wayback_import.import_reference_repository(
                        root,
                        start=dt.date(2022, 3, 1),
                        end=dt.date(2022, 3, 2),
                        commit=DEFAULT_COMMIT,
                        force=False,
                    )
                self.assertEqual(covered, {dt.date(2022, 3, 1), dt.date(2022, 3, 2)})
                index = json.loads((data / "index.json").read_text(encoding="utf-8"))
                self.assertEqual(len(index["records"]), 1)
                self.assertEqual(index["records"][0]["dateStart"], "2022-03-01")
                self.assertEqual(index["records"][0]["dateEnd"], "2022-03-02")
                self.assertEqual(index["records"][0]["variantCount"], 2)
                self.assertEqual(
                    {variant.get("referenceMode") for variant in index["records"][0]["variants"]},
                    {"normal", "interactive"},
                )
                manifests = list(data.joinpath("archive").glob("*/banner.json"))
                self.assertEqual(len(manifests), 2)
                for path in manifests:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    self.assertIn("referenceId", manifest["source"])
                    self.assertEqual(manifest["source"]["referenceCommit"], DEFAULT_COMMIT)
            finally:
                capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR = original

    def test_winter_shared_video_is_copied_and_enabled_as_split_layer(self) -> None:
        root = self.make_repository()
        banner_root = root / "res" / "bilibanner"
        winter_root = banner_root / "2022winter"
        winter_root.mkdir(parents=True)
        (winter_root / "bfs").mkdir(parents=True)
        (winter_root / "bfs" / "winter.png").write_bytes(b"winter")
        shared_video = (
            banner_root
            / "2022spring"
            / "blackboard"
            / "static"
            / "20220314"
            / "00979505aec5edd6e5c2f8c096fa0f62"
            / "ZlmaPe9AZv.mp4"
        )
        shared_video.parent.mkdir(parents=True)
        shared_video.write_bytes(b"video")
        split_layer = {
            "version": "1",
            "layers": [{
                "id": 0,
                "resources": [{
                    "src": (
                        "https://activity.hdslb.com/blackboard/static/"
                        "20220314/00979505aec5edd6e5c2f8c096fa0f62/"
                        "ZlmaPe9AZv.mp4"
                    ),
                    "id": 0,
                }],
            }],
        }
        (winter_root / "manifest.json").write_text(
            json.dumps({
                "code": 0,
                "data": {
                    "pic": "bfs/winter.png",
                    "litpic": "bfs/winter.png",
                    "is_split_layer": 0,
                    "split_layer": json.dumps(split_layer),
                },
            }),
            encoding="utf-8",
        )
        seasonal_path = banner_root / "gallery" / "seasonal.json"
        seasonal = json.loads(seasonal_path.read_text(encoding="utf-8"))
        seasonal.append({
            "id": "2022winter",
            "title": "2022 冬",
            "startDate": "2022-12-22",
            "endDate": "2023-03-19",
            "group": "winter",
        })
        seasonal_path.write_text(
            json.dumps(seasonal, ensure_ascii=False),
            encoding="utf-8",
        )

        repository = ReferenceRepository(root, commit=DEFAULT_COMMIT)
        entry = next(item for item in repository.entries()
                     if item.reference_id == "2022winter")
        with tempfile.TemporaryDirectory() as target:
            manifest = repository.build_manifest(
                entry,
                dt.date(2023, 1, 1),
                Path(target),
            )
            self.assertEqual(manifest["mode"], "split")
            self.assertEqual(manifest["layers"][0]["assetType"], "video")
            copied = Path(target) / "blackboard" / "static" / "20220314" / "00979505aec5edd6e5c2f8c096fa0f62" / "ZlmaPe9AZv.mp4"
            self.assertTrue(copied.is_file())
            source_manifest = json.loads(
                (Path(target) / "reference-manifest.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(source_manifest["data"]["is_split_layer"], 1)
            source_split = json.loads(source_manifest["data"]["split_layer"])
            self.assertEqual(
                source_split["layers"][0]["resources"][0]["src"],
                "blackboard/static/20220314/00979505aec5edd6e5c2f8c096fa0f62/ZlmaPe9AZv.mp4",
            )


if __name__ == "__main__":
    unittest.main()
