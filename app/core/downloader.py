from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}


@dataclass(frozen=True)
class CopiedFile:
    file_name: str
    local_path: Path
    file_size: int


@dataclass(frozen=True)
class DownloadFailure:
    code: str
    message: str
    detail: str


class DriveDownloadError(RuntimeError):
    pass


class DriveNetworkError(DriveDownloadError):
    pass


ERROR_LABELS = {
    "network_error": "网络连接失败",
    "invalid_drive_url": "链接格式错误",
    "drive_download_failed": "Drive 下载失败",
    "no_images_found": "未找到图片",
    "unknown_error": "未知错误",
}


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def extract_drive_folder_id(url: str) -> str:
    parsed = urlparse(url)
    if not is_google_drive_url(url):
        raise ValueError("不是 Google Drive 链接")

    match = re.search(r"/drive/folders/([^/?#]+)", parsed.path)
    if match:
        return match.group(1)
    raise ValueError("Google Drive 链接中没有找到文件夹 ID")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "downloaded_image"


def next_available_path(directory: Path, filename: str) -> Path:
    filename = safe_filename(filename)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def iter_image_files(directory: Path) -> Iterable[Path]:
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def import_gdown():
    try:
        import gdown  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: gdown\n"
            "Install it with:\n"
            "  python3 -m pip install -r requirements.txt"
        ) from exc
    return gdown


def classify_download_failure(exc: Exception) -> DownloadFailure:
    message = str(exc)
    network_types = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    if isinstance(exc, network_types) or "SSLEOFError" in message:
        return DownloadFailure(
            code="network_error",
            message="服务器连接 Google Drive 失败，可重试；多次失败请手动打开 Drive 下载。",
            detail=message,
        )
    if isinstance(exc, ValueError):
        return DownloadFailure(
            code="invalid_drive_url",
            message=message or "Google Drive 链接格式不正确。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and "no image files" in message:
        return DownloadFailure(
            code="no_images_found",
            message="Google Drive 文件夹里没有找到可下载图片。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError):
        return DownloadFailure(
            code="drive_download_failed",
            message="Google Drive 下载失败，可重试；多次失败请手动打开 Drive 下载。",
            detail=message,
        )
    return DownloadFailure(
        code="unknown_error",
        message="下载失败，需要检查链接或手动下载。",
        detail=message,
    )


def download_drive_folder(url: str, temp_dir: Path) -> None:
    folder_id = extract_drive_folder_id(url)
    download_drive_folder_by_id(folder_id, temp_dir)


def download_drive_folder_by_id(folder_id: str, output_dir: Path) -> None:
    gdown = import_gdown()
    attempts = [
        {"use_cookies": False, "delay": 0},
        {"use_cookies": False, "delay": 3},
        {"use_cookies": True, "delay": 10},
    ]
    last_error: Optional[Exception] = None

    output_dir.mkdir(parents=True, exist_ok=True)
    for attempt in attempts:
        if attempt["delay"]:
            time.sleep(int(attempt["delay"]))
        try:
            result = gdown.download_folder(
                id=folder_id,
                output=str(output_dir),
                quiet=False,
                use_cookies=bool(attempt["use_cookies"]),
                remaining_ok=True,
            )
            if result is None:
                raise DriveDownloadError("gdown returned no downloaded files")
            return
        except Exception as exc:  # noqa: BLE001 - classify and retry download errors.
            failure = classify_download_failure(exc)
            if failure.code == "network_error":
                last_error = DriveNetworkError(failure.message)
            else:
                last_error = DriveDownloadError(failure.message)

    assert last_error is not None
    raise last_error


def copy_images(download_dir: Path, order_dir: Path) -> list[CopiedFile]:
    order_dir.mkdir(parents=True, exist_ok=True)
    copied: list[CopiedFile] = []
    for image_path in iter_image_files(download_dir):
        target = next_available_path(order_dir, image_path.name)
        shutil.copy2(image_path, target)
        copied.append(
            CopiedFile(
                file_name=target.name,
                local_path=target,
                file_size=target.stat().st_size,
            )
        )
    return copied


def download_design_images(url: str, order_dir: Path) -> list[CopiedFile]:
    if not is_google_drive_url(url):
        raise ValueError("not a Google Drive URL")

    with tempfile.TemporaryDirectory(prefix="order-drive-") as tmp:
        temp_dir = Path(tmp)
        download_drive_folder(url, temp_dir)
        return copy_images(temp_dir, order_dir)


def cached_drive_folder(url: str, cache_root: Path) -> Path:
    folder_id = extract_drive_folder_id(url)
    cache_dir = cache_root / folder_id
    if any(iter_image_files(cache_dir)):
        return cache_dir

    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    download_drive_folder_by_id(folder_id, cache_dir)
    if not any(iter_image_files(cache_dir)):
        raise DriveDownloadError("Google Drive folder downloaded, but no image files were found")
    return cache_dir
