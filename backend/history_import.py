"""Compatibility entry point for multi-provider historical Banner imports."""

from __future__ import annotations

try:
    from .wayback_import import main
except ImportError:
    from wayback_import import main


if __name__ == "__main__":
    main()
