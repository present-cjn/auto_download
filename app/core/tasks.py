from __future__ import annotations

import shutil
import threading
import zipfile
from pathlib import Path
from typing import Optional

from app.core import database as db
from app.core.downloader import (
    cached_drive_folder,
    classify_download_failure,
    copy_images,
    safe_filename,
)
from app.core.excel_parser import (
    build_import_summary,
    list_worksheet_names,
    parse_order_items,
    single_visible_worksheet_name,
)


DATA_DIR = Path("data")
UPLOADS_DIR = DATA_DIR / "uploads"
ORDERS_DIR = DATA_DIR / "orders"
ARCHIVES_DIR = DATA_DIR / "archives"
CACHE_DIR = DATA_DIR / "cache"


def ensure_data_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def ensure_batch_order_dir(batch_id: int) -> Path:
    batch_order_dir = ORDERS_DIR / str(batch_id)
    batch_order_dir.mkdir(parents=True, exist_ok=True)
    return batch_order_dir


def parse_batch(
    batch_id: int, source_path: Path, sheet_name: Optional[str] = None
) -> None:
    db.update_batch_status(batch_id, "parsing")
    try:
        available_sheet_names = list_worksheet_names(source_path, visible_only=True)
        selected_sheet_name = sheet_name or single_visible_worksheet_name(source_path)
        items = parse_order_items(source_path, selected_sheet_name)
        summary = build_import_summary(items)
        summary["sheet_name"] = selected_sheet_name
        summary["available_sheet_names"] = available_sheet_names
        db.set_batch_import_summary(batch_id, summary)
        db.insert_import_items(batch_id, items)
        ensure_batch_order_dir(batch_id)
        db.update_batch_status(
            batch_id,
            "review_ready" if summary.get("can_start_download", True) else "needs_fix",
        )
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


def create_order_archive(batch_id: int, order_id: int) -> Path:
    order = db.get_order(order_id)
    if not order or int(order["batch_id"]) != batch_id:
        raise FileNotFoundError("Order not found")

    batch_dir = ORDERS_DIR / str(batch_id)
    order_items = db.get_batch_orders(batch_id)
    order_skus = []
    for batch_order in order_items:
        if int(batch_order["id"]) != order_id:
            continue
        order_skus = [safe_filename(item["sku"] or f"row-{item['row_number']}") for item in batch_order["items"]]
        break
    order_files = []
    for sku in order_skus:
        sku_dir = batch_dir / sku
        if sku_dir.exists():
            order_files.extend(path for path in sku_dir.rglob("*") if path.is_file())
    if not order_files:
        raise FileNotFoundError("Order files are not ready")

    order_no = order["order_no"]
    archive_name = f"batch-{batch_id}-order-{order_id}-{safe_filename(order_no)}.zip"
    archive_path = ARCHIVES_DIR / archive_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(order_files):
            archive.write(path, path.relative_to(batch_dir))
    return archive_path


def process_one_download_item(item: dict) -> None:
    download_item_id = int(item["id"])
    batch_id = int(item["batch_id"])
    source_type = item.get("source_type") or "design"
    sku = item["item_sku"] or item["sku"] or f"row-{item['row_number']}"
    db.clear_downloaded_files(download_item_id)
    db.mark_download_started(download_item_id)
    try:
        print(
            f"[batch {batch_id}] downloading {source_type} for {sku}: {item['design_link']}",
            flush=True,
        )
        sku_dir = safe_filename(sku)
        target_dir = ORDERS_DIR / str(batch_id) / sku_dir
        source_dir = cached_drive_folder(item["design_link"], CACHE_DIR / str(batch_id))
        copied_files = copy_images(source_dir, target_dir)
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
        print(
            f"[batch {batch_id}] downloaded {len(copied_files)} image(s) for {sku} ({source_type})",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - keep the batch running.
        failure = classify_download_failure(exc)
        db.mark_download_failed(
            download_item_id,
            failure.message,
            error_code=failure.code,
            error_detail=failure.detail,
        )
        print(
            f"[batch {batch_id}] failed {source_type} for {sku}: "
            f"{failure.message} [{failure.code}] {failure.detail[:500]}",
            flush=True,
        )
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
    ensure_batch_order_dir(batch_id)
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
    ensure_batch_order_dir(batch_id)
    db.update_batch_status(batch_id, "downloading")
    process_one_download_item(item)
    finish_download_batch(batch_id)


def process_batch(batch_id: int, source_path: Path) -> None:
    try:
        parse_batch(batch_id, source_path, sheet_name=None)
    except Exception:
        db.refresh_batch_counts(batch_id)


def start_download(batch_id: int) -> None:
    process_download_items(batch_id, failed_only=False)


def retry_failed(batch_id: int) -> None:
    process_download_items(batch_id, failed_only=True)


def retry_download_item(download_item_id: int) -> None:
    process_download_item(download_item_id)


def mark_download_item_manual_done(download_item_id: int) -> None:
    item = db.get_download_item(download_item_id)
    if not item:
        return
    db.mark_download_manual_done(download_item_id)
    batch_id = int(item["batch_id"])
    db.refresh_batch_counts(batch_id)
    batch = db.get_batch(batch_id)
    if (
        batch
        and batch["status"] == "completed_with_errors"
        and int(batch["failed_count"]) == 0
    ):
        db.update_batch_status(batch_id, "completed")


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
    shutil.rmtree(CACHE_DIR / str(batch_id), ignore_errors=True)
    archive_path = ARCHIVES_DIR / f"batch-{batch_id}.zip"
    if archive_path.exists():
        archive_path.unlink()
