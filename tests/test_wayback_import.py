from __future__ import annotations

import datetime as dt
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import wayback_import
from backend.providers.history import HistoricalResult
from backend.providers.palxiao_history import PalxiaoHistoryProvider


class WaybackImportTests(unittest.TestCase):
    def test_backfill_range_rejects_dates_before_january_2019(self) -> None:
        with self.assertRaisesRegex(ValueError, "2019-01-01"):
            wayback_import.validate_backfill_range(
                dt.date(2018, 12, 31),
                dt.date(2020, 1, 1),
            )
        wayback_import.validate_backfill_range(
            dt.date(2019, 1, 1),
            dt.date(2020, 1, 1),
        )

    def test_snapshot_rejects_dates_before_january_2019(self) -> None:
        with self.assertRaisesRegex(ValueError, "2019-01-01"):
            wayback_import.validate_snapshot_timestamp("20181231235959")
        self.assertEqual(
            wayback_import.validate_snapshot_timestamp("20190101000000"),
            "20190101000000",
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

    def test_archive_with_only_auxiliary_missing_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static.png").write_bytes(b"primary")
            manifest = {
                "mode": "static",
                "type": ["static"],
                "static": {"file": "static.png"},
                "layers": [],
                "interaction": {"model": "none", "effects": []},
                "structureEvidence": {"root": {"tag": "img"}, "signals": {}},
                "completeness": "partial",
                "missing_assets": ["auxiliary_000: optional logo unavailable"],
                "assets": [
                    {"role": "primary", "local_file": "static.png"},
                    {"role": "auxiliary", "local_file": "logo.png"},
                ],
            }
            self.assertTrue(wayback_import.archive_is_reusable(root, manifest))

    def test_archive_with_missing_required_asset_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static.png").write_bytes(b"primary")
            manifest = {
                "mode": "static",
                "type": ["static"],
                "static": {"file": "static.png"},
                "layers": [],
                "interaction": {"model": "none", "effects": []},
                "structureEvidence": {"root": {"tag": "img"}, "signals": {}},
                "completeness": "partial",
                "missing_assets": ["static: primary asset unavailable"],
                "assets": [{"role": "primary", "local_file": "static.png"}],
            }
            self.assertFalse(wayback_import.archive_is_reusable(root, manifest))

    def test_archive_with_unconfirmed_interaction_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static.png").write_bytes(b"primary")
            manifest = {
                "mode": "static",
                "type": ["static", "interactive"],
                "static": {"file": "static.png"},
                "layers": [],
                "interaction": {"model": "none", "effects": []},
                "interactionState": "interactive",
                "structureEvidence": {
                    "root": {"tag": "img"},
                    "signals": {"hasInteraction": True},
                },
                "completeness": "complete",
                "missing_assets": [],
                "assets": [{"role": "primary", "local_file": "static.png"}],
            }
            self.assertFalse(wayback_import.archive_is_reusable(root, manifest))

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

    def test_capture_results_can_run_in_parallel(self) -> None:
        active = 0
        max_active = 0
        state_lock = wayback_import.threading.Lock()

        def fake_capture(snapshot, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            wayback_import.time.sleep(0.02)
            with state_lock:
                active -= 1
            return {"timestamp": snapshot["timestamp"], "status": "unchanged"}

        snapshots = [{"timestamp": f"2020010100000{index}"} for index in range(3)]
        with mock.patch(
            "backend.wayback_import.capture_snapshot_job",
            side_effect=fake_capture,
        ):
            results = list(
                wayback_import.iter_capture_results(
                    snapshots,
                    workers=3,
                    replay_base="replay",
                    force=False,
                    cdx_api="cdx",
                    max_header_api_delta_seconds=1,
                    verify_dom=False,
                    provider="auto",
                    palxiao_provider=None,
                )
            )

        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(error is None for _, _, error in results))

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

    def test_three_hour_targets_generate_eight_local_slots_per_day(self) -> None:
        targets = list(
            wayback_import.target_slots(
                dt.date(2019, 1, 1),
                dt.date(2019, 1, 2),
            )
        )
        self.assertEqual(len(targets), 16)
        self.assertEqual([slot for _, slot in targets[:8]], list(range(8)))
        self.assertEqual(targets[8], (dt.date(2019, 1, 2), 0))

    def test_reference_coverage_can_cover_the_full_requested_range(self) -> None:
        self.assertTrue(
            wayback_import.date_range_fully_covered(
                dt.date(2022, 3, 1),
                dt.date(2022, 3, 3),
                {
                    dt.date(2022, 3, 1),
                    dt.date(2022, 3, 2),
                    dt.date(2022, 3, 3),
                },
            )
        )
        self.assertFalse(
            wayback_import.date_range_fully_covered(
                dt.date(2022, 3, 1),
                dt.date(2022, 3, 3),
                {dt.date(2022, 3, 1), dt.date(2022, 3, 3)},
            )
        )

    def test_reference_import_is_checkpointed_after_rebuilding_index(self) -> None:
        with mock.patch(
            "backend.wayback_import.core.rebuild_index",
            return_value=True,
        ) as rebuild, mock.patch(
            "backend.wayback_import.run_checkpoint",
        ) as checkpoint:
            changed = wayback_import.checkpoint_reference_import(
                {dt.date(2022, 12, 22)},
                "checkpoint.py",
            )
        self.assertTrue(changed)
        rebuild.assert_called_once_with()
        checkpoint.assert_called_once_with(
            "checkpoint.py",
            processed=0,
            succeeded=0,
            changed=1,
            final=False,
        )

    def test_reference_import_runs_checkpoint_when_index_is_already_current(self) -> None:
        with mock.patch(
            "backend.wayback_import.core.rebuild_index",
            return_value=False,
        ) as rebuild, mock.patch(
            "backend.wayback_import.run_checkpoint",
        ) as checkpoint:
            changed = wayback_import.checkpoint_reference_import(
                {dt.date(2022, 12, 22)},
                "checkpoint.py",
            )
        self.assertFalse(changed)
        rebuild.assert_called_once_with()
        checkpoint.assert_called_once_with(
            "checkpoint.py",
            processed=0,
            succeeded=0,
            changed=0,
            final=False,
        )

    def test_three_hour_availability_uses_utc_timestamp(self) -> None:
        local_target = dt.datetime(
            2019, 1, 1, 3, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))
        )
        url = wayback_import.availability_url(local_target, "https://example.test/available")
        self.assertIn("timestamp=20181231190000", url)

    def test_cdx_discovery_url_uses_local_day_bounds(self) -> None:
        url = wayback_import.cdx_discovery_url(
            dt.date(2019, 1, 1),
            dt.date(2019, 1, 1),
            cdx_api="https://example.test/cdx",
        )
        query = wayback_import.urllib.parse.parse_qs(
            wayback_import.urllib.parse.urlsplit(url).query
        )
        self.assertEqual(query["from"], ["20181231160000"])
        self.assertEqual(query["to"], ["20190101155959"])

    def test_slot_center_uses_the_middle_of_each_three_hour_slot(self) -> None:
        self.assertEqual(
            wayback_import.slot_center(dt.date(2019, 1, 1), 0),
            dt.datetime(
                2019,
                1,
                1,
                1,
                30,
                tzinfo=dt.timezone(dt.timedelta(hours=8)),
            ),
        )
        self.assertEqual(
            wayback_import.slot_center(dt.date(2019, 1, 1), 7).time(),
            dt.time(22, 30),
        )

    def test_cdx_discovery_maps_slots_to_nearest_snapshot_center(self) -> None:
        def candidate(timestamp: str) -> dict:
            return {
                "timestamp": timestamp,
                "original": wayback_import.ORIGINAL_PAGE,
                "moment": wayback_import.snapshot_moment(timestamp),
                "cdxUrl": "https://example.test/cdx",
            }

        candidates = [
            candidate("20181231180000"),  # local 2019-01-01 02:00
            candidate("20190101020000"),  # local 2019-01-01 10:00
            candidate("20190101100000"),  # local 2019-01-01 18:00
            candidate("20190101150000"),  # local 2019-01-01 23:00
        ]
        with mock.patch(
            "backend.wayback_import.query_cdx_homepage_range",
            return_value=candidates,
        ) as query:
            snapshots = wayback_import.discover_snapshots(
                dt.date(2019, 1, 1),
                dt.date(2019, 1, 1),
                cadence="3h",
                api_url="https://example.test/available",
            )
        self.assertEqual(query.call_count, 1)
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(snapshots[0]["timestamp"], "20181231180000")
        self.assertEqual(snapshots[0]["targetSlot"], 0)
        self.assertEqual(snapshots[0]["targetSlots"], [0, 1])
        self.assertEqual(snapshots[0]["availabilityUrl"], "")
        self.assertEqual(snapshots[0]["cdxUrl"], "https://example.test/cdx")

    def test_cdx_discovery_uses_cached_matches_without_querying_cdx(self) -> None:
        cached = {
            "2019-01-01": {
                str(slot): {
                    "timestamp": "20190101120000",
                    "source": "archive",
                }
                for slot in range(wayback_import.core.SLOT_COUNT)
            }
        }
        with mock.patch(
            "backend.wayback_import.query_cdx_homepage_range",
        ) as query:
            snapshots = wayback_import.discover_snapshots(
                dt.date(2019, 1, 1),
                dt.date(2019, 1, 1),
                cadence="3h",
                api_url="https://example.test/available",
                match_cache=cached,
            )
        query.assert_not_called()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["timestamp"], "20190101120000")
        self.assertEqual(snapshots[0]["targetSlots"], list(range(8)))
        self.assertEqual(
            snapshots[0]["targetMappings"],
            [
                {"date": "2019-01-01", "slot": slot}
                for slot in range(wayback_import.core.SLOT_COUNT)
            ],
        )

    def test_reference_covered_dates_are_not_queried(self) -> None:
        with mock.patch(
            "backend.wayback_import.read_json",
            side_effect=AssertionError("Wayback must not be queried"),
        ) as read_json:
            snapshots = wayback_import.discover_snapshots(
                dt.date(2021, 6, 21),
                dt.date(2021, 6, 21),
                cadence="3h",
                api_url="https://example.test/available",
                excluded_dates={dt.date(2021, 6, 21)},
            )
        self.assertEqual(snapshots, [])
        read_json.assert_not_called()

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

    def test_snapshot_observation_groups_keep_each_target_date(self) -> None:
        snapshot = {
            "timestamp": "20200102030405",
            "targetMappings": [
                {"date": "2020-01-01", "slot": 7},
                {"date": "2020-01-02", "slot": 0},
                {"date": "2020-01-02", "slot": 1},
            ],
        }
        self.assertEqual(
            wayback_import.snapshot_observation_groups(snapshot),
            [("2020-01-01", [7]), ("2020-01-02", [0, 1])],
        )

    def test_raw_page_replay_is_tried_before_normal_replay(self) -> None:
        snapshot = {
            "timestamp": "20200102030405",
            "original": wayback_import.ORIGINAL_PAGE,
        }
        with mock.patch(
            "backend.wayback_import.read_text",
            side_effect=[RuntimeError("raw refused"), "<html></html>"],
        ) as read_text:
            text, replay = wayback_import.fetch_archived_page(
                snapshot,
                replay_base="https://web.archive.org/web",
            )
        self.assertEqual(text, "<html></html>")
        self.assertEqual(
            replay,
            "https://web.archive.org/web/20200102030405/"
            "https://www.bilibili.com/",
        )
        self.assertIn("20200102030405id_", read_text.call_args_list[0].args[0])

    def test_single_background_image_is_static(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <html><body>
              <div class="bili-banner" style="background-image:url('/banner-2019.png')"></div>
              <img src="/unrelated.png">
            </body></html>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(
            parsed["visualRecords"][0]["src"],
            "https://www.bilibili.com/banner-2019.png",
        )
        self.assertEqual(parsed["evidence"]["layerCount"], 0)
        self.assertFalse(parsed["evidence"]["signals"]["isSplitLayer"])

    def test_single_img_is_static(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            "<div id='banner_link'><picture><source srcset='/banner.webp'>"
            "<img src='/banner.png'></picture></div>",
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(len(parsed["visualRecords"]), 1)

    def test_video_src_and_source_are_primary_over_poster(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <div id='banner_link'>
              <video src='/banner.webm' poster='/poster.jpg'>
                <source src='/backup.mp4' type='video/mp4'>
              </video>
            </div>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(parsed["primaryRecord"]["tag"], "video")
        self.assertEqual(
            parsed["primaryRecord"]["src"],
            "https://www.bilibili.com/banner.webm",
        )
        self.assertEqual(
            parsed["primaryRecord"]["sourceCandidates"],
            [
                "https://www.bilibili.com/banner.webm",
                "https://www.bilibili.com/backup.mp4",
            ],
        )
        self.assertEqual(
            parsed["primaryRecord"]["poster"],
            "https://www.bilibili.com/poster.jpg",
        )
        self.assertNotEqual(parsed["primaryRecord"]["src"], parsed["primaryRecord"]["poster"])
        self.assertTrue(parsed["evidence"]["signals"]["hasVideo"])

    def test_video_poster_without_source_is_not_static_image(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            "<div id='banner_link'><video poster='/poster.jpg'></video></div>",
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "ambiguous")
        self.assertIsNone(parsed["primaryRecord"])
        self.assertTrue(parsed["evidence"]["signals"]["hasVideo"])
        self.assertTrue(parsed["visualRecords"][0]["isPoster"])

    def test_multiple_explicit_banner_layers_are_layered(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <div class="animated-banner">
              <div class="layer"><img src="/layer-a.png"></div>
              <div class="layer"><img src="/layer-b.png"></div>
            </div>
            <img src="/unrelated.png">
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "split")
        self.assertEqual(len(parsed["layerGroups"]), 2)
        self.assertEqual(parsed["evidence"]["layerCount"], 2)

    def test_http_fallback_succeeds_when_playwright_cannot_open_page(self) -> None:
        snapshot = {
            "timestamp": "20190102030405",
            "original": wayback_import.ORIGINAL_PAGE,
        }
        result = {
            "timestamp": snapshot["timestamp"],
            "status": "created",
            "contentHash": "hash",
            "archive": "archive",
            "captureMethod": "wayback-http-html-css",
        }
        with mock.patch(
            "backend.wayback_import.capture_snapshot_api",
            side_effect=RuntimeError("HTTP 404"),
        ), mock.patch(
            "backend.wayback_import.capture_snapshot_http",
            return_value=result,
        ), mock.patch(
            "backend.wayback_import.verify_snapshot_dom",
            side_effect=RuntimeError("net::ERR_CONNECTION_REFUSED"),
        ), mock.patch("backend.wayback_import.sync_playwright") as playwright:
            captured = wayback_import.capture_snapshot(
                snapshot,
                replay_base="https://web.archive.org/web",
                force=False,
            )
        self.assertEqual(captured["captureMethod"], "wayback-http-html-css")
        playwright.assert_not_called()

        with mock.patch(
            "backend.wayback_import.capture_snapshot_api",
            side_effect=RuntimeError("HTTP 404"),
        ), mock.patch(
            "backend.wayback_import.capture_snapshot_http",
            return_value=result,
        ), mock.patch(
            "backend.wayback_import.verify_snapshot_dom",
            side_effect=RuntimeError("net::ERR_CONNECTION_REFUSED"),
        ):
            verified = wayback_import.capture_snapshot(
                snapshot,
                replay_base="https://web.archive.org/web",
                force=False,
                verify_dom=True,
            )
        self.assertEqual(verified["captureMethod"], "wayback-http-html-css")
        self.assertEqual(verified["domVerification"]["status"], "failed")

    def test_gzip_json_and_utf8_sig_are_decoded(self) -> None:
        class Response:
            headers = {"Content-Encoding": "gzip"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return gzip.compress("\ufeff{\"ok\": true}".encode("utf-8"))

        with mock.patch(
            "backend.wayback_import.urllib.request.urlopen",
            return_value=Response(),
        ) as urlopen:
            self.assertEqual(wayback_import.read_json("https://example.test/data"), {"ok": True})
        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["accept-encoding"], "identity")

    def test_http_reader_retries_transport_errors_with_backoff(self) -> None:
        class Response:
            headers = {"Content-Encoding": ""}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"ok"

        with mock.patch(
            "backend.wayback_import.urllib.request.urlopen",
            side_effect=[ConnectionError("connection refused"), Response()],
        ), mock.patch(
            "backend.wayback_import.wait_for_wayback_request",
        ), mock.patch(
            "backend.wayback_import.time.sleep",
        ) as sleep:
            self.assertEqual(
                wayback_import.read_bytes("https://example.test/data", attempts=2),
                b"ok",
            )
        sleep.assert_called_once_with(wayback_import.RETRY_BASE_SECONDS)
        self.assertAlmostEqual(wayback_import.REQUEST_DELAY_SECONDS, 0.6)

    def test_real_split_layer_api_is_saved_as_layered_with_timestamp_metadata(self) -> None:
        endpoint = wayback_import.header_api.DEFAULT_ENDPOINTS[0]
        api_data = wayback_import.header_api.parse_header_api(
            {
                "code": 0,
                "data": {
                    "is_split_layer": 1,
                    "split_layer": json.dumps(
                        {
                            "version": "1",
                            "layers": [
                                {
                                    "id": "one",
                                    "resources": [
                                        {"id": "asset", "src": "https://i0.hdslb.com/layer.png"}
                                    ],
                                }
                            ],
                        }
                    ),
                },
            },
            endpoint,
        )
        match = {
            "homepageWaybackTimestamp": "20200102030405",
            "headerApiWaybackTimestamp": "20200102030305",
            "headerApiTimeDeltaSeconds": 60,
            "headerApiWaybackReplay": "api-replay",
            "headerApiCdxUrl": "cdx",
        }

        def fake_download(src, folder, stem, *, referer="", **_kwargs):
            path = folder / f"{stem}.png"
            path.write_bytes(b"layer")
            return {
                "src": src,
                "requestedSrc": src,
                "normalizedIdentity": src,
                "file": path.name,
                "contentType": "image/png",
                "tag": "img",
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = (wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR)
            try:
                wayback_import.core.DATA_DIR = root
                wayback_import.core.ARCHIVE_DIR = root / "archive"
                wayback_import.core.CURRENT_DIR = root / "current"
                with mock.patch(
                    "backend.wayback_import.fetch_archived_header_api",
                    return_value=(api_data, "api-replay", match),
                ), mock.patch(
                    "backend.wayback_import.core._download_http_asset",
                    side_effect=fake_download,
                ):
                    result = wayback_import.capture_snapshot_api(
                        {
                            "timestamp": "20200102030405",
                            "original": wayback_import.ORIGINAL_PAGE,
                            "targetMappings": [
                                {"date": "2020-01-02", "slot": 0},
                                {"date": "2020-01-03", "slot": 1},
                            ],
                        },
                        replay_base="https://web.archive.org/web",
                        force=False,
                    )
                manifest = json.loads(
                    (Path(result["archive"]) / "banner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["mode"], "split")
                self.assertEqual(manifest["type"][0], "layered")
                self.assertNotIn("types", manifest)
                self.assertEqual(
                    manifest["source"]["homepageWaybackTimestamp"],
                    "20200102030405",
                )
                self.assertEqual(
                    manifest["source"]["headerApiWaybackTimestamp"],
                    "20200102030305",
                )
                self.assertEqual(
                    manifest["source"]["headerApiTimeDeltaSeconds"],
                    60,
                )
                self.assertEqual(
                    [(item["date"], item["slots"]) for item in manifest["observations"]],
                    [("2020-01-02", [0]), ("2020-01-03", [1])],
                )
            finally:
                wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR = original

    def test_http_html_css_capture_restores_2019_single_background_as_static(self) -> None:
        snapshot = {
            "timestamp": "20190102030405",
            "original": wayback_import.ORIGINAL_PAGE,
            "targetMappings": [
                {"date": "2019-01-02", "slot": 0},
                {"date": "2019-01-03", "slot": 0},
            ],
        }

        def fake_download(src, folder, stem, *, referer=""):
            path = folder / f"{stem}.png"
            path.write_bytes(b"static")
            return {
                "src": src,
                "requestedSrc": src,
                "normalizedIdentity": src,
                "file": path.name,
                "contentType": "image/png",
                "tag": "img",
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = (wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR)
            try:
                wayback_import.core.DATA_DIR = root
                wayback_import.core.ARCHIVE_DIR = root / "archive"
                wayback_import.core.CURRENT_DIR = root / "current"
                with mock.patch(
                    "backend.wayback_import.fetch_archived_page",
                    return_value=(
                        "<div class='bili-banner' style=\"background-image:url('/banner.png')\"></div>",
                        "page-replay",
                    ),
                ), mock.patch(
                    "backend.wayback_import.core._download_http_asset",
                    side_effect=fake_download,
                ):
                    result = wayback_import.capture_snapshot_http(
                        snapshot,
                        replay_base="https://web.archive.org/web",
                        force=False,
                    )
                manifest = json.loads(
                    (Path(result["archive"]) / "banner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["mode"], "static")
                self.assertEqual(manifest["type"], ["static"])
                self.assertEqual(manifest["source"]["captureMethod"], "wayback-http-html-css")
                self.assertEqual(
                    manifest["source"]["homepageWaybackTimestamp"],
                    "20190102030405",
                )
                self.assertEqual(
                    [(item["date"], item["slots"]) for item in manifest["observations"]],
                    [("2019-01-02", [0]), ("2019-01-03", [0])],
                )
            finally:
                wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR = original

    def test_http_video_archive_keeps_video_and_poster_preview_separate(self) -> None:
        snapshot = {
            "timestamp": "20190102030405",
            "original": wayback_import.ORIGINAL_PAGE,
        }

        def fake_download(src, folder, stem, *, referer="", **_kwargs):
            is_video = stem == "static"
            suffix = ".webm" if is_video else ".jpg"
            path = folder / f"{stem}{suffix}"
            path.write_bytes(b"video" if is_video else b"poster")
            return {
                "src": src,
                "requestedSrc": src,
                "normalizedIdentity": src,
                "file": path.name,
                "contentType": "video/webm" if is_video else "image/jpeg",
                "tag": "video" if is_video else "img",
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = (
                wayback_import.core.DATA_DIR,
                wayback_import.core.ARCHIVE_DIR,
                wayback_import.core.CURRENT_DIR,
            )
            try:
                wayback_import.core.DATA_DIR = root
                wayback_import.core.ARCHIVE_DIR = root / "archive"
                wayback_import.core.CURRENT_DIR = root / "current"
                with mock.patch(
                    "backend.wayback_import.fetch_archived_page",
                    return_value=(
                        "<div id='banner_link'><video src='/banner.webm' "
                        "poster='/poster.jpg'><source src='/backup.mp4'></video></div>",
                        "page-replay",
                    ),
                ), mock.patch(
                    "backend.wayback_import.core._download_http_asset",
                    side_effect=fake_download,
                ), mock.patch(
                    "backend.wayback_import.wait_for_wayback_request",
                ):
                    result = wayback_import.capture_snapshot_http(
                        snapshot,
                        replay_base="https://web.archive.org/web",
                        force=False,
                    )
                archive = Path(result["archive"])
                manifest = json.loads(
                    (archive / "banner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["static"]["assetType"], "video")
                self.assertTrue(manifest["static"]["file"].endswith(".webm"))
                self.assertTrue(manifest["preview_image"].endswith(".jpg"))
                self.assertIn("video", manifest["type"])
                self.assertNotEqual(manifest["static"]["src"], "https://www.bilibili.com/poster.jpg")
            finally:
                (
                    wayback_import.core.DATA_DIR,
                    wayback_import.core.ARCHIVE_DIR,
                    wayback_import.core.CURRENT_DIR,
                ) = original

    def test_verify_dom_does_not_take_screenshot(self) -> None:
        page = mock.MagicMock()
        page.url = "https://web.archive.org/web/20200102030405id_/https://www.bilibili.com/"
        page.locator.return_value.count.return_value = 1
        context = mock.MagicMock()
        context.new_page.return_value = page
        browser = mock.MagicMock()
        browser.new_context.return_value = context
        playwright = mock.MagicMock()
        playwright.chromium.launch.return_value = browser
        manager = mock.MagicMock()
        manager.__enter__.return_value = playwright
        with mock.patch(
            "backend.wayback_import.sync_playwright",
            return_value=manager,
        ):
            result = wayback_import.verify_snapshot_dom(
                {
                    "timestamp": "20200102030405",
                    "original": wayback_import.ORIGINAL_PAGE,
                },
                replay_base="https://web.archive.org/web",
            )
        self.assertEqual(result["status"], "ok")
        page.screenshot.assert_not_called()


    def test_archived_header_api_matches_a_different_nearby_timestamp(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "is_split_layer": 1,
                "split_layer": json.dumps({"version": "1", "layers": []}),
            },
        }
        endpoint = wayback_import.header_api.DEFAULT_ENDPOINTS[0]
        candidate = {
            "timestamp": "20230101115900",
            "original": endpoint,
            "deltaSeconds": 60,
            "cdxUrl": "https://example.test/cdx",
        }
        with mock.patch(
            "backend.wayback_import.query_cdx_snapshots",
            side_effect=lambda current, *_args, **_kwargs: [candidate]
            if current == endpoint
            else [],
        ), mock.patch(
            "backend.wayback_import.read_json",
            return_value=payload,
        ) as read_json:
            parsed, replay, metadata = wayback_import.fetch_archived_header_api(
                "20230101120000",
                "https://web.archive.org/web",
            )
        expected = (
            "https://web.archive.org/web/20230101115900id_/"
            f"{endpoint}"
        )
        self.assertEqual(read_json.call_args.args[0], expected)
        self.assertEqual(replay, expected)
        self.assertTrue(parsed["is_split_layer"])
        self.assertEqual(metadata["homepageWaybackTimestamp"], "20230101120000")
        self.assertEqual(metadata["headerApiWaybackTimestamp"], "20230101115900")
        self.assertEqual(metadata["headerApiTimeDeltaSeconds"], 60)

    def test_archived_header_api_uses_nearest_successful_candidate(self) -> None:
        endpoint = wayback_import.header_api.DEFAULT_ENDPOINTS[0]
        candidates = [
            {
                "timestamp": "20230101130000",
                "original": endpoint,
                "deltaSeconds": 3600,
                "cdxUrl": "cdx",
            },
            {
                "timestamp": "20230101115900",
                "original": endpoint,
                "deltaSeconds": 60,
                "cdxUrl": "cdx",
            },
        ]
        payload = {"code": 0, "data": {"is_split_layer": 0}}
        with mock.patch(
            "backend.wayback_import.query_cdx_snapshots",
            side_effect=lambda current, *_args, **_kwargs: candidates
            if current == endpoint
            else [],
        ), mock.patch(
            "backend.wayback_import.read_json",
            side_effect=[{"code": -1}, payload],
        ) as read_json:
            _parsed, replay, metadata = wayback_import.fetch_archived_header_api(
                "20230101120000",
                "https://web.archive.org/web",
            )
        self.assertIn("20230101130000id_", replay)
        self.assertEqual(metadata["headerApiTimeDeltaSeconds"], 3600)
        self.assertEqual(read_json.call_count, 2)

    def test_archived_header_api_rejects_missing_nearby_candidates(self) -> None:
        with mock.patch(
            "backend.wayback_import.query_cdx_snapshots",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "no archived Header API snapshot within",
            ):
                wayback_import.fetch_archived_header_api(
                    "20230101120000",
                    "https://web.archive.org/web",
                )

    def test_cdx_network_failure_is_distinguished_from_success_empty(self) -> None:
        with mock.patch(
            "backend.wayback_import.query_cdx_snapshots",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "network failure prevented confirming",
            ):
                wayback_import.fetch_archived_header_api(
                    "20230101120000",
                    "https://web.archive.org/web",
                )

        with mock.patch(
            "backend.wayback_import.query_cdx_snapshots",
            return_value=[],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "CDX query succeeded but returned no matching snapshot",
            ):
                wayback_import.fetch_archived_header_api(
                    "20230101120000",
                    "https://web.archive.org/web",
                )

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

    def test_precise_banner_root_wins_over_parent_and_global_css(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <style>
              .banner { background-image: url(global.jpg); }
              #banner_link { background-image: url(main.jpg); }
            </style>
            <div class="banner"><a id="banner_link">
              <img class="logo" src="logo.png">
            </a></div>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(parsed["selectedRoot"]["id"], "banner_link")
        self.assertEqual(parsed["primaryRecord"]["src"], "https://www.bilibili.com/main.jpg")
        self.assertEqual(parsed["auxiliaryRecords"][0]["src"], "https://www.bilibili.com/logo.png")
        self.assertNotIn("global.jpg", [item["src"] for item in parsed["visualRecords"]])

    def test_pseudo_element_is_static_auxiliary_not_layer(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <style>
              #banner_link { background-image: url(main.jpg); }
              #banner_link::before { content: ''; background-image: url(decor.png); }
            </style>
            <a id="banner_link"></a>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(parsed["primaryRecord"]["src"], "https://www.bilibili.com/main.jpg")
        self.assertEqual(parsed["auxiliaryRecords"][0]["origin"]["pseudoElement"], "before")
        self.assertFalse(parsed["evidence"]["signals"]["isSplitLayer"])

    def test_same_level_banner_roots_remain_ambiguous(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <div class="banner" style="background-image:url(one.jpg)"></div>
            <div class="banner" style="background-image:url(two.jpg)"></div>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "ambiguous")
        self.assertIsNone(parsed["primaryRecord"])
        self.assertEqual(len(parsed["roots"]), 2)

    def test_transcoded_urls_are_one_logical_candidate(self) -> None:
        parsed = wayback_import.parse_banner_resources(
            """
            <style>
              #banner_link { background-image: url(https://i0.hdslb.com/a.png); }
              #banner_link::after { background-image: url(https://i0.hdslb.com/a.png@1c.webp); }
            </style>
            <div id="banner_link"></div>
            """,
            page_url="https://www.bilibili.com/",
        )
        self.assertEqual(parsed["mode"], "static")
        self.assertEqual(len(parsed["visualRecords"]), 1)

    def test_palxiao_provider_discovers_and_preserves_raw_layers(self) -> None:
        tree_url = "https://example.test/tree"
        data_url = "https://raw.example/assets/2023-08-21/data.json"
        payloads = {
            tree_url: json.dumps(
                {
                    "sha": "commit-sha",
                    "tree": [
                        {"path": "assets/2023-08-21/data.json", "type": "blob"},
                        {"path": "assets/2023-08-21/hero.webp", "type": "blob"},
                    ],
                }
            ).encode("utf-8"),
            data_url: json.dumps(
                [
                    {
                        "tagName": "img",
                        "src": "./assets/2023-08-21/hero.webp",
                        "transform": [1, 0, 0, 1, 10, 2],
                        "width": 1650,
                        "height": 155,
                        "a": 0.01,
                        "g": 0.02,
                        "f": 0.0001,
                        "deg": 0.001,
                        "opacity": [1, 0.8],
                        "unknownField": {"keep": True},
                    }
                ]
            ).encode("utf-8"),
        }

        def loader(url: str) -> bytes:
            return payloads[url]

        provider = PalxiaoHistoryProvider(
            loader=loader,
            api_tree_url=tree_url,
            raw_base_url="https://raw.example",
        )
        self.assertEqual(provider.discover_dates(), ["2023-08-21"])
        self.assertIsNone(provider.date_for_timestamp("20230822120000"))
        discovered = wayback_import.discover_palxiao_snapshots(
            dt.date(2023, 8, 21),
            dt.date(2023, 8, 21),
            provider=provider,
        )
        self.assertEqual(discovered[0]["palxiaoObservedAt"], "2023-08-21")
        with self.assertRaisesRegex(ValueError, "does not exactly match"):
            wayback_import._exact_palxiao_date(
                {
                    "timestamp": "20230821120000",
                    "palxiaoDate": "2023-08-20",
                },
                provider,
            )
        result = provider.load("2023-08-21")
        self.assertIsInstance(result, HistoricalResult)
        self.assertEqual(result.provider, "palxiao-bilibili-banner")
        self.assertEqual(result.mode, "layered")
        self.assertEqual(result.layers[0]["src"], data_url.replace("/data.json", "/hero.webp"))
        self.assertEqual(result.layers[0]["sourceLayer"]["unknownField"]["keep"], True)
        self.assertEqual(result.layers[0]["a"], 0.01)
        self.assertNotIn("blur", result.layers[0])
        self.assertEqual(result.raw_payload[0]["unknownField"]["keep"], True)

    def test_api_and_palxiao_provenance_records_conflict_without_merging(self) -> None:
        result = HistoricalResult(
            provider="palxiao-bilibili-banner",
            observed_at="2023-08-21",
            source_url="palxiao",
            layers=[{"src": "https://i0.hdslb.com/palxiao.webp"}],
        )
        provenance = wayback_import.compare_api_with_palxiao(
            {
                "layers": [{"resources": [{"src": "https://i0.hdslb.com/api.webp"}]}],
                "resources": [
                    {
                        "src": "https://i0.hdslb.com/api.webp",
                        "normalizedIdentity": "https://i0.hdslb.com/api.webp",
                    }
                ],
            },
            result,
        )
        self.assertEqual(provenance["primaryProvider"], "wayback-header-api")
        self.assertEqual(provenance["supportingProviders"], ["palxiao-bilibili-banner"])
        self.assertTrue(provenance["conflicts"])

    def test_cross_provider_canonical_hash_is_stable_but_source_differs(self) -> None:
        def manifest(provider: str, model: str) -> dict:
            return {
                "mode": "split",
                "type": ["layered", "interactive"],
                "source": {
                    "provider": provider,
                    "captureMethod": provider,
                    "resolvedUrl": f"https://source.test/{provider}",
                },
                "interaction": {"model": model, "effects": ["translateX"]},
                "layers": [
                    {
                        "index": 0,
                        "tag": "img",
                        "assetType": "image",
                        "file": "layer.webp",
                        "width": 1650,
                        "height": 155,
                        "transform": [1, 0, 0, 1, 0, 0],
                        "opacity": [1, 1],
                        "zIndex": 0,
                    }
                ],
                "static": None,
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "layer.webp").write_bytes(b"same visual bytes")
            api_hashes = wayback_import.core.calculate_manifest_hashes(
                root,
                manifest("wayback-header-api", "bilibili-header-api-v1"),
            )
            palxiao_hashes = wayback_import.core.calculate_manifest_hashes(
                root,
                manifest("palxiao-bilibili-banner", "palxiao-reconstructed-v1"),
            )
        self.assertNotEqual(api_hashes["contentHash"], palxiao_hashes["contentHash"])
        self.assertEqual(
            api_hashes["canonicalContentHash"],
            palxiao_hashes["canonicalContentHash"],
        )
        self.assertNotEqual(
            api_hashes["sourceFingerprint"],
            palxiao_hashes["sourceFingerprint"],
        )

    def test_palxiao_capture_writes_observed_at_and_independent_model(self) -> None:
        source_layer = {
            "index": 0,
            "sourceProvider": "palxiao-bilibili-banner",
            "sourceSrc": "./hero.webp",
            "sourceLayer": {"tagName": "img", "src": "./hero.webp", "unknown": "keep"},
            "palxiao": {"tagName": "img", "src": "./hero.webp", "unknown": "keep"},
            "src": "https://raw.example/assets/2023-08-21/hero.webp",
            "assetUrl": "https://raw.example/assets/2023-08-21/hero.webp",
            "tag": "img",
            "assetType": "image",
            "width": 1650,
            "height": 155,
            "transform": [1, 0, 0, 1, 0, 0],
            "opacity": [1, 1],
        }

        class FakeProvider:
            def date_for_timestamp(self, _timestamp):
                return "2023-08-21"

            def load(self, _date):
                return HistoricalResult(
                    provider="palxiao-bilibili-banner",
                    observed_at="2023-08-21",
                    source_url="https://raw.example/assets/2023-08-21/data.json",
                    confidence="high",
                    mode="split",
                    raw_payload=[source_layer["sourceLayer"]],
                    layers=[source_layer],
                )

        def fake_download(src, folder, stem, *, referer="", **_kwargs):
            path = folder / f"{stem}.webp"
            path.write_bytes(b"hero")
            return {
                "src": src,
                "file": path.name,
                "contentType": "image/webp",
                "tag": "img",
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = (
                wayback_import.core.DATA_DIR,
                wayback_import.core.ARCHIVE_DIR,
                wayback_import.core.CURRENT_DIR,
            )
            try:
                wayback_import.core.DATA_DIR = root
                wayback_import.core.ARCHIVE_DIR = root / "archive"
                wayback_import.core.CURRENT_DIR = root / "current"
                with mock.patch(
                    "backend.wayback_import.core._download_http_asset",
                    side_effect=fake_download,
                ):
                    result = wayback_import.capture_snapshot_palxiao(
                        {
                            "timestamp": "20230821120000",
                            "palxiaoDate": "2023-08-21",
                        },
                        provider=FakeProvider(),
                        force=False,
                    )
                manifest = json.loads(
                    (Path(result["archive"]) / "banner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["source"]["observedAt"], "2023-08-21")
                self.assertNotIn("effectiveFrom", manifest["source"])
                self.assertEqual(
                    manifest["interaction"]["model"],
                    "palxiao-reconstructed-v1",
                )
                self.assertEqual(
                    manifest["layers"][0]["sourceLayer"]["unknown"],
                    "keep",
                )
                self.assertIn("type", manifest)
                self.assertNotIn("types", manifest)
                self.assertTrue(manifest["canonicalContentHash"])
                self.assertTrue(manifest["sourceFingerprint"])
            finally:
                (
                    wayback_import.core.DATA_DIR,
                    wayback_import.core.ARCHIVE_DIR,
                    wayback_import.core.CURRENT_DIR,
                ) = original

    def test_data_writing_workflows_share_non_canceling_concurrency(self) -> None:
        wayback = Path(".github/workflows/wayback-import.yml").read_text(encoding="utf-8")
        self.assertIn("actions: write", wayback)
        for filename in ("daily-update.yml", "wayback-import.yml"):
            content = Path(".github/workflows", filename).read_text(encoding="utf-8")
            self.assertIn("group: bilibili-banner-data-writes", content)
            self.assertIn("cancel-in-progress: false", content)
        pages = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("group: bilibili-banner-pages", pages)
        self.assertIn("cancel-in-progress: false", pages)
        self.assertIn("python backend/capture.py --rebuild-index", pages)
        daily = Path(".github/workflows/daily-update.yml").read_text(encoding="utf-8")
        self.assertIn("python backend/capture.py --rebuild-index", daily)
        checkpoint = Path("scripts/checkpoint_wayback.py").read_text(encoding="utf-8")
        self.assertIn("backend/capture.py", checkpoint)
        self.assertIn("--rebuild-index", checkpoint)
        self.assertIn('WAYBACK_REQUEST_DELAY: "0.6"', wayback)
        self.assertIn('WAYBACK_WORKERS: "3"', wayback)


if __name__ == "__main__":
    unittest.main()
