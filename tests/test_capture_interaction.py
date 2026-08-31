from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

import backend.capture as capture


class CaptureInteractionTests(unittest.TestCase):
    def test_sampled_nonlinear_interaction_and_return(self) -> None:
        original = {
            "VIEWPORT_WIDTH": capture.VIEWPORT_WIDTH,
            "VIEWPORT_HEIGHT": capture.VIEWPORT_HEIGHT,
            "MOTION_PROBE_PX": capture.MOTION_PROBE_PX,
            "MOTION_RESET_MS": capture.MOTION_RESET_MS,
            "MOTION_ENTER_SETTLE_MS": capture.MOTION_ENTER_SETTLE_MS,
            "MOTION_SETTLE_MS": capture.MOTION_SETTLE_MS,
            "RETURN_SAMPLE_TIMES_MS": capture.RETURN_SAMPLE_TIMES_MS,
        }
        capture.VIEWPORT_WIDTH = 1200
        capture.VIEWPORT_HEIGHT = 500
        capture.MOTION_PROBE_PX = 996
        capture.MOTION_RESET_MS = 30
        capture.MOTION_ENTER_SETTLE_MS = 5
        capture.MOTION_SETTLE_MS = 10
        capture.RETURN_SAMPLE_TIMES_MS = (0, 10, 20, 40, 80)

        html = """
        <style>
          .animated-banner { position: relative; width: 1000px; height: 150px; }
          .layer { position: absolute; inset: 0; }
          .layer img { width: 1000px; height: 150px; }
        </style>
        <div class="animated-banner">
          <div class="layer"><img id="one" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='150'/>"></div>
          <div class="layer"><img id="two" src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='150'/>"></div>
        </div>
        <script>
          const banner = document.querySelector('.animated-banner');
          const layers = [...document.querySelectorAll('.layer img')];
          let enteredAt = 0;
          let frame = 0;
          banner.addEventListener('mouseenter', event => { enteredAt = event.clientX; });
          banner.addEventListener('mousemove', event => {
            cancelAnimationFrame(frame);
            const dx = event.clientX - enteredAt;
            const curved = 0.015 * dx + 0.000015 * dx * Math.abs(dx);
            layers[0].style.transform = `matrix(1,0,0,1,${curved},0)`;
            layers[1].style.transform = `matrix(1,0,0,1,${-0.02 * dx},0)`;
            layers[1].style.opacity = String(Math.max(0.25, 1 - Math.abs(dx) / 2000));
          });
          banner.addEventListener('mouseleave', () => {
            const starts = layers.map(layer => {
              const matrix = new DOMMatrix(getComputedStyle(layer).transform);
              return { x: matrix.e, opacity: Number(getComputedStyle(layer).opacity) };
            });
            const start = performance.now();
            const tick = now => {
              const progress = Math.min((now - start) / 60, 1);
              const remaining = (1 - progress) ** 2;
              layers.forEach((layer, index) => {
                layer.style.transform = `matrix(1,0,0,1,${starts[index].x * remaining},0)`;
                layer.style.opacity = String(1 + (starts[index].opacity - 1) * remaining);
              });
              if (progress < 1) frame = requestAnimationFrame(tick);
            };
            frame = requestAnimationFrame(tick);
          });
        </script>
        """

        try:
            with sync_playwright() as playwright:
                launch_kwargs = {"headless": True}
                browser_path = capture.find_system_browser()
                if browser_path:
                    launch_kwargs["executable_path"] = browser_path
                browser = playwright.chromium.launch(**launch_kwargs)
                page = browser.new_page(viewport={"width": 1200, "height": 500})
                page.set_content(html)
                geometry = capture.get_banner_geometry(page)
                layers, interaction, motions = capture.sample_interaction(page, geometry)
                browser.close()

            self.assertEqual(len(layers), 2)
            self.assertEqual(interaction["model"], "bilibili-sampled-horizontal-v1")
            self.assertIn("translateX", interaction["effects"])
            self.assertIn("opacity", interaction["effects"])
            self.assertLess(interaction["inputSamplesPx"][0], 0)
            self.assertGreater(interaction["inputSamplesPx"][-1], 0)
            self.assertTrue(all(sample[5] == 0 for sample in motions[0]["matrixDelta"]))
            half = motions[0]["matrixDelta"][6][4]
            full = motions[0]["matrixDelta"][8][4]
            self.assertNotAlmostEqual(full, half * 2, places=3)
            self.assertLess(abs(motions[0]["returnRemaining"][-1]), 0.1)
        finally:
            for name, value in original.items():
                setattr(capture, name, value)

    def test_vertical_motion_is_rejected(self) -> None:
        baseline = {
            "index": 0,
            "transform": [1, 0, 0, 1, 0, 0],
            "layerOpacity": 1,
            "mediaOpacity": 1,
        }
        moved = {**baseline, "transform": [1, 0, 0, 1, 10, 2]}
        with self.assertRaises(capture.InteractionProbeError) as raised:
            capture._layer_effect_delta(baseline, moved)
        self.assertEqual(raised.exception.reason, "vertical-motion-detected")


class TimedVariantIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_paths = (capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR)
        capture.DATA_DIR = Path(self.temp.name)
        capture.ARCHIVE_DIR = capture.DATA_DIR / "archive"
        capture.CURRENT_DIR = capture.DATA_DIR / "current"
        capture.ARCHIVE_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        capture.DATA_DIR, capture.ARCHIVE_DIR, capture.CURRENT_DIR = self.original_paths
        self.temp.cleanup()

    def test_rebuild_index_groups_time_variants(self) -> None:
        family_id = "2026-08-31_layout_family"
        for index, (content_hash, slot) in enumerate((("hash-day", 360), ("hash-night", 1080))):
            folder = capture.ARCHIVE_DIR / f"variant-{index}"
            folder.mkdir()
            manifest = {
                "version": 10.0,
                "date": "2026-08-31",
                "season": "summer",
                "capturedAt": f"2026-08-31T{6 + index * 12:02d}:17:00+08:00",
                "lastObservedAt": f"2026-08-31T{6 + index * 12:02d}:17:00+08:00",
                "mode": "split",
                "layers": [{"index": 0}],
                "contentHash": content_hash,
                "familyId": family_id,
                "observedSlots": [slot],
                "timeZone": "Asia/Shanghai",
            }
            (folder / "banner.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

        capture.rebuild_index()
        index = json.loads((capture.DATA_DIR / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["version"], 10.1)
        self.assertEqual(len(index["records"]), 1)
        self.assertEqual(index["records"][0]["variantCount"], 2)
        self.assertEqual(
            [variant["observedSlots"] for variant in index["records"][0]["variants"]],
            [[360], [1080]],
        )

    def test_duplicate_asset_adds_slot_once_and_upgrades_effect(self) -> None:
        folder = capture.ARCHIVE_DIR / "existing"
        folder.mkdir()
        archived = {
            "version": 9.2,
            "date": "2026-08-31",
            "season": "summer",
            "capturedAt": "2026-08-31T00:17:00+08:00",
            "mode": "split",
            "banner": {},
            "viewport": {},
            "layers": [{"index": 0, "file": "layer.webp", "a": 0.1}],
            "interaction": {"model": "bilibili-moveX-times-a"},
            "contentHash": "same-assets",
            "observedSlots": [0],
        }
        (folder / "banner.json").write_text(json.dumps(archived), encoding="utf-8")
        fresh = {
            **archived,
            "version": 10.0,
            "banner": {"width": 1650},
            "viewport": {"width": 1650, "height": 800},
            "layers": [
                {
                    "index": 0,
                    "file": "new-name.webp",
                    "a": 0.2,
                    "motion": {"matrixDelta": [[0, 0, 0, 0, 0, 0]]},
                }
            ],
            "interaction": {"model": "bilibili-sampled-horizontal-v1"},
        }
        moment = capture.dt.datetime.fromisoformat("2026-08-31T03:17:00+08:00")

        changed = capture.merge_duplicate_metadata(
            folder,
            archived,
            fresh,
            moment=moment,
            force=False,
        )
        self.assertTrue(changed)
        merged = json.loads((folder / "banner.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["observedSlots"], [0, 180])
        self.assertEqual(merged["interaction"]["model"], "bilibili-sampled-horizontal-v1")
        self.assertEqual(merged["layers"][0]["file"], "layer.webp")

        changed_again = capture.merge_duplicate_metadata(
            folder,
            merged,
            fresh,
            moment=moment,
            force=False,
        )
        self.assertFalse(changed_again)

    def test_same_asset_can_appear_on_two_dates_without_duplicate_archive(self) -> None:
        folder = capture.ARCHIVE_DIR / "one-physical-archive"
        folder.mkdir()
        archived = {
            "version": 10.1,
            "date": "2020-01-01",
            "season": "winter",
            "capturedAt": "2020-01-01T08:00:00+08:00",
            "lastObservedAt": "2020-01-01T08:00:00+08:00",
            "mode": "static",
            "static": {"file": "static.webp"},
            "layers": [],
            "interaction": {"model": "none"},
            "contentHash": "same-physical-asset",
            "layoutHash": "same-layout",
            "familyId": "family-2020-01-01",
            "observedSlots": [480],
            "timeZone": "Asia/Shanghai",
        }
        (folder / "banner.json").write_text(json.dumps(archived), encoding="utf-8")
        fresh = {
            **archived,
            "date": "2020-01-02",
            "capturedAt": "2020-01-02T20:00:00+08:00",
            "lastObservedAt": "2020-01-02T20:00:00+08:00",
            "familyId": "family-2020-01-02",
            "observedSlots": [1200],
        }
        moment = capture.dt.datetime.fromisoformat("2020-01-02T20:00:00+08:00")

        changed = capture.merge_duplicate_metadata(
            folder,
            archived,
            fresh,
            moment=moment,
            force=False,
            record_observation=True,
        )
        self.assertTrue(changed)
        self.assertEqual(len(list(capture.ARCHIVE_DIR.iterdir())), 1)

        capture.rebuild_index()
        index = json.loads((capture.DATA_DIR / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [record["date"] for record in index["records"]],
            ["2020-01-02", "2020-01-01"],
        )
        self.assertTrue(
            all(record["contentHash"] == "same-physical-asset" for record in index["records"])
        )


if __name__ == "__main__":
    unittest.main()
