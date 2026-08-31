from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("BANNER_DATA_DIR", PROJECT_ROOT / "data")).resolve()
ARCHIVE_DIR = DATA_DIR / "archive"
CURRENT_DIR = DATA_DIR / "current"

SITE = os.environ.get("BANNER_SOURCE_URL", "https://www.bilibili.com/")
TIMEZONE = os.environ.get("BANNER_TIMEZONE", "Asia/Shanghai")
TIME_SLOT_MINUTES = max(
    1,
    min(1440, int(os.environ.get("BANNER_TIME_SLOT_MINUTES", "180"))),
)
VIEWPORT_WIDTH = int(os.environ.get("BANNER_VIEWPORT_WIDTH", "1650"))
VIEWPORT_HEIGHT = int(os.environ.get("BANNER_VIEWPORT_HEIGHT", "800"))
VIEWPORT = {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

BANNER_WAIT_MS = 18000
MOTION_PROBE_PX = 1600
MOTION_SAMPLE_FRACTIONS = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
MOTION_ENTER_SETTLE_MS = 80
MOTION_SETTLE_MS = 180
MOTION_RESET_MS = 1200
RETURN_SAMPLE_TIMES_MS = (0, 16, 33, 50, 75, 100, 150, 225, 325, 475, 700, 1000, 1400)
RETURN_SETTLED_RATIO = 0.01
MOTION_EPSILON = 1e-6
Y_MOTION_EPSILON = 0.05

BANNER_SELECTORS = (
    ".animated-banner",
    ".bili-header__banner",
    ".head-banner",
    ".header-banner",
    ".bili-banner",
    "#banner_link",
    ".banner_link",
    ".banner-link",
)


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
    if "svg" in ct:
        return ".svg"
    if "gif" in ct:
        return ".gif"
    if "avif" in ct:
        return ".avif"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    return ".webm" if tag == "video" else ".bin"


def media_type(tag: str, src: str, content_type: str, *, animated: bool = False) -> str:
    value = f"{src} {content_type}".lower()
    if tag == "video" or any(token in value for token in ("video/", ".mp4", ".webm", ".m3u8")):
        return "video"
    if tag == "svg" or "image/svg" in value or ".svg" in value:
        return "svg"
    if animated or any(token in value for token in ("image/gif", "image/apng", ".gif", ".apng")):
        return "animated"
    if tag in {"img", "picture", "source"} or value.startswith("image/"):
        return "image"
    return "other"


def write_data_uri(src: str, output: Path) -> str:
    header, separator, payload = src.partition(",")
    if not separator:
        raise ValueError("invalid data URI")
    metadata = header[5:].split(";")
    content_type = metadata[0] or "application/octet-stream"
    if "base64" in metadata[1:]:
        output.write_bytes(base64.b64decode(payload))
    else:
        output.write_bytes(urllib.parse.unquote_to_bytes(payload))
    return content_type


def inspect_banner_structure(page) -> dict[str, Any]:
    """Collect structure evidence without rasterizing or flattening the Banner."""
    return page.evaluate(
        r"""() => {
            const selectors = [
                ".animated-banner", ".bili-header__banner", ".head-banner",
                ".header-banner", ".bili-banner", "#banner_link",
                ".banner_link", ".banner-link"
            ];
            const root = selectors.map(s => document.querySelector(s)).find(Boolean);
            const css = element => {
                const cs = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const transform = cs.transform && cs.transform !== "none"
                    ? new DOMMatrix(cs.transform) : new DOMMatrix();
                return {
                    animationName: cs.animationName,
                    animationDuration: cs.animationDuration,
                    animationDelay: cs.animationDelay,
                    animationIterationCount: cs.animationIterationCount,
                    animationTimingFunction: cs.animationTimingFunction,
                    animationDirection: cs.animationDirection,
                    animationFillMode: cs.animationFillMode,
                    animationPlayState: cs.animationPlayState,
                    transitionProperty: cs.transitionProperty,
                    transitionDuration: cs.transitionDuration,
                    transitionTimingFunction: cs.transitionTimingFunction,
                    transform: [
                        transform.a, transform.b, transform.c,
                        transform.d, transform.e, transform.f
                    ],
                    transformOrigin: cs.transformOrigin,
                    objectFit: cs.objectFit,
                    objectPosition: cs.objectPosition,
                    position: cs.position,
                    opacity: cs.opacity,
                    zIndex: cs.zIndex,
                    width: rect.width,
                    height: rect.height,
                    rectLeft: rect.left,
                    rectTop: rect.top,
                    left: cs.left,
                    top: cs.top,
                    right: cs.right,
                    bottom: cs.bottom,
                };
            };
            const urlOf = element => element.currentSrc || element.src ||
                element.getAttribute("data-src") || element.getAttribute("data-url") || "";
            const media = [...(root ? [root, ...root.querySelectorAll("img,video,source,svg,canvas")] : [])]
                .map((element, index) => ({
                    index,
                    tag: element.tagName.toLowerCase(),
                    src: urlOf(element),
                    inlineContent: element.tagName.toLowerCase() === "svg"
                        ? `data:image/svg+xml,${encodeURIComponent(element.outerHTML)}` : "",
                    srcset: element.getAttribute?.("srcset") || "",
                    sources: element.tagName.toLowerCase() === "video"
                        ? [...element.querySelectorAll("source")].map(urlOf).filter(Boolean)
                        : [],
                    naturalWidth: element.naturalWidth || element.videoWidth || 0,
                    naturalHeight: element.naturalHeight || element.videoHeight || 0,
                    data: Object.fromEntries([...element.attributes || []]
                        .filter(attribute => attribute.name.startsWith("data-"))
                        .map(attribute => [attribute.name, attribute.value])),
                    css: css(element),
                }));
            const html = document.documentElement?.outerHTML || "";
            const scriptText = [...document.scripts].map(script => script.textContent || "").join("\n");
            const styleText = [...document.querySelectorAll("style")].map(style => style.textContent || "").join("\n");
            const stylesheetText = [...document.styleSheets].map(sheet => {
                try {
                    return [...(sheet.cssRules || [])].map(rule => rule.cssText).join("\n");
                } catch (_) {
                    return "";
                }
            }).join("\n");
            const keyframes = [];
            const collectKeyframes = rules => {
                for (const rule of [...(rules || [])]) {
                    if (rule.type === CSSRule.KEYFRAMES_RULE) keyframes.push(rule.cssText);
                    else if (rule.cssRules) collectKeyframes(rule.cssRules);
                }
            };
            for (const sheet of [...document.styleSheets]) {
                try { collectKeyframes(sheet.cssRules); } catch (_) {}
            }
            const cssText = `${styleText}\n${stylesheetText}`;
            const sourceText = `${html}\n${scriptText}\n${cssText}`;
            const bannerScriptText = [...document.scripts]
                .map(script => script.textContent || "")
                .filter(text => /animated-banner|split_layer|is_split_layer|bili-banner|\.layer/i.test(text))
                .join("\n");
            const resources = performance.getEntriesByType("resource")
                .map(entry => ({name: entry.name, initiatorType: entry.initiatorType || ""}))
                .filter(entry => /banner|hdslb|bfs|bilibili|\.m3u8|\.mp4|\.webm/i.test(entry.name))
                .slice(0, 200);
            const stylesheets = [...document.styleSheets].map(sheet => sheet.href).filter(Boolean);
            const scripts = [...document.scripts].map(script => script.src).filter(Boolean);
            const layerElements = [...document.querySelectorAll(".animated-banner .layer")];
            const hasCssAnimation = media.some(item => item.css.animationName && item.css.animationName !== "none")
                || /@keyframes|animation(?:-name)?\s*:/i.test(cssText);
            const hasInteraction = /pointermove|mousemove|mouseenter|mouseleave|clientX|pageX|parallax|translate[XY]|scale\(|rotate\(/i.test(
                `${root?.outerHTML || ""}\n${bannerScriptText}`
            );
            const splitSignal = /(?:is_split_layer|split_layer)\s*[=:"']+\s*1\b|["']layers["']\s*:/i.test(sourceText)
                || media.some(item => /(?:is_split_layer|split_layer)/i.test(JSON.stringify(item.data)));
            return {
                root: root ? {tag: root.tagName.toLowerCase(), id: root.id || "", className: String(root.className || "")} : null,
                layerCount: layerElements.length,
                mediaCount: media.length,
                visibleMediaCount: media.filter(item => ["img", "video", "svg", "canvas"].includes(item.tag)).length,
                media,
                resourceUrls: resources,
                stylesheetUrls: stylesheets,
                scriptUrls: scripts,
                animationCss: keyframes.join("\n").slice(0, 100000),
                signals: {
                    isSplitLayer: splitSignal,
                    hasVideo: media.some(item => item.tag === "video" || item.tag === "source"),
                    hasCanvas: media.some(item => item.tag === "canvas"),
                    hasSvg: media.some(item => item.tag === "svg"),
                    hasSvgAnimation: Boolean(root?.querySelector("svg animate, svg set, svg animateTransform, svg animateMotion")),
                    hasCssAnimation,
                    hasInteraction,
                    hasDynamicSource: /blob:|m3u8|srcset|data-src|data-url/i.test(sourceText),
                },
            };
        }"""
    )


def save_source_evidence(page, folder: Path) -> dict[str, str]:
    source = page.evaluate(
        r"""() => {
            const selectors = [
                ".animated-banner", ".bili-header__banner", ".head-banner",
                ".header-banner", ".bili-banner", "#banner_link",
                ".banner_link", ".banner-link"
            ];
            const root = selectors.map(selector => document.querySelector(selector)).find(Boolean);
            const relevant = /banner|split_layer|is_split_layer|animated-banner|pointermove|mousemove|parallax|keyframes/i;
            const styles = [...document.querySelectorAll("style")]
                .map(element => element.textContent || "")
                .filter(text => relevant.test(text))
                .join("\n\n");
            const scripts = [...document.scripts]
                .filter(element => !element.src)
                .map(element => element.textContent || "")
                .filter(text => relevant.test(text))
                .join("\n\n");
            const json = [...document.querySelectorAll('script[type*="json" i]')]
                .map(element => element.textContent || "")
                .filter(text => relevant.test(text));
            return {
                html: root?.outerHTML || "",
                styles: styles.slice(0, 500000),
                scripts: scripts.slice(0, 500000),
                json: json.slice(0, 100),
            };
        }"""
    )
    source_dir = folder / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for key, name in (("html", "page.html"), ("styles", "styles.css"), ("scripts", "script.js")):
        content = str(source.get(key) or "")
        if not content:
            continue
        path = source_dir / name
        path.write_text(content, encoding="utf-8")
        files[key] = f"source/{name}"
    json_values = source.get("json") or []
    if json_values:
        path = source_dir / "api.json"
        path.write_text(
            json.dumps(json_values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        files["json"] = "source/api.json"
    if not files:
        source_dir.rmdir()
    return files


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


def download_asset(
    context,
    page,
    src: str,
    folder: Path,
    index: int,
    tag: str,
    *,
    referer: str = SITE,
) -> tuple[str, str]:
    if src.startswith("data:"):
        content_type = write_data_uri(
            src,
            folder / f"layer_{index:02d}_data.tmp",
        )
        ext = ext_for("", content_type, tag)
        filename = f"layer_{index:02d}_data{ext}"
        (folder / f"layer_{index:02d}_data.tmp").replace(folder / filename)
        return filename, content_type

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
        headers={"Referer": referer, "User-Agent": USER_AGENT},
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

                    const media = target.matches?.("img,video,svg,canvas")
                        ? target
                        : target.querySelector?.("img,video,svg,canvas");

                    if (!media) return null;

                    const tcs = getComputedStyle(target);
                    const mcs = getComputedStyle(media);
                    const lcs = getComputedStyle(layer);
                    const matrix = matrixOf(target);
                    const animationStyle = source => ({
                        name: source.animationName,
                        duration: source.animationDuration,
                        delay: source.animationDelay,
                        iterationCount: source.animationIterationCount,
                        timingFunction: source.animationTimingFunction,
                        direction: source.animationDirection,
                        fillMode: source.animationFillMode,
                        playState: source.animationPlayState,
                        transitionProperty: source.transitionProperty,
                        transitionDuration: source.transitionDuration,
                        transitionTimingFunction: source.transitionTimingFunction,
                    });
                    const targetAnimation = animationStyle(tcs);
                    const mediaAnimation = animationStyle(mcs);
                    const animation = targetAnimation.name && targetAnimation.name !== "none"
                        ? targetAnimation : mediaAnimation;
                    const inlineContent = media.tagName.toLowerCase() === "svg"
                        ? `data:image/svg+xml,${encodeURIComponent(media.outerHTML)}` : "";

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
                        src: media.currentSrc || media.src || media.getAttribute("href")
                            || media.getAttribute("data-src") || inlineContent,
                        inlineContent,
                        width,
                        height,
                        naturalWidth: media.naturalWidth || media.videoWidth || 0,
                        naturalHeight: media.naturalHeight || media.videoHeight || 0,
                        objectFit: mcs.objectFit,
                        objectPosition: mcs.objectPosition,
                        transformOrigin: tcs.transformOrigin,
                        transform: matrix,
                        transformX: matrix[4],
                        transformY: matrix[5],
                        layerOpacity: Number.parseFloat(lcs.opacity || "1"),
                        mediaOpacity: Number.parseFloat(mcs.opacity || "1"),
                        zIndex: Number.parseInt(lcs.zIndex || "0", 10) || 0,
                        position: {
                            left: tcs.left,
                            top: tcs.top,
                            right: tcs.right,
                            bottom: tcs.bottom,
                        },
                        animation,
                        animationTarget: targetAnimation.name && targetAnimation.name !== "none"
                            ? "target" : "media",
                        targetTag: target.tagName.toLowerCase(),
                        inlineTransform: target.style?.transform || ""
                    };
                })
                .filter(Boolean);
        }"""
    )


def get_banner_geometry(page) -> dict[str, float] | None:
    return page.evaluate(
        r"""() => {
            const selectors = [
                ".animated-banner",
                ".bili-header__banner",
                ".head-banner",
                ".header-banner",
                ".bili-banner",
                "#banner_link",
                ".banner_link",
                ".banner-link"
            ];
            const el = selectors
                .map(selector => document.querySelector(selector))
                .find(candidate => {
                    if (!candidate) return false;
                    const r = candidate.getBoundingClientRect();
                    return r.width > 100 && (
                        r.height > 40 || candidate.matches(".bili-banner")
                    );
                });
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x:r.x, y:r.y, width:r.width, height:r.height};
        }"""
    )


class InteractionProbeError(RuntimeError):
    def __init__(self, reason: str, message: str, *, before=None, after=None):
        super().__init__(message)
        self.reason = reason
        self.before = before
        self.after = after


def _rounded(value: float) -> float:
    return round(float(value), 8)


def _outside_point(geometry: dict[str, float]) -> tuple[float, float]:
    x = geometry["x"] + geometry["width"] * 0.5
    below = geometry["y"] + geometry["height"] + 20
    if below < VIEWPORT_HEIGHT - 2:
        return x, below
    return x, max(1.0, geometry["y"] - 20)


def _assert_same_layers(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> None:
    if len(reference) != len(candidate):
        raise InteractionProbeError(
            "layer-count-changed-during-motion-probe",
            "Layer count changed while probing interaction.",
            before=reference,
            after=candidate,
        )

    for expected, actual in zip(reference, candidate):
        expected_key = (expected.get("index"), expected.get("tag"), expected.get("src"))
        actual_key = (actual.get("index"), actual.get("tag"), actual.get("src"))
        if expected_key != actual_key:
            raise InteractionProbeError(
                "layer-identity-changed-during-motion-probe",
                "Layer identity changed while probing interaction.",
                before=reference,
                after=candidate,
            )


def _layer_effect_delta(
    baseline: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    baseline_matrix = [float(x) for x in baseline["transform"]]
    state_matrix = [float(x) for x in state["transform"]]
    matrix_delta = [_rounded(state_matrix[i] - baseline_matrix[i]) for i in range(6)]
    return {
        "matrix": matrix_delta,
        "layerOpacity": _rounded(
            float(state.get("layerOpacity", 1)) - float(baseline.get("layerOpacity", 1))
        ),
        "mediaOpacity": _rounded(
            float(state.get("mediaOpacity", 1)) - float(baseline.get("mediaOpacity", 1))
        ),
    }


def _effect_vector(effect: dict[str, Any]) -> list[float]:
    return [
        *[float(x) for x in effect["matrix"][:6]],
        float(effect["layerOpacity"]),
        float(effect["mediaOpacity"]),
    ]


def _remaining_ratio(start: dict[str, Any], current: dict[str, Any]) -> float:
    start_vector = _effect_vector(start)
    current_vector = _effect_vector(current)
    denominator = sum(value * value for value in start_vector)
    if denominator <= MOTION_EPSILON:
        return 0.0
    return _rounded(
        sum(a * b for a, b in zip(start_vector, current_vector)) / denominator
    )


def _legacy_slope(inputs: list[float], outputs: list[float]) -> float:
    denominator = sum(value * value for value in inputs)
    if denominator <= MOTION_EPSILON:
        return 0.0
    return _rounded(sum(x * y for x, y in zip(inputs, outputs)) / denominator)


def _reset_and_probe(
    page,
    geometry: dict[str, float],
    delta_x: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    left = geometry["x"] + 2
    right = geometry["x"] + geometry["width"] - 2
    anchor_x = left if delta_x >= 0 else right
    target_x = anchor_x + delta_x
    y_inside = geometry["y"] + min(
        max(20, geometry["height"] * 0.5),
        max(20, geometry["height"] - 3),
    )
    outside_x, outside_y = _outside_point(geometry)

    page.mouse.move(outside_x, outside_y)
    page.wait_for_timeout(MOTION_RESET_MS)
    page.mouse.move(anchor_x, y_inside)
    page.wait_for_timeout(MOTION_ENTER_SETTLE_MS)
    baseline = read_layers(page)
    page.mouse.move(target_x, y_inside, steps=1)
    page.wait_for_timeout(MOTION_SETTLE_MS)
    moved = read_layers(page)
    _assert_same_layers(baseline, moved)
    return baseline, moved


def sample_interaction(page, geometry: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    max_delta = min(MOTION_PROBE_PX, max(0.0, geometry["width"] - 4))
    input_samples = [_rounded(max_delta * fraction) for fraction in MOTION_SAMPLE_FRACTIONS]
    outside_x, outside_y = _outside_point(geometry)
    center_x = geometry["x"] + geometry["width"] * 0.5
    y_inside = geometry["y"] + min(
        max(20, geometry["height"] * 0.5),
        max(20, geometry["height"] - 3),
    )

    page.mouse.move(outside_x, outside_y)
    page.wait_for_timeout(MOTION_RESET_MS)
    page.mouse.move(center_x, y_inside)
    page.wait_for_timeout(MOTION_ENTER_SETTLE_MS)
    initial_layers = read_layers(page)

    if not initial_layers:
        return [], {"model": "none", "positionAxis": "observed", "effects": []}, []

    motions: list[dict[str, Any]] = [
        {
            "matrixDelta": [],
            "layerOpacityDelta": [],
            "mediaOpacityDelta": [],
            "returnRemaining": [],
        }
        for _ in initial_layers
    ]
    sampled_effects: list[list[dict[str, Any]]] = []

    for delta_x in input_samples:
        if abs(delta_x) <= MOTION_EPSILON:
            effects = [
                {"matrix": [0.0] * 6, "layerOpacity": 0.0, "mediaOpacity": 0.0}
                for _ in initial_layers
            ]
        else:
            baseline, moved = _reset_and_probe(page, geometry, delta_x)
            _assert_same_layers(initial_layers, baseline)
            effects = [
                _layer_effect_delta(before, after)
                for before, after in zip(baseline, moved)
            ]

        sampled_effects.append(effects)
        for motion, effect in zip(motions, effects):
            motion["matrixDelta"].append(effect["matrix"])
            motion["layerOpacityDelta"].append(effect["layerOpacity"])
            motion["mediaOpacityDelta"].append(effect["mediaOpacity"])

    strongest_sample = max(
        range(len(input_samples)),
        key=lambda sample_index: sum(
            sum(abs(value) for value in _effect_vector(layer_effect))
            for layer_effect in sampled_effects[sample_index]
        ),
    )
    strongest_delta = input_samples[strongest_sample]
    baseline, moved = _reset_and_probe(page, geometry, strongest_delta)
    _assert_same_layers(initial_layers, baseline)
    start_effects = [
        _layer_effect_delta(before, after)
        for before, after in zip(baseline, moved)
    ]

    return_times = [0]
    return_values: list[list[float]] = [[1.0] for _ in initial_layers]
    page.mouse.move(outside_x, outside_y)

    previous_time = 0
    settled_at: int | None = None
    for elapsed_ms in RETURN_SAMPLE_TIMES_MS[1:]:
        page.wait_for_timeout(elapsed_ms - previous_time)
        previous_time = elapsed_ms
        state = read_layers(page)
        _assert_same_layers(baseline, state)
        current_effects = [
            _layer_effect_delta(before, after)
            for before, after in zip(baseline, state)
        ]
        ratios = [
            _remaining_ratio(start, current)
            for start, current in zip(start_effects, current_effects)
        ]
        return_times.append(elapsed_ms)
        for values, ratio in zip(return_values, ratios):
            values.append(ratio)

        moving_ratios = [
            abs(ratio)
            for start, ratio in zip(start_effects, ratios)
            if sum(abs(value) for value in _effect_vector(start)) > MOTION_EPSILON
        ]
        if moving_ratios and max(moving_ratios) <= RETURN_SETTLED_RATIO:
            settled_at = len(return_times)
            break

    if settled_at is not None:
        return_times = return_times[:settled_at]
        return_values = [values[:settled_at] for values in return_values]

    for motion, remaining in zip(motions, return_values):
        motion["returnRemaining"] = remaining

    has_matrix = any(
        abs(value) > MOTION_EPSILON
        for motion in motions
        for sample in motion["matrixDelta"]
        for value in sample[:4]
    )
    has_translate_x = any(
        abs(sample[4]) > MOTION_EPSILON
        for motion in motions
        for sample in motion["matrixDelta"]
    )
    has_translate_y = any(
        abs(sample[5]) > MOTION_EPSILON
        for motion in motions
        for sample in motion["matrixDelta"]
    )
    has_opacity = any(
        abs(value) > MOTION_EPSILON
        for motion in motions
        for key in ("layerOpacityDelta", "mediaOpacityDelta")
        for value in motion[key]
    )
    effects = []
    if has_matrix:
        effects.append("matrix")
    if has_translate_x:
        effects.append("translateX")
    if has_translate_y:
        effects.append("translateY")
    if has_opacity:
        effects.append("opacity")

    interaction = {
        "model": "bilibili-sampled-horizontal-v1",
        "positionAxis": "observed",
        "inputMode": "relative-from-pointer-enter",
        "inputSamplesPx": input_samples,
        "effects": effects,
        "returnSamplesMs": return_times,
        "returnDurationMs": return_times[-1] if return_times else 0,
        "samplingViewportWidth": VIEWPORT_WIDTH,
    }
    return initial_layers, interaction, motions


def capture_static(
    page,
    context,
    folder: Path,
    *,
    referer: str = SITE,
    url_rewriter: Callable[[str], str] | None = None,
    url_candidates: Callable[[str], list[str]] | None = None,
    structured: bool = False,
) -> dict[str, Any] | None:
    info = page.evaluate(
        r"""(structured) => {
            const animated = document.querySelector(".animated-banner");
            const hasLayers = Boolean(animated?.querySelector(".layer"));
            const hasStructure = Boolean(structured) || hasLayers;
            if (structured && !hasLayers) return null;
            const usable = element => Boolean(element)
                && !(hasLayers && element.closest(".animated-banner .layer"));
            const root = [
                ".animated-banner", ".bili-header__banner", ".head-banner",
                ".header-banner", ".bili-banner", "#banner_link",
                ".banner_link", ".banner-link"
            ].map(selector => document.querySelector(selector)).find(element =>
                usable(element) && !(hasStructure && (
                    element.matches(".animated-banner") || element.querySelector(".layer")
                ))
            );
            const video = usable(root?.matches?.("video") ? root : root?.querySelector?.("video"))
                ? (root?.matches?.("video") ? root : root?.querySelector?.("video"))
                : null;
            if (video) {
                const cs = getComputedStyle(video);
                const sources = [...video.querySelectorAll("source")]
                    .map(source => source.src || source.getAttribute("data-src") || "")
                    .filter(Boolean);
                return {
                    src: video.currentSrc || video.src || sources[0] || "",
                    sources,
                    poster: video.poster || "",
                    naturalWidth: video.videoWidth || 0,
                    naturalHeight: video.videoHeight || 0,
                    objectFit: cs.objectFit,
                    objectPosition: cs.objectPosition,
                    animation: {
                        name: cs.animationName,
                        duration: cs.animationDuration,
                        delay: cs.animationDelay,
                        iterationCount: cs.animationIterationCount,
                        timingFunction: cs.animationTimingFunction,
                        direction: cs.animationDirection,
                        fillMode: cs.animationFillMode,
                        playState: cs.animationPlayState,
                    },
                    sourceKind: "video",
                    tag: "video"
                };
            }
            const svg = usable(root?.matches?.("svg") ? root : root?.querySelector?.("svg"))
                ? (root?.matches?.("svg") ? root : root?.querySelector?.("svg"))
                : null;
            if (svg) {
                const cs = getComputedStyle(svg);
                return {
                    src: `data:image/svg+xml,${encodeURIComponent(svg.outerHTML)}`,
                    naturalWidth: svg.viewBox?.baseVal?.width || svg.clientWidth || 0,
                    naturalHeight: svg.viewBox?.baseVal?.height || svg.clientHeight || 0,
                    objectFit: cs.objectFit,
                    objectPosition: cs.objectPosition,
                    animation: {
                        name: cs.animationName,
                        duration: cs.animationDuration,
                        delay: cs.animationDelay,
                        iterationCount: cs.animationIterationCount,
                        timingFunction: cs.animationTimingFunction,
                        direction: cs.animationDirection,
                        fillMode: cs.animationFillMode,
                        playState: cs.animationPlayState,
                    },
                    sourceKind: "inline-svg",
                    tag: "svg"
                };
            }
            const nestedImg = root?.matches?.("img") ? root : root?.querySelector?.("img");
            const img = (usable(nestedImg) ? nestedImg : null)
                || [
                    "picture.banner-img img",
                    ".bili-header__banner picture img",
                    ".bili-header__banner > img",
                    ".head-banner img",
                    ".header-banner img",
                    "#banner_link img",
                    ".banner_link img",
                    ".banner-link img"
                ].map(selector => document.querySelector(selector)).find(usable);
            if (img) {
                const cs = getComputedStyle(img);
                return {
                    src: img.currentSrc || img.src || "",
                    srcset: img.srcset || "",
                    naturalWidth: img.naturalWidth || 0,
                    naturalHeight: img.naturalHeight || 0,
                    objectFit: cs.objectFit,
                    objectPosition: cs.objectPosition,
                    animation: {
                        name: cs.animationName,
                        duration: cs.animationDuration,
                        delay: cs.animationDelay,
                        iterationCount: cs.animationIterationCount,
                        timingFunction: cs.animationTimingFunction,
                        direction: cs.animationDirection,
                        fillMode: cs.animationFillMode,
                        playState: cs.animationPlayState,
                    },
                    sourceKind: "img",
                    tag: "img"
                };
            }

            const selectors = [
                ".bili-header__banner",
                ".head-banner",
                ".header-banner",
                ".bili-banner",
                "#banner_link",
                ".banner_link",
                ".banner-link"
            ];
            const urlFrom = value => {
                const match = /url\(["']?(.+?)["']?\)/.exec(value || "");
                return match ? match[1] : "";
            };
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el) continue;
                if (hasStructure && (
                    el.matches(".animated-banner") || el.querySelector(".layer")
                )) continue;
                const cs = getComputedStyle(el);
                const src = urlFrom(cs.backgroundImage)
                    || urlFrom(getComputedStyle(el, "::before").backgroundImage)
                    || urlFrom(getComputedStyle(el, "::after").backgroundImage);
                if (!src) continue;
                return {
                    src: new URL(src, document.baseURI).href,
                    naturalWidth: 0,
                    naturalHeight: 0,
                    objectFit: cs.backgroundSize === "contain" ? "contain" : "cover",
                    objectPosition: cs.backgroundPosition || "50% 50%",
                    animation: {
                        name: cs.animationName,
                        duration: cs.animationDuration,
                        delay: cs.animationDelay,
                        iterationCount: cs.animationIterationCount,
                        timingFunction: cs.animationTimingFunction,
                        direction: cs.animationDirection,
                        fillMode: cs.animationFillMode,
                        playState: cs.animationPlayState,
                    },
                    sourceKind: "background-image",
                    tag: "img"
                };
            }
            return null;
        }""",
        structured,
    )

    if not info or not info.get("src"):
        return None

    original_src = str(info["src"])
    candidates = url_candidates(original_src) if url_candidates else [
        url_rewriter(original_src) if url_rewriter else original_src
    ]
    for candidate in candidates:
        info["src"] = candidate
        try:
            if candidate.startswith("data:"):
                probe = folder / "static_data.tmp"
                content_type = write_data_uri(candidate, probe)
                ext = ext_for("", content_type, str(info.get("tag") or "img"))
                filename = "static" + ext
                probe.replace(folder / filename)
                info["contentType"] = content_type
            elif candidate.startswith("blob:"):
                probe = folder / "static_blob.tmp"
                content_type = save_blob(page, candidate, probe)
                ext = ext_for("", content_type, str(info.get("tag") or "img"))
                filename = "static" + ext
                probe.replace(folder / filename)
                info["contentType"] = content_type
            else:
                response = context.request.get(
                    candidate,
                    headers={"Referer": referer, "User-Agent": USER_AGENT},
                    timeout=30000,
                )
                if not response.ok:
                    raise RuntimeError(f"asset HTTP {response.status}: {candidate}")
                info["contentType"] = response.headers.get("content-type", "")
                ext = ext_for(candidate, info["contentType"], str(info.get("tag") or "img"))
                filename = "static" + ext
                (folder / filename).write_bytes(response.body())
            break
        except Exception:
            for probe_name in ("static_data.tmp", "static_blob.tmp"):
                (folder / probe_name).unlink(missing_ok=True)
    else:
        return None

    info["file"] = filename
    info["assetType"] = media_type(
        str(info.get("tag") or "img"),
        str(info.get("src") or ""),
        info["contentType"],
    )
    return info


def capture_structured_media(
    page,
    context,
    folder: Path,
    evidence: dict[str, Any],
    *,
    referer: str = SITE,
    url_rewriter: Callable[[str], str] | None = None,
    url_candidates: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Save observed DOM media independently when no standard `.layer` exists."""
    layers: list[dict[str, Any]] = []
    missing: list[str] = []
    for media in evidence.get("media") or []:
        tag = str(media.get("tag") or "").lower()
        if tag not in {"img", "video", "svg", "canvas"}:
            continue
        if tag == "canvas":
            missing.append("canvas/WebGL rendering cannot be serialized as an original asset")
            continue

        src = str(media.get("src") or media.get("inlineContent") or "")
        if not src:
            missing.append(f"{tag} media has no recoverable original source")
            continue
        index = len(layers)
        candidates = url_candidates(src) if url_candidates else [
            url_rewriter(src) if url_rewriter else src
        ]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                local_file, content_type = download_asset(
                    context,
                    page,
                    candidate,
                    folder,
                    index,
                    tag,
                    referer=referer,
                )
                src = candidate
                break
            except Exception as exc:
                last_error = exc
        else:
            missing.append(f"structured_media_{index:03d}: {last_error}")
            continue

        css = media.get("css") or {}
        animation = {
            "name": css.get("animationName", "none"),
            "duration": css.get("animationDuration", "0s"),
            "delay": css.get("animationDelay", "0s"),
            "iterationCount": css.get("animationIterationCount", "1"),
            "timingFunction": css.get("animationTimingFunction", "ease"),
            "direction": css.get("animationDirection", "normal"),
            "fillMode": css.get("animationFillMode", "none"),
            "playState": css.get("animationPlayState", "running"),
            "transitionProperty": css.get("transitionProperty", "all"),
            "transitionDuration": css.get("transitionDuration", "0s"),
            "transitionTimingFunction": css.get("transitionTimingFunction", "ease"),
        }
        try:
            opacity = float(css.get("opacity", 1))
        except (TypeError, ValueError):
            opacity = 1.0
        try:
            z_index = int(css.get("zIndex", 0))
        except (TypeError, ValueError):
            z_index = 0
        transform = css.get("transform")
        if not isinstance(transform, list) or len(transform) != 6:
            transform = [1, 0, 0, 1, 0, 0]

        layers.append(
            {
                "index": index,
                "tag": tag,
                "assetType": media_type(
                    tag,
                    src,
                    content_type,
                    animated=animation["name"] not in {None, "", "none"},
                ),
                "src": src,
                "file": local_file,
                "contentType": content_type,
                "width": float(css.get("width") or 0),
                "height": float(css.get("height") or 0),
                "naturalWidth": int(media.get("naturalWidth") or 0),
                "naturalHeight": int(media.get("naturalHeight") or 0),
                "objectFit": css.get("objectFit", "fill"),
                "objectPosition": css.get("objectPosition", "50% 50%"),
                "transformOrigin": css.get("transformOrigin", "50% 50%"),
                "transform": [float(value) for value in transform],
                "opacity": [1, opacity],
                "position": {
                    "type": css.get("position", "static"),
                    "left": css.get("left", "auto"),
                    "top": css.get("top", "auto"),
                    "right": css.get("right", "auto"),
                    "bottom": css.get("bottom", "auto"),
                },
                "zIndex": z_index,
                "animation": animation,
                "animationTarget": "target",
                "motion": None,
                "a": 0,
                "captureTargetTag": tag,
            }
        )

    return layers, sorted(set(missing))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def manifest_types(manifest: dict[str, Any]) -> list[str]:
    values = manifest.get("type")
    if isinstance(values, list) and values:
        return [str(value) for value in values]
    if manifest.get("mode") == "split":
        return ["layered"]
    static = manifest.get("static") or {}
    if str(static.get("tag") or "") == "video":
        return ["video"]
    return ["static"] if static else []


