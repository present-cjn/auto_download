from __future__ import annotations

import sys
from pathlib import Path

from app.core.paths import app_data_dir, app_runtime_root


def test_app_data_dir_uses_repo_data_in_source_mode(monkeypatch) -> None:
    monkeypatch.delenv("APP_DATA_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)

    repo_root = Path(__file__).resolve().parents[1]

    assert app_runtime_root() == repo_root
    assert app_data_dir() == repo_root / "data"


def test_app_data_dir_moves_outside_dist_bundle(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    exe_path = project_root / "dist" / "AutoDownload" / "AutoDownload.exe"
    monkeypatch.delenv("APP_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))

    assert app_runtime_root() == exe_path.parent
    assert app_data_dir() == project_root / "data"


def test_app_data_dir_can_be_overridden(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "custom-data"
    monkeypatch.setenv("APP_DATA_DIR", str(configured))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dist" / "AutoDownload" / "AutoDownload.exe"))

    assert app_data_dir() == configured

