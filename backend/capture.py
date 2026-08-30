from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("BANNER_DATA_DIR", PROJECT_ROOT / "data")).resolve()
ARCHIVE_DIR = DATA_DIR / "archive"
CURRENT_DIR = DATA_DIR / "current"

SITE = os.environ.get("BANNER_SOURCE_URL", "https://www.bilibili.com/")
TIMEZONE = os.environ.get("BANNER_TIMEZONE", "Asia/Shanghai")
VIEWPORT_WIDTH = int(os.environ.get("BANNER_VIEWPORT_WIDTH", "1650"))
VIEWPORT_HEIGHT = int(os.environ.get("BANNER_VIEWPORT_HEIGHT", "800"))
VIEWPORT = {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

BANNER_WAIT_MS = 18000
MOTION_PROBE_PX = 1000
MOTION_SETTLE_MS = 1200
MOTION_EPSILON = 1e-6


def now_local() -> dt.datetime:
    try:
        return dt.datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return dt.datetime.now().astimezone()


def season_of(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def find_system_browser() -> str | None:
    manual = os.environ.get("BROWSER_EXE")
    if manual and Path(manual).exists():
        return manual

    candidates: list[Path] = []

    if os.name == "nt":
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        pfx86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        candidates.extend(
            [
                pfx86 / "Microsoft/Edge/Application/msedge.exe",
                pf / "Microsoft/Edge/Application/msedge.exe",
                local / "Microsoft/Edge/Application/msedge.exe",
                pf / "Google/Chrome/Application/chrome.exe",
                pfx86 / "Google/Chrome/Application/chrome.exe",
                local / "Google/Chrome/Application/chrome.exe",
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/microsoft-edge"),
                Path("/usr/bin/microsoft-edge-stable"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def ext_for(url: str, content_type: str, tag: str) -> str:
    try:
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    except Exception:
        ext = ""

    if ext and 1 < len(ext) <= 8:
        return ext

    ct = (content_type or "").lower()
    if "webm" in ct:
        return ".webm"
    if "mp4" in ct:
        return ".mp4"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    return ".webm" if tag == "video" else ".bin"


def save_blob(page, src: str, output: Path) -> str:
    result = page.evaluate(
        """async (url) => {
            const r = await fetch(url);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const blob = await r.blob();
            const ab = await blob.arrayBuffer();
            const bytes = new Uint8Array(ab);
            let binary = "";
            const block = 0x8000;
            for (let i = 0; i < bytes.length; i += block) {
                binary += String.fromCharCode(...bytes.subarray(i, i + block));
            }
            return {b64:btoa(binary), type:blob.type};
        }""",
        src,
    )
    output.write_bytes(base64.b64decode(result["b64"]))
    return str(result.get("type") or "")


def download_asset(context, page, src: str, folder: Path, index: int, tag: str) -> tuple[str, str]:
    if src.startswith("blob:"):
        probe = folder / f"layer_{index:02d}_blob.tmp"
        content_type = save_blob(page, src, probe)
        ext = ext_for("", content_type, tag)
        filename = f"layer_{index:02d}_blob{ext}"
        final = folder / filename
        probe.replace(final)
        return filename, content_type

    response = context.request.get(
        src,
        headers={"Referer": SITE, "User-Agent": USER_AGENT},
        timeout=30000,
    )
    if not response.ok:
        raise RuntimeError(f"asset HTTP {response.status}: {src}")

    content_type = response.headers.get("content-type", "")
    ext = ext_for(src, content_type, tag)
    digest = hashlib.sha1(src.encode("utf-8")).hexdigest()[:10]
    filename = f"layer_{index:02d}_{digest}{ext}"
    (folder / filename).write_bytes(response.body())
    return filename, content_type


def read_layers(page) -> list[dict[str, Any]]:
    """
    Important: Bilibili's parallax transform is read from `.layer.firstElementChild`,
    matching the known public capture implementation. The media asset can be that
    element itself or a nested img/video.
    """
    return page.evaluate(
        r"""() => {
            const matrixOf = (el) => {
                const inline = el.style?.transform || "";
                const computed = getComputedStyle(el).transform;
                const source = inline && inline !== "none" ? inline : computed;
                const m = source && source !== "none"
                    ? new DOMMatrix(source)
                    : new DOMMatrix();
                return [m.a, m.b, m.c, m.d, m.e, m.f];
            };

            return [...document.querySelectorAll(".animated-banner .layer")]
                .map((layer, index) => {
                    const target = layer.firstElementChild;
                    if (!target) return null;

                    const media = target.matches?.("img,video")
                        ? target
                        : target.querySelector?.("img,video");

                    if (!media) return null;

                    const tcs = getComputedStyle(target);
                    const mcs = getComputedStyle(media);
                    const lcs = getComputedStyle(layer);
                    const matrix = matrixOf(target);

                    const width =
                        Number(target.width)
                        || parseFloat(tcs.width)
                        || Number(media.width)
                        || parseFloat(mcs.width)
                        || media.clientWidth
                        || 0;

                    const height =
                        Number(target.height)
                        || parseFloat(tcs.height)
                        || Number(media.height)
                        || parseFloat(mcs.height)
                        || media.clientHeight
                        || 0;

                    return {
                        index,
                        tag: media.tagName.toLowerCase(),
                        src: media.currentSrc || media.src || "",
                        width,
                        height,
                        naturalWidth: media.naturalWidth || media.videoWidth || 0,
                        naturalHeight: media.naturalHeight || media.videoHeight || 0,
                        objectFit: mcs.objectFit,
                        objectPosition: mcs.objectPosition,
                        transformOrigin: tcs.transformOrigin,
                        transform: matrix,
                        transformX: matrix[4],
                        layerOpacity: Number.parseFloat(lcs.opacity || "1"),
                        mediaOpacity: Number.parseFloat(mcs.opacity || "1"),
                        targetTag: target.tagName.toLowerCase(),
                        inlineTransform: target.style?.transform || ""
                    };
                })
                .filter(Boolean);
        }"""
    )


def get_banner_geometry(page) -> dict[str, float] | None:
    return page.evaluate(
        """() => {
            const el = document.querySelector(".animated-banner");
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x:r.x, y:r.y, width:r.width, height:r.height};
        }"""
    )


def capture_static(page, context, folder: Path) -> dict[str, Any] | None:
    info = page.evaluate(
        """() => {
            const img =
                document.querySelector("picture.banner-img img")
                || document.querySelector(".bili-header__banner picture img")
                || document.querySelector(".bili-header__banner > img");
            if (!img) return null;
            const cs = getComputedStyle(img);
            return {
                src: img.currentSrc || img.src || "",
                naturalWidth: img.naturalWidth || 0,
                naturalHeight: img.naturalHeight || 0,
                objectFit: cs.objectFit,
                objectPosition: cs.objectPosition
            };
        }"""
    )

    if not info or not info.get("src"):
        return None

    response = context.request.get(
        info["src"],
        headers={"Referer": SITE, "User-Agent": USER_AGENT},
        timeout=30000,
    )
    if not response.ok:
        return None

    ext = ext_for(info["src"], response.headers.get("content-type", ""), "img")
    filename = "static" + ext
    (folder / filename).write_bytes(response.body())
    info["file"] = filename
    return info


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def content_fingerprint(folder: Path, manifest: dict[str, Any]) -> str:
    entries: list[dict[str, Any]] = []

    if manifest.get("mode") == "split":
        for layer in sorted(manifest.get("layers", []), key=lambda x: int(x.get("index", 0))):
            file = layer.get("file")
            if not file:
                continue
            p = folder / file
            if not p.exists():
                continue
            entries.append(
                {
                    "index": int(layer.get("index", 0)),
                    "tag": layer.get("tag"),
                    "sha256": sha256_file(p),
                    "size": p.stat().st_size,
                    "naturalWidth": int(layer.get("naturalWidth") or 0),
                    "naturalHeight": int(layer.get("naturalHeight") or 0),
                }
            )
    else:
        static_file = (manifest.get("static") or {}).get("file")
        if static_file:
            p = folder / static_file
            if p.exists():
                entries.append(
                    {
                        "kind": "static",
                        "sha256": sha256_file(p),
                        "size": p.stat().st_size,
                    }
                )

    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def read_manifest(folder: Path) -> dict[str, Any] | None:
    file = folder / "banner.json"
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None


def archive_hashes() -> set[str]:
    hashes: set[str] = set()
    if not ARCHIVE_DIR.exists():
        return hashes

    for folder in ARCHIVE_DIR.iterdir():
        if not folder.is_dir():
            continue
        manifest = read_manifest(folder)
        if not manifest:
            continue
        h = manifest.get("contentHash")
        if isinstance(h, str) and h:
            hashes.add(h)
            continue
        try:
            hashes.add(content_fingerprint(folder, manifest))
        except Exception:
            pass

    return hashes


def replace_current(temp_dir: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup = DATA_DIR / ".current_old"

    shutil.rmtree(backup, ignore_errors=True)

    if CURRENT_DIR.exists():
        CURRENT_DIR.replace(backup)

    try:
        shutil.copytree(temp_dir, CURRENT_DIR)
    except Exception:
        shutil.rmtree(CURRENT_DIR, ignore_errors=True)
        if backup.exists():
            backup.replace(CURRENT_DIR)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def rebuild_index() -> None:
    candidates: list[dict[str, Any]] = []

    if ARCHIVE_DIR.exists():
        for folder in ARCHIVE_DIR.iterdir():
            if not folder.is_dir():
                continue

            item = read_manifest(folder)
            if not item:
                continue

            content_hash = item.get("contentHash")
            if not content_hash:
                try:
                    content_hash = content_fingerprint(folder, item)
                except Exception:
                    content_hash = folder.name

            candidates.append(
                {
                    "id": folder.name,
                    "date": item["date"],
                    "year": item["date"][:4],
                    "month": item["date"][5:7],
                    "yearMonth": item["date"][:7],
                    "season": item["season"],
                    "capturedAt": item["capturedAt"],
                    "mode": item["mode"],
                    "layerCount": len(item.get("layers", [])),
                    "contentHash": content_hash,
                    "manifest": f"./data/archive/{folder.name}/banner.json",
                }
            )

    candidates.sort(key=lambda x: x["capturedAt"], reverse=True)

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for record in candidates:
        h = str(record.get("contentHash") or record["id"])
        if h in seen:
            continue
        seen.add(h)
        records.append(record)

    payload = {
        "version": 9.2,
        "generatedAt": now_local().isoformat(timespec="seconds"),
        "records": records,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_diagnostic(page, *, reason: str, before=None, after=None) -> None:
    diagnostic = {
        "capturedAt": now_local().isoformat(timespec="seconds"),
        "reason": reason,
        "url": page.url,
        "title": page.title(),
        "animatedCount": page.locator(".animated-banner").count(),
        "layerCount": page.locator(".animated-banner .layer").count(),
        "before": before,
        "after": after,
    }
    (DATA_DIR / "diagnostic.json").write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def capture(*, force: bool) -> int:
    """
    Always headless.
    No local/GitHub/NAS mode is allowed to pop open a visible Bilibili page.
    """
    system_browser = find_system_browser()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    now = now_local()
    date_text = now.strftime("%Y-%m-%d")

    # Persistent browser profile is optional, but it is still used headlessly.
    if os.environ.get("CI") or os.environ.get("BANNER_PROFILE_MODE", "").lower() == "temporary":
        profile_dir = Path(tempfile.mkdtemp(prefix="bilibili-banner-profile-"))
        remove_profile = True
    else:
        profile_dir = Path(
            os.environ.get("BANNER_PROFILE_DIR", PROJECT_ROOT / ".runtime/browser-profile")
        ).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        remove_profile = False

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": True,
            "viewport": VIEWPORT,
            "user_agent": USER_AGENT,
            "locale": "zh-CN",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if system_browser:
            launch_kwargs["executable_path"] = system_browser

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            print("Fetching current Bilibili banner in hidden/headless mode...")

            # Two navigations intentionally mirror the established public
            # capture approach and improve initialization reliability.
            page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            page.goto(SITE, wait_until="domcontentloaded", timeout=60000)

            try:
                page.wait_for_function(
                    """() =>
                        document.querySelectorAll(".animated-banner .layer").length > 0
                        || document.querySelector("picture.banner-img img")
                        || document.querySelector(".bili-header__banner img")
                    """,
                    timeout=BANNER_WAIT_MS,
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(1800)

            geometry = get_banner_geometry(page)
            if not geometry:
                save_diagnostic(page, reason="animated-banner-not-found")
                raise RuntimeError("No .animated-banner was found. See data/diagnostic.json")

            temp = Path(tempfile.mkdtemp(prefix=".capture_", dir=DATA_DIR))
            try:

                static = capture_static(page, context, temp)
                before = read_layers(page)

                layers: list[dict[str, Any]] = []
                mode = "split" if before else "static"

                if before:
                    x0 = geometry["x"] + 2
                    y_inside = geometry["y"] + min(
                        max(20, geometry["height"] * 0.5),
                        max(20, geometry["height"] - 3),
                    )

                    distance = min(
                        MOTION_PROBE_PX,
                        max(200, geometry["width"] - 6),
                    )

                    # Exactly the relevant interaction sequence:
                    # enter the banner, move horizontally, wait for the site's
                    # own JS to update each firstElementChild.style.transform.
                    page.mouse.move(x0, y_inside)
                    page.mouse.move(x0 + distance, y_inside, steps=1)
                    page.wait_for_timeout(MOTION_SETTLE_MS)
                    after = read_layers(page)

                    if len(after) != len(before):
                        save_diagnostic(
                            page,
                            reason="layer-count-changed-during-motion-probe",
                            before=before,
                            after=after,
                        )
                        raise RuntimeError(
                            "Layer count changed while probing interaction. "
                            "See data/diagnostic.json"
                        )

                    measured_a: list[float] = []

                    for i, item in enumerate(before):
                        src = item.get("src") or ""
                        if not src:
                            save_diagnostic(
                                page,
                                reason="layer-asset-url-missing",
                                before=before,
                                after=after,
                            )
                            raise RuntimeError(
                                f"Layer {i} has no downloadable asset URL. "
                                "See data/diagnostic.json"
                            )

                        a = (
                            float(after[i]["transformX"]) - float(item["transformX"])
                        ) / float(distance)

                        measured_a.append(a)

                        try:
                            local_file, content_type = download_asset(
                                context, page, src, temp, i, item["tag"]
                            )
                        except Exception as exc:
                            save_diagnostic(
                                page,
                                reason="layer-asset-download-failed",
                                before=before,
                                after=after,
                            )
                            raise RuntimeError(
                                f"Layer {i} asset download failed: {exc}. "
                                "See data/diagnostic.json"
                            ) from exc

                        layers.append(
                            {
                                "index": i,
                                "tag": item["tag"],
                                "src": src,
                                "file": local_file,
                                "contentType": content_type,
                                "width": item["width"],
                                "height": item["height"],
                                "naturalWidth": item["naturalWidth"],
                                "naturalHeight": item["naturalHeight"],
                                "objectFit": item["objectFit"],
                                "objectPosition": item["objectPosition"],
                                "transformOrigin": item["transformOrigin"],
                                # This is the initial transform that reconstructs
                                # the Bilibili layer composition.
                                "transform": item["transform"],
                                "opacity": [
                                    item["layerOpacity"],
                                    item["mediaOpacity"],
                                ],
                                # Same meaning as the public reproduction:
                                # moveX * a
                                "a": a,
                                "captureTargetTag": item["targetTag"],
                            }
                        )

                    max_motion = max((abs(x) for x in measured_a), default=0.0)

                    # Do not silently archive an inert "interactive" banner.
                    if layers and max_motion <= MOTION_EPSILON:
                        save_diagnostic(
                            page,
                            reason="all-layer-horizontal-motion-is-zero",
                            before=before,
                            after=after,
                        )
                        raise RuntimeError(
                            "All measured layer movement is zero; capture aborted "
                            "instead of creating a non-interactive archive. "
                            "See data/diagnostic.json"
                        )

                    if not layers:
                        mode = "static"

                manifest: dict[str, Any] = {
                    "version": 9.2,
                    "capturedAt": now.isoformat(timespec="seconds"),
                    "date": date_text,
                    "season": season_of(now.month),
                    "source": {
                        "page": SITE,
                        "resolvedUrl": page.url,
                        "captureMethod": "hidden-rendered-homepage-dom",
                    },
                    "viewport": VIEWPORT,
                    "banner": geometry,
                    "mode": mode,
                    "static": static,
                    "layers": layers,
                    "interaction": {
                        "model": "bilibili-moveX-times-a",
                        "positionAxis": "x-only",
                        "effects": ["translateX"],
                        "returnDurationMs": 300,
                    },
                }

                content_hash = content_fingerprint(temp, manifest)
                manifest["contentHash"] = content_hash

                (temp / "banner.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                current_manifest = read_manifest(CURRENT_DIR)
                current_hash = (
                    current_manifest.get("contentHash")
                    if current_manifest
                    else None
                )
                existing_hashes = archive_hashes()

                # Any duplicate visual is deliberately a no-op. Updating the
                # tracked current manifest with a new timestamp would create a
                # meaningless daily commit when the Banner did not change.
                if not force and current_hash == content_hash:
                    print(f"No visual change: {content_hash}")
                    return 0

                if not force and content_hash in existing_hashes:
                    print(
                        "Visual already exists in history; "
                        f"archive and current update skipped: {content_hash}"
                    )
                    return 0

                replace_current(temp)

                archive_name = (
                    f"{now.strftime('%Y-%m-%d_%H%M%S')}_{content_hash}"
                )
                archive_dir = ARCHIVE_DIR / archive_name
                shutil.copytree(temp, archive_dir)

                rebuild_index()

                print(
                    f"Captured new unique banner: mode={mode}, "
                    f"layers={len(layers)}, date={date_text}, hash={content_hash}"
                )
                print(f"Archive: {archive_dir}")
                return 0

            finally:
                shutil.rmtree(temp, ignore_errors=True)

        finally:
            context.close()
            if remove_profile:
                shutil.rmtree(profile_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Headlessly capture and archive the current Bilibili homepage banner. "
            "This program never opens a visible Bilibili browser window."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="archive even when duplicated",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="rebuild data/index.json from existing archive only",
    )
    args = parser.parse_args()

    if args.rebuild_index:
        rebuild_index()
        print("Rebuilt data/index.json")
        return

    capture(force=args.force)


if __name__ == "__main__":
    main()
