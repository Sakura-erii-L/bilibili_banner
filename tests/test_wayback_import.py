from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import wayback_import


class WaybackImportTests(unittest.TestCase):
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
                dt.date(2018, 1, 1),
                dt.date(2018, 3, 31),
                "monthly",
            )
        )
        self.assertEqual(
            targets,
            [dt.date(2018, 1, 1), dt.date(2018, 2, 1), dt.date(2018, 3, 1)],
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

    def test_direct_bilibili_and_hdslb_requests_are_blocked(self) -> None:
        self.assertTrue(
            wayback_import.is_direct_bilibili_request("https://www.bilibili.com/")
        )
        self.assertTrue(
            wayback_import.is_direct_bilibili_request("https://i0.hdslb.com/a.webp")
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


if __name__ == "__main__":
    unittest.main()
