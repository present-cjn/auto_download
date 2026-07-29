from __future__ import annotations

import base64
import re
import json
import shutil
import subprocess
import tempfile
import time
import hashlib
import sys
import zipfile
from dataclasses import dataclass
from io import BytesIO
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Empty
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
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
DEFAULT_RCLONE_TRANSFERS = "1"
DEFAULT_RCLONE_CHECKERS = "1"
DEFAULT_RCLONE_DRIVE_PACER_MIN_SLEEP = "500ms"
DEFAULT_RCLONE_DRIVE_PACER_BURST = "5"
DEFAULT_RCLONE_LOCAL_ENCODING = "Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Del,Ctl,InvalidUtf8,Dot"
LOCAL_SETTINGS_PATH = Path("data/local_settings.json")
DEFAULT_PROXY_URL = "http://127.0.0.1:7890"
DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS = 30
DEFAULT_PRINTERVAL_IMAGE_TIMEOUT_BUDGET_SECONDS = 90
DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_IMAGE_FILE_SIZE_MB = 100
DEFAULT_MIN_FREE_DISK_SPACE_MB = 1024
CONTENT_TYPE_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
}
IMAGE_SIGNATURE_EXTENSIONS = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
]
PRINTERVAL_ASSETS_BASE_URL = "https://assets.printerval.com/"
PRINTERVAL_DOWNLOAD_BASE_URL = "https://dl.printerval.com/"
PRINTERVAL_IMAGE_REFERER = "https://printerval.com/"
PRINTERVAL_IMAGE_MAX_ATTEMPTS = 3
DIRECT_IMAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


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


@dataclass(frozen=True)
class PrintervalZipMetadata:
    links: list[str]
    csrf_token: str
    product_id: str


class DriveDownloadError(RuntimeError):
    pass


class DriveNetworkError(DriveDownloadError):
    pass


class DriveDownloadTimeout(DriveDownloadError):
    pass


class RcloneDownloadError(DriveDownloadError):
    pass


class PrintervalCloudflareChallengeError(DriveDownloadError):
    pass


ERROR_LABELS = {
    "network_error": "网络连接失败",
    "invalid_drive_url": "链接格式错误",
    "drive_rate_limited_or_permission": "Drive 限流或权限受限",
    "drive_download_failed": "Drive 下载失败",
    "direct_image_download_failed": "图片直链下载失败",
    "printerval_challenge_required": "Printerval 验证拦截",
    "image_too_large": "图片文件过大",
    "disk_space_low": "磁盘空间不足",
    "download_timeout": "下载超时",
    "no_images_found": "未找到图片",
    "extension_download_failed": "插件下载失败",
    "extension_stopped_by_user": "用户停止插件下载",
    "extension_non_image_download": "下载到非图片",
    "extension_google_apps_file": "链接不是原始图片",
    "drive_not_found_or_permission": "Drive 文件不存在或权限受限",
    "drive_permission_denied": "Drive 权限受限",
    "extension_download_interrupted": "浏览器下载中断",
    "extension_download_timeout": "浏览器下载超时",
    "extension_download_stalled": "浏览器下载无进展",
    "extension_fetch_timeout": "插件请求超时",
    "interrupted": "任务中断",
    "unknown_error": "未知错误",
}


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme.lower() in {"http", "https"}


def is_printerval_design_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host in {"printerval.com", "www.printerval.com"} and "/folder-design" in parsed.path


def parse_printerval_design_image_urls(url: str) -> list[str]:
    if not is_printerval_design_url(url):
        return []

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    raw_values = query.get("design_urls", []) + query.get("design_url", [])
    if not raw_values:
        return []

    image_urls: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for part in raw_value.split(","):
            candidate = unquote(part).strip()
            if not candidate:
                continue
            if candidate.startswith("//"):
                candidate = f"https:{candidate}"
            elif candidate.startswith("/"):
                candidate = urljoin(PRINTERVAL_ASSETS_BASE_URL, candidate.lstrip("/"))
            elif not is_http_url(candidate):
                candidate = urljoin(PRINTERVAL_ASSETS_BASE_URL, candidate)
            if not is_http_url(candidate) or candidate in seen:
                continue
            seen.add(candidate)
            image_urls.append(candidate)
    return image_urls


def printerval_download_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.path.startswith("/psd-renderer/") and host in {"assets.printerval.com", "dl.printerval.com"}:
        return urljoin(PRINTERVAL_DOWNLOAD_BASE_URL, parsed.path.lstrip("/"))
    return url


def parse_printerval_js_string_array(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and is_http_url(item)]


