"""Read-only application assets in a source checkout or a PyInstaller bundle."""
from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Never use this path for credentials or generated reports."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled).resolve() if bundled else Path(__file__).resolve().parents[1]


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)
