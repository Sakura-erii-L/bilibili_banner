from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from . import capture as core
except ImportError:
    import capture as core


ORIGINAL_PAGE = "https://www.bilibili.com/"
AVAILABILITY_API = os.environ.get(
    "WAYBACK_AVAILABILITY_API",
    "https://archive.org/wayback/available",
)
REPLAY_BASE = os.environ.get(
    "WAYBACK_REPLAY_BASE",
    "https://web.archive.org/web",
)
REQUEST_DELAY_SECONDS = float(os.environ.get("WAYBACK_REQUEST_DELAY", "0.2"))


def run_checkpoint(
    script: str,
    *,
    processed: int,
    succeeded: int,
    changed: int,
    final: bool,
) -> None:
    script_path = Path(script).resolve()
    if not script_path.is_file():
        raise RuntimeError(f"checkpoint script does not exist: {script_path}")

    env = os.environ.copy()
    env.update(
        {
            "WAYBACK_CHECKPOINT_PROCESSED": str(processed),
            "WAYBACK_CHECKPOINT_SUCCEEDED": str(succeeded),
            "WAYBACK_CHECKPOINT_CHANGED": str(changed),
            "WAYBACK_CHECKPOINT_FINAL": "1" if final else "0",
        }
    )
    subprocess.run([sys.executable, str(script_path)], check=True, env=env)


def parse_date(value: str, *, end: bool = False) -> dt.date:
    value = value.strip()
    if len(value) == 4 and value.isdigit():
        year = int(value)
        return dt.date(year, 12 if end else 1, 31 if end else 1)
    return dt.date.fromisoformat(value)


def target_dates(start: dt.date, end: dt.date, cadence: str) -> Iterable[dt.date]:
    if cadence == "monthly":
        current = start.replace(day=1)
        while current <= end:
            candidate = max(current, start)
            if candidate <= end:
                yield candidate
            current = (
                current.replace(year=current.year + 1, month=1)
                if current.month == 12
                else current.replace(month=current.month + 1)
            )
        return

    step = dt.timedelta(days=1 if cadence == "daily" else 7)
    current = start
    while current <= end:
        yield current
        current += step


def read_json(url: str, *, timeout: int = 45, attempts: int = 3) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": core.USER_AGENT,
        },
    )
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Wayback API request failed after {attempts} attempts: {error}")


def availability_url(target: dt.date, api_url: str) -> str:
    query = urllib.parse.urlencode(
        {
            "url": ORIGINAL_PAGE,
            "timestamp": target.strftime("%Y%m%d120000"),
        }
    )
    return f"{api_url}?{query}"


def discover_snapshots(
    start: dt.date,
    end: dt.date,
    *,
    cadence: str,
    api_url: str,
) -> list[dict[str, str]]:
    snapshots: dict[str, dict[str, str]] = {}
    for target in target_dates(start, end, cadence):
        try:
            payload = read_json(availability_url(target, api_url))
        except Exception as exc:
            print(f"Wayback discovery failed near {target.isoformat()}: {exc}")
            continue
        closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
        timestamp = str(closest.get("timestamp") or "")
        if not closest.get("available") or len(timestamp) != 14:
            print(f"No snapshot near {target.isoformat()}")
            continue

        captured_date = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").date()
        if not start <= captured_date <= end:
            print(
                f"Skipped out-of-range snapshot {timestamp} "
                f"for target {target.isoformat()}"
            )
            continue

        snapshots[timestamp] = {
            "timestamp": timestamp,
            "original": ORIGINAL_PAGE,
            "availabilityUrl": availability_url(target, api_url),
        }
        print(f"Discovered {timestamp} near {target.isoformat()}")
        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    return [snapshots[key] for key in sorted(snapshots)]


def imported_wayback_timestamps() -> set[str]:
    timestamps: set[str] = set()
    for _, manifest in core.iter_archive_manifests():
        for observation in core.manifest_observations(manifest):
            source = observation.get("source") or {}
            timestamp = str(source.get("waybackTimestamp") or "")
            if len(timestamp) == 14 and timestamp.isdigit():
                timestamps.add(timestamp)
    return timestamps


def snapshot_moment(timestamp: str) -> dt.datetime:
    utc = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=dt.timezone.utc
    )
    return utc.astimezone(ZoneInfo(core.TIMEZONE))


def replay_url(timestamp: str, original: str, replay_base: str) -> str:
    return f"{replay_base.rstrip('/')}/{timestamp}/{original}"


def archived_asset_url(timestamp: str, src: str, replay_base: str) -> str:
    if src.startswith(("blob:", "data:")):
        return src
    absolute = urllib.parse.urljoin(ORIGINAL_PAGE, src)
    host = (urllib.parse.urlparse(absolute).hostname or "").lower()
    if host in {"web.archive.org", "wayback.archive-it.org"}:
        return absolute
    return f"{replay_base.rstrip('/')}/{timestamp}id_/{absolute}"


def is_direct_bilibili_request(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "hdslb.com"
        or host.endswith(".hdslb.com")
    )


