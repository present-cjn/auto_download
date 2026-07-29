from __future__ import annotations

import json
from pathlib import Path
from typing import Any


LOCAL_SETTINGS_PATH = Path("data/local_settings.json")


def load_local_settings() -> dict[str, Any]:
    try:
        with LOCAL_SETTINGS_PATH.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_local_settings(settings: dict[str, Any]) -> None:
    LOCAL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_SETTINGS_PATH.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
        file.write("\n")


def setting_value(name: str, default: Any) -> Any:
    return load_local_settings().get(name, default)


def env_or_setting(raw_env: str, setting_name: str, default: Any) -> tuple[Any, str]:
    if raw_env.strip():
        return raw_env.strip(), "environment"
    settings = load_local_settings()
    if setting_name in settings:
        return settings[setting_name], "local_settings"
    return default, "default"
