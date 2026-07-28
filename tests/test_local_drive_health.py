from __future__ import annotations

from app import main as app_main


def test_local_drive_health_reports_missing_rclone(monkeypatch) -> None:
    monkeypatch.setattr(app_main, "drive_download_backend", lambda: "rclone")
    monkeypatch.setattr(app_main, "rclone_bin", lambda: "rclone")
    monkeypatch.setattr(app_main.shutil, "which", lambda _name: None)

    health = app_main.local_drive_health()

    assert health["download_backend"] == "rclone"
    assert health["rclone_installed"] is False
    assert health["remote_configured"] is False
    assert health["remote_accessible"] is False
    assert health["ok"] is False
    assert "未找到 rclone" in health["error"]


def test_rclone_auth_command_creates_missing_remote() -> None:
    mode, command = app_main.rclone_auth_command(
        {
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": False,
        }
    )

    assert mode == "create"
    assert command == [
        "rclone",
        "config",
        "create",
        "gdrive",
        "drive",
        "scope",
        "drive.readonly",
        "config_is_local",
        "true",
    ]


def test_rclone_auth_command_reconnects_existing_remote() -> None:
    mode, command = app_main.rclone_auth_command(
        {
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": True,
        }
    )

    assert mode == "reconnect"
    assert command == ["rclone", "config", "reconnect", "gdrive:"]