def describe_banner_dom(page) -> list[dict[str, Any]]:
    return page.evaluate(
        r"""() => {
            const roots = [
                ...document.querySelectorAll(
                    ".animated-banner, .bili-header__banner, .head-banner, "
                    + ".header-banner, .bili-banner, #banner_link, "
                    + ".banner_link, .banner-link, "
                    + "[id*='banner' i], [class*='banner' i], "
                    + "[id*='header' i], [class*='header' i]"
                )
            ];
            const candidates = [];
            for (const root of roots) {
                let current = root;
                for (let depth = 0; current && depth < 4; depth += 1) {
                    if (!candidates.includes(current)) candidates.push(current);
                    current = current.parentElement;
                }
            }
            return candidates.slice(0, 24).map(element => {
                const cs = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const images = [...element.querySelectorAll("img")]
                    .slice(0, 5)
                    .map(img => img.currentSrc || img.src || "");
                return {
                    tag: element.tagName.toLowerCase(),
                    id: element.id || "",
                    className: String(element.className || "").slice(0, 300),
                    width: rect.width,
                    height: rect.height,
                    backgroundImage: cs.backgroundImage,
                    beforeBackgroundImage: getComputedStyle(element, "::before").backgroundImage,
                    afterBackgroundImage: getComputedStyle(element, "::after").backgroundImage,
                    images
                };
            });
        }"""
    )


