"""HistoricalResult adapter for Wayback's archived Header API."""

from __future__ import annotations

from typing import Any

from .history import HistoricalResult


PROVIDER_NAME = "wayback-header-api"


class WaybackHeaderApiProvider:
    """Delegate archive I/O to the compatibility importer without writing archives."""

    def resolve(
        self,
        timestamp: str,
        *,
        replay_base: str,
        cdx_api: str,
        max_delta_seconds: int,
    ) -> HistoricalResult:
        from .. import wayback_import

        parsed, replay, metadata = wayback_import.fetch_archived_header_api(
            timestamp,
            replay_base,
            cdx_api=cdx_api,
            max_delta_seconds=max_delta_seconds,
        )
        mode = "split" if parsed.get("is_split_layer") or parsed.get("layers") else "static"
        return HistoricalResult(
            provider=PROVIDER_NAME,
            observed_at=timestamp,
            source_url=replay,
            confidence="high",
            mode=mode,
            raw_metadata=metadata,
            layers=parsed.get("layers") or [],
            static_asset={"src": parsed.get("pic")} if parsed.get("pic") else None,
        )
