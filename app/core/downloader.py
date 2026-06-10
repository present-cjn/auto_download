from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Empty
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import urlparse
import os

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
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 180


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


@dataclass(frozen=True)
class DriveResource:
    kind: Literal["folder", "file"]
    resource_id: str


class DriveDownloadError(RuntimeError):
    pass


class DriveNetworkError(DriveDownloadError):
    pass


class DriveDownloadTimeout(DriveDownloadError):
    pass


ERROR_LABELS = {
    "network_error": "网络连接失败",
    "invalid_drive_url": "链接格式错误",
    "drive_rate_limited_or_permission": "Drive 限流或权限受限",
    "drive_download_failed": "Drive 下载失败",
    "download_timeout": "下载超时",
    "no_images_found": "未找到图片",
    "interrupted": "任务中断",
    "unknown_error": "未知错误",
}


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def parse_drive_resource(url: str) -> DriveResource:
    parsed = urlparse(url)
    if not is_google_drive_url(url):
        raise ValueError("不是 Google Drive 链接")

    folder_match = re.search(r"/drive/folders/([^/?#]+)", parsed.path)
    if folder_match:
        return DriveResource("folder", folder_match.group(1))

    file_match = re.search(r"/file/d/([^/?#]+)", parsed.path)
    if file_match:
        return DriveResource("file", file_match.group(1))

    raise ValueError("Google Drive 链接中没有找到文件夹或文件 ID")


def extract_drive_folder_id(url: str) -> str:
    resource = parse_drive_resource(url)
    if resource.kind != "folder":
        raise ValueError("Google Drive 链接不是文件夹链接")
    return resource.resource_id


def extract_drive_file_id(url: str) -> str:
    resource = parse_drive_resource(url)
    if resource.kind != "file":
        raise ValueError("Google Drive 链接不是文件链接")
    return resource.resource_id


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


def describe_directory_files(directory: Path, limit: int = 10) -> str:
    if not directory.exists():
        return "directory does not exist"
    files = [str(path.relative_to(directory)) for path in sorted(directory.rglob("*")) if path.is_file()]
    if not files:
        return "no files"
    sample = ", ".join(files[:limit])
    if len(files) > limit:
        sample = f"{sample}, ... ({len(files)} total)"
    return sample


def google_drive_uc_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?id={file_id}"


def format_attempt_error(
    *,
    resource_kind: str,
    resource_id: str,
    attempt_number: int,
    method: str,
    use_cookies: bool,
    exc: Exception,
) -> str:
    message = str(exc) or "<empty>"
    return (
        f"attempt={attempt_number} resource={resource_kind}:{resource_id} "
        f"method={method} use_cookies={use_cookies} "
        f"error={exc.__class__.__name__}: {message}"
    )


def raise_download_attempts_error(errors: list[str], network_failures: int) -> None:
    detail = " | ".join(errors) if errors else "unknown gdown failure"
    if network_failures and network_failures == len(errors):
        raise DriveNetworkError(detail)
    raise DriveDownloadError(detail)


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


def drive_download_timeout_seconds() -> int:
    raw_value = os.getenv("DRIVE_DOWNLOAD_TIMEOUT_SECONDS", "")
    if not raw_value:
        return DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    return max(1, value)


def _run_download_worker(
    download_func: Callable[[str, Path], None], resource_id: str, output_dir: str, queue: Queue
) -> None:
    try:
        download_func(resource_id, Path(output_dir))
    except BaseException as exc:  # noqa: BLE001 - serialize child-process failures.
        queue.put(("error", exc.__class__.__name__, str(exc)))
    else:
        queue.put(("ok", "", ""))


