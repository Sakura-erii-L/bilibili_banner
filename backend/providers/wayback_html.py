"""HistoricalResult adapter for Wayback HTML/CSS recovery."""

from __future__ import annotations

from typing import Any, Iterable

from .history import HistoricalResult


PROVIDER_NAME = "wayback-html"


class WaybackHtmlProvider:
    """Normalize HTML/CSS parser output without owning archive writes."""

    def parse(
        self,
        html_text: str,
        *,
        page_url: str,
        css_texts: Iterable[tuple[str, str]] = (),
    ) -> HistoricalResult:
        from .. import wayback_import

        parsed = wayback_import.parse_banner_resources(
            html_text,
            page_url=page_url,
            css_texts=css_texts,
        )
        return HistoricalResult(
            provider=PROVIDER_NAME,
            observed_at="",
            source_url=page_url,
            confidence="high" if parsed.get("mode") == "static" else "medium",
            mode=str(parsed.get("mode") or "unresolved"),
            raw_metadata={
                "selectedRoot": parsed.get("selectedRoot"),
                "roots": parsed.get("roots") or [],
                "evidence": parsed.get("evidence") or {},
            },
            layers=parsed.get("layerGroups") or [],
            static_asset=parsed.get("primaryRecord"),
            auxiliary_assets=parsed.get("auxiliaryRecords") or [],
        )