def calculate_manifest_hashes(folder: Path, manifest: dict[str, Any]) -> dict[str, str]:
    resources: list[dict[str, Any]] = []
    layers = manifest.get("layers") or []
    for layer in sorted(layers, key=lambda item: int(item.get("index", 0))):
        file = str(layer.get("file") or "")
        path = folder / file if file else None
        if not path or not path.exists():
            continue
        resources.append(
            {
                "role": "layer",
                "index": int(layer.get("index", 0)),
                "type": layer.get("assetType") or layer.get("tag") or "other",
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )

    static = manifest.get("static") or {}
    static_file = str(static.get("file") or "")
    static_path = folder / static_file if static_file else None
    include_static = "layered" not in manifest_types(manifest)
    if include_static and static_path and static_path.exists():
        resources.append(
            {
                "role": "primary-or-fallback",
                "type": static.get("assetType") or static.get("tag") or "image",
                "sha256": sha256_file(static_path),
                "size": static_path.stat().st_size,
            }
        )

    resource_hash = _json_hash(resources)
    source_files = manifest.get("sourceFiles") or {}
    source_hashes: dict[str, str] = {}
    for role in ("scripts", "json"):
        relative = str(source_files.get(role) or "")
        path = folder / relative if relative else None
        if path and path.is_file():
            source_hashes[role] = sha256_file(path)
    structure = {
        "type": manifest_types(manifest),
        "mode": manifest.get("mode"),
        "banner": manifest.get("banner") or {},
        "layers": [
            {
                key: layer.get(key)
                for key in (
                    "index", "assetType", "tag", "width", "height",
                    "naturalWidth", "naturalHeight", "position", "transform",
                    "transformOrigin", "opacity", "zIndex", "objectFit",
                    "objectPosition", "animation", "animationTarget",
                )
                if key in layer
            }
            for layer in sorted(layers, key=lambda item: int(item.get("index", 0)))
        ],
        "static": {
            key: static.get(key)
            for key in ("tag", "assetType", "sourceKind", "objectFit", "objectPosition")
            if key in static
        },
        "missing_assets": sorted(str(item) for item in manifest.get("missing_assets") or []),
        "animationCssHash": _json_hash(manifest.get("animationCss") or ""),
        "evidence": {
            "layerCount": int((manifest.get("structureEvidence") or {}).get("layerCount") or 0),
            "signals": (manifest.get("structureEvidence") or {}).get("signals") or {},
        },
    }
    structure_hash = _json_hash(structure)
    unknown_interaction = bool(
        ((manifest.get("structureEvidence") or {}).get("signals") or {}).get("hasInteraction")
        and not (manifest.get("interaction") or {}).get("effects")
    )
    interaction = {
        "interaction": manifest.get("interaction") or {},
        "sourceScriptHash": source_hashes.get("scripts", "") if unknown_interaction else "",
        "sourceJsonHash": source_hashes.get("json", "") if unknown_interaction else "",
        "layerMotions": [
            {
                "index": layer.get("index"),
                "motion": layer.get("motion"),
                "animation": layer.get("animation"),
            }
            for layer in sorted(layers, key=lambda item: int(item.get("index", 0)))
        ],
    }
    interaction_hash = _json_hash(interaction)
    content_hash = _json_hash(
        {
            "resources": resource_hash,
            "structure": structure_hash,
            "interaction": interaction_hash,
        }
    )
    return {
        "resourceHash": resource_hash,
        "structureHash": structure_hash,
        "interactionHash": interaction_hash,
        "contentHash": content_hash,
    }


def enrich_manifest_metadata(
    manifest: dict[str, Any],
    evidence: dict[str, Any] | None,
    *,
    missing_assets: list[str] | None = None,
) -> dict[str, Any]:
    evidence = evidence or {}
    signals = evidence.get("signals") or {}
    layers = manifest.get("layers") or []
    static = manifest.get("static") or {}
    types: list[str] = []
    if layers or int(evidence.get("layerCount") or 0) > 0 or signals.get("isSplitLayer"):
        types.append("layered")
    evidence_resources = evidence.get("resourceUrls") or []
    has_video = signals.get("hasVideo") or static.get("tag") == "video" or any(
        layer.get("assetType") == "video" or layer.get("tag") == "video" for layer in layers
    ) or any(
        re.search(r"\.(?:m3u8|mp4|webm)(?:$|[?#])", str(item.get("name") or ""), re.I)
        for item in evidence_resources
    )
    if has_video:
        types.append("video")
    has_animated_media = static.get("assetType") == "animated" or any(
        layer.get("assetType") == "animated" for layer in layers
    ) or any(
        media.get("tag") == "canvas"
        or re.search(r"\.(?:gif|apng)(?:$|[?#])", str(media.get("src") or ""), re.I)
        for media in evidence.get("media") or []
    ) or any(
        re.search(r"\.(?:gif|apng)(?:$|[?#])", str(item.get("name") or ""), re.I)
        for item in evidence_resources
    )
    if signals.get("hasCssAnimation") or signals.get("hasSvgAnimation") or has_animated_media:
        types.append("animated")
    has_interaction = bool(signals.get("hasInteraction")) or bool(
        (manifest.get("interaction") or {}).get("effects")
    )
    if has_interaction:
        types.append("interactive")
    if not types:
        types.append("static")

    missing_values = [str(item) for item in (missing_assets or [])]
    saved_video = static.get("assetType") == "video" and static.get("file") or any(
        layer.get("assetType") == "video" and layer.get("file") for layer in layers
    )
    if has_video and not saved_video:
        missing_values.append("video source detected but no original video asset was saved")
    if signals.get("hasCanvas"):
        missing_values.append("canvas/WebGL source logic unavailable")
    if signals.get("hasCssAnimation") and not evidence.get("animationCss"):
        missing_values.append("CSS animation detected but keyframes were unavailable")
    if has_interaction and not (manifest.get("interaction") or {}).get("effects"):
        missing_values.append("interaction detected but no replay parameters were captured")
    missing = sorted(set(missing_values))
    structure_expected = bool(
        layers
        or int(evidence.get("layerCount") or 0) > 0
        or int(evidence.get("visibleMediaCount") or 0) > 1
        or signals.get("isSplitLayer")
    )
    if missing or (structure_expected and not layers):
        completeness = "partial"
    elif evidence.get("root"):
        completeness = "complete"
    else:
        completeness = "unverified"

    assets: list[dict[str, Any]] = []
    if static.get("file"):
        assets.append(
            {
                "role": "fallback_image" if "layered" in types else "primary",
                "type": static.get("assetType") or static.get("tag") or "image",
                "src": static.get("src", ""),
                "local_file": static["file"],
                "content_type": static.get("contentType", ""),
            }
        )
    for layer in layers:
        assets.append(
            {
                "role": "layer",
                "index": layer.get("index"),
                "type": layer.get("assetType") or layer.get("tag") or "other",
                "src": layer.get("src", ""),
                "local_file": layer.get("file", ""),
                "content_type": layer.get("contentType", ""),
            }
        )

    for media in evidence.get("media") or []:
        src = str(media.get("src") or "")
        if media.get("tag") not in {"video", "source", "svg", "canvas"} and not src:
            continue
        if src and any(str(item.get("src") or "") == src for item in assets):
            continue
        assets.append(
            {
                "role": "evidence",
                "type": media_type(
                    str(media.get("tag") or "other"),
                    src,
                    "",
                ),
                "src": src,
                "local_file": "",
                "content_type": "",
            }
        )

    manifest["type"] = types
    manifest["is_split_layer"] = bool("layered" in types)
    manifest["fallback_image"] = static.get("file") if "layered" in types else None
    manifest["preview_image"] = manifest.get("preview_image")
    manifest["completeness"] = completeness
    manifest["missing_assets"] = missing
    manifest["structureEvidence"] = evidence
    manifest["assets"] = assets
    source = manifest.setdefault("source", {})
    source["discoveredResources"] = evidence.get("resourceUrls") or []
    source["stylesheetUrls"] = evidence.get("stylesheetUrls") or []
    source["scriptUrls"] = evidence.get("scriptUrls") or []
    manifest["animationCss"] = evidence.get("animationCss") or ""
    return manifest


def content_fingerprint(folder: Path, manifest: dict[str, Any]) -> str:
    return calculate_manifest_hashes(folder, manifest)["contentHash"]


def read_manifest(folder: Path) -> dict[str, Any] | None:
    file = folder / "banner.json"
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None


def observed_time_slot(moment: dt.datetime) -> int:
    interval = max(1, min(1440, TIME_SLOT_MINUTES))
    minute = moment.hour * 60 + moment.minute
    return (minute // interval) * interval


def layout_fingerprint(manifest: dict[str, Any]) -> str:
    if manifest.get("mode") == "split" or "layered" in manifest_types(manifest):
        layout = {
            "mode": "split",
            "layers": [
                {
                    "index": int(layer.get("index", 0)),
                    "tag": layer.get("tag"),
                    "naturalWidth": int(layer.get("naturalWidth") or 0),
                    "naturalHeight": int(layer.get("naturalHeight") or 0),
                    "width": round(float(layer.get("width") or 0), 1),
                    "height": round(float(layer.get("height") or 0), 1),
                    "transform": layer.get("transform"),
                    "opacity": layer.get("opacity"),
                    "zIndex": layer.get("zIndex", 0),
                    "animation": layer.get("animation") or {},
                    "animationTarget": layer.get("animationTarget", "media"),
                }
                for layer in sorted(
                    manifest.get("layers", []),
                    key=lambda item: int(item.get("index", 0)),
                )
            ],
        }
    else:
        static = manifest.get("static") or {}
        layout = {
            "mode": "static",
            "naturalWidth": int(static.get("naturalWidth") or 0),
            "naturalHeight": int(static.get("naturalHeight") or 0),
        }

    payload = json.dumps(layout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def iter_archive_manifests() -> list[tuple[Path, dict[str, Any]]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    if not ARCHIVE_DIR.exists():
        return entries

    for folder in ARCHIVE_DIR.iterdir():
        if not folder.is_dir():
            continue
        manifest = read_manifest(folder)
        if manifest:
            entries.append((folder, manifest))
    return entries


def derived_family_id(manifest: dict[str, Any]) -> str:
    date_text = str(manifest.get("date") or "unknown-date")
    layout_hash = str(manifest.get("layoutHash") or layout_fingerprint(manifest))
    content_hash = str(manifest.get("contentHash") or "unknown")
    return f"{date_text}_{layout_hash[:12]}_{content_hash[:8]}"


def observation_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    captured_at = str(manifest.get("capturedAt") or "")
    observed_slots = [int(value) for value in manifest.get("observedSlots", [])]
    if not observed_slots and captured_at:
        try:
            observed_slots = [observed_time_slot(dt.datetime.fromisoformat(captured_at))]
        except Exception:
            observed_slots = []
    return {
        "capturedAt": captured_at,
        "lastObservedAt": str(manifest.get("lastObservedAt") or captured_at),
        "date": str(manifest.get("date") or captured_at[:10]),
        "season": str(manifest.get("season") or ""),
        "timeZone": str(manifest.get("timeZone") or TIMEZONE),
        "observedSlots": sorted(set(observed_slots)),
        "familyId": str(manifest.get("familyId") or derived_family_id(manifest)),
        "source": manifest.get("source") or {},
    }


def manifest_observations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    observations = manifest.get("observations")
    if isinstance(observations, list) and observations:
        return [dict(item) for item in observations if isinstance(item, dict)]
    return [observation_from_manifest(manifest)]


def observation_key(observation: dict[str, Any]) -> tuple[str, str, str]:
    source = observation.get("source") or {}
    source_id = str(
        source.get("waybackTimestamp")
        or source.get("resolvedUrl")
        or source.get("page")
        or ""
    )
    return (
        str(observation.get("capturedAt") or ""),
        str(observation.get("familyId") or ""),
        source_id,
    )


def choose_family_id(manifest: dict[str, Any]) -> str:
    layout_hash = str(manifest.get("layoutHash") or layout_fingerprint(manifest))
    date_text = str(manifest.get("date") or "")
    candidates: list[tuple[str, str]] = []

    for _, existing in iter_archive_manifests():
        existing_layout = str(existing.get("layoutHash") or layout_fingerprint(existing))
        if existing_layout != layout_hash:
            continue
        for observation in manifest_observations(existing):
            if observation.get("date") != date_text:
                continue
            candidate_manifest = {**existing, "date": date_text}
            candidates.append(
                (
                    str(observation.get("capturedAt") or ""),
                    str(
                        observation.get("familyId")
                        or derived_family_id(candidate_manifest)
                    ),
                )
            )

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return derived_family_id(manifest)


def find_archive_by_hash(content_hash: str) -> tuple[Path, dict[str, Any]] | None:
    for folder, manifest in iter_archive_manifests():
        try:
            # Recompute legacy manifests too. Their stored v9/v10 hash did not
            # include structure and interaction configuration.
            manifest_hash = content_fingerprint(folder, manifest)
        except Exception:
            continue
        if manifest_hash == content_hash:
            return folder, manifest
    return None


def merge_duplicate_metadata(
    folder: Path,
    archived: dict[str, Any],
    fresh: dict[str, Any],
    *,
    moment: dt.datetime,
    force: bool,
    record_observation: bool = False,
) -> bool:
    before = json.dumps(archived, ensure_ascii=False, sort_keys=True)
    slot = observed_time_slot(moment)
    slots = sorted({int(value) for value in archived.get("observedSlots", [])} | {slot})
    archived["observedSlots"] = slots
    archived["timeZone"] = str(archived.get("timeZone") or TIMEZONE)
    for field in (
        "type", "is_split_layer", "fallback_image", "preview_image",
        "completeness", "missing_assets", "structureEvidence", "assets", "hashes",
        "sourceFiles", "animationCss",
    ):
        if field in fresh and field not in archived:
            archived[field] = fresh[field]
    archived["layoutHash"] = str(archived.get("layoutHash") or layout_fingerprint(archived))
    archived["familyId"] = str(archived.get("familyId") or derived_family_id(archived))

    archived_observations = manifest_observations(archived)
    fresh_observation = observation_from_manifest(fresh)
    fresh_key = observation_key(fresh_observation)
    matched_observation = False
    for observation in archived_observations:
        same_family_day = (
            observation.get("familyId") == fresh_observation.get("familyId")
            and observation.get("date") == fresh_observation.get("date")
        )
        if observation_key(observation) == fresh_key or (
            not record_observation and same_family_day
        ):
            observation["observedSlots"] = sorted(
                {
                    int(value)
                    for value in observation.get("observedSlots", [])
                }
                | {slot}
            )
            matched_observation = True
            break
    if record_observation and not matched_observation:
        archived_observations.append(fresh_observation)
    archived["observations"] = sorted(
        archived_observations,
        key=lambda item: str(item.get("capturedAt") or ""),
    )

    archived_model = (archived.get("interaction") or {}).get("model")
    fresh_model = (fresh.get("interaction") or {}).get("model")
    should_refresh_effect = force or (
        fresh_model == "bilibili-sampled-horizontal-v1"
        and archived_model != "bilibili-sampled-horizontal-v1"
    )

    if should_refresh_effect and len(archived.get("layers", [])) == len(fresh.get("layers", [])):
        archived["version"] = 10.1
        archived["interaction"] = fresh.get("interaction")
        archived["banner"] = fresh.get("banner")
        archived["viewport"] = fresh.get("viewport")
        layer_fields = (
            "width",
            "height",
            "objectFit",
            "objectPosition",
            "transformOrigin",
            "transform",
            "opacity",
            "motion",
            "a",
            "captureTargetTag",
        )
        for old_layer, new_layer in zip(archived["layers"], fresh["layers"]):
            for field in layer_fields:
                if field in new_layer:
                    old_layer[field] = new_layer[field]
        archived["interactionUpdatedAt"] = moment.isoformat(timespec="seconds")

    after = json.dumps(archived, ensure_ascii=False, sort_keys=True)
    if before == after:
        return False

    archived["lastObservedAt"] = max(
        str(archived.get("lastObservedAt") or ""),
        moment.isoformat(timespec="seconds"),
    )
    (folder / "banner.json").write_text(
        json.dumps(archived, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    current = read_manifest(CURRENT_DIR)
    if current and current.get("contentHash") == archived.get("contentHash"):
        (CURRENT_DIR / "banner.json").write_text(
            json.dumps(archived, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return True


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


def archive_capture(
    temp_dir: Path,
    manifest: dict[str, Any],
    *,
    moment: dt.datetime,
    force: bool = False,
    update_current: bool = True,
    record_observation: bool = False,
) -> dict[str, Any]:
    hashes = calculate_manifest_hashes(temp_dir, manifest)
    content_hash = hashes["contentHash"]
    manifest["version"] = 10.1
    manifest["contentHash"] = content_hash
    manifest["hashes"] = hashes
    for asset in manifest.get("assets") or []:
        file = str(asset.get("local_file") or "")
        path = temp_dir / file if file else None
        if path and path.exists():
            asset["sha256"] = sha256_file(path)
    manifest["layoutHash"] = layout_fingerprint(manifest)
    manifest["familyId"] = choose_family_id(manifest)
    manifest["observedSlots"] = [observed_time_slot(moment)]
    manifest["timeZone"] = str(manifest.get("timeZone") or TIMEZONE)
    manifest["lastObservedAt"] = str(
        manifest.get("lastObservedAt") or manifest["capturedAt"]
    )
    manifest["observations"] = [observation_from_manifest(manifest)]

    (temp_dir / "banner.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    existing = find_archive_by_hash(content_hash)
    if existing:
        archive_dir, archived_manifest = existing
        if not archived_manifest.get("sourceFiles"):
            for relative in (manifest.get("sourceFiles") or {}).values():
                source_path = temp_dir / str(relative)
                target_path = archive_dir / str(relative)
                if source_path.is_file() and not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target_path)
        changed = merge_duplicate_metadata(
            archive_dir,
            archived_manifest,
            manifest,
            moment=moment,
            force=force,
            record_observation=record_observation,
        )
        if changed:
            rebuild_index()
        return {
            "status": "updated" if changed else "unchanged",
            "contentHash": content_hash,
            "archive": archive_dir,
        }

    if update_current:
        replace_current(temp_dir)

    archive_name = f"{moment.strftime('%Y-%m-%d_%H%M%S')}_{content_hash}"
    archive_dir = ARCHIVE_DIR / archive_name
    shutil.copytree(temp_dir, archive_dir)
    rebuild_index()
    return {
        "status": "created",
        "contentHash": content_hash,
        "archive": archive_dir,
    }


def rebuild_index() -> None:
    families: dict[str, dict[str, Any]] = {}

    for folder, item in iter_archive_manifests():
        content_hash = item.get("contentHash")
        if not content_hash:
            try:
                content_hash = content_fingerprint(folder, item)
            except Exception:
                content_hash = folder.name

        for observation in manifest_observations(item):
            observed_slots = [
                int(value) for value in observation.get("observedSlots", [])
            ]
            captured_at = str(observation.get("capturedAt") or item["capturedAt"])
            family_id = str(
                observation.get("familyId") or item.get("familyId")
                or derived_family_id(item)
            )
            variant = {
                "contentHash": content_hash,
                "capturedAt": captured_at,
                "lastObservedAt": str(
                    observation.get("lastObservedAt") or captured_at
                ),
                "mode": item["mode"],
                "type": manifest_types(item),
                "layerCount": len(item.get("layers", [])),
                "completeness": item.get("completeness", "unverified"),
                "missing_assets": list(item.get("missing_assets") or []),
                "observedSlots": sorted(set(observed_slots)),
                "manifest": f"./data/archive/{folder.name}/banner.json",
            }

            family = families.setdefault(
                family_id,
                {
                    "id": family_id,
                    "date": str(observation.get("date") or item["date"]),
                    "season": str(observation.get("season") or item["season"]),
                    "timeZone": str(
                        observation.get("timeZone")
                        or item.get("timeZone")
                        or TIMEZONE
                    ),
                    "variantsByHash": {},
                },
            )
            existing_variant = family["variantsByHash"].get(content_hash)
            if existing_variant:
                merged_slots = sorted(
                    set(existing_variant["observedSlots"])
                    | set(variant["observedSlots"])
                )
                if variant["lastObservedAt"] > existing_variant["lastObservedAt"]:
                    existing_variant.update(variant)
                existing_variant["observedSlots"] = merged_slots
            else:
                family["variantsByHash"][content_hash] = variant

    records: list[dict[str, Any]] = []
    for family in families.values():
        variants = sorted(
            family.pop("variantsByHash").values(),
            key=lambda item: item["capturedAt"],
        )
        representative = max(variants, key=lambda item: item["lastObservedAt"])
        date_text = family["date"]
        records.append(
            {
                **family,
                "year": date_text[:4],
                "month": date_text[5:7],
                "yearMonth": date_text[:7],
                "capturedAt": max(item["capturedAt"] for item in variants),
                "mode": representative["mode"],
                "type": representative.get("type", ["static"]),
                "layerCount": representative["layerCount"],
                "completeness": representative.get("completeness", "unverified"),
                "missing_assets": representative.get("missing_assets", []),
                "contentHash": representative["contentHash"],
                "manifest": representative["manifest"],
                "variantCount": len(variants),
                "variants": variants,
            }
        )

    records.sort(key=lambda item: item["capturedAt"], reverse=True)

    payload = {
        "version": 10.1,
        "generatedAt": now_local().isoformat(timespec="seconds"),
        "timeZone": TIMEZONE,
        "timeSlotMinutes": TIME_SLOT_MINUTES,
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
                raise RuntimeError("No Bilibili banner container was found. See data/diagnostic.json")

            temp = Path(tempfile.mkdtemp(prefix=".capture_", dir=DATA_DIR))
            try:
                evidence = inspect_banner_structure(page)
                source_files = save_source_evidence(page, temp)
                detected_layers = read_layers(page)

                layers: list[dict[str, Any]] = []
                structure_expected = bool(
                    detected_layers
                    or int(evidence.get("layerCount") or 0) > 0
                    or int(evidence.get("visibleMediaCount") or 0) > 1
                    or (evidence.get("signals") or {}).get("isSplitLayer")
                )
                mode = "split" if structure_expected else "static"
                static = capture_static(
                    page,
                    context,
                    temp,
                    structured=structure_expected,
                )
                interaction: dict[str, Any] = {
                    "model": "none",
                    "positionAxis": "observed",
                    "effects": [],
                }
                missing_assets: list[str] = []

                if detected_layers:
                    try:
                        initial_layers, interaction, motions = sample_interaction(page, geometry)
                    except InteractionProbeError as exc:
                        save_diagnostic(
                            page,
                            reason=exc.reason,
                            before=exc.before,
                            after=exc.after,
                        )
                        raise RuntimeError(f"{exc} See data/diagnostic.json") from exc

                    for i, (item, motion) in enumerate(zip(initial_layers, motions)):
                        src = item.get("src") or ""
                        if not src:
                            missing_assets.append(
                                f"layer_{i:03d}: original asset URL missing"
                            )
                            continue

                        try:
                            local_file, content_type = download_asset(
                                context, page, src, temp, i, item["tag"]
                            )
                        except Exception as exc:
                            missing_assets.append(f"layer_{i:03d}: {exc}")
                            continue

                        layers.append(
                            {
                                "index": i,
                                "tag": item["tag"],
                                "assetType": media_type(
                                    item["tag"], src, content_type,
                                    animated=bool((item.get("animation") or {}).get("name") not in {None, "", "none"}),
                                ),
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
                                "position": item.get("position", {}),
                                "zIndex": item.get("zIndex", 0),
                                "animation": item.get("animation", {}),
                                "animationTarget": item.get("animationTarget", "media"),
                                "motion": motion,
                                # Retained for v9.2 frontends and old exports.
                                "a": _legacy_slope(
                                    interaction["inputSamplesPx"],
                                    [sample[4] for sample in motion["matrixDelta"]],
                                ),
                                "captureTargetTag": item["targetTag"],
                            }
                        )

                    expected_layer_count = int(evidence.get("layerCount") or len(initial_layers))
                    if len(layers) < expected_layer_count:
                        missing_assets.append(
                            f"{expected_layer_count - len(layers)} layer asset(s)"
                        )
                elif structure_expected:
                    layers, structured_missing = capture_structured_media(
                        page,
                        context,
                        temp,
                        evidence,
                    )
                    missing_assets.extend(structured_missing)
                    missing_assets.append(
                        "interaction behavior unverified without a sampleable .layer model"
                    )

                signals = evidence.get("signals") or {}
                complex_evidence = bool(
                    signals.get("hasVideo")
                    or signals.get("hasCanvas")
                    or signals.get("hasSvg")
                    or any(
                        re.search(r"\.(?:m3u8|mp4|webm|gif|apng|svg)(?:$|[?#])", str(item.get("name") or ""), re.I)
                        for item in evidence.get("resourceUrls") or []
                    )
                )
                if signals.get("hasCanvas"):
                    missing_assets.append("canvas/WebGL rendering cannot be serialized as an original asset")
                if mode == "static" and not static and not complex_evidence:
                    save_diagnostic(page, reason="banner-asset-not-found")
                    raise RuntimeError("No downloadable Bilibili banner asset was found.")

                if structure_expected and not layers and not static:
                    missing_assets.append("all structured Banner assets unavailable")

                manifest: dict[str, Any] = {
                    "version": 10.1,
                    "capturedAt": now.isoformat(timespec="seconds"),
                    "date": date_text,
                    "season": season_of(now.month),
                    "source": {
                        "page": SITE,
                        "resolvedUrl": page.url,
                        "captureMethod": "hidden-rendered-homepage-dom-sampled",
                    },
                    "viewport": VIEWPORT,
                    "banner": geometry,
                    "mode": mode,
                    "static": static,
                    "layers": layers,
                    "interaction": interaction,
                    "sourceFiles": source_files,
                }
                enrich_manifest_metadata(
                    manifest,
                    evidence,
                    missing_assets=missing_assets,
                )

                result = archive_capture(
                    temp,
                    manifest,
                    moment=now,
                    force=force,
                    update_current=True,
                )
                content_hash = result["contentHash"]
                if result["status"] == "updated":
                    print(
                        "Updated existing Banner time/effect metadata: "
                        f"slot={observed_time_slot(now)}, hash={content_hash}"
                    )
                elif result["status"] == "unchanged":
                    print(f"No visual, time-slot, or effect change: {content_hash}")
                else:
                    print(
                        f"Captured new unique banner: mode={mode}, "
                        f"layers={len(layers)}, date={date_text}, hash={content_hash}"
                    )
                    print(f"Archive: {result['archive']}")
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
        help="refresh interaction metadata without duplicating identical assets",
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
