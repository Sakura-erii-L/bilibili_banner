"""Shared result types for historical Banner providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HistoricalResult:
    """Provider output before the archive layer downloads or writes anything."""

    provider: str
    observed_at: str
    source_url: str
    confidence: str = "unverified"
    mode: str = "unresolved"
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    raw_payload: Any = None
    layers: list[dict[str, Any]] = field(default_factory=list)
    static_asset: dict[str, Any] | None = None
    auxiliary_assets: list[dict[str, Any]] = field(default_factory=list)
    missing_assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
