from __future__ import annotations

import shutil
import threading
import zipfile
from pathlib import Path

from app.core import database as db
from app.core.downloader import download_design_images, safe_filename
from app.core.excel_parser import DEFAULT_SHEET, parse_order_items


DATA_DIR = Path("data")
UPLOADS_DIR = DATA_DIR / "uploads"
ORDERS_DIR = DATA_DIR / "orders"
ARCHIVES_DIR = DATA_DIR / "archives"


def ensure_data_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)


def parse_batch(batch_id: int, source_path: Path, sheet_name: str = DEFAULT_SHEET) -> None:
    db.update_batch_status(batch_id, "parsing")
    try:
        items = parse_order_items(source_path, sheet_name)
        db.insert_import_items(batch_id, items)
        db.update_batch_status(batch_id, "review_ready")
    except Exception as exc:  # noqa: BLE001 - surface parsing failure in UI.
        db.update_batch_status(batch_id, "failed", str(exc))
        raise


def create_archive(batch_id: int) -> Path:
    batch_order_dir = ORDERS_DIR / str(batch_id)
    archive_path = ARCHIVES_DIR / f"batch-{batch_id}.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        if batch_order_dir.exists():
            for path in sorted(batch_order_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(batch_order_dir))
    db.set_batch_archive(batch_id, archive_path)
    return archive_path


def process_one_download_item(item: dict) -> None:
    download_item_id = int(item["id"])
    batch_id = int(item["batch_id"])
    db.clear_downloaded_files(download_item_id)
    db.mark_download_started(download_item_id)
    try:
        sku_dir = safe_filename(item["item_sku"] or item["sku"] or f"row-{item['row_number']}")
        target_dir = ORDERS_DIR / str(batch_id) / item["order_no"] / sku_dir
        copied_files = download_design_images(item["design_link"], target_dir)
        for copied in copied_files:
            db.add_downloaded_file(
                download_item_id=download_item_id,
                batch_id=batch_id,
                order_id=int(item["order_id"]),
                order_no=item["order_no"],
                file_name=copied.file_name,
                local_path=copied.local_path,
                file_size=copied.file_size,
            )
        db.mark_download_success(download_item_id, len(copied_files))
    except Exception as exc:  # noqa: BLE001 - keep the batch running.
        db.mark_download_failed(download_item_id, str(exc))
    finally:
        db.refresh_batch_counts(batch_id)


def finish_download_batch(batch_id: int) -> None:
    create_archive(batch_id)
    db.refresh_batch_counts(batch_id)
    batch = db.get_batch(batch_id)
    if batch and int(batch["failed_count"]) > 0:
        db.update_batch_status(batch_id, "completed_with_errors")
    else:
        db.update_batch_status(batch_id, "completed")


def process_download_items(batch_id: int, failed_only: bool = False) -> None:
    db.update_batch_status(batch_id, "downloading")
    items = (
        db.get_failed_download_items(batch_id)
        if failed_only
        else db.get_pending_download_items(batch_id)
    )

    for item in items:
        process_one_download_item(item)

    finish_download_batch(batch_id)


def process_download_item(download_item_id: int) -> None:
    item = db.get_download_item(download_item_id)
    if not item:
        return
    batch_id = int(item["batch_id"])
    db.update_batch_status(batch_id, "downloading")
    process_one_download_item(item)
    finish_download_batch(batch_id)


def process_batch(batch_id: int, source_path: Path) -> None:
    try:
        parse_batch(batch_id, source_path)
    except Exception:
        db.refresh_batch_counts(batch_id)


def start_download(batch_id: int) -> None:
    process_download_items(batch_id, failed_only=False)


def retry_failed(batch_id: int) -> None:
    process_download_items(batch_id, failed_only=True)


def retry_download_item(download_item_id: int) -> None:
    process_download_item(download_item_id)


def start_background(target, *args) -> None:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


def save_upload(batch_id: int, filename: str, content: bytes) -> Path:
    batch_dir = UPLOADS_DIR / str(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".xlsx"
    target = batch_dir / f"source{suffix}"
    target.write_bytes(content)
    return target


def remove_batch_files(batch_id: int) -> None:
    shutil.rmtree(UPLOADS_DIR / str(batch_id), ignore_errors=True)
    shutil.rmtree(ORDERS_DIR / str(batch_id), ignore_errors=True)
    archive_path = ARCHIVES_DIR / f"batch-{batch_id}.zip"
    if archive_path.exists():
        archive_path.unlink()
