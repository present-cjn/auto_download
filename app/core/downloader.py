from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


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


def is_google_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "drive.google.com" or host.endswith(".drive.google.com")


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
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
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


def download_drive_folder(url: str, temp_dir: Path) -> None:
    gdown = import_gdown()
    result = gdown.download_folder(
        url=url,
        output=str(temp_dir),
        quiet=False,
        use_cookies=False,
        remaining_ok=True,
    )
    if result is None:
        raise RuntimeError("gdown returned no downloaded files")


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