def run_download_with_timeout(
    download_func: Callable[[str, Path], None],
    resource_id: str,
    output_dir: Path,
    timeout_seconds: Optional[int] = None,
) -> None:
    timeout = timeout_seconds or drive_download_timeout_seconds()
    queue: Queue = Queue(maxsize=1)
    process = Process(
        target=_run_download_worker,
        args=(download_func, resource_id, str(output_dir), queue),
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        raise DriveDownloadTimeout(f"Google Drive 下载超过 {timeout} 秒，已终止。")

    try:
        status, exc_type, message = queue.get_nowait()
    except Empty:
        if process.exitcode == 0:
            return
        raise DriveDownloadError(f"Google Drive 下载进程异常退出: {process.exitcode}")
    if status == "ok":
        return
    raise DriveDownloadError(f"{exc_type}: {message}")


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
    if isinstance(exc, DriveDownloadTimeout):
        return DownloadFailure(
            code="download_timeout",
            message="Google Drive 下载超时，可重试；多次失败请手动打开 Drive 下载。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "FileURLRetrievalError" in message
        or "Cannot retrieve the public link" in message
        or "have had many accesses" in message
    ):
        return DownloadFailure(
            code="drive_rate_limited_or_permission",
            message="Google Drive 限流或权限不可公开下载，可稍后重试；多次失败请手动打开 Drive 下载。",
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


def download_drive_resource(url: str, output_dir: Path) -> None:
    resource = parse_drive_resource(url)
    if resource.kind == "folder":
        download_drive_folder_by_id(resource.resource_id, output_dir)
    else:
        download_drive_file_by_id(resource.resource_id, output_dir)


def download_drive_folder_by_id(folder_id: str, output_dir: Path) -> None:
    gdown = import_gdown()
    attempts = [
        {"use_cookies": False, "delay": 0},
        {"use_cookies": False, "delay": 3},
        {"use_cookies": True, "delay": 10},
    ]
    errors: list[str] = []
    network_failures = 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for index, attempt in enumerate(attempts, start=1):
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
                raise DriveDownloadError(
                    "gdown returned None for folder "
                    f"id={folder_id}; files={describe_directory_files(output_dir)}"
                )
            return
        except Exception as exc:  # noqa: BLE001 - classify and retry download errors.
            failure = classify_download_failure(exc)
            if failure.code == "network_error":
                network_failures += 1
            errors.append(
                format_attempt_error(
                    resource_kind="folder",
                    resource_id=folder_id,
                    attempt_number=index,
                    method="folder_id",
                    use_cookies=bool(attempt["use_cookies"]),
                    exc=exc,
                )
            )

    raise_download_attempts_error(errors, network_failures)


def download_drive_file_by_id(file_id: str, output_dir: Path) -> None:
    gdown = import_gdown()
    attempts = [
        {"use_cookies": False, "delay": 0},
        {"use_cookies": False, "delay": 3},
        {"use_cookies": True, "delay": 10},
    ]
    errors: list[str] = []
    network_failures = 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for index, attempt in enumerate(attempts, start=1):
        if attempt["delay"]:
            time.sleep(int(attempt["delay"]))
        use_cookies = bool(attempt["use_cookies"])
        for method in ("file_id", "uc_url"):
            try:
                if method == "file_id":
                    result = gdown.download(
                        id=file_id,
                        output=f"{output_dir}{os.sep}",
                        quiet=False,
                        use_cookies=use_cookies,
                    )
                else:
                    result = gdown.download(
                        url=google_drive_uc_url(file_id),
                        output=f"{output_dir}{os.sep}",
                        quiet=False,
                        use_cookies=use_cookies,
                    )
                if result is None:
                    raise DriveDownloadError(
                        "gdown returned None for file "
                        f"id={file_id} method={method}; "
                        f"files={describe_directory_files(output_dir)}"
                    )
                return
            except Exception as exc:  # noqa: BLE001 - classify and retry download errors.
                failure = classify_download_failure(exc)
                if failure.code == "network_error":
                    network_failures += 1
                errors.append(
                    format_attempt_error(
                        resource_kind="file",
                        resource_id=file_id,
                        attempt_number=index,
                        method=method,
                        use_cookies=use_cookies,
                        exc=exc,
                    )
                )

    raise_download_attempts_error(errors, network_failures)


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
        download_drive_resource(url, temp_dir)
        return copy_images(temp_dir, order_dir)


def cached_drive_folder(url: str, cache_root: Path) -> Path:
    resource = parse_drive_resource(url)
    cache_dir = cache_root / f"{resource.kind}-{resource.resource_id}"
    if any(iter_image_files(cache_dir)):
        return cache_dir

    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if resource.kind == "folder":
        run_download_with_timeout(download_drive_folder_by_id, resource.resource_id, cache_dir)
    else:
        run_download_with_timeout(download_drive_file_by_id, resource.resource_id, cache_dir)
    if not any(iter_image_files(cache_dir)):
        raise DriveDownloadError(
            "Google Drive resource downloaded, but no image files were found; "
            f"resource={resource.kind}:{resource.resource_id}; "
            f"files={describe_directory_files(cache_dir)}"
        )
    return cache_dir
