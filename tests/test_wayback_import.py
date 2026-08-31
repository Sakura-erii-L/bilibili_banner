from __future__ import annotations

import datetime as dt
import gzip
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

        def fake_download(src, folder, stem, *, referer=""):
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
                        },
                        replay_base="https://web.archive.org/web",
                        force=False,
                    )
                manifest = json.loads(
                    (Path(result["archive"]) / "banner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["mode"], "split")
                self.assertEqual(manifest["type"][0], "layered")
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
            finally:
                wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR = original

    def test_http_html_css_capture_restores_2019_single_background_as_static(self) -> None:
        snapshot = {
            "timestamp": "20190102030405",
            "original": wayback_import.ORIGINAL_PAGE,
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
            finally:
                wayback_import.core.DATA_DIR, wayback_import.core.ARCHIVE_DIR, wayback_import.core.CURRENT_DIR = original

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
