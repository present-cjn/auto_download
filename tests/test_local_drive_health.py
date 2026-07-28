from __future__ import annotations

from pathlib import Path

import pytest

from app import main as app_main


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_main, "LOCAL_SETTINGS_PATH", tmp_path / "local_settings.json")
    monkeypatch.delenv("RCLONE_DRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("RCLONE_DRIVE_CLIENT_SECRET", raising=False)


def test_local_drive_health_reports_missing_rclone(monkeypatch) -> None:
    monkeypatch.setattr(app_main, "drive_download_backend", lambda: "rclone")
    monkeypatch.setattr(app_main, "rclone_bin", lambda: "rclone")
    monkeypatch.setattr(app_main.shutil, "which", lambda _name: None)

    health = app_main.local_drive_health()

    assert health["download_backend"] == "rclone"
    assert health["rclone_installed"] is False
    assert health["remote_configured"] is False
    assert health["remote_accessible"] is False
    assert health["oauth_client_configured"] is False
    assert health["oauth_client_source"] == "rclone_shared"
    assert health["ok"] is False
    assert "未找到 rclone" in health["error"]
    assert "共享 client" in health["warning"]


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


def test_drive_oauth_config_reads_local_settings() -> None:
    app_main.save_local_settings(
        {
            "rclone_drive_client_id": "local-client-id",
            "rclone_drive_client_secret": "local-client-secret",
        }
    )

    config = app_main.drive_oauth_config()

    assert config["configured"] is True
    assert config["source"] == "local_settings"
    assert config["client_id"] == "local-client-id"
    assert config["client_secret"] == "local-client-secret"
    assert config["client_id_masked"] == "local-...t-id"


def test_drive_oauth_config_prefers_environment(monkeypatch) -> None:
    app_main.save_local_settings(
        {
            "rclone_drive_client_id": "local-client-id",
            "rclone_drive_client_secret": "local-client-secret",
        }
    )
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_ID", "env-client-id")
    monkeypatch.setenv("RCLONE_DRIVE_CLIENT_SECRET", "env-client-secret")

    config = app_main.drive_oauth_config()

    assert config["configured"] is True
    assert config["source"] == "environment"
    assert config["client_id"] == "env-client-id"
    assert config["client_secret"] == "env-client-secret"


def test_run_drive_auth_command_updates_oauth_before_reconnect(monkeypatch) -> None:
    calls = []
    app_main.save_local_settings(
        {
            "rclone_drive_client_id": "client-id",
            "rclone_drive_client_secret": "client-secret",
        }
    )

    def fake_run(command, cwd, check):
        calls.append(command)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(app_main.subprocess, "run", fake_run)
    monkeypatch.setattr(
        app_main,
        "local_drive_health",
        lambda: {
            "rclone_bin": "rclone",
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": True,
            "remote_accessible": True,
            "ok": True,
            "error": "",
        },
    )
    app_main.update_drive_auth_state(running=True, command=[], error="", mode="reconnect")

    app_main.run_drive_auth_command("reconnect", ["rclone", "config", "reconnect", "gdrive:"])

    assert calls == [
        [
            "rclone",
            "config",
            "update",
            "gdrive",
            "scope",
            "drive.readonly",
            "config_is_local",
            "true",
            "client_id",
            "client-id",
            "client_secret",
            "client-secret",
        ],
        ["rclone", "config", "reconnect", "gdrive:"],
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


def test_start_drive_auth_resets_empty_token_remote_before_create(monkeypatch) -> None:
    reset_calls = []
    commands = []
    health_checks = [
        {
            "rclone_installed": True,
            "rclone_bin": "rclone",
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": True,
            "remote_accessible": False,
            "ok": False,
            "error": "empty token found - please run \"rclone config reconnect gdrive:\"",
        },
        {
            "rclone_installed": True,
            "rclone_bin": "rclone",
            "rclone_path": "rclone",
            "remote_name": "gdrive",
            "remote_configured": False,
            "remote_accessible": False,
            "ok": False,
            "error": "rclone remote `gdrive` 尚未配置。",
        },
    ]

    class FakeThread:
        def __init__(self, target, args, daemon):
            commands.append(args[1])

        def start(self):
            return None

    def fake_reset(health=None):
        reset_calls.append(health)

    monkeypatch.setattr(app_main, "local_drive_health", lambda timeout_seconds=15: health_checks.pop(0))
    monkeypatch.setattr(app_main, "reset_drive_remote", fake_reset)
    monkeypatch.setattr(app_main.threading, "Thread", FakeThread)
    app_main.update_drive_auth_state(running=False, command=[], error="", mode="")

    state = app_main.start_drive_auth()

    assert len(reset_calls) == 1
    assert state["running"] is True
    assert state["mode"] == "create"
    assert commands == [
        [
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
    ]
