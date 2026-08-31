from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import wayback_import


class WaybackImportTests(unittest.TestCase):
    def test_backfill_range_rejects_dates_before_august_2019(self) -> None:
        with self.assertRaisesRegex(ValueError, "2019-08-01"):
            wayback_import.validate_backfill_range(
                dt.date(2019, 7, 31),
                dt.date(2020, 1, 1),
            )
        wayback_import.validate_backfill_range(
            dt.date(2019, 8, 1),
            dt.date(2020, 1, 1),
        )

    def test_snapshot_rejects_dates_before_august_2019(self) -> None:
        with self.assertRaisesRegex(ValueError, "2019-08-01"):
            wayback_import.validate_snapshot_timestamp("20190731235959")
        self.assertEqual(
            wayback_import.validate_snapshot_timestamp("20190801000000"),
            "20190801000000",
        )

    def test_archive_requires_a_saved_primary_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(
                wayback_import.has_saved_primary_assets(
                    root,
                    mode="split",
                    static=None,
                    layers=[{"file": "missing.webp"}],
                )
            )
            (root / "layer.webp").write_bytes(b"layer")
            self.assertTrue(
                wayback_import.has_saved_primary_assets(
                    root,
                    mode="split",
                    static=None,
                    layers=[{"file": "layer.webp"}],
                )
            )

    def test_checkpoint_script_receives_progress_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "checkpoint.py"
            script.write_text("# test hook\n", encoding="utf-8")
            with mock.patch("backend.wayback_import.subprocess.run") as run:
                wayback_import.run_checkpoint(
                    str(script),
                    processed=17,
                    succeeded=12,
                    changed=10,
                    final=False,
                )

            command = run.call_args.args[0]
            env = run.call_args.kwargs["env"]
            self.assertEqual(command[1], str(script.resolve()))
            self.assertEqual(env["WAYBACK_CHECKPOINT_PROCESSED"], "17")
            self.assertEqual(env["WAYBACK_CHECKPOINT_SUCCEEDED"], "12")
            self.assertEqual(env["WAYBACK_CHECKPOINT_CHANGED"], "10")
            self.assertEqual(env["WAYBACK_CHECKPOINT_FINAL"], "0")

    def test_monthly_targets_stay_in_range(self) -> None:
        targets = list(
            wayback_import.target_dates(
                dt.date(2019, 8, 1),
                dt.date(2019, 10, 31),
                "monthly",
            )
        )
        self.assertEqual(
            targets,
            [dt.date(2019, 8, 1), dt.date(2019, 9, 1), dt.date(2019, 10, 1)],
        )

    def test_original_asset_is_rewritten_to_wayback_raw_capture(self) -> None:
        url = wayback_import.archived_asset_url(
            "20200102030405",
            "https://i0.hdslb.com/bfs/banner/example.webp",
            "https://web.archive.org/web",
        )
        self.assertEqual(
            url,
            "https://web.archive.org/web/20200102030405id_/"
            "https://i0.hdslb.com/bfs/banner/example.webp",
        )

    def test_asset_candidates_try_raw_archive_then_original_cdn(self) -> None:
        replay = (
            "https://web.archive.org/web/20230101120000im_/"
            "https://i0.hdslb.com/bfs/banner/example.webp"
        )
        self.assertEqual(
            wayback_import.archived_asset_candidates(
                "20230101120000",
                replay,
                "https://web.archive.org/web",
            ),
            [
                "https://web.archive.org/web/20230101120000id_/"
                "https://i0.hdslb.com/bfs/banner/example.webp",
                "https://i0.hdslb.com/bfs/banner/example.webp",
            ],
        )


    def test_archived_header_api_is_requested_through_wayback_raw_replay(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "is_split_layer": 1,
                "split_layer": json.dumps({"version": "1", "layers": []}),
            },
        }
        with mock.patch("backend.wayback_import.read_json", return_value=payload) as read_json:
            parsed, replay = wayback_import.fetch_archived_header_api(
                "20230101120000",
                "https://web.archive.org/web",
            )
        expected = (
            "https://web.archive.org/web/20230101120000id_/"
            "https://api.bilibili.com/x/web-show/page/header/v2?resource_id=142"
        )
        self.assertEqual(read_json.call_args.args[0], expected)
        self.assertEqual(replay, expected)
        self.assertTrue(parsed["is_split_layer"])

    def test_direct_bilibili_and_hdslb_requests_are_blocked(self) -> None:
        self.assertTrue(
            wayback_import.is_direct_bilibili_request("https://www.bilibili.com/")
        )
        self.assertTrue(
            wayback_import.is_direct_bilibili_request("https://i0.hdslb.com/a.webp")
        )
        self.assertTrue(
            wayback_import.is_direct_bilibili_request(
                "https://upos-sz-mirrorcos.bilivideo.com/a.webm"
            )
        )
        self.assertFalse(
            wayback_import.is_direct_bilibili_request(
                "https://web.archive.org/web/20200101/https://www.bilibili.com/"
            )
        )

    def test_imported_wayback_timestamps_reads_observation_sources(self) -> None:
        core = wayback_import.core
        original = (core.DATA_DIR, core.ARCHIVE_DIR, core.CURRENT_DIR)
        with tempfile.TemporaryDirectory() as temp:
            try:
                core.DATA_DIR = Path(temp)
                core.ARCHIVE_DIR = core.DATA_DIR / "archive"
                core.CURRENT_DIR = core.DATA_DIR / "current"
                folder = core.ARCHIVE_DIR / "one"
                folder.mkdir(parents=True)
                manifest = {
                    "capturedAt": "2022-07-01T19:59:29+08:00",
                    "date": "2022-07-01",
                    "season": "summer",
                    "mode": "static",
                    "layers": [],
                    "contentHash": "hash",
                    "familyId": "family",
                    "observations": [
                        {
                            "capturedAt": "2022-07-01T19:59:29+08:00",
                            "familyId": "family",
                            "source": {"waybackTimestamp": "20220701115929"},
                        }
                    ],
                }
                (folder / "banner.json").write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                self.assertEqual(
                    wayback_import.imported_wayback_timestamps(),
                    {"20220701115929"},
                )
            finally:
                core.DATA_DIR, core.ARCHIVE_DIR, core.CURRENT_DIR = original

    def test_wayback_structured_evidence_keeps_partial_layers(self) -> None:
        manifest = {
            "mode": "split",
            "layers": [{"index": 0, "tag": "img", "assetType": "image", "file": "layer.webp"}],
            "static": {"file": "pic.webp", "tag": "img"},
            "interaction": {"model": "none", "effects": []},
        }
        wayback_import.core.enrich_manifest_metadata(
            manifest,
            {
                "root": {"className": "animated-banner"},
                "layerCount": 2,
                "signals": {"isSplitLayer": True},
            },
            missing_assets=["layer_001.webp"],
        )
        self.assertIn("layered", manifest["type"])
        self.assertEqual(manifest["completeness"], "partial")
        self.assertEqual(manifest["fallback_image"], "pic.webp")


if __name__ == "__main__":
    unittest.main()