def capture_snapshot(
    context,
    page,
    snapshot: dict[str, str],
    *,
    replay_base: str,
    force: bool,
) -> dict[str, Any]:
    timestamp = snapshot["timestamp"]
    moment = snapshot_moment(timestamp)
    url = replay_url(timestamp, snapshot["original"], replay_base)
    print(f"Opening Wayback snapshot {timestamp} in headless mode...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        print(
            f"Wayback navigation timed out for {timestamp}; "
            "continuing with the DOM already received."
        )

    try:
        page.wait_for_function(
            """() =>
                document.querySelectorAll(".animated-banner .layer").length > 0
                || document.querySelector("picture.banner-img img")
                || document.querySelector(".bili-header__banner")
                || document.querySelector(".head-banner")
                || document.querySelector(".header-banner")
                || document.querySelector(".bili-banner")
                || document.querySelector("#banner_link")
                || document.querySelector(".banner_link")
                || document.querySelector(".banner-link")
            """,
            timeout=core.BANNER_WAIT_MS,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1800)

    geometry = core.get_banner_geometry(page)
    if not geometry:
        print(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "pageUrl": page.url,
                    "title": page.title(),
                    "bannerDom": describe_banner_dom(page),
                },
                ensure_ascii=False,
            )
        )
        raise RuntimeError("no supported Banner container in archived DOM")

    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".wayback_", dir=core.DATA_DIR))
    rewrite = lambda src: archived_asset_url(timestamp, src, replay_base)

    try:
        static = core.capture_static(
            page,
            context,
            temp,
            referer=url,
            url_rewriter=rewrite,
        )
        detected_layers = core.read_layers(page)
        layers: list[dict[str, Any]] = []
        mode = "split" if detected_layers else "static"
        interaction: dict[str, Any] = {
            "model": "none",
            "positionAxis": "x-only",
            "effects": [],
        }

        if detected_layers:
            initial_layers, interaction, motions = core.sample_interaction(page, geometry)
            for index, (item, motion) in enumerate(zip(initial_layers, motions)):
                src = str(item.get("src") or "")
                if not src:
                    raise RuntimeError(f"layer {index} has no archived asset URL")
                src = rewrite(src)
                local_file, content_type = core.download_asset(
                    context,
                    page,
                    src,
                    temp,
                    index,
                    item["tag"],
                    referer=url,
                )
                layers.append(
                    {
                        "index": index,
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
                        "transform": item["transform"],
                        "opacity": [item["layerOpacity"], item["mediaOpacity"]],
                        "motion": motion,
                        "a": core._legacy_slope(
                            interaction["inputSamplesPx"],
                            [sample[4] for sample in motion["matrixDelta"]],
                        ),
                        "captureTargetTag": item["targetTag"],
                    }
                )

            if layers and not interaction.get("effects"):
                raise RuntimeError(
                    "all archived layer interaction effects are zero; "
                    "refusing to create a fake split archive"
                )

        if mode == "static" and not static:
            print(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "pageUrl": page.url,
                        "title": page.title(),
                        "bannerDom": describe_banner_dom(page),
                    },
                    ensure_ascii=False,
                )
            )
            raise RuntimeError("no downloadable archived Banner asset found")

        captured_at = moment.isoformat(timespec="seconds")
        manifest: dict[str, Any] = {
            "version": 10.1,
            "capturedAt": captured_at,
            "date": moment.strftime("%Y-%m-%d"),
            "season": core.season_of(moment.month),
            "source": {
                "page": ORIGINAL_PAGE,
                "resolvedUrl": page.url,
                "captureMethod": "wayback-hidden-rendered-dom-sampled",
                "waybackTimestamp": timestamp,
                "waybackReplay": url,
                "availabilityUrl": snapshot.get("availabilityUrl"),
            },
            "viewport": core.VIEWPORT,
            "banner": geometry,
            "mode": mode,
            "static": static,
            "layers": layers,
            "interaction": interaction,
            "timeZone": core.TIMEZONE,
            "lastObservedAt": captured_at,
        }
        result = core.archive_capture(
            temp,
            manifest,
            moment=moment,
            force=force,
            update_current=False,
            record_observation=True,
        )
        return {
            "timestamp": timestamp,
            "status": result["status"],
            "contentHash": result["contentHash"],
            "archive": str(result["archive"]),
        }
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def main() -> None:
    today = dt.datetime.now(ZoneInfo(core.TIMEZONE)).date()
    parser = argparse.ArgumentParser(
        description=(
            "Headlessly import real Bilibili Banner assets from Wayback snapshots. "
            "No screenshots are created and direct Bilibili requests are blocked."
        )
    )
    parser.add_argument("--from-date", default="2018-01-01")
    parser.add_argument("--to-date", default=today.isoformat())
    parser.add_argument(
        "--cadence",
        choices=("monthly", "weekly", "daily"),
        default="monthly",
    )
    parser.add_argument("--snapshot", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="Run --checkpoint-script after this many created/updated records.",
    )
    parser.add_argument(
        "--checkpoint-script",
        help="Python script called at each checkpoint and once at the end.",
    )
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--availability-api", default=AVAILABILITY_API)
    parser.add_argument("--replay-base", default=REPLAY_BASE)
    args = parser.parse_args()

    if args.checkpoint_every < 0:
        parser.error("--checkpoint-every must not be negative")
    if bool(args.checkpoint_every) != bool(args.checkpoint_script):
        parser.error(
            "--checkpoint-every and --checkpoint-script must be used together"
        )

    start = parse_date(args.from_date)
    end = parse_date(args.to_date, end=True)
    if start > end:
        parser.error("--from-date must not be after --to-date")

    if args.snapshot:
        snapshots = [
            {"timestamp": value, "original": ORIGINAL_PAGE, "availabilityUrl": ""}
            for value in sorted(set(args.snapshot))
        ]
    else:
        snapshots = discover_snapshots(
            start,
            end,
            cadence=args.cadence,
            api_url=args.availability_api,
        )

    if args.limit > 0:
        snapshots = snapshots[: args.limit]
    skipped_known = 0
    if not args.force:
        known_timestamps = imported_wayback_timestamps()
        skipped_known = sum(
            snapshot["timestamp"] in known_timestamps for snapshot in snapshots
        )
        snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot["timestamp"] not in known_timestamps
        ]
        if skipped_known:
            print(f"Skipped {skipped_known} already imported Wayback snapshots.")
    print(json.dumps({"snapshotCount": len(snapshots)}, ensure_ascii=False))
    if args.discovery_only:
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return
    if not snapshots:
        if skipped_known:
            print("All discovered Wayback snapshots were already imported.")
            return
        raise SystemExit("No Wayback snapshots were discovered in the requested range.")

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    changed_since_checkpoint = 0
    processed = 0
    system_browser = core.find_system_browser()
    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if system_browser:
            launch_kwargs["executable_path"] = system_browser
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport=core.VIEWPORT,
            user_agent=core.USER_AGENT,
            locale="zh-CN",
        )

        def route_request(route) -> None:
            if is_direct_bilibili_request(route.request.url):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            for processed, snapshot in enumerate(snapshots, start=1):
                try:
                    result = capture_snapshot(
                        context,
                        page,
                        snapshot,
                        replay_base=args.replay_base,
                        force=args.force,
                    )
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False))
                    if result["status"] in {"created", "updated"}:
                        changed_since_checkpoint += 1
                except Exception as exc:
                    failure = {
                        "timestamp": snapshot["timestamp"],
                        "error": str(exc),
                    }
                    failures.append(failure)
                    print(json.dumps(failure, ensure_ascii=False))
                    continue

                if (
                    args.checkpoint_script
                    and changed_since_checkpoint >= args.checkpoint_every
                    and processed < len(snapshots)
                ):
                    run_checkpoint(
                        args.checkpoint_script,
                        processed=processed,
                        succeeded=len(results),
                        changed=changed_since_checkpoint,
                        final=False,
                    )
                    changed_since_checkpoint = 0
        finally:
            context.close()
            browser.close()

    if args.checkpoint_script:
        run_checkpoint(
            args.checkpoint_script,
            processed=processed,
            succeeded=len(results),
            changed=changed_since_checkpoint,
            final=True,
        )

    summary = {
        "requested": len(snapshots),
        "succeeded": len(results),
        "created": sum(item["status"] == "created" for item in results),
        "updated": sum(item["status"] == "updated" for item in results),
        "unchanged": sum(item["status"] == "unchanged" for item in results),
        "failed": len(failures),
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