def parse_printerval_zip_metadata(html: str) -> Optional[PrintervalZipMetadata]:
    links_match = re.search(r"\bconst\s+links\s*=\s*(\[[^\]]*\])\s*;", html)
    csrf_match = re.search(r"['\"]X-CSRF-TOKEN['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
    if not links_match or not csrf_match:
        return None

    links: list[str] = []
    seen: set[str] = set()
    for link in parse_printerval_js_string_array(links_match.group(1)):
        if link in seen:
            continue
        seen.add(link)
        links.append(link)
    if not links:
        return None

    product_id = ""
    product_match = re.search(r"\bconst\s+productId\s*=\s*['\"]?([^;'\"]+)['\"]?\s*;", html)
    if product_match:
        product_id = product_match.group(1).strip()
    return PrintervalZipMetadata(links=links, csrf_token=csrf_match.group(1), product_id=product_id)


def printerval_zip_endpoint_url(page_url: str) -> str:
    return urljoin(page_url, "/service/pod/download-images-from-links")


def parse_drive_resource(url: str) -> DriveResource:
    parsed = urlparse(url)
    if not is_google_drive_url(url):
        raise ValueError("不是 Google Drive 链接")

    folder_match = re.search(r"/drive/(?:u/\d+/)?folders/([^/?#]+)", parsed.path)
    if folder_match:
        return DriveResource("folder", folder_match.group(1))

    file_match = re.search(r"/file/d/([^/?#]+)", parsed.path)
    if file_match:
        return DriveResource("file", file_match.group(1))

    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if query_id and parsed.path in {"/open", "/uc"}:
        return DriveResource("file", query_id)

    raise ValueError("Google Drive 链接中没有找到文件夹或文件 ID")


def parse_drive_resource_for_download(url: str) -> DriveResource:
    parsed = urlparse(url)
    resource = parse_drive_resource(url)
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if (
        drive_download_backend() == "rclone"
        and resource.kind == "file"
        and query_id
        and parsed.path == "/open"
    ):
        return resolve_rclone_drive_resource(query_id)
    return resource


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


def direct_url_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def content_type_without_parameters(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def image_extension_for_content_type(content_type: str) -> str:
    return CONTENT_TYPE_IMAGE_EXTENSIONS.get(content_type_without_parameters(content_type), "")


def image_extension_for_file(path: Path) -> str:
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return ""
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    for signature, extension in IMAGE_SIGNATURE_EXTENSIONS:
        if header.startswith(signature):
            return extension
    return ""


def image_filename_with_extension(filename: str, extension: str) -> str:
    cleaned = safe_filename(filename)
    if Path(cleaned).suffix.lower() in IMAGE_EXTENSIONS or not extension:
        return cleaned
    return f"{cleaned}{extension}"


def filename_from_content_disposition(value: str) -> str:
    if not value:
        return ""
    filename_star = re.search(r"filename\*=([^']*)''([^;]+)", value, flags=re.IGNORECASE)
    if filename_star:
        return unquote(filename_star.group(2).strip().strip('"'))
    filename = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if filename:
        return unquote(filename.group(1).strip())
    return ""


def direct_url_filename(url: str, content_type: str = "", content_disposition: str = "") -> str:
    parsed = urlparse(url)
    name = filename_from_content_disposition(content_disposition)
    if not name:
        name = unquote(Path(parsed.path).name)
    name = safe_filename(name or "downloaded-image")
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        extension = image_extension_for_content_type(content_type)
        if extension:
            name = f"{name}{extension}"
    return name


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


def normalize_image_file_extensions(directory: Path) -> None:
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() in IMAGE_EXTENSIONS:
            continue
        extension = image_extension_for_file(path)
        if not extension:
            continue
        target = next_available_path(path.parent, image_filename_with_extension(path.name, extension))
        path.rename(target)


def image_file_count(directory: Path) -> int:
    return sum(1 for _ in iter_image_files(directory))


def max_image_file_size_mb() -> int:
    raw_value = os.getenv("MAX_IMAGE_FILE_SIZE_MB", "").strip()
    if not raw_value:
        return DEFAULT_MAX_IMAGE_FILE_SIZE_MB
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MAX_IMAGE_FILE_SIZE_MB
    return max(1, value)


def max_image_file_size_bytes() -> int:
    return max_image_file_size_mb() * 1024 * 1024


def validate_image_file_size(path: Path) -> None:
    limit_bytes = max_image_file_size_bytes()
    file_size = path.stat().st_size
    if file_size > limit_bytes:
        raise DriveDownloadError(
            "Image file too large; "
            f"file={path.name}; size={file_size}; "
            f"limit={limit_bytes}; limit_mb={max_image_file_size_mb()}"
        )


def validate_image_file_sizes(directory: Path) -> None:
    for image_path in iter_image_files(directory):
        validate_image_file_size(image_path)


def min_free_disk_space_mb() -> int:
    raw_value = os.getenv("MIN_FREE_DISK_SPACE_MB", "").strip()
    if not raw_value:
        return DEFAULT_MIN_FREE_DISK_SPACE_MB
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MIN_FREE_DISK_SPACE_MB
    return max(0, value)


def ensure_min_free_disk_space(path: Path) -> None:
    required_mb = min_free_disk_space_mb()
    if required_mb <= 0:
        return
    path.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(path).free
    required_bytes = required_mb * 1024 * 1024
    if free_bytes < required_bytes:
        raise DriveDownloadError(
            "Insufficient disk space for image download; "
            f"path={path}; free={free_bytes}; required={required_bytes}; required_mb={required_mb}"
        )


def cache_manifest_path(cache_dir: Path) -> Path:
    return cache_dir / ".download-manifest.json"


def write_cache_manifest(
    cache_dir: Path,
    source_url: str,
    resource_label: str,
    expected_images: Optional[int],
) -> None:
    files = [
        {
            "name": path.name,
            "relative_path": str(path.relative_to(cache_dir)),
            "size": path.stat().st_size,
        }
        for path in sorted(iter_image_files(cache_dir))
    ]
    manifest = {
        "source_url": source_url,
        "resource": resource_label,
        "expected_images": expected_images,
        "actual_images": len(files),
        "files": files,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    cache_manifest_path(cache_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cache_manifest_is_complete(cache_dir: Path, expected_images: Optional[int]) -> bool:
    if expected_images is None:
        return True
    manifest_file = cache_manifest_path(cache_dir)
    if not manifest_file.exists():
        return False
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if int(manifest.get("expected_images") or 0) != expected_images:
        return False
    if int(manifest.get("actual_images") or 0) < expected_images:
        return False
    for file_info in manifest.get("files", []):
        relative_path = file_info.get("relative_path")
        if not isinstance(relative_path, str) or not (cache_dir / relative_path).exists():
            return False
    return True


def create_cache_staging_dir(cache_root: Path, cache_dir: Path) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{cache_dir.name}.staging-", dir=cache_root))


def publish_cache_staging_dir(staging_dir: Path, cache_dir: Path) -> None:
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    staging_dir.rename(cache_dir)


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


def import_curl_cffi_requests():
    try:
        from curl_cffi import requests as curl_cffi_requests  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: curl_cffi\n"
            "Install it with:\n"
            "  python3 -m pip install -r requirements.txt"
        ) from exc
    return curl_cffi_requests


def import_playwright_sync_api():
    try:
        from playwright.sync_api import Error as PlaywrightError  # type: ignore
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
    except ModuleNotFoundError as exc:
        raise DriveDownloadError(
            "Missing dependency: playwright. Install it with: "
            ".venv/bin/python -m pip install -r requirements.txt && "
            ".venv/bin/python -m playwright install chromium"
        ) from exc
    return sync_playwright, PlaywrightError, PlaywrightTimeoutError


def drive_download_timeout_seconds() -> int:
    raw_value = os.getenv("DRIVE_DOWNLOAD_TIMEOUT_SECONDS", "")
    if not raw_value:
        return DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    return max(1, value)


def printerval_curl_timeout_seconds() -> int:
    raw_value = os.getenv("PRINTERVAL_CURL_TIMEOUT_SECONDS", "")
    if not raw_value:
        return DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS
    return max(1, value)


def printerval_download_timeout_seconds(image_count: int) -> int:
    return max(
        drive_download_timeout_seconds(),
        max(1, image_count) * DEFAULT_PRINTERVAL_IMAGE_TIMEOUT_BUDGET_SECONDS,
    )


def printerval_playwright_enabled() -> bool:
    raw_value = os.getenv("PRINTERVAL_PLAYWRIGHT_ENABLED", "1").strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def printerval_playwright_headless() -> bool:
    raw_value = os.getenv("PRINTERVAL_PLAYWRIGHT_HEADLESS", "1").strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def printerval_playwright_timeout_seconds() -> int:
    raw_value = os.getenv("PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS", "")
    if not raw_value:
        return DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS
    return max(1, value)


def printerval_playwright_storage_state_path() -> str:
    return os.getenv("PRINTERVAL_PLAYWRIGHT_STORAGE_STATE", "").strip()


def printerval_playwright_user_data_dir() -> str:
    return os.getenv("PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR", "").strip()


def printerval_direct_image_enabled() -> bool:
    raw_value = os.getenv("PRINTERVAL_DIRECT_IMAGE_ENABLED", "").strip().lower()
    if raw_value:
        return raw_value not in {"0", "false", "no", "off"}
    return not (printerval_playwright_enabled() and bool(printerval_playwright_user_data_dir()))


def printerval_playwright_log(stage: str, detail: str = "") -> None:
    message = f"Printerval Playwright stage={stage}"
    if detail:
        message = f"{message} {detail}"
    print(message, flush=True)


def is_playwright_event_loop_closed_error(exc: Exception) -> bool:
    return "event loop is closed" in str(exc).lower()


def detect_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    if "downloadalldesignimages" in lowered or re.search(r"\bconst\s+links\s*=", html):
        return False

    challenge_markers = [
        "<title>just a moment",
        "cf-turnstile-response",
        "you are being in a challenge",
        "enable javascript and cookies to continue",
        "verification successful. waiting for",
        "cf-chl-widget",
        "challenge-error-text",
        "checking your browser",
        "attention required",
        "challenges.cloudflare.com/turnstile",
    ]
    if any(marker in lowered for marker in challenge_markers):
        return True

    return "ctype: 'managed'" in lowered and "printerval" not in lowered


def drive_download_backend() -> str:
    backend = os.getenv("DRIVE_DOWNLOAD_BACKEND", "rclone").strip().lower()
    return backend if backend in {"rclone", "gdown"} else "rclone"


def app_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_resource_roots() -> list[Path]:
    roots = []
    configured = os.getenv("APP_RESOURCE_ROOT", "").strip()
    if configured:
        roots.append(Path(configured))
    bundled_root = getattr(sys, "_MEIPASS", "")
    if bundled_root:
        roots.append(Path(str(bundled_root)))
    roots.append(app_runtime_root())

    unique_roots = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            unique_roots.append(root)
            seen.add(key)
    return unique_roots


def bundled_rclone_bin() -> Optional[Path]:
    executable_name = "rclone.exe" if os.name == "nt" else "rclone"
    for root in app_resource_roots():
        candidate = root / "vendor" / "rclone" / executable_name
        if candidate.exists():
            return candidate
    return None


def rclone_search_locations() -> list[Path]:
    executable_name = "rclone.exe" if os.name == "nt" else "rclone"
    return [root / "vendor" / "rclone" / executable_name for root in app_resource_roots()]


def rclone_bin() -> str:
    configured = os.getenv("RCLONE_BIN", "").strip()
    if configured:
        return configured
    bundled = bundled_rclone_bin()
    if bundled:
        return str(bundled)
    return "rclone"


def printerval_curl_bin() -> str:
    return os.getenv("PRINTERVAL_CURL_BIN", "curl").strip() or "curl"


def load_local_settings() -> dict:
    try:
        with LOCAL_SETTINGS_PATH.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def rclone_proxy_config() -> dict:
    settings = load_local_settings()
    env_proxy = os.getenv("HTTPS_PROXY", "").strip() or os.getenv("HTTP_PROXY", "").strip()
    if env_proxy:
        return {
            "enabled": True,
            "url": env_proxy,
            "source": "environment",
            "source_label": "环境变量",
        }
    enabled = settings.get("proxy_enabled", True)
    url = str(settings.get("proxy_url") or DEFAULT_PROXY_URL).strip()
    return {
        "enabled": bool(enabled),
        "url": url,
        "source": "local_settings",
        "source_label": "本地配置",
    }


def rclone_subprocess_env() -> dict[str, str]:
    env = {**os.environ}
    config = rclone_proxy_config()
    if config["enabled"] and config["url"]:
        env["HTTP_PROXY"] = config["url"]
        env["HTTPS_PROXY"] = config["url"]
        env["NO_PROXY"] = env.get("NO_PROXY", "127.0.0.1,localhost")
    return env


def rclone_drive_remotes() -> list[str]:
    raw_value = os.getenv("RCLONE_DRIVE_REMOTES", "gdrive")
    remotes = []
    for part in raw_value.split(","):
        remote = part.strip().rstrip(":")
        if remote:
            remotes.append(remote)
    return remotes or ["gdrive"]


def rclone_transfers() -> str:
    return os.getenv("RCLONE_TRANSFERS", DEFAULT_RCLONE_TRANSFERS).strip() or DEFAULT_RCLONE_TRANSFERS


def rclone_checkers() -> str:
    return os.getenv("RCLONE_CHECKERS", DEFAULT_RCLONE_CHECKERS).strip() or DEFAULT_RCLONE_CHECKERS


def rclone_drive_pacer_min_sleep() -> str:
    return (
        os.getenv("RCLONE_DRIVE_PACER_MIN_SLEEP", DEFAULT_RCLONE_DRIVE_PACER_MIN_SLEEP).strip()
        or DEFAULT_RCLONE_DRIVE_PACER_MIN_SLEEP
    )


def rclone_drive_pacer_burst() -> str:
    return (
        os.getenv("RCLONE_DRIVE_PACER_BURST", DEFAULT_RCLONE_DRIVE_PACER_BURST).strip()
        or DEFAULT_RCLONE_DRIVE_PACER_BURST
    )


def rclone_local_encoding() -> str:
    return os.getenv("RCLONE_LOCAL_ENCODING", DEFAULT_RCLONE_LOCAL_ENCODING).strip() or DEFAULT_RCLONE_LOCAL_ENCODING


def rclone_common_flags() -> list[str]:
    return [
        "--drive-pacer-min-sleep",
        rclone_drive_pacer_min_sleep(),
        "--drive-pacer-burst",
        rclone_drive_pacer_burst(),
    ]


def rclone_local_flags() -> list[str]:
    return ["--local-encoding", rclone_local_encoding()]


def rclone_copy_flags() -> list[str]:
    return [
        "--transfers",
        rclone_transfers(),
        "--checkers",
        rclone_checkers(),
        *rclone_local_flags(),
    ]


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
        raise DriveDownloadTimeout(f"服务器下载超过 {timeout} 秒，已终止。")

    try:
        status, exc_type, message = queue.get_nowait()
    except Empty:
        if process.exitcode == 0:
            return
        raise DriveDownloadError(f"Google Drive 下载进程异常退出: {process.exitcode}")
    if status == "ok":
        return
    if exc_type == "DriveDownloadTimeout":
        raise DriveDownloadTimeout(message)
    if exc_type == "DriveNetworkError":
        raise DriveNetworkError(message)
    if exc_type == "PrintervalCloudflareChallengeError":
        raise PrintervalCloudflareChallengeError(message)
    if exc_type == "ValueError":
        raise ValueError(message)
    raise DriveDownloadError(f"{exc_type}: {message}")


def classify_download_failure(exc: Exception) -> DownloadFailure:
    message = str(exc)
    lower_message = message.lower()
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
            message="服务器下载超时，可重试；多次失败请手动下载。",
            detail=message,
        )
    if isinstance(exc, PrintervalCloudflareChallengeError) or (
        "cloudflare challenge" in lower_message
        or "printerval cloudflare" in lower_message
        or "printerval 触发 cloudflare" in lower_message
    ):
        return DownloadFailure(
            code="printerval_challenge_required",
            message="Printerval 触发 Cloudflare 验证，请先刷新服务器浏览器会话后重试。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "image file too large" in lower_message
        or "图片文件过大" in lower_message
    ):
        return DownloadFailure(
            code="image_too_large",
            message="图片文件过大，请人工判断或手动下载。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and "insufficient disk space" in lower_message:
        return DownloadFailure(
            code="disk_space_low",
            message="服务器磁盘空间不足，请清理缓存或归档后重试。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "FileURLRetrievalError" in message
        or "Cannot retrieve the public link" in message
        or "have had many accesses" in message
        or "ratelimit" in lower_message
        or "rate limit" in lower_message
        or "rate_limit" in lower_message
        or "quota" in lower_message
        or "too many" in lower_message
    ):
        return DownloadFailure(
            code="drive_rate_limited_or_permission",
            message="Google Drive 限流或权限不可公开下载，可稍后重试；多次失败请手动打开 Drive 下载。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "direct image url returned http" in lower_message
        or "direct image url curl" in lower_message
        or "direct image url curl_cffi" in lower_message
        or "printerval image download failed" in lower_message
    ):
        return DownloadFailure(
            code="direct_image_download_failed",
            message="图片直链下载失败，请确认链接可直接打开图片。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "permission denied" in lower_message
        or "insufficient permissions" in lower_message
        or "access denied" in lower_message
        or "forbidden" in lower_message
    ):
        return DownloadFailure(
            code="drive_permission_denied",
            message="Google Drive 权限受限，请确认授权账号可访问该文件。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "not found" in lower_message
        or "404" in lower_message
        or "couldn't find" in lower_message
    ):
        return DownloadFailure(
            code="drive_not_found_or_permission",
            message="Google Drive 文件不存在或当前账号无权访问。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and "no image files" in message:
        return DownloadFailure(
            code="no_images_found",
            message="下载结果里没有找到可下载图片。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "non-image" in lower_message
        or "not an image" in lower_message
        or "不是图片" in lower_message
        or "content-type=text/html" in lower_message
        or "content-type=application/json" in lower_message
        or "content-type=application/pdf" in lower_message
    ):
        return DownloadFailure(
            code="extension_non_image_download",
            message="下载到的内容不是图片，请确认链接直接打开后是图片文件。",
            detail=message,
        )
    if isinstance(exc, DriveDownloadError) and (
        "google apps" in lower_message
        or "can't download" in lower_message
        or "cannot download" in lower_message
    ):
        return DownloadFailure(
            code="extension_google_apps_file",
            message="链接不是原始图片文件，请导出为图片后再下载。",
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


def validate_direct_image_response(url: str, response: requests.Response) -> str:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        body_sample = ""
        try:
            body_sample = str(response.text or "")[:120].replace("\n", " ").replace("\r", " ")
        except Exception:
            body_sample = "<unreadable>"
        raise DriveDownloadError(
            f"Direct image URL returned HTTP {status_code}: {url}; "
            f"content-type={response.headers.get('Content-Type', '')}; "
            f"server={response.headers.get('Server', '')}; "
            f"body={body_sample}"
        )

    content_type = content_type_without_parameters(response.headers.get("Content-Type", ""))
    url_suffix = Path(urlparse(url).path).suffix.lower()
    if content_type.startswith("image/"):
        return content_type
    if content_type in {"", "application/octet-stream"} and url_suffix in IMAGE_EXTENSIONS:
        return content_type
    if content_type == "application/octet-stream":
        return content_type
    raise DriveDownloadError(f"Direct URL response is non-image: content-type={content_type or '<empty>'} url={url}")


def direct_image_request_headers(referer_url: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": DIRECT_IMAGE_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer_url:
        headers["Referer"] = referer_url
    return headers


def printerval_page_request_headers() -> dict[str, str]:
    return {
        "User-Agent": DIRECT_IMAGE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def download_direct_image_url(
    url: str,
    output_dir: Path,
    referer_url: str = "",
    session: Optional[requests.Session] = None,
) -> None:
    if not is_http_url(url):
        raise ValueError("普通图片链接必须是 http 或 https URL")

    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = drive_download_timeout_seconds()
    requester = session or requests
    response = requester.get(
        url,
        stream=True,
        timeout=timeout,
        headers=direct_image_request_headers(referer_url),
    )
    try:
        content_type = validate_direct_image_response(url, response)
        filename = direct_url_filename(
            url,
            content_type=content_type,
            content_disposition=response.headers.get("Content-Disposition", ""),
        )
        target = next_available_path(output_dir, filename)
        with tempfile.NamedTemporaryFile(
            prefix="direct-image-",
            suffix=target.suffix or ".tmp",
            dir=output_dir,
            delete=False,
        ) as tmp_file:
            temp_path = Path(tmp_file.name)
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    tmp_file.write(chunk)
        if temp_path.stat().st_size <= 0:
            temp_path.unlink(missing_ok=True)
            raise DriveDownloadError(f"Direct image URL returned an empty file: {url}")
        shutil.move(str(temp_path), target)
    except Exception:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def response_text_sample(response, limit: int = 160) -> str:
    try:
        text = str(response.text or "")
    except Exception:
        try:
            text = response.content.decode("utf-8", errors="replace")
        except Exception:
            text = "<unreadable>"
    return text[:limit].replace("\n", " ").replace("\r", " ")


def validate_curl_cffi_image_response(url: str, response) -> str:
    status_code = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    if status_code >= 400:
        raise DriveDownloadError(
            f"Direct image URL curl_cffi returned HTTP {status_code}: {url}; "
            f"content-type={headers.get('Content-Type', '')}; "
            f"server={headers.get('Server', '')}; "
            f"body={response_text_sample(response)}"
        )

    content_type = content_type_without_parameters(headers.get("Content-Type", ""))
    url_suffix = Path(urlparse(url).path).suffix.lower()
    if content_type.startswith("image/"):
        return content_type
    if content_type in {"", "application/octet-stream"} and url_suffix in IMAGE_EXTENSIONS:
        return content_type
    if content_type == "application/octet-stream":
        return content_type
    raise DriveDownloadError(
        f"Direct image URL curl_cffi response is non-image: content-type={content_type or '<empty>'} url={url}; "
        f"body={response_text_sample(response)}"
    )


def write_curl_cffi_response_to_file(response, target: Path) -> None:
    with tempfile.NamedTemporaryFile(
        prefix="printerval-image-",
        suffix=target.suffix or ".tmp",
        dir=target.parent,
        delete=False,
    ) as tmp_file:
        temp_path = Path(tmp_file.name)
        content = getattr(response, "content", b"")
        if content:
            tmp_file.write(content)
        elif hasattr(response, "iter_content"):
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    tmp_file.write(chunk)
    try:
        if temp_path.stat().st_size <= 0:
            raise DriveDownloadError("Direct image URL curl_cffi returned an empty file")
        if is_probably_html_file(temp_path):
            raise DriveDownloadError("Direct image URL curl_cffi downloaded HTML instead of image")
        shutil.move(str(temp_path), target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def printerval_zip_request_headers(page_url: str, csrf_token: str) -> dict[str, str]:
    headers = direct_image_request_headers(page_url)
    headers.update(
        {
            "Accept": "application/zip,application/octet-stream,*/*;q=0.8",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf_token,
        }
    )
    return headers


def curl_cffi_response_content(response) -> bytes:
    content = getattr(response, "content", b"")
    if content:
        return bytes(content)
    if hasattr(response, "iter_content"):
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)
    return b""


def validate_printerval_zip_response(response, endpoint_url: str) -> bytes:
    status_code = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    body = curl_cffi_response_content(response)
    if status_code >= 400:
        raise DriveDownloadError(
            f"Printerval ZIP endpoint returned HTTP {status_code}: {endpoint_url}; "
            f"content-type={headers.get('Content-Type', '')}; "
            f"server={headers.get('Server', '')}; "
            f"body={response_text_sample(response)}"
        )
    content_type = content_type_without_parameters(headers.get("Content-Type", ""))
    if body.startswith(b"PK\x03\x04") or content_type in {
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    }:
        if body.startswith(b"PK"):
            return body
    raise DriveDownloadError(
        f"Printerval ZIP endpoint response is not a zip: content-type={content_type or '<empty>'}; "
        f"body={response_text_sample(response)}"
    )


def safe_extract_printerval_zip(zip_bytes: bytes, output_dir: Path, expected_count: int) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted_count = 0
    try:
        archive = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise DriveDownloadError("Printerval ZIP endpoint returned an invalid zip file") from exc

    with archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise DriveDownloadError(f"Printerval ZIP contains unsafe path: {member.filename}")
            filename = safe_filename(member_path.name)
            if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            target = next_available_path(output_dir, filename)
            with archive.open(member) as source, tempfile.NamedTemporaryFile(
                prefix="printerval-zip-",
                suffix=target.suffix or ".tmp",
                dir=output_dir,
                delete=False,
            ) as tmp_file:
                temp_path = Path(tmp_file.name)
                shutil.copyfileobj(source, tmp_file)
            try:
                if temp_path.stat().st_size <= 0:
                    raise DriveDownloadError(f"Printerval ZIP extracted an empty image: {member.filename}")
                if is_probably_html_file(temp_path):
                    raise DriveDownloadError(f"Printerval ZIP extracted HTML instead of image: {member.filename}")
                shutil.move(str(temp_path), target)
                extracted_count += 1
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

    if extracted_count < expected_count:
        raise DriveDownloadError(
            f"Printerval ZIP expected {expected_count} images, got {extracted_count}; "
            f"files={describe_directory_files(output_dir)}"
        )
    return extracted_count


def download_printerval_zip_endpoint(
    page_url: str,
    output_dir: Path,
    session,
    page_html: str,
) -> int:
    metadata = parse_printerval_zip_metadata(page_html)
    if metadata is None:
        raise DriveDownloadError("Printerval page does not expose ZIP download metadata")

    endpoint_url = printerval_zip_endpoint_url(page_url)
    label = f" product_id={metadata.product_id}" if metadata.product_id else ""
    print(f"Printerval ZIP endpoint start:{label} links={len(metadata.links)}", flush=True)
    response = session.post(
        endpoint_url,
        timeout=printerval_download_timeout_seconds(len(metadata.links)),
        headers=printerval_zip_request_headers(page_url, metadata.csrf_token),
        json={"links": metadata.links},
    )
    try:
        headers = getattr(response, "headers", {}) or {}
        print(
            f"Printerval ZIP endpoint response: status={getattr(response, 'status_code', '')} "
            f"content-type={headers.get('Content-Type', '')}",
            flush=True,
        )
        zip_bytes = validate_printerval_zip_response(response, endpoint_url)
        extracted_count = safe_extract_printerval_zip(zip_bytes, output_dir, len(metadata.links))
        print(f"Printerval ZIP endpoint extracted {extracted_count} images", flush=True)
        return extracted_count
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def save_printerval_playwright_download(download, output_dir: Path, expected_count: int) -> int:
    suggested_name = safe_filename(getattr(download, "suggested_filename", "") or "printerval-download.zip")
    suffix = Path(suggested_name).suffix.lower()
    target = next_available_path(output_dir, suggested_name)
    with tempfile.NamedTemporaryFile(
        prefix="printerval-playwright-",
        suffix=suffix or ".tmp",
        dir=output_dir,
        delete=False,
    ) as tmp_file:
        temp_path = Path(tmp_file.name)

    try:
        download.save_as(str(temp_path))
        if temp_path.stat().st_size <= 0:
            raise DriveDownloadError("Printerval Playwright download returned an empty file")
        if temp_path.read_bytes()[:2] == b"PK":
            zip_bytes = temp_path.read_bytes()
            temp_path.unlink(missing_ok=True)
            return safe_extract_printerval_zip(zip_bytes, output_dir, expected_count)
        if suffix in IMAGE_EXTENSIONS and not is_probably_html_file(temp_path):
            shutil.move(str(temp_path), target)
            return 1
        sample = temp_path.read_bytes()[:240].decode("utf-8", errors="replace").replace("\n", " ")
        raise DriveDownloadError(
            f"Printerval Playwright download is not a zip/image: filename={suggested_name} "
            f"size={temp_path.stat().st_size} body={sample}"
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def playwright_fetch_image(page, image_url: str, download_url: str) -> dict[str, str]:
    context = getattr(page, "context", None)
    request_context = getattr(context, "request", None)
    if request_context is not None:
        page_url = getattr(page, "url", "") or PRINTERVAL_IMAGE_REFERER
        response = request_context.get(
            download_url,
            headers=direct_image_request_headers(page_url),
            timeout=printerval_playwright_timeout_seconds() * 1000,
        )
        headers = getattr(response, "headers", {}) or {}
        body = response.body()
        content_type = headers.get("content-type", headers.get("Content-Type", ""))
        sample = ""
        if not content_type_without_parameters(content_type).startswith("image/"):
            sample = body[:240].decode("utf-8", errors="replace")
        return {
            "imageUrl": image_url,
            "downloadUrl": download_url,
            "status": str(getattr(response, "status", 0) or 0),
            "contentType": content_type,
            "bodyBase64": base64.b64encode(body).decode("ascii"),
            "sample": sample,
        }

    return page.evaluate(
        """
        async ({ imageUrl, downloadUrl }) => {
            const response = await fetch(downloadUrl, {
                credentials: "include",
                referrer: window.location.href,
            });
            const buffer = await response.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binary = "";
            const chunkSize = 0x8000;
            for (let index = 0; index < bytes.length; index += chunkSize) {
                binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
            }
            let sample = "";
            if (!response.headers.get("content-type")?.startsWith("image/")) {
                sample = new TextDecoder("utf-8", { fatal: false }).decode(bytes.slice(0, 240));
            }
            return {
                imageUrl,
                downloadUrl,
                status: String(response.status),
                contentType: response.headers.get("content-type") || "",
                bodyBase64: btoa(binary),
                sample,
            };
        }
        """,
        {"imageUrl": image_url, "downloadUrl": download_url},
    )


def validate_playwright_fetch_image_result(image_url: str, result: dict[str, str]) -> bytes:
    status = int(result.get("status", "0") or 0)
    content_type = content_type_without_parameters(result.get("contentType", ""))
    download_url = result.get("downloadUrl", image_url)
    url_suffix = Path(urlparse(download_url).path).suffix.lower()
    if status >= 400:
        raise DriveDownloadError(
            f"Printerval Playwright browser fetch returned HTTP {status}: {download_url}; "
            f"content-type={content_type}; body={result.get('sample', '')[:180]}"
        )
    if not (
        content_type.startswith("image/")
        or (content_type in {"", "application/octet-stream"} and url_suffix in IMAGE_EXTENSIONS)
    ):
        raise DriveDownloadError(
            f"Printerval Playwright browser fetch returned non-image: {download_url}; "
            f"content-type={content_type or '<empty>'}; body={result.get('sample', '')[:180]}"
        )
    try:
        content = base64.b64decode(result.get("bodyBase64", ""), validate=True)
    except Exception as exc:
        raise DriveDownloadError(f"Printerval Playwright browser fetch returned invalid base64: {download_url}") from exc
    if not content:
        raise DriveDownloadError(f"Printerval Playwright browser fetch returned an empty file: {download_url}")
    return content


def save_playwright_fetch_image(image_url: str, content_type: str, content: bytes, output_dir: Path) -> Path:
    filename = direct_url_filename(image_url, content_type=content_type)
    target = next_available_path(output_dir, filename)
    with tempfile.NamedTemporaryFile(
        prefix="printerval-browser-fetch-",
        suffix=target.suffix or ".tmp",
        dir=output_dir,
        delete=False,
    ) as tmp_file:
        temp_path = Path(tmp_file.name)
        tmp_file.write(content)
    try:
        if is_probably_html_file(temp_path):
            raise DriveDownloadError(f"Printerval Playwright browser fetch downloaded HTML instead of image: {image_url}")
        shutil.move(str(temp_path), target)
        return target
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def download_printerval_images_with_playwright_fetch(
    page,
    image_urls: list[str],
    output_dir: Path,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    total_started_at = time.monotonic()
    total_bytes = 0
    for index, image_url in enumerate(image_urls, start=1):
        variants = [("original", image_url)]
        download_url = printerval_download_url(image_url)
        if download_url != image_url:
            variants.append(("dl", download_url))
        errors: list[str] = []
        for variant, candidate_url in variants:
            variant_started_at = time.monotonic()
            try:
                printerval_playwright_log(
                    "browser_fetch_start",
                    f"image={index}/{len(image_urls)} variant={variant} url={candidate_url}",
                )
                result = playwright_fetch_image(page, image_url, candidate_url)
                content = validate_playwright_fetch_image_result(candidate_url, result)
                elapsed_ms = max(1, int((time.monotonic() - variant_started_at) * 1000))
                saved_path = save_playwright_fetch_image(
                    image_url,
                    result.get("contentType", ""),
                    content,
                    output_dir,
                )
                file_size = saved_path.stat().st_size
                total_bytes += file_size
                rate_kbps = int((file_size / 1024) / (elapsed_ms / 1000))
                printerval_playwright_log(
                    "browser_fetch_saved",
                    f"image={index}/{len(image_urls)} file={saved_path.name} size={file_size} "
                    f"elapsed_ms={elapsed_ms} rate_kbps={rate_kbps}",
                )
                break
            except Exception as exc:  # noqa: BLE001 - collect both URL variants.
                elapsed_ms = max(1, int((time.monotonic() - variant_started_at) * 1000))
                printerval_playwright_log(
                    "browser_fetch_failed",
                    f"image={index}/{len(image_urls)} variant={variant} elapsed_ms={elapsed_ms} "
                    f"{exc.__class__.__name__}: {str(exc)[:300]}",
                )
                errors.append(f"variant={variant} error={exc.__class__.__name__}: {exc}")
        else:
            raise DriveDownloadError(
                f"Printerval Playwright browser fetch failed: image={index}/{len(image_urls)} "
                f"url={image_url}; " + " | ".join(errors)
            )
    total_elapsed_ms = max(1, int((time.monotonic() - total_started_at) * 1000))
    total_rate_kbps = int((total_bytes / 1024) / (total_elapsed_ms / 1000)) if total_bytes else 0
    printerval_playwright_log(
        "browser_fetch_all_done",
        f"images={len(image_urls)} elapsed_ms={total_elapsed_ms} bytes={total_bytes} "
        f"rate_kbps={total_rate_kbps}",
    )
    return len(image_urls)


def trigger_printerval_playwright_download(page, timeout_ms: int) -> str:
    has_download_function = page.evaluate("() => typeof window.downloadAllDesignImages === 'function'")
    if has_download_function:
        printerval_playwright_log("trigger_function", "name=downloadAllDesignImages")
        page.evaluate("() => window.downloadAllDesignImages()")
        return "function:downloadAllDesignImages"

    selectors = [
        "#downloadAllBtn",
        "button:has-text('Download all')",
        "button:has-text('Download All')",
        "button:has-text('Download')",
        "a:has-text('Download all')",
        "a:has-text('Download All')",
        "a:has-text('Download')",
        "button:has-text('下载')",
        "a:has-text('下载')",
    ]
    errors: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=min(timeout_ms, 10_000))
            printerval_playwright_log("trigger_selector", f"selector={selector}")
            locator.click(timeout=min(timeout_ms, 10_000))
            return f"selector:{selector}"
        except Exception as exc:  # noqa: BLE001 - collect selector diagnostics.
            errors.append(f"{selector}: {exc.__class__.__name__}: {str(exc)[:160]}")
    raise DriveDownloadError("Printerval Playwright could not find a download trigger: " + " | ".join(errors))


def save_printerval_playwright_diagnostics(page, diagnostics_dir: Path, prefix: str) -> str:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        screenshot_path = diagnostics_dir / f"{prefix}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        saved.append(str(screenshot_path))
    except Exception as exc:  # noqa: BLE001 - diagnostics should not mask the real failure.
        saved.append(f"screenshot_error={exc.__class__.__name__}:{str(exc)[:120]}")
    try:
        html_path = diagnostics_dir / f"{prefix}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        saved.append(str(html_path))
    except Exception as exc:  # noqa: BLE001
        saved.append(f"html_error={exc.__class__.__name__}:{str(exc)[:120]}")
    return ",".join(saved)


def download_printerval_with_playwright(url: str, output_dir: Path, expected_count: int) -> int:
    if not printerval_playwright_enabled():
        raise DriveDownloadError("Printerval Playwright fallback is disabled")

    sync_playwright, PlaywrightError, PlaywrightTimeoutError = import_playwright_sync_api()
    timeout_seconds = printerval_playwright_timeout_seconds()
    timeout_ms = timeout_seconds * 1000
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_dir = output_dir / ".printerval-playwright-diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    stage = "start"
    page = None
    browser = None
    context = None
    printerval_playwright_log(
        stage,
        f"headless={printerval_playwright_headless()} timeout={timeout_seconds}s "
        f"expected_count={expected_count}",
    )
    try:
        with sync_playwright() as playwright:
            user_data_dir = printerval_playwright_user_data_dir()
            storage_state_path = printerval_playwright_storage_state_path()
            launch_kwargs = {
                "headless": printerval_playwright_headless(),
            }
            stage = "launch_start"
            printerval_playwright_log(
                stage,
                f"user_data_dir={'yes' if user_data_dir else 'no'} "
                f"storage_state={'yes' if storage_state_path else 'no'}",
            )
            if user_data_dir:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir,
                    accept_downloads=True,
                    downloads_path=str(output_dir),
                    **launch_kwargs,
                )
            else:
                browser = playwright.chromium.launch(**launch_kwargs)
                context_kwargs = {
                    "accept_downloads": True,
                }
                if storage_state_path:
                    context_kwargs["storage_state"] = storage_state_path
                context = browser.new_context(**context_kwargs)
            stage = "launch_done"
            printerval_playwright_log(stage)
            stage = "new_page"
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            stage = "goto_start"
            printerval_playwright_log(stage, f"url={url}")
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = getattr(response, "status", None)
            current_url = getattr(page, "url", "")
            try:
                title = page.title()
            except Exception:
                title = "<unavailable>"
            stage = "goto_done"
            printerval_playwright_log(stage, f"status={status} current_url={current_url} title={title[:120]}")
            try:
                stage = "networkidle_wait"
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
                printerval_playwright_log("networkidle_done")
            except Exception:
                printerval_playwright_log("networkidle_timeout", "non_fatal=true")
            stage = "diagnostics_before"
            content = page.content()
            cloudflare_detected = detect_cloudflare_challenge(content)
            printerval_playwright_log(
                "page_inspect",
                f"cloudflare_detected={cloudflare_detected} html_bytes={len(content.encode('utf-8'))}",
            )
            saved = save_printerval_playwright_diagnostics(page, diagnostics_dir, "before-download")
            printerval_playwright_log("diagnostics_saved", saved)
            if cloudflare_detected:
                raise PrintervalCloudflareChallengeError(
                    "Printerval Cloudflare challenge required; "
                    f"stage={stage}; diagnostics={diagnostics_dir}; "
                    "run `.venv/bin/python -m app.tools.printerval_session "
                    f"--url {url!r} --profile {printerval_playwright_user_data_dir() or 'data/browser-profiles/printerval-main'}` "
                    "on the server, complete verification, then retry."
                )
            image_urls = parse_printerval_design_image_urls(url)
            if image_urls:
                try:
                    stage = "browser_fetch_images"
                    fetched_count = download_printerval_images_with_playwright_fetch(
                        page,
                        image_urls[:expected_count],
                        output_dir,
                    )
                    if fetched_count >= expected_count:
                        printerval_playwright_log("browser_fetch_done", f"images={fetched_count}")
                        return fetched_count
                    raise DriveDownloadError(
                        f"Printerval Playwright browser fetch saved {fetched_count} images, expected {expected_count}"
                    )
                except Exception as exc:  # noqa: BLE001 - keep page download fallback available.
                    printerval_playwright_log(
                        "browser_fetch_fallback",
                        f"{exc.__class__.__name__}: {str(exc)[:500]}",
                    )
            stage = "expect_download"
            with page.expect_download(timeout=timeout_ms) as download_info:
                trigger = trigger_printerval_playwright_download(page, timeout_ms)
                printerval_playwright_log("trigger_done", trigger)
            stage = "download_event"
            download = download_info.value
            printerval_playwright_log(
                stage,
                f"suggested_filename={getattr(download, 'suggested_filename', '')}",
            )
            stage = "save_download"
            printerval_playwright_log(stage, "start")
            extracted_count = save_printerval_playwright_download(download, output_dir, expected_count)
            printerval_playwright_log("saved", f"images={extracted_count}")
            return extracted_count
    except PlaywrightTimeoutError as exc:
        if page is not None:
            saved = save_printerval_playwright_diagnostics(page, diagnostics_dir, "failure")
            printerval_playwright_log("failure_diagnostics_saved", saved)
        raise DriveDownloadTimeout(
            f"Printerval Playwright timed out after {timeout_seconds}s; "
            f"stage={stage}; diagnostics={diagnostics_dir}"
        ) from exc
    except PlaywrightError as exc:
        if page is not None:
            saved = save_printerval_playwright_diagnostics(page, diagnostics_dir, "failure")
            printerval_playwright_log("failure_diagnostics_saved", saved)
        raise DriveDownloadError(
            f"Printerval Playwright failed: stage={stage}; diagnostics={diagnostics_dir}; {exc}"
        ) from exc
    except DriveDownloadError:
        if page is not None:
            saved = save_printerval_playwright_diagnostics(page, diagnostics_dir, "failure")
            printerval_playwright_log("failure_diagnostics_saved", saved)
        raise
    except Exception as exc:
        if page is not None:
            saved = save_printerval_playwright_diagnostics(page, diagnostics_dir, "failure")
            printerval_playwright_log("failure_diagnostics_saved", saved)
        raise DriveDownloadError(
            f"Printerval Playwright failed: stage={stage}; diagnostics={diagnostics_dir}; "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        if context is not None:
            try:
                printerval_playwright_log("context_close")
                context.close()
            except Exception as exc:  # noqa: BLE001 - close errors must not hide the download failure.
                if not is_playwright_event_loop_closed_error(exc):
                    printerval_playwright_log("context_close_failed", f"{exc.__class__.__name__}: {exc}")
        if browser is not None:
            try:
                printerval_playwright_log("browser_close")
                browser.close()
            except Exception as exc:  # noqa: BLE001
                if not is_playwright_event_loop_closed_error(exc):
                    printerval_playwright_log("browser_close_failed", f"{exc.__class__.__name__}: {exc}")


def create_printerval_session():
    curl_cffi_requests = import_curl_cffi_requests()
    return curl_cffi_requests.Session(impersonate="chrome")


def warm_printerval_curl_cffi_session(session, url: str) -> str:
    detail, _html = fetch_printerval_page_with_curl_cffi(session, url)
    return detail


def fetch_printerval_page_with_curl_cffi(session, url: str) -> tuple[str, str]:
    try:
        response = session.get(
            url,
            timeout=printerval_curl_timeout_seconds(),
            headers=printerval_page_request_headers(),
        )
    except Exception as exc:
        return f"strategy=curl_cffi_warmup error={exc.__class__.__name__}: {exc}", ""
    try:
        headers = getattr(response, "headers", {}) or {}
        body = str(getattr(response, "text", "") or "")
        return (
            f"strategy=curl_cffi_warmup status={getattr(response, 'status_code', '')} "
            f"content-type={headers.get('Content-Type', '')}"
        ), body
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def download_printerval_image_with_curl_cffi(
    image_url: str,
    output_dir: Path,
    session,
    referer_url: str,
    strategy: str,
    use_download_domain: bool = True,
) -> Path:
    if not is_http_url(image_url):
        raise ValueError("Printerval 图片链接必须是 http 或 https URL")

    output_dir.mkdir(parents=True, exist_ok=True)
    download_url = printerval_download_url(image_url) if use_download_domain else image_url
    response = session.get(
        download_url,
        timeout=printerval_curl_timeout_seconds(),
        headers=direct_image_request_headers(referer_url),
    )
    try:
        content_type = validate_curl_cffi_image_response(download_url, response)
        headers = getattr(response, "headers", {}) or {}
        filename = direct_url_filename(
            download_url,
            content_type=content_type,
            content_disposition=headers.get("Content-Disposition", ""),
        )
        target = next_available_path(output_dir, filename)
        write_curl_cffi_response_to_file(response, target)
        return target
    except Exception as exc:
        if isinstance(exc, DriveDownloadError):
            raise DriveDownloadError(
                f"Direct image URL curl_cffi failed: strategy={strategy} "
                f"url={image_url} download_url={download_url} {exc}"
            ) from exc
        raise
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def is_probably_html_file(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:512].lstrip().lower()
    except OSError:
        return False
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def printerval_curl_error_message(command: list[str], completed: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in [completed.stderr, completed.stdout] if part).strip()
    if not output:
        output = f"curl exited with code {completed.returncode}"
    return f"command={' '.join(command)}\n{output[-2000:]}"


def printerval_curl_image_command(
    *,
    image_url: str,
    output_path: Path,
    referer_url: str,
    cookie_jar_path: Optional[Path] = None,
) -> list[str]:
    command = [
        printerval_curl_bin(),
        "--http1.1",
        "-L",
        "--fail",
        "--show-error",
        "--silent",
        "--max-time",
        str(printerval_curl_timeout_seconds()),
        "-H",
        f"User-Agent: {DIRECT_IMAGE_USER_AGENT}",
        "-H",
        "Accept: image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        f"Referer: {referer_url}",
    ]
    if cookie_jar_path is not None:
        command.extend(["--cookie", str(cookie_jar_path), "--cookie-jar", str(cookie_jar_path)])
    command.extend(["-o", str(output_path), image_url])
    return command


def printerval_curl_page_warmup_command(page_url: str, cookie_jar_path: Path) -> list[str]:
    return [
        printerval_curl_bin(),
        "--http1.1",
        "-L",
        "--fail",
        "--show-error",
        "--silent",
        "--max-time",
        str(printerval_curl_timeout_seconds()),
        "-H",
        f"User-Agent: {DIRECT_IMAGE_USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "--cookie-jar",
        str(cookie_jar_path),
        "-o",
        os.devnull,
        page_url,
    ]


def run_printerval_curl_command(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 5,
        )
    except subprocess.TimeoutExpired as exc:
        raise DriveDownloadTimeout(f"Direct image URL curl timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise DriveDownloadError(f"Direct image URL curl failed to start: {exc}") from exc


def download_printerval_image_with_curl(
    image_url: str,
    output_dir: Path,
    referer_url: str = PRINTERVAL_IMAGE_REFERER,
    cookie_jar_path: Optional[Path] = None,
    strategy: str = "short_referer",
) -> Path:
    if not is_http_url(image_url):
        raise ValueError("Printerval 图片链接必须是 http 或 https URL")

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = direct_url_filename(image_url)
    target = next_available_path(output_dir, filename)
    timeout = printerval_curl_timeout_seconds()
    with tempfile.NamedTemporaryFile(
        prefix="printerval-image-",
        suffix=target.suffix or ".tmp",
        dir=output_dir,
        delete=False,
    ) as tmp_file:
        temp_path = Path(tmp_file.name)

    command = printerval_curl_image_command(
        image_url=image_url,
        output_path=temp_path,
        referer_url=referer_url,
        cookie_jar_path=cookie_jar_path,
    )
    try:
        completed = run_printerval_curl_command(command, timeout)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    if completed.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise DriveDownloadError(
            f"Direct image URL curl failed: strategy={strategy} url={image_url} "
            f"exit={completed.returncode} {printerval_curl_error_message(command, completed)}"
        )
    try:
        if temp_path.stat().st_size <= 0:
            raise DriveDownloadError(f"Direct image URL curl returned an empty file: strategy={strategy} url={image_url}")
        if is_probably_html_file(temp_path):
            raise DriveDownloadError(
                f"Direct image URL curl downloaded HTML instead of image: strategy={strategy} url={image_url}"
            )
        shutil.move(str(temp_path), target)
        return target
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def warm_printerval_curl_cookie_jar(page_url: str, cookie_jar_path: Path) -> str:
    command = printerval_curl_page_warmup_command(page_url, cookie_jar_path)
    try:
        completed = run_printerval_curl_command(command, printerval_curl_timeout_seconds())
    except Exception as exc:
        return f"strategy=cookie_jar_warmup error={exc.__class__.__name__}: {exc}"
    if completed.returncode != 0:
        return (
            "strategy=cookie_jar_warmup "
            f"exit={completed.returncode} {printerval_curl_error_message(command, completed)}"
        )
    return "strategy=cookie_jar_warmup status=ok"


def warm_printerval_session(session: requests.Session, url: str) -> str:
    try:
        response = session.get(
            url,
            stream=False,
            timeout=min(30, drive_download_timeout_seconds()),
            headers=printerval_page_request_headers(),
        )
    except Exception as exc:
        return f"error={exc.__class__.__name__}: {exc}"
    try:
        cookie_jar = getattr(session, "cookies", None)
        cookie_dict = cookie_jar.get_dict() if cookie_jar and hasattr(cookie_jar, "get_dict") else {}
        cookie_names = ",".join(sorted(cookie_dict.keys()))
        return (
            f"status={getattr(response, 'status_code', '')} "
            f"content-type={response.headers.get('Content-Type', '')} "
            f"cookies={cookie_names or '<none>'}"
        )
    finally:
        response.close()


def download_printerval_image_with_retries(
    image_url: str,
    output_dir: Path,
    page_url: str,
    image_index: int,
    image_count: int,
    session,
) -> None:
    errors: list[str] = []
    for attempt in range(1, PRINTERVAL_IMAGE_MAX_ATTEMPTS + 1):
        referer_strategies: list[tuple[str, str]] = [
            ("short_referer", PRINTERVAL_IMAGE_REFERER),
            ("page_referer", page_url),
        ]
        for strategy, referer_url in referer_strategies:
            download_variants = [("dl", True)]
            if printerval_download_url(image_url) != image_url:
                download_variants.append(("original", False))
            for variant, use_download_domain in download_variants:
                try:
                    print(
                        f"Printerval image {image_index}/{image_count} strategy={strategy} variant={variant} "
                        f"start: {image_url} "
                        f"download_url={printerval_download_url(image_url) if use_download_domain else image_url}",
                        flush=True,
                    )
                    saved_path = download_printerval_image_with_curl_cffi(
                        image_url,
                        output_dir,
                        session,
                        referer_url=referer_url,
                        strategy=f"{strategy}/{variant}",
                        use_download_domain=use_download_domain,
                    )
                    print(
                        f"Printerval image {image_index}/{image_count} saved {saved_path.name} "
                        f"size={saved_path.stat().st_size}",
                        flush=True,
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - keep per-image retry detail.
                    print(
                        f"Printerval image {image_index}/{image_count} strategy={strategy} variant={variant} failed: "
                        f"{exc.__class__.__name__}: {str(exc)[:500]}",
                        flush=True,
                    )
                    errors.append(
                        f"attempt={attempt} strategy={strategy} variant={variant} "
                        f"error={exc.__class__.__name__}: {exc}"
                    )
        if attempt < PRINTERVAL_IMAGE_MAX_ATTEMPTS:
            time.sleep(1)
    raise DriveDownloadError(" | ".join(errors))


def download_printerval_design_url(url: str, output_dir: Path) -> None:
    image_urls = parse_printerval_design_image_urls(url)
    if not image_urls:
        raise ValueError("Printerval 链接中没有找到 design_urls 图片列表。")

    output_dir.mkdir(parents=True, exist_ok=True)
    session = create_printerval_session()
    warmup_detail, page_html = fetch_printerval_page_with_curl_cffi(session, url)
    zip_detail = ""
    if page_html:
        try:
            download_printerval_zip_endpoint(url, output_dir, session, page_html)
            downloaded_count = image_file_count(output_dir)
            if downloaded_count >= len(image_urls):
                return
            raise DriveDownloadError(
                f"Printerval ZIP endpoint saved {downloaded_count} images, expected {len(image_urls)}"
            )
        except Exception as exc:  # noqa: BLE001 - keep fallback available when page endpoint is blocked.
            zip_detail = f"; zip_endpoint={exc.__class__.__name__}: {exc}"
            print(f"Printerval ZIP endpoint failed, falling back to images: {exc.__class__.__name__}: {exc}", flush=True)

    errors: list[str] = []
    if printerval_direct_image_enabled():
        for index, image_url in enumerate(image_urls, start=1):
            try:
                download_printerval_image_with_retries(image_url, output_dir, url, index, len(image_urls), session)
            except Exception as exc:  # noqa: BLE001 - aggregate per-image failures.
                errors.append(f"image={index}/{len(image_urls)} url={image_url} error={exc.__class__.__name__}: {exc}")
    else:
        errors.append("direct_image_downloads=skipped because Playwright profile is configured")

    if errors:
        playwright_detail = ""
        try:
            print("Printerval image downloads failed, trying Playwright fallback", flush=True)
            download_printerval_with_playwright(url, output_dir, len(image_urls))
            downloaded_count = image_file_count(output_dir)
            if downloaded_count >= len(image_urls):
                return
            raise DriveDownloadError(
                f"Printerval Playwright saved {downloaded_count} images, expected {len(image_urls)}"
            )
        except Exception as exc:  # noqa: BLE001 - include browser fallback diagnostics in final error.
            playwright_detail = f"; playwright={exc.__class__.__name__}: {exc}"
            print(f"Printerval Playwright fallback failed: {exc.__class__.__name__}: {exc}", flush=True)
        raise DriveDownloadError(
            f"Printerval image download failed: warmup={warmup_detail}{zip_detail}{playwright_detail}; "
            + " | ".join(errors)
        )
    downloaded_count = image_file_count(output_dir)
    if downloaded_count < len(image_urls):
        raise DriveDownloadError(
            f"Printerval expected {len(image_urls)} images, got {downloaded_count}; "
            f"files={describe_directory_files(output_dir)}"
        )


def download_drive_folder_by_id(folder_id: str, output_dir: Path) -> None:
    if drive_download_backend() == "gdown":
        download_drive_folder_by_id_gdown(folder_id, output_dir)
    else:
        download_drive_folder_by_id_rclone(folder_id, output_dir)


def download_drive_file_by_id(file_id: str, output_dir: Path) -> None:
    if drive_download_backend() == "gdown":
        download_drive_file_by_id_gdown(file_id, output_dir)
    else:
        download_drive_file_by_id_rclone(file_id, output_dir)


def should_try_next_rclone_remote(exc: Exception) -> bool:
    failure = classify_download_failure(exc)
    return failure.code in {
        "network_error",
        "download_timeout",
        "drive_rate_limited_or_permission",
        "drive_download_failed",
    }


def rclone_error_message(command: list[str], completed: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in [completed.stderr, completed.stdout] if part).strip()
    if not output:
        output = f"rclone exited with code {completed.returncode}"
    return f"command={' '.join(command)}\n{output[-4000:]}"


def run_rclone_command(
    command: list[str], timeout_seconds: Optional[int] = None
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=rclone_subprocess_env(),
            errors="replace",
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DriveDownloadTimeout(f"rclone command timed out after {timeout_seconds}s: {' '.join(command)}") from exc
    except OSError as exc:
        raise DriveNetworkError(f"failed to run rclone: {exc}") from exc

    if completed.returncode != 0:
        raise RcloneDownloadError(rclone_error_message(command, completed))
    return completed


def rclone_metadata_timeout_seconds() -> int:
    return min(60, drive_download_timeout_seconds())


def is_rclone_folder_probe_file_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in [
            "can't use file as root folder",
            "can not use file as root folder",
            "cannot use file as root folder",
            "not a directory",
            "is a file",
            "is not a folder",
            "not a folder",
            "not a directory object",
        ]
    )


def rclone_probe_drive_folder_id(remote: str, resource_id: str) -> None:
    run_rclone_command(
        [
            rclone_bin(),
            "lsf",
            f"{remote}:",
            "--drive-root-folder-id",
            resource_id,
            "--max-depth",
            "1",
            *rclone_common_flags(),
        ],
        timeout_seconds=rclone_metadata_timeout_seconds(),
    )


def rclone_drive_folder_image_paths(folder_id: str) -> list[str]:
    errors: list[str] = []
    remotes = rclone_drive_remotes()
    for index, remote in enumerate(remotes, start=1):
        command = [
            rclone_bin(),
            "lsf",
            f"{remote}:",
            "--drive-root-folder-id",
            folder_id,
            "--recursive",
            "--files-only",
            *rclone_common_flags(),
        ]
        try:
            completed = run_rclone_command(command, timeout_seconds=rclone_metadata_timeout_seconds())
            paths: list[str] = []
            for line in (completed.stdout or "").splitlines():
                path = line.strip()
                if path and Path(path).suffix.lower() in IMAGE_EXTENSIONS:
                    paths.append(path)
            return paths
        except Exception as exc:  # noqa: BLE001 - aggregate remote pool failures.
            errors.append(f"remote={remote} error={exc.__class__.__name__}: {exc}")
            if index >= len(remotes) or not should_try_next_rclone_remote(exc):
                break
    raise RcloneDownloadError(" | ".join(errors) if errors else f"could not inspect Drive folder id={folder_id}")


def resolve_rclone_drive_resource(resource_id: str) -> DriveResource:
    errors: list[str] = []
    remotes = rclone_drive_remotes()
    for index, remote in enumerate(remotes, start=1):
        try:
            rclone_probe_drive_folder_id(remote, resource_id)
            return DriveResource("folder", resource_id)
        except Exception as exc:  # noqa: BLE001 - aggregate remote pool failures.
            errors.append(f"remote={remote} error={exc.__class__.__name__}: {exc}")
            if is_rclone_folder_probe_file_error(exc):
                return DriveResource("file", resource_id)
            if index >= len(remotes):
                break
            if should_try_next_rclone_remote(exc):
                continue
            return DriveResource("file", resource_id)
    raise RcloneDownloadError(" | ".join(errors) if errors else f"could not resolve Drive id={resource_id}")


def is_copyid_directory_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "can't copyid directory" in message or "cant copyid directory" in message


def run_with_rclone_remote_pool(build_command: Callable[[str], list[str]]) -> None:
    errors: list[str] = []
    remotes = rclone_drive_remotes()
    for index, remote in enumerate(remotes, start=1):
        command = build_command(remote)
        try:
            run_rclone_command(command)
            return
        except Exception as exc:  # noqa: BLE001 - aggregate remote pool failures.
            errors.append(f"remote={remote} error={exc.__class__.__name__}: {exc}")
            if index >= len(remotes) or not should_try_next_rclone_remote(exc):
                break
    raise RcloneDownloadError(" | ".join(errors) if errors else "rclone download failed")


def download_drive_folder_by_id_rclone(folder_id: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    def command_for_remote(remote: str) -> list[str]:
        return [
            rclone_bin(),
            "copy",
            f"{remote}:",
            str(output_dir),
            *rclone_copy_flags(),
            "--drive-root-folder-id",
            folder_id,
            *rclone_common_flags(),
        ]

    run_with_rclone_remote_pool(command_for_remote)


def download_drive_file_by_id_rclone(file_id: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    def command_for_remote(remote: str) -> list[str]:
        return [
            rclone_bin(),
            "backend",
            "copyid",
            f"{remote}:",
            file_id,
            f"{output_dir}{os.sep}",
            *rclone_local_flags(),
            *rclone_common_flags(),
        ]

    run_with_rclone_remote_pool(command_for_remote)


def download_drive_folder_by_id_gdown(folder_id: str, output_dir: Path) -> None:
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


def download_drive_file_by_id_gdown(file_id: str, output_dir: Path) -> None:
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
        validate_image_file_size(image_path)
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
    if not is_google_drive_url(url) and not is_http_url(url):
        raise ValueError("not a supported image URL")

    with tempfile.TemporaryDirectory(prefix="order-drive-") as tmp:
        temp_dir = Path(tmp)
        if is_google_drive_url(url):
            download_drive_resource(url, temp_dir)
        elif is_printerval_design_url(url):
            download_printerval_design_url(url, temp_dir)
        else:
            download_direct_image_url(url, temp_dir)
        normalize_image_file_extensions(temp_dir)
        return copy_images(temp_dir, order_dir)


def cached_drive_folder(url: str, cache_root: Path) -> Path:
    expected_printerval_image_count = 0
    expected_drive_image_count: Optional[int] = None
    expected_images: Optional[int] = None
    if is_google_drive_url(url):
        resource = parse_drive_resource_for_download(url)
        cache_dir = cache_root / f"{resource.kind}-{resource.resource_id}"
        resource_label = f"{resource.kind}:{resource.resource_id}"
        if drive_download_backend() == "rclone" and resource.kind == "folder":
            expected_drive_image_count = len(rclone_drive_folder_image_paths(resource.resource_id))
            expected_images = expected_drive_image_count
    elif is_printerval_design_url(url):
        expected_printerval_image_count = len(parse_printerval_design_image_urls(url))
        expected_images = expected_printerval_image_count
        resource = DriveResource("folder", direct_url_cache_key(url))
        cache_dir = cache_root / f"printerval-{resource.resource_id}"
        resource_label = f"printerval:{resource.resource_id}"
    elif is_http_url(url):
        resource = DriveResource("file", direct_url_cache_key(url))
        cache_dir = cache_root / f"url-{resource.resource_id}"
        resource_label = f"url:{resource.resource_id}"
    else:
        raise ValueError("链接必须是 Google Drive、http 或 https URL")

    cached_count = image_file_count(cache_dir)
    if expected_printerval_image_count:
        if cached_count >= expected_printerval_image_count and cache_manifest_is_complete(
            cache_dir, expected_printerval_image_count
        ):
            validate_image_file_sizes(cache_dir)
            return cache_dir
    elif expected_drive_image_count is not None:
        print(
            f"Drive folder inspect resource={resource_label} expected_images={expected_drive_image_count} "
            f"cached_images={cached_count}",
            flush=True,
        )
        if (
            expected_drive_image_count > 0
            and cached_count >= expected_drive_image_count
            and cache_manifest_is_complete(cache_dir, expected_drive_image_count)
        ):
            validate_image_file_sizes(cache_dir)
            return cache_dir
    elif cached_count:
        validate_image_file_sizes(cache_dir)
        return cache_dir

    ensure_min_free_disk_space(cache_root)
    staging_dir = create_cache_staging_dir(cache_root, cache_dir)
    try:
        if is_google_drive_url(url) and resource.kind == "folder":
            run_download_with_timeout(download_drive_folder_by_id, resource.resource_id, staging_dir)
        elif is_google_drive_url(url):
            try:
                run_download_with_timeout(download_drive_file_by_id, resource.resource_id, staging_dir)
            except Exception as exc:
                if not is_copyid_directory_error(exc):
                    raise
                shutil.rmtree(staging_dir, ignore_errors=True)
                cache_dir = cache_root / f"folder-{resource.resource_id}"
                resource_label = f"folder:{resource.resource_id}"
                if drive_download_backend() == "rclone":
                    expected_drive_image_count = len(rclone_drive_folder_image_paths(resource.resource_id))
                    expected_images = expected_drive_image_count
                fallback_cached_count = image_file_count(cache_dir)
                if expected_drive_image_count is not None:
                    print(
                        f"Drive folder inspect resource={resource_label} expected_images={expected_drive_image_count} "
                        f"cached_images={fallback_cached_count}",
                        flush=True,
                    )
                    if (
                        expected_drive_image_count > 0
                        and fallback_cached_count >= expected_drive_image_count
                        and cache_manifest_is_complete(cache_dir, expected_drive_image_count)
                    ):
                        validate_image_file_sizes(cache_dir)
                        return cache_dir
                elif fallback_cached_count:
                    validate_image_file_sizes(cache_dir)
                    return cache_dir
                staging_dir = create_cache_staging_dir(cache_root, cache_dir)
                run_download_with_timeout(download_drive_folder_by_id, resource.resource_id, staging_dir)
        elif is_printerval_design_url(url):
            run_download_with_timeout(
                download_printerval_design_url,
                url,
                staging_dir,
                timeout_seconds=printerval_download_timeout_seconds(expected_printerval_image_count),
            )
        else:
            run_download_with_timeout(download_direct_image_url, url, staging_dir)

        normalize_image_file_extensions(staging_dir)
        downloaded_count = image_file_count(staging_dir)
        if expected_drive_image_count is not None:
            print(
                f"Drive folder downloaded resource={resource_label} expected_images={expected_drive_image_count} "
                f"downloaded_images={downloaded_count}",
                flush=True,
            )
            if downloaded_count < expected_drive_image_count:
                raise DriveDownloadError(
                    "Google Drive folder download incomplete; "
                    f"resource={resource_label}; expected_images={expected_drive_image_count}; "
                    f"downloaded_images={downloaded_count}; files={describe_directory_files(staging_dir)}"
                )
        if expected_printerval_image_count and downloaded_count < expected_printerval_image_count:
            raise DriveDownloadError(
                "Printerval download incomplete; "
                f"resource={resource_label}; expected_images={expected_printerval_image_count}; "
                f"downloaded_images={downloaded_count}; files={describe_directory_files(staging_dir)}"
            )
        if not any(iter_image_files(staging_dir)):
            raise DriveDownloadError(
                "Download completed, but no image files were found; "
                f"resource={resource_label}; "
                f"files={describe_directory_files(staging_dir)}"
            )
        validate_image_file_sizes(staging_dir)
        write_cache_manifest(staging_dir, url, resource_label, expected_images)
        publish_cache_staging_dir(staging_dir, cache_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return cache_dir
