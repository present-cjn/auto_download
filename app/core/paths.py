from __future__ import annotations

import os
import sys
from pathlib import Path


def app_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_data_dir() -> Path:
    configured = os.getenv("APP_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    runtime_root = app_runtime_root()
    if getattr(sys, "frozen", False):
        if runtime_root.parent.name.lower() == "dist":
            return runtime_root.parent.parent / "data"
        return runtime_root.parent / "data"

    return runtime_root / "data"

