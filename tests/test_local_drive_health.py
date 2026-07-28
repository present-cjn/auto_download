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


def test_rclone_auth_command_includes_custom_oauth_client(monkeypatch) -> None:
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_ID", "client-id")
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_SECRET", "client-secret")

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
        "client_id",
        "client-id",
        "client_secret",
        "client-secret",
    ]


def test_run_drive_auth_command_reconnects_after_empty_token(monkeypatch) -> None:
    calls = []
    health_checks = [
        {
            "rclone_bin": "rclone",
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": True,
            "remote_accessible": False,
            "ok": False,
            "error": "empty token found - please run \"rclone config reconnect gdrive:\"",
        },
        {
            "rclone_bin": "rclone",
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": True,
            "remote_accessible": True,
            "ok": True,
            "error": "",
        },
    ]

    def fake_run(command, cwd, check):
        calls.append(command)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(app_main.subprocess, "run", fake_run)
    monkeypatch.setattr(app_main, "local_drive_health", lambda: health_checks.pop(0))
    app_main.update_drive_auth_state(running=True, command=[], error="", mode="create")

    app_main.run_drive_auth_command(
        "create",
        [
            "rclone",
            "config",
            "create",
            "gdrive",
            "drive",
            "scope",
            "drive.readonly",
        ],
    )

    assert calls == [
        ["rclone", "config", "create", "gdrive", "drive", "scope", "drive.readonly"],
        ["rclone", "config", "reconnect", "gdrive:"],
    ]
    state = app_main.drive_auth_state()
    assert state["running"] is False
    assert state["returncode"] == 0
    assert state["error"] == ""
