from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import database as db
from app.core.downloader import ERROR_LABELS, parse_drive_resource, safe_filename
from app.core.downloader import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_IMAGE_FILE_SIZE_MB,
    DEFAULT_MIN_FREE_DISK_SPACE_MB,
    DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS,
    DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS,
    drive_download_backend,
    drive_download_timeout_seconds,
    DEFAULT_PROXY_URL,
    max_image_file_size_mb,
    min_free_disk_space_mb,
    printerval_curl_timeout_seconds,
    printerval_playwright_enabled,
    printerval_playwright_timeout_seconds,
    rclone_bin,
    rclone_checkers,
    rclone_drive_remotes,
    rclone_proxy_config,
    rclone_search_locations,
    rclone_subprocess_env,
    rclone_transfers,
)
from app.core.excel_parser import build_import_summary
from app.core.local_settings import env_or_setting, load_local_settings, save_local_settings
from app.core.paths import app_data_dir
from app.core.security import (
    hash_password,
    new_session_token,
    session_expiry_string,
    utc_now_string,
    verify_password,
)
from app.core.tasks import (
    ARCHIVES_DIR,
    configured_download_delay_seconds,
    configured_retry_backoff_seconds,
    ensure_data_dirs,
    mark_download_item_manual_done,
    process_batch,
    retry_download_item,
    retry_failed,
    retry_failed_limited,
    remove_batch_files,
    save_upload,
    start_download,
    start_download_limited,
    start_background,
)
from scripts.download_speed_report import build_speed_report, format_seconds


def resource_root() -> Path:
    configured = os.getenv("APP_RESOURCE_ROOT", "").strip()
    if configured:
        return Path(configured)
    bundled_root = getattr(sys, "_MEIPASS", "")
    if bundled_root:
        return Path(str(bundled_root))
    return Path(".")


def resource_path(relative_path: str) -> Path:
    return resource_root() / relative_path


app = FastAPI(title="Order Design Image Downloader")
templates = Jinja2Templates(directory=str(resource_path("templates")))
app.mount("/static", StaticFiles(directory=str(resource_path("static"))), name="static")

SESSION_COOKIE = "app_session"
RESOURCES_DIR = app_data_dir() / "resources"
VALID_ROLES = {"developer", "admin", "operator"}
ROLE_LABELS = {
    "developer": "开发者",
    "admin": "管理员",
    "operator": "业务员",
    "legacy": "历史批次",
}
RESOURCE_CATEGORY_LABELS = {
    "header_template": "表头模板",
    "browser_extension": "浏览器插件",
    "guide": "使用说明",
}
RESOURCE_ALLOWED_EXTENSIONS = {
    "header_template": {".xlsx"},
    "browser_extension": {".zip"},
    "guide": {".pdf", ".docx", ".xlsx", ".zip"},
}
AUTO_BROWSER_EXTENSION_FILES = [
    "manifest.json",
    "service_worker.js",
    "content_script.js",
    "popup.html",
    "popup.css",
    "popup.js",
]
AUTO_BROWSER_EXTENSION_DIR = resource_path("browser-extension")


STATUS_LABELS = {
    "uploaded": "已上传",
    "parsing": "解析中",
    "precheck_ready": "待确认",
    "precheck_failed": "预检失败",
    "confirmed": "已确认",
    "processing": "下载中",
    "discarded": "已作废",
    "pending": "等待",
    "review_ready": "已解析",
    "needs_fix": "需修正",
    "downloading": "下载中",
    "failed": "失败",
    "downloaded": "成功",
    "completed": "已完成",
    "completed_with_errors": "有失败项",
    "skipped": "已跳过",
    "manual_done": "手动完成",
}


WORK_STATE_LABELS = {
    "action_required": "需要处理",
    "running": "下载中",
    "ready": "可开始",
    "needs_confirm": "待确认",
    "discarded": "已作废",
    "blocked": "需修正",
    "complete": "已完成",
    "waiting": "处理中",
}


DOWNLOAD_ALLOWED_BATCH_STATUSES = {"confirmed", "processing", "completed_with_errors"}
DOWNLOAD_START_ALLOWED_BATCH_STATUSES = {"confirmed", "completed_with_errors"}
PRECHECK_TAB_BATCH_STATUSES = {
    "uploaded",
    "parsing",
    "precheck_ready",
    "precheck_failed",
    "needs_fix",
    "discarded",
}
DOWNLOAD_TAB_BATCH_STATUSES = {"confirmed", "processing", "completed", "completed_with_errors"}
COMPLETED_DOWNLOAD_DURATION_STATUSES = {"completed", "completed_with_errors"}
DRIVE_AUTH_LOCK = threading.Lock()
DRIVE_AUTH_STATE: dict[str, Any] = {
    "running": False,
    "command": [],
    "started_at": "",
    "completed_at": "",
    "returncode": None,
    "error": "",
    "mode": "",
}


def configured_extension_max_attempts() -> int:
    try:
        return int(os.getenv("EXTENSION_DOWNLOAD_MAX_ATTEMPTS", "4"))
    except ValueError:
        return 4


EXTENSION_MAX_ATTEMPTS = configured_extension_max_attempts()
EXTENSION_RETRYABLE_ERROR_CODES = {
    "network_error",
    "download_timeout",
    "extension_download_failed",
    "extension_download_interrupted",
    "extension_download_timeout",
    "extension_download_stalled",
    "extension_fetch_timeout",
}
EXTENSION_NON_RETRYABLE_ERROR_CODES = {
    "extension_stopped_by_user",
    "extension_non_image_download",
    "extension_google_apps_file",
    "invalid_drive_url",
    "drive_not_found_or_permission",
    "drive_permission_denied",
}


def initial_developer_credentials() -> Tuple[str, str]:
    return (
        os.getenv("DEVELOPER_USERNAME", os.getenv("ADMIN_USERNAME", "admin")),
        os.getenv(
            "DEVELOPER_PASSWORD",
            os.getenv("ADMIN_PASSWORD", os.getenv("APP_PASSWORD", "change-me")),
        ),
    )


def initial_seed_admin_credentials() -> list[Tuple[str, str]]:
    default_password = os.getenv("ADMIN_PASSWORD", os.getenv("APP_PASSWORD", "change-me"))
    return [
        (
            os.getenv("ADMIN1_USERNAME", "admin1"),
            os.getenv("ADMIN1_PASSWORD", default_password),
        ),
        (
            os.getenv("ADMIN2_USERNAME", "admin2"),
            os.getenv("ADMIN2_PASSWORD", default_password),
        ),
    ]


def create_user_if_missing(username: str, password: str, role: str) -> None:
    username = username.strip()
    if not username:
        return
    existing_user = db.get_user_by_username(username)
    if existing_user:
        if role == "developer" and existing_user["role"] != "developer":
            db.update_user_role(int(existing_user["id"]), "developer")
        return
    db.create_user(username, hash_password(password), role=role)


def ensure_initial_accounts() -> None:
    developer_username, developer_password = initial_developer_credentials()
    create_user_if_missing(developer_username, developer_password, "developer")
    for username, password in initial_seed_admin_credentials():
        create_user_if_missing(username, password, "admin")


def auto_resource_uploader() -> Optional[dict]:
    developer_username, _password = initial_developer_credentials()
    developer = db.get_user_by_username(developer_username)
    if developer and developer["role"] == "developer":
        return developer
    for user in db.list_users():
        if user["role"] == "developer" and user["status"] == "active":
            return user
    return None


def browser_extension_manifest(extension_dir: Path) -> Optional[dict[str, Any]]:
    manifest_path = extension_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def browser_extension_source_hash(extension_dir: Path) -> Optional[str]:
    digest = hashlib.sha256()
    for relative_name in AUTO_BROWSER_EXTENSION_FILES:
        path = extension_dir / relative_name
        if not path.exists() or not path.is_file():
            return None
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def build_browser_extension_zip(extension_dir: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_name in AUTO_BROWSER_EXTENSION_FILES:
            archive.write(extension_dir / relative_name, arcname=relative_name)


def publish_browser_extension_resource() -> Optional[int]:
    extension_dir = AUTO_BROWSER_EXTENSION_DIR
    if not extension_dir.exists() or not extension_dir.is_dir():
        return None
    manifest = browser_extension_manifest(extension_dir)
    source_hash = browser_extension_source_hash(extension_dir)
    uploader = auto_resource_uploader()
    if not manifest or not source_hash or not uploader:
        return None

    version = str(manifest.get("version") or "0.0.0").strip() or "0.0.0"
    filename = f"browser-extension-{version}-{source_hash}.zip"
    version_note = f"自动发布；source_hash={source_hash}"
    target_path = RESOURCES_DIR / "auto" / filename

    existing = db.get_resource_file_by_category_and_version_note(
        "browser_extension",
        version_note,
        include_disabled=True,
    )
    if existing:
        storage_path = Path(existing["storage_path"] or "")
        if not storage_path.exists() or not storage_path.is_file():
            build_browser_extension_zip(extension_dir, target_path)
            db.update_resource_file_storage(
                int(existing["id"]),
                target_path,
                target_path.stat().st_size,
            )
        db.set_resource_category_active_only("browser_extension", int(existing["id"]))
        return int(existing["id"])

    build_browser_extension_zip(extension_dir, target_path)
    resource_id = db.create_resource_file(
        f"浏览器插件 v{version}",
        "browser_extension",
        filename,
        int(uploader["id"]),
        version_note,
    )
    db.update_resource_file_storage(resource_id, target_path, target_path.stat().st_size)
    db.set_resource_category_active_only("browser_extension", resource_id)
    return resource_id


def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE) or request.headers.get("x-app-session")
    if not token:
        return None
    return db.get_user_by_session(token, utc_now_string())


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_developer(user: dict) -> None:
    if user["role"] != "developer":
        raise HTTPException(status_code=403, detail="Developer access required")


def can_access_batch(user: dict, batch: dict) -> bool:
    if user["role"] in {"developer", "admin"}:
        return True
    return batch.get("created_by_user_id") == user["id"]


def can_operate_batch(user: dict, batch: dict) -> bool:
    if user["role"] == "developer":
        return True
    return batch.get("created_by_user_id") == user["id"]


def require_batch_access(batch_id: int, user: dict) -> dict:
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if not can_access_batch(user, batch):
        raise HTTPException(status_code=403, detail="Batch access denied")
    return batch


def require_batch_operation(batch_id: int, user: dict) -> dict:
    batch = require_batch_access(batch_id, user)
    if not can_operate_batch(user, batch):
        raise HTTPException(status_code=403, detail="Batch operation denied")
    return batch


def template_context(user: dict, **extra):
    context = {
        "current_user": user,
        "role_labels": ROLE_LABELS,
        "resource_category_labels": RESOURCE_CATEGORY_LABELS,
    }
    context.update(extra)
    return context


def format_file_size(size: int) -> str:
    size = max(0, int(size or 0))
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def grouped_resource_files(resources: list[dict]) -> dict[str, list[dict]]:
    return {
        category: [row for row in resources if row["category"] == category]
        for category in RESOURCE_CATEGORY_LABELS
    }


def validate_resource_upload(category: str, filename: str) -> None:
    if category not in RESOURCE_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Invalid resource category")
    suffix = Path(filename).suffix.lower()
    if suffix not in RESOURCE_ALLOWED_EXTENSIONS[category]:
        allowed = ", ".join(sorted(RESOURCE_ALLOWED_EXTENSIONS[category]))
        raise HTTPException(status_code=400, detail=f"该分类只允许上传：{allowed}")


def save_resource_file(resource_id: int, filename: str, content: bytes) -> Path:
    resource_dir = RESOURCES_DIR / str(resource_id)
    resource_dir.mkdir(parents=True, exist_ok=True)
    target = resource_dir / safe_filename(filename)
    target.write_bytes(content)
    return target


def batch_display_name(batch: dict) -> str:
    business_date = batch.get("business_date")
    sequence = batch.get("user_daily_sequence")
    if business_date and sequence:
        return f"订单-{business_date}-{int(sequence):03d}"
    created_at = str(batch.get("created_at") or "")
    fallback_date = created_at[:10].replace("-", "") if len(created_at) >= 10 else "unknown"
    return f"订单-{fallback_date}-{int(batch['id']):03d}"


def enrich_batch_identity(batch: dict) -> dict:
    return {
        **batch,
        "display_name": batch_display_name(batch),
        "owner_label": batch.get("created_by_username") or "历史批次",
    }


def batch_work_state(batch: dict, counts: dict[str, int]) -> dict[str, str]:
    pending_or_failed = int(counts["pending"]) + int(counts["failed"])
    if batch["status"] == "discarded":
        return {
            "code": "discarded",
            "label": WORK_STATE_LABELS["discarded"],
            "next_action": "已作废，仅保留上传记录",
        }
    if batch["status"] == "precheck_ready":
        return {
            "code": "needs_confirm",
            "label": WORK_STATE_LABELS["needs_confirm"],
            "next_action": "人工确认后导入正式订单批次",
        }
    if batch["status"] in {"precheck_failed", "needs_fix"}:
        return {
            "code": "blocked",
            "label": WORK_STATE_LABELS["blocked"],
            "next_action": "修正表格后重新上传",
        }
    if batch["status"] in {"parsing", "uploaded", "pending"}:
        return {
            "code": "waiting",
            "label": WORK_STATE_LABELS["waiting"],
            "next_action": "等待解析完成",
        }
    if int(counts["downloading"]) > 0 or batch["status"] in {"processing", "downloading"}:
        return {
            "code": "running",
            "label": WORK_STATE_LABELS["running"],
            "next_action": "查看进度或停止本批次",
        }
    if int(counts["failed"]) > 0:
        return {
            "code": "action_required",
            "label": WORK_STATE_LABELS["action_required"],
            "next_action": "重试失败项或标记已处理",
        }
    if pending_or_failed > 0 and batch["status"] in {"confirmed", "review_ready"}:
        return {
            "code": "ready",
            "label": WORK_STATE_LABELS["ready"],
            "next_action": "开始下载图片",
        }
    return {
        "code": "complete",
        "label": WORK_STATE_LABELS["complete"],
        "next_action": "下载批次 ZIP 或归档",
    }


def enrich_batch_work_queue(batches: list[dict]) -> list[dict]:
    priority = {
        "needs_confirm": 1,
        "action_required": 1,
        "running": 2,
        "ready": 3,
        "blocked": 4,
        "waiting": 5,
        "complete": 6,
        "discarded": 7,
    }
    enriched = []
    for batch in batches:
        counts = db.get_batch_status_counts(int(batch["id"]))
        work_state = batch_work_state(batch, counts)
        row = {
            **enrich_batch_identity(batch),
            "status_label": STATUS_LABELS.get(batch["status"], batch["status"]),
            "status_counts": counts,
            "work_state": work_state,
            "pending_or_failed_count": int(counts["pending"]) + int(counts["failed"]),
            "handled_count": int(counts["downloaded"]) + int(counts["manual_done"]),
            "queue_group": batch_queue_group(batch),
        }
        enriched.append(row)
    return sorted(
        enriched,
        key=lambda row: (
            priority.get(row["work_state"]["code"], 99),
            -int(row["id"]),
        ),
    )


def batch_queue_group(batch: dict) -> str:
    status = batch["status"]
    if status in {"uploaded", "parsing", "precheck_ready", "precheck_failed"}:
        return "precheck"
    if status == "discarded":
        return "discarded"
    return "formal"


def group_batches_for_index(batches: list[dict]) -> dict[str, list[dict]]:
    return {
        "precheck": [batch for batch in batches if batch["queue_group"] == "precheck"],
        "formal": [batch for batch in batches if batch["queue_group"] == "formal"],
        "discarded": [batch for batch in batches if batch["queue_group"] == "discarded"],
    }


def list_batches_for_scope(user: dict, scope: str = "my") -> tuple[list[dict], str]:
    normalized_scope = scope if scope in {"my", "team"} else "my"
    if user["role"] not in {"developer", "admin"}:
        normalized_scope = "my"
    if normalized_scope == "team":
        batches = db.list_batches_for_user(user)
    else:
        batches = [
            batch
            for batch in db.list_batches_for_user(user)
            if batch.get("created_by_user_id") == user["id"]
        ]
    return batches, normalized_scope


def recent_precheck_batches_for_user(user: dict, limit: int = 3) -> list[dict]:
    own_batches, _scope = list_batches_for_scope(user, "my")
    batches = enrich_batch_work_queue(own_batches)
    precheck_batches = [batch for batch in batches if batch["queue_group"] == "precheck"]
    return sorted(precheck_batches, key=lambda batch: int(batch["id"]), reverse=True)[:limit]


def usage_rows_for_user(user: dict) -> list[dict]:
    rows = db.list_download_usage_by_user()
    if user["role"] in {"developer", "admin"}:
        return rows
    return [row for row in rows if row.get("user_id") == user["id"]]


def batch_detail_tab(batch: dict, requested_tab: str = "") -> dict[str, Any]:
    can_show_download_tab = batch["status"] in DOWNLOAD_TAB_BATCH_STATUSES
    default_tab = "download" if can_show_download_tab else "precheck"
    active_tab = requested_tab if requested_tab in {"precheck", "download"} else default_tab
    if active_tab == "download" and not can_show_download_tab:
        active_tab = "precheck"
    return {
        "active_tab": active_tab,
        "can_show_download_tab": can_show_download_tab,
    }


def batch_download_duration_rows_for_user(user: dict) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in COMPLETED_DOWNLOAD_DURATION_STATUSES)
    params: list[Any] = sorted(COMPLETED_DOWNLOAD_DURATION_STATUSES)
    user_clause = ""
    if user["role"] not in {"developer", "admin"}:
        user_clause = "AND ib.created_by_user_id = ?"
        params.append(int(user["id"]))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                ib.id,
                ib.file_name,
                ib.download_name,
                ib.status,
                ib.created_at,
                ib.business_date,
                ib.user_daily_sequence,
                ib.created_by_user_id,
                COALESCE(u.username, '历史批次') AS username,
                MIN(di.started_at) AS started_at,
                MAX(di.completed_at) AS completed_at
            FROM import_batches ib
            JOIN download_items di ON di.batch_id = ib.id
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            WHERE ib.status IN ({placeholders})
              AND di.started_at IS NOT NULL
              AND di.completed_at IS NOT NULL
              {user_clause}
            GROUP BY ib.id
            ORDER BY completed_at DESC, ib.id DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
    duration_rows = []
    for row in rows:
        item = db.row_to_dict(row)
        started_at = parse_db_timestamp(item.get("started_at"))
        completed_at = parse_db_timestamp(item.get("completed_at"))
        if not started_at or not completed_at:
            continue
        duration_seconds = int((completed_at - started_at).total_seconds())
        if duration_seconds < 0:
            continue
        duration_rows.append(
            {
                **item,
                "display_name": batch_display_name(item),
                "duration_label": format_duration_seconds(duration_seconds),
            }
        )
    return duration_rows


def sort_rows_by_excel_row(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: int(row["item"].get("row_number") or 0))


def normalized_limit(limit: int) -> Optional[int]:
    return limit if limit > 0 else None


def cleaned_download_name(download_name: str) -> str:
    if not download_name.strip():
        raise HTTPException(status_code=400, detail="下载文件夹名不能为空。")
    cleaned_name = safe_filename(download_name).strip(" .")
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="下载文件夹名不能为空。")
    return cleaned_name


def current_quota_month() -> str:
    return datetime.now().strftime("%Y-%m")


def normalize_quota_month(value: str) -> str:
    value = (value or "").strip() or current_quota_month()
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="月份格式必须是 YYYY-MM") from exc
    return value


def monthly_quota_context(user: dict, quota_month: str) -> dict[str, Any]:
    quota = db.get_monthly_team_quota(quota_month)
    usage_rows = db.list_monthly_quota_usage_by_user(quota_month)
    visible_usage_rows = (
        usage_rows
        if user["role"] in {"developer", "admin"}
        else [row for row in usage_rows if row.get("user_id") == user["id"]]
    )
    totals = db.monthly_quota_totals(quota, usage_rows)
    quota_percent = 0
    if int(totals["total_quota"]) > 0:
        quota_percent = min(
            100,
            round(int(totals["used_count"]) * 100 / int(totals["total_quota"])),
        )
    return {
        "quota_month": quota_month,
        "quota": quota,
        "quota_usage_rows": visible_usage_rows,
        "quota_totals": totals,
        "quota_percent": quota_percent,
        "unassigned_success_count": (
            db.count_unassigned_successful_downloads()
            if user["role"] == "developer"
            else 0
        ),
    }


def parse_db_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def format_duration_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def enrich_download_task_duration(task: dict, now: Optional[datetime] = None) -> None:
    started_at = parse_db_timestamp(task.get("download_started_at"))
    completed_at = parse_db_timestamp(task.get("download_completed_at"))
    if not started_at:
        task["download_duration_label"] = None
        return
    end_at = completed_at or now or datetime.utcnow()
    prefix = "已用时" if task.get("download_status") == "downloading" else "耗时"
    task["download_duration_label"] = (
        f"{prefix} {format_duration_seconds(int((end_at - started_at).total_seconds()))}"
    )


def enrich_order_download_durations(orders: list[dict]) -> None:
    now = datetime.utcnow()
    for order in orders:
        for item in order["items"]:
            for task in item.get("download_items", []):
                enrich_download_task_duration(task, now)


def current_download_task(display_rows: list[dict]) -> Optional[dict]:
    for row in display_rows:
        for task in row["item"].get("download_items", []):
            if task.get("download_status") == "downloading":
                return {
                    "download_item_id": task.get("download_item_id"),
                    "sku": row["item"].get("sku") or "-",
                    "source_type": task.get("source_type") or "design",
                    "duration_label": task.get("download_duration_label"),
                }
    return None


def current_download_task_from_orders(orders: list[dict]) -> Optional[dict]:
    for order in orders:
        for item in order.get("items", []):
            for task in item.get("download_items", []):
                if task.get("download_status") == "downloading":
                    return {
                        "download_item_id": task.get("download_item_id"),
                        "sku": item.get("sku") or "-",
                        "source_type": task.get("source_type") or "design",
                        "duration_label": task.get("download_duration_label"),
                    }
    return None


def batch_download_actions(batch: dict, counts: dict[str, int]) -> dict[str, bool]:
    has_pending_work = int(counts["pending"]) + int(counts["failed"]) > 0
    has_downloading = int(counts["downloading"]) > 0
    can_start = (
        batch["status"] in DOWNLOAD_START_ALLOWED_BATCH_STATUSES
        and has_pending_work
        and not has_downloading
    )
    return {
        "can_start_local_download": can_start,
        "can_start_extension": can_start,
        "can_retry_failed": (
            batch["status"] in DOWNLOAD_START_ALLOWED_BATCH_STATUSES
            and int(counts["failed"]) > 0
            and not has_downloading
        ),
        "can_refresh": True,
    }


def server_download_controls(batch: dict, counts: dict[str, int], handled_count: int) -> dict[str, bool]:
    has_downloading = int(counts["downloading"]) > 0
    is_running = batch["status"] == "processing" or has_downloading
    can_start_or_continue = (
        batch["status"] in DOWNLOAD_START_ALLOWED_BATCH_STATUSES
        and int(counts["pending"]) > 0
        and handled_count < int(batch["link_count"] or 0)
        and not has_downloading
    )
    can_retry_failed = (
        batch["status"] in DOWNLOAD_START_ALLOWED_BATCH_STATUSES
        and int(counts["failed"]) > 0
        and not has_downloading
    )
    return {
        "is_running": is_running,
        "can_start_or_continue": can_start_or_continue,
        "can_pause": is_running,
        "can_retry_failed": can_retry_failed,
        "is_pausing": bool(int(batch.get("server_stop_requested") or 0)),
    }


def local_drive_health(timeout_seconds: int = 15) -> dict[str, Any]:
    backend = drive_download_backend()
    configured_bin = rclone_bin()
    oauth_config = drive_oauth_config()
    proxy_config = rclone_proxy_config()
    configured_path = Path(configured_bin)
    resolved_bin = (
        str(configured_path)
        if configured_path.exists()
        else shutil.which(configured_bin)
    )
    remotes = rclone_drive_remotes()
    primary_remote = remotes[0] if remotes else "gdrive"
    health: dict[str, Any] = {
        "download_backend": backend,
        "rclone_bin": configured_bin,
        "rclone_path": resolved_bin or "",
        "rclone_installed": bool(resolved_bin),
        "remote_name": primary_remote,
        "remote_configured": False,
        "remote_accessible": False,
        "oauth_client_configured": oauth_config["configured"],
        "oauth_client_source": oauth_config["source"],
        "oauth_client_id": oauth_config["client_id_masked"],
        "proxy_enabled": proxy_config["enabled"],
        "proxy_url": proxy_config["url"],
        "proxy_source": proxy_config["source"],
        "proxy_source_label": proxy_config["source_label"],
        "ok": False,
        "error": "",
        "warning": "" if oauth_config["configured"] else "当前未配置公司 Google OAuth client，会回退使用 rclone 共享 client；该共享 client 在 2026 年有中断风险。",
    }
    if backend != "rclone":
        health["error"] = "当前下载后端不是 rclone。"
        return health
    if not resolved_bin:
        locations = "；".join(str(path) for path in rclone_search_locations())
        health["error"] = f"未找到 rclone 程序：{configured_bin}。已检查：{locations}"
        return health

    try:
        remotes_result = subprocess.run(
            [resolved_bin, "listremotes"],
            capture_output=True,
            env=rclone_subprocess_env(),
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        health["error"] = f"无法读取 rclone remote：{exc}"
        return health

    configured_remotes = {
        line.strip().rstrip(":")
        for line in remotes_result.stdout.splitlines()
        if line.strip()
    }
    health["remote_configured"] = primary_remote in configured_remotes
    if not health["remote_configured"]:
        stderr = remotes_result.stderr.strip()
        health["error"] = stderr or f"rclone remote `{primary_remote}` 尚未配置。"
        return health

    try:
        about_result = subprocess.run(
            [resolved_bin, "about", f"{primary_remote}:"],
            capture_output=True,
            env=rclone_subprocess_env(),
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        health["error"] = f"无法检测 Drive 授权：{exc}"
        return health

    health["remote_accessible"] = about_result.returncode == 0
    health["ok"] = bool(health["remote_accessible"])
    if not health["ok"]:
        health["error"] = about_result.stderr.strip() or about_result.stdout.strip() or "Drive 授权检测失败。"
    return health


def drive_auth_state() -> dict[str, Any]:
    with DRIVE_AUTH_LOCK:
        return {
            **DRIVE_AUTH_STATE,
            "command": list(DRIVE_AUTH_STATE.get("command") or []),
        }


def update_drive_auth_state(**patch: Any) -> None:
    with DRIVE_AUTH_LOCK:
        DRIVE_AUTH_STATE.update(patch)


def masked_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "..." + value[-2:]
    return value[:6] + "..." + value[-4:]


def drive_oauth_config() -> dict[str, Any]:
    settings = load_local_settings()
    client_id = os.getenv("RCLONE_DRIVE_CLIENT_ID", "").strip()
    client_secret = os.getenv("RCLONE_DRIVE_CLIENT_SECRET", "").strip()
    source = "environment" if client_id or client_secret else ""
    if not client_id:
        client_id = str(settings.get("rclone_drive_client_id") or "").strip()
        source = "local_settings" if client_id else source
    if not client_secret:
        client_secret = str(settings.get("rclone_drive_client_secret") or "").strip()
        source = "local_settings" if client_secret and not source else source
    configured = bool(client_id and client_secret)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_masked": masked_secret(client_id),
        "client_secret_masked": masked_secret(client_secret),
        "configured": configured,
        "source": source or "rclone_shared",
        "source_label": {
            "environment": "环境变量",
            "local_settings": "本地配置文件",
            "rclone_shared": "rclone 共享 client",
        }.get(source or "rclone_shared", source or "rclone 共享 client"),
    }


def save_proxy_settings(enabled: bool, proxy_url: str) -> None:
    settings = load_local_settings()
    settings["proxy_enabled"] = enabled
    settings["proxy_url"] = proxy_url.strip() or DEFAULT_PROXY_URL
    save_local_settings(settings)


def test_proxy_connection(timeout_seconds: int = 10) -> dict[str, Any]:
    proxy_config = rclone_proxy_config()
    proxy_url = proxy_config["url"] if proxy_config["enabled"] else ""
    handlers = [ProxyHandler({"http": proxy_url, "https": proxy_url})] if proxy_url else [ProxyHandler({})]
    opener = build_opener(*handlers)
    request = UrlRequest("https://oauth2.googleapis.com/token", method="GET")
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return {
                "ok": True,
                "status": response.status,
                "error": "",
                "proxy": proxy_config,
            }
    except HTTPError as exc:
        return {
            "ok": True,
            "status": exc.code,
            "error": "",
            "proxy": proxy_config,
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": 0,
            "error": str(exc),
            "proxy": proxy_config,
        }


DOWNLOAD_SETTING_KEYS = {
    "drive_download_timeout_seconds",
    "drive_item_retry_backoff_seconds",
    "drive_download_delay_seconds",
    "max_image_file_size_mb",
    "min_free_disk_space_mb",
    "rclone_transfers",
    "rclone_checkers",
    "printerval_curl_timeout_seconds",
    "printerval_playwright_timeout_seconds",
    "printerval_playwright_enabled",
}


def download_settings_context() -> dict[str, Any]:
    values = {
        "drive_download_timeout_seconds": drive_download_timeout_seconds(),
        "drive_item_retry_backoff_seconds": ",".join(str(value) for value in configured_retry_backoff_seconds()),
        "drive_download_delay_seconds": configured_download_delay_seconds(),
        "max_image_file_size_mb": max_image_file_size_mb(),
        "min_free_disk_space_mb": min_free_disk_space_mb(),
        "rclone_transfers": rclone_transfers(),
        "rclone_checkers": rclone_checkers(),
        "printerval_curl_timeout_seconds": printerval_curl_timeout_seconds(),
        "printerval_playwright_timeout_seconds": printerval_playwright_timeout_seconds(),
        "printerval_playwright_enabled": printerval_playwright_enabled(),
    }
    sources = {
        "drive_download_timeout_seconds": env_or_setting(
            os.getenv("DRIVE_DOWNLOAD_TIMEOUT_SECONDS", ""),
            "drive_download_timeout_seconds",
            DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        )[1],
        "drive_item_retry_backoff_seconds": env_or_setting(
            os.getenv("DRIVE_ITEM_RETRY_BACKOFF_SECONDS", ""),
            "drive_item_retry_backoff_seconds",
            "30,90",
        )[1],
        "drive_download_delay_seconds": env_or_setting(
            os.getenv("DRIVE_DOWNLOAD_DELAY_SECONDS", ""),
            "drive_download_delay_seconds",
            8,
        )[1],
        "max_image_file_size_mb": env_or_setting(
            os.getenv("MAX_IMAGE_FILE_SIZE_MB", ""),
            "max_image_file_size_mb",
            DEFAULT_MAX_IMAGE_FILE_SIZE_MB,
        )[1],
        "min_free_disk_space_mb": env_or_setting(
            os.getenv("MIN_FREE_DISK_SPACE_MB", ""),
            "min_free_disk_space_mb",
            DEFAULT_MIN_FREE_DISK_SPACE_MB,
        )[1],
        "rclone_transfers": env_or_setting(os.getenv("RCLONE_TRANSFERS", ""), "rclone_transfers", "1")[1],
        "rclone_checkers": env_or_setting(os.getenv("RCLONE_CHECKERS", ""), "rclone_checkers", "1")[1],
        "printerval_curl_timeout_seconds": env_or_setting(
            os.getenv("PRINTERVAL_CURL_TIMEOUT_SECONDS", ""),
            "printerval_curl_timeout_seconds",
            DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS,
        )[1],
        "printerval_playwright_timeout_seconds": env_or_setting(
            os.getenv("PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS", ""),
            "printerval_playwright_timeout_seconds",
            DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS,
        )[1],
        "printerval_playwright_enabled": env_or_setting(
            os.getenv("PRINTERVAL_PLAYWRIGHT_ENABLED", ""),
            "printerval_playwright_enabled",
            True,
        )[1],
    }
    source_labels = {
        "environment": "环境变量",
        "local_settings": "本地配置",
        "default": "默认值",
    }
    return {
        "values": values,
        "sources": sources,
        "source_labels": source_labels,
    }


def normalized_positive_int(value: str, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def normalized_backoffs(value: str) -> str:
    parts = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed = int(part)
        except ValueError:
            continue
        if parsed >= 0:
            parts.append(str(parsed))
    return ",".join(parts) if parts else "30,90"


def save_download_settings(form: dict[str, Any]) -> None:
    settings = load_local_settings()
    settings.update(
        {
            "drive_download_timeout_seconds": normalized_positive_int(
                str(form.get("drive_download_timeout_seconds", "")),
                DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
            ),
            "drive_item_retry_backoff_seconds": normalized_backoffs(
                str(form.get("drive_item_retry_backoff_seconds", ""))
            ),
            "drive_download_delay_seconds": normalized_positive_int(
                str(form.get("drive_download_delay_seconds", "")),
                8,
                0,
            ),
            "max_image_file_size_mb": normalized_positive_int(
                str(form.get("max_image_file_size_mb", "")),
                DEFAULT_MAX_IMAGE_FILE_SIZE_MB,
            ),
            "min_free_disk_space_mb": normalized_positive_int(
                str(form.get("min_free_disk_space_mb", "")),
                DEFAULT_MIN_FREE_DISK_SPACE_MB,
                0,
            ),
            "rclone_transfers": str(
                normalized_positive_int(str(form.get("rclone_transfers", "")), 1)
            ),
            "rclone_checkers": str(
                normalized_positive_int(str(form.get("rclone_checkers", "")), 1)
            ),
            "printerval_curl_timeout_seconds": normalized_positive_int(
                str(form.get("printerval_curl_timeout_seconds", "")),
                DEFAULT_PRINTERVAL_CURL_TIMEOUT_SECONDS,
            ),
            "printerval_playwright_timeout_seconds": normalized_positive_int(
                str(form.get("printerval_playwright_timeout_seconds", "")),
                DEFAULT_PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS,
            ),
            "printerval_playwright_enabled": str(form.get("printerval_playwright_enabled", "0")) == "1",
        }
    )
    save_local_settings(settings)


def reset_download_settings() -> None:
    settings = load_local_settings()
    for key in DOWNLOAD_SETTING_KEYS:
        settings.pop(key, None)
    save_local_settings(settings)


def rclone_drive_oauth_config_options() -> list[str]:
    options = [
        "scope",
        "drive.readonly",
        "config_is_local",
        "true",
    ]
    oauth_config = drive_oauth_config()
    client_id = oauth_config["client_id"]
    client_secret = oauth_config["client_secret"]
    if client_id:
        options.extend(["client_id", client_id])
    if client_secret:
        options.extend(["client_secret", client_secret])
    return options


def rclone_update_command(rclone_exe: str, remote_name: str) -> list[str]:
    return [
        str(rclone_exe),
        "config",
        "update",
        remote_name,
        *rclone_drive_oauth_config_options(),
    ]


def rclone_reconnect_command(rclone_exe: str, remote_name: str) -> list[str]:
    return [str(rclone_exe), "config", "reconnect", f"{remote_name}:"]


def rclone_auth_command(health: dict[str, Any]) -> tuple[str, list[str]]:
    rclone_exe = health.get("rclone_path") or health.get("rclone_bin") or rclone_bin()
    remote_name = str(health.get("remote_name") or "gdrive").rstrip(":")
    if health.get("remote_configured"):
        return "reconnect", rclone_reconnect_command(str(rclone_exe), remote_name)
    return (
        "create",
        [
            str(rclone_exe),
            "config",
            "create",
            remote_name,
            "drive",
            *rclone_drive_oauth_config_options(),
        ],
    )


def should_retry_drive_auth_with_reconnect(health: dict[str, Any]) -> bool:
    error = str(health.get("error") or "").lower()
    return bool(health.get("remote_configured")) and (
        "empty token" in error
        or "please run" in error and "config reconnect" in error
        or not health.get("remote_accessible")
    )


def has_empty_drive_token_error(health: dict[str, Any]) -> bool:
    error = str(health.get("error") or "").lower()
    return "empty token" in error and "config reconnect" in error


def run_drive_auth_command(mode: str, command: list[str]) -> None:
    completed: subprocess.CompletedProcess[Any]
    if mode == "reconnect" and drive_oauth_config()["configured"] and len(command) >= 4:
        rclone_exe = command[0]
        remote_name = str(command[3]).rstrip(":")
        update_command = rclone_update_command(rclone_exe, remote_name)
        update_drive_auth_state(command=update_command, mode="update")
        try:
            completed = subprocess.run(update_command, cwd=Path.cwd(), env=rclone_subprocess_env(), check=False)
        except OSError as exc:
            update_drive_auth_state(
                running=False,
                completed_at=utc_now_string(),
                returncode=-1,
                error=str(exc),
            )
            return
        if completed.returncode != 0:
            update_drive_auth_state(
                running=False,
                completed_at=utc_now_string(),
                returncode=completed.returncode,
                error=f"rclone OAuth 配置更新失败，退出码 {completed.returncode}",
            )
            return
        update_drive_auth_state(command=command, mode="reconnect")
    try:
        completed = subprocess.run(command, cwd=Path.cwd(), env=rclone_subprocess_env(), check=False)
    except OSError as exc:
        update_drive_auth_state(
            running=False,
            completed_at=utc_now_string(),
            returncode=-1,
            error=str(exc),
        )
        return
    health = local_drive_health()
    if mode == "create" and should_retry_drive_auth_with_reconnect(health):
        rclone_exe = health.get("rclone_path") or health.get("rclone_bin") or rclone_bin()
        remote_name = str(health.get("remote_name") or "gdrive").rstrip(":")
        reconnect_command = rclone_reconnect_command(str(rclone_exe), remote_name)
        update_drive_auth_state(command=reconnect_command, mode="reconnect")
        try:
            completed = subprocess.run(reconnect_command, cwd=Path.cwd(), env=rclone_subprocess_env(), check=False)
        except OSError as exc:
            update_drive_auth_state(
                running=False,
                completed_at=utc_now_string(),
                returncode=-1,
                error=str(exc),
            )
            return
        health = local_drive_health()
    error = ""
    if completed.returncode != 0:
        error = f"rclone 授权命令退出码 {completed.returncode}"
    elif not health.get("ok"):
        error = str(health.get("error") or "授权完成后仍无法访问 Google Drive。")
    update_drive_auth_state(
        running=False,
        completed_at=utc_now_string(),
        returncode=completed.returncode,
        error=error,
    )


def start_drive_auth() -> dict[str, Any]:
    health = local_drive_health(timeout_seconds=5)
    if not health["rclone_installed"]:
        raise HTTPException(status_code=400, detail=health["error"] or "未找到 rclone。")
    state = drive_auth_state()
    if state["running"]:
        return state
    if has_empty_drive_token_error(health):
        reset_drive_remote(health)
        health = local_drive_health(timeout_seconds=5)
    mode, command = rclone_auth_command(health)
    update_drive_auth_state(
        running=True,
        command=command,
        started_at=utc_now_string(),
        completed_at="",
        returncode=None,
        error="",
        mode=mode,
    )
    worker = threading.Thread(
        target=run_drive_auth_command,
        args=(mode, command),
        daemon=True,
    )
    worker.start()
    return drive_auth_state()


def reset_drive_remote(health: Optional[dict[str, Any]] = None) -> None:
    health = health or local_drive_health(timeout_seconds=5)
    if not health["rclone_installed"]:
        raise HTTPException(status_code=400, detail=health["error"] or "未找到 rclone。")
    state = drive_auth_state()
    if state["running"]:
        raise HTTPException(status_code=400, detail="Google Drive 授权正在运行，完成后再重建。")
    remote_name = str(health.get("remote_name") or "gdrive").rstrip(":")
    if not health["remote_configured"]:
        return
    try:
        completed = subprocess.run(
            [health["rclone_path"], "config", "delete", remote_name],
            cwd=Path.cwd(),
            capture_output=True,
            env=rclone_subprocess_env(),
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=400, detail=f"rclone 删除 remote 失败：{exc}") from exc
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or f"rclone 删除 remote 失败，退出码 {completed.returncode}"
        raise HTTPException(status_code=400, detail=error)


def batch_primary_action(batch: dict, counts: dict[str, int]) -> dict[str, str]:
    work_state = batch_work_state(batch, counts)
    actions = batch_download_actions(batch, counts)
    if work_state["code"] == "needs_confirm":
        title = "上传预检已完成"
        body = "确认订单、SKU 和链接无误后，再导入为正式订单批次。确认前不会进入下载队列和正式统计。"
        cta = "确认导入"
    elif work_state["code"] == "discarded":
        title = "上传记录已作废"
        body = "该记录仅用于追溯上传错误，不会进入正式订单批次、下载队列或统计。"
        cta = "返回批次列表"
    elif work_state["code"] == "blocked":
        title = "导入预检未通过"
        body = "补齐 SKU 和 Design Link 后重新上传。"
        cta = "查看预检问题"
    elif work_state["code"] == "running":
        title = "正在下载素材"
        body = "本机正在处理当前批次。下载中可暂停本轮任务，其他处理动作会暂时禁用。"
        cta = "查看当前下载"
    elif actions["can_retry_failed"]:
        title = "有失败项需要处理"
        body = "失败项已集中列在下方。可以重试失败项，或确认已人工处理后标记完成。"
        cta = "处理失败项"
    elif actions["can_start_local_download"]:
        title = "可以下载图片"
        body = "开始下载当前批次的图片。"
        cta = "开始下载图片"
    elif work_state["code"] == "complete":
        title = "批次已完成"
        body = "当前没有待处理下载项。可以下载 ZIP 或查看明细。"
        cta = "下载批次 ZIP"
    else:
        title = "批次处理中"
        body = "系统正在处理导入或状态恢复，请稍后刷新。"
        cta = "刷新状态"
    return {
        **work_state,
        "title": title,
        "body": body,
        "cta": cta,
    }


def refresh_batch_status_after_extension_update(batch_id: int) -> None:
    db.refresh_batch_counts(batch_id)
    batch = db.get_batch(batch_id)
    if not batch or batch["status"] not in DOWNLOAD_ALLOWED_BATCH_STATUSES:
        return
    counts = db.get_batch_status_counts(batch_id)
    if counts["downloading"] > 0:
        db.update_batch_status(batch_id, "processing")
    elif counts["failed"] > 0:
        db.update_batch_status(batch_id, "completed_with_errors")
    elif counts["pending"] > 0:
        db.update_batch_status(batch_id, "confirmed")
    else:
        db.update_batch_status(batch_id, "completed")


def recover_stale_extension_downloads(batch_id: int) -> int:
    recovered = db.recover_stale_downloading_items(batch_id)
    if recovered:
        refresh_batch_status_after_extension_update(batch_id)
    return recovered


def drive_resource_payload(url: str) -> dict[str, str]:
    try:
        resource = parse_drive_resource(url)
    except ValueError:
        return {"resource_kind": "url", "resource_id": ""}
    return {"resource_kind": resource.kind, "resource_id": resource.resource_id}


def extension_download_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    source_type = item.get("source_type") or "design"
    sku = item.get("item_sku") or item.get("sku") or f"row-{item['row_number']}"
    download_name = item.get("batch_download_name") or f"batch-{item['batch_id']}"
    folder = f"auto-download/{safe_filename(str(download_name))}/{safe_filename(str(sku))}"
    payload = {
        "download_item_id": int(item["id"]),
        "batch_id": int(item["batch_id"]),
        "order_id": int(item["order_id"]),
        "order_no": item["order_no"],
        "row_number": int(item["row_number"]),
        "sku": sku,
        "sku_folder": folder,
        "source_type": source_type,
        "url": item["design_link"],
        "filename_prefix": f"{source_type}-{item['id']}-",
        "status": item["status"],
        "attempt_count": int(item.get("attempt_count") or 0),
        "max_attempts": extension_max_attempts(),
    }
    payload.update(drive_resource_payload(str(item["design_link"])))
    return payload


def request_json_dict(body: Any) -> dict[str, Any]:
    return body if isinstance(body, dict) else {}


def extension_max_attempts() -> int:
    return max(1, EXTENSION_MAX_ATTEMPTS)


def normalize_extension_raw_error(data: dict[str, Any]) -> tuple[str, str, str]:
    raw_code = str(
        data.get("raw_error_code")
        or data.get("error_code")
        or "extension_download_failed"
    )
    raw_message = str(
        data.get("raw_error_message")
        or data.get("error_message")
        or "浏览器插件下载失败。"
    )
    raw_detail = str(
        data.get("raw_error_detail")
        or data.get("error_detail")
        or raw_message
    )
    return raw_code, raw_message, raw_detail


def classify_extension_failure(data: dict[str, Any]) -> dict[str, str | bool]:
    raw_code, raw_message, raw_detail = normalize_extension_raw_error(data)
    combined = f"{raw_code} {raw_message} {raw_detail}".lower()
    if raw_code == "extension_stopped_by_user" or "用户停止" in combined:
        code = "extension_stopped_by_user"
        message = "用户停止了插件下载。"
    elif raw_code in {"extension_non_image_download", "extension_google_apps_file"}:
        code = raw_code
        message = (
            "链接指向 Google 在线文件，不是原始图片。"
            if raw_code == "extension_google_apps_file"
            else "插件下载到了非图片文件。"
        )
    elif "google apps" in combined or "application/vnd.google-apps" in combined:
        code = "extension_google_apps_file"
        message = "链接指向 Google 在线文件，不是原始图片。"
    elif "non_image" in combined or "不是图片" in combined or "html" in combined:
        code = "extension_non_image_download"
        message = "插件下载到了非图片文件。"
    elif "404" in combined or "403" in combined or "permission" in combined or "权限" in combined:
        code = "drive_not_found_or_permission"
        message = "Drive 文件不存在或当前 Google 账号没有访问权限。"
    elif "timeout" in combined or "stalled" in combined or "超时" in combined:
        code = "download_timeout"
        message = "浏览器下载超时或长时间没有进展。"
    elif "network" in combined or "server_unreachable" in combined:
        code = "network_error"
        message = "浏览器网络连接失败，可稍后重试。"
    else:
        code = raw_code if raw_code else "extension_download_failed"
        message = raw_message or "浏览器插件下载失败。"
    retryable = code in EXTENSION_RETRYABLE_ERROR_CODES and code not in EXTENSION_NON_RETRYABLE_ERROR_CODES
    return {
        "code": code,
        "message": message,
        "detail": raw_detail,
        "retryable": retryable,
    }


def should_retry_extension_failure(item: dict, classified: dict[str, str | bool]) -> bool:
    if not bool(classified["retryable"]):
        return False
    return int(item.get("attempt_count") or 0) < extension_max_attempts()


def require_download_allowed(batch: dict) -> None:
    if batch["status"] not in DOWNLOAD_ALLOWED_BATCH_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="该批次尚未确认导入，不能开始下载。",
        )


def require_download_start_allowed(batch: dict) -> None:
    if batch["status"] not in DOWNLOAD_START_ALLOWED_BATCH_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="该批次尚未确认导入，或当前已有下载处理中。",
        )


@app.on_event("startup")
def startup() -> None:
    ensure_data_dirs()
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    ensure_initial_accounts()
    publish_browser_extension_resource()
    db.delete_expired_sessions(utc_now_string())


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "username": ""},
    )


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    user = db.get_user_by_username(username)
    if (
        not user
        or user["status"] != "active"
        or not verify_password(password, user["password_hash"])
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "用户名或密码不正确", "username": username},
            status_code=401,
        )
    token = new_session_token()
    db.create_session(token, int(user["id"]), session_expiry_string())
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/")
def index(request: Request):
    require_user(request)
    return RedirectResponse("/uploads/new", status_code=303)


@app.get("/batches")
def batches_page(request: Request, scope: str = "my"):
    user = require_user(request)
    raw_batches, active_scope = list_batches_for_scope(user, scope)
    batches = enrich_batch_work_queue(raw_batches)
    can_view_team = user["role"] in {"developer", "admin"}
    return templates.TemplateResponse(
        request=request,
        name="batches.html",
        context=template_context(
            user,
            batches=batches,
            batch_groups=group_batches_for_index(batches),
            active_scope=active_scope,
            can_view_team=can_view_team,
        ),
    )


@app.get("/stats")
def stats_page(request: Request):
    user = require_user(request)
    usage_rows = usage_rows_for_user(user)
    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context=template_context(
            user,
            usage_rows=usage_rows,
            usage_totals=db.download_usage_totals(usage_rows),
            batch_duration_rows=batch_download_duration_rows_for_user(user),
        ),
    )


@app.get("/quota")
def quota_page(request: Request, month: str = ""):
    user = require_user(request)
    require_developer(user)
    quota_month = normalize_quota_month(month)
    return templates.TemplateResponse(
        request=request,
        name="quota.html",
        context=template_context(
            user,
            **monthly_quota_context(user, quota_month),
        ),
    )


@app.get("/settings/drive")
def drive_settings_page(request: Request):
    user = require_user(request)
    health = local_drive_health()
    oauth_config = drive_oauth_config()
    proxy_test = None
    if request.query_params.get("proxy_test") == "1":
        proxy_test = test_proxy_connection()
    return templates.TemplateResponse(
        request=request,
        name="drive_settings.html",
        context=template_context(
            user,
            health=health,
            auth_state=drive_auth_state(),
            oauth_config=oauth_config,
            proxy_config=rclone_proxy_config(),
            proxy_test=proxy_test,
        ),
    )


@app.get("/api/local/drive-health")
def local_drive_health_api(request: Request):
    require_user(request)
    return {"health": local_drive_health(), "auth_state": drive_auth_state()}


@app.post("/settings/drive/login")
def start_drive_login(request: Request):
    require_user(request)
    start_drive_auth()
    return RedirectResponse("/settings/drive", status_code=303)


@app.post("/settings/drive/oauth")
def save_drive_oauth_settings(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
):
    require_user(request)
    settings = load_local_settings()
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if client_id and client_secret:
        settings["rclone_drive_client_id"] = client_id
        settings["rclone_drive_client_secret"] = client_secret
    elif not client_id and not client_secret:
        settings.pop("rclone_drive_client_id", None)
        settings.pop("rclone_drive_client_secret", None)
    else:
        raise HTTPException(status_code=400, detail="client_id 和 client_secret 需要同时填写，或同时留空。")
    save_local_settings(settings)
    return RedirectResponse("/settings/drive", status_code=303)


@app.post("/settings/drive/oauth/clear")
def clear_drive_oauth_settings(request: Request):
    require_user(request)
    settings = load_local_settings()
    settings.pop("rclone_drive_client_id", None)
    settings.pop("rclone_drive_client_secret", None)
    save_local_settings(settings)
    return RedirectResponse("/settings/drive", status_code=303)


@app.post("/settings/drive/proxy")
def save_drive_proxy_settings(
    request: Request,
    proxy_enabled: str = Form(""),
    proxy_url: str = Form(DEFAULT_PROXY_URL),
):
    require_user(request)
    save_proxy_settings(proxy_enabled == "1", proxy_url)
    return RedirectResponse("/settings/drive", status_code=303)


@app.post("/settings/drive/proxy/test")
def test_drive_proxy(request: Request):
    require_user(request)
    return RedirectResponse("/settings/drive?proxy_test=1", status_code=303)


@app.post("/settings/drive/reset")
def reset_drive_login(request: Request):
    require_user(request)
    reset_drive_remote()
    return RedirectResponse("/settings/drive", status_code=303)


@app.get("/settings/download")
def download_settings_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(
        request=request,
        name="download_settings.html",
        context=template_context(user, download_settings=download_settings_context()),
    )


@app.post("/settings/download")
async def update_download_settings(request: Request):
    require_user(request)
    form = await request.form()
    save_download_settings(dict(form))
    return RedirectResponse("/settings/download", status_code=303)


@app.post("/settings/download/reset")
def reset_download_settings_page(request: Request):
    require_user(request)
    reset_download_settings()
    return RedirectResponse("/settings/download", status_code=303)


@app.post("/quota")
def update_quota(
    request: Request,
    quota_month: str = Form(""),
    total_quota: int = Form(0),
    note: str = Form(""),
):
    user = require_user(request)
    require_developer(user)
    normalized_month = normalize_quota_month(quota_month)
    if int(total_quota) < 0:
        raise HTTPException(status_code=400, detail="额度不能小于 0")
    db.set_monthly_team_quota(normalized_month, int(total_quota), int(user["id"]), note)
    return RedirectResponse(f"/quota?month={normalized_month}", status_code=303)


@app.get("/uploads/new")
def upload_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=template_context(
            user,
            recent_precheck_batches=recent_precheck_batches_for_user(user),
        ),
    )


@app.post("/uploads")
async def upload_excel(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    filename = file.filename or "orders.xlsx"
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported.")

    batch_id = db.create_batch(filename, Path(""), created_by_user_id=int(user["id"]))
    content = await file.read()
    source_path = save_upload(batch_id, filename, content)
    db.update_batch_source(batch_id, source_path)
    start_background(process_batch, batch_id, source_path)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.get("/resources")
def resources_page(request: Request):
    user = require_user(request)
    include_disabled = user["role"] == "developer"
    resources = db.list_resource_files(include_disabled=include_disabled)
    return templates.TemplateResponse(
        request=request,
        name="resources.html",
        context=template_context(
            user,
            resource_groups=grouped_resource_files(resources),
            format_file_size=format_file_size,
        ),
    )


@app.post("/resources")
async def upload_resource(
    request: Request,
    title: str = Form(""),
    category: str = Form(""),
    version_note: str = Form(""),
    file: UploadFile = File(...),
):
    user = require_user(request)
    require_developer(user)
    filename = file.filename or "resource"
    validate_resource_upload(category, filename)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件不能为空")
    display_title = title.strip() or Path(filename).stem
    resource_id = db.create_resource_file(
        display_title,
        category,
        filename,
        int(user["id"]),
        version_note,
    )
    storage_path = save_resource_file(resource_id, filename, content)
    db.update_resource_file_storage(resource_id, storage_path, len(content))
    return RedirectResponse("/resources", status_code=303)


@app.get("/resources/{resource_id}/download")
def download_resource(request: Request, resource_id: int):
    user = require_user(request)
    include_disabled = user["role"] == "developer"
    resource = db.get_resource_file(resource_id, include_disabled=include_disabled)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    storage_path = Path(resource["storage_path"] or "")
    if not storage_path.exists() or not storage_path.is_file():
        raise HTTPException(status_code=404, detail="Resource file is not ready")
    return FileResponse(
        storage_path,
        media_type="application/octet-stream",
        filename=resource["original_file_name"],
    )


@app.post("/resources/{resource_id}/status")
def update_resource_status(
    request: Request, resource_id: int, status: str = Form("")
):
    user = require_user(request)
    require_developer(user)
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    if not db.update_resource_file_status(resource_id, status):
        raise HTTPException(status_code=404, detail="Resource not found")
    return RedirectResponse("/resources", status_code=303)


@app.get("/users")
def users_page(request: Request):
    user = require_user(request)
    require_developer(user)
    usage_by_user = {
        row.get("user_id"): row for row in db.list_download_usage_by_user()
    }
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context=template_context(
            user,
            users=db.list_users(),
            usage_by_user=usage_by_user,
            error=None,
        ),
    )


@app.post("/users")
def create_user(request: Request, username: str = Form(""), password: str = Form(""), role: str = Form("operator")):
    user = require_user(request)
    require_developer(user)
    username = username.strip()
    usage_by_user = {
        row.get("user_id"): row for row in db.list_download_usage_by_user()
    }
    if not username or not password:
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context=template_context(
                user,
                users=db.list_users(),
                usage_by_user=usage_by_user,
                error="用户名和密码不能为空",
            ),
            status_code=400,
        )
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    try:
        db.create_user(username, hash_password(password), role=role)
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context=template_context(
                user,
                users=db.list_users(),
                usage_by_user=usage_by_user,
                error="用户名已存在",
            ),
            status_code=400,
        )
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/status")
def update_user_status(request: Request, user_id: int, status: str = Form("")):
    user = require_user(request)
    require_developer(user)
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    if int(user["id"]) == user_id and status == "disabled":
        raise HTTPException(status_code=400, detail="Cannot disable current user")
    if not db.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    db.update_user_status(user_id, status)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/password")
def reset_user_password(request: Request, user_id: int, password: str = Form("")):
    user = require_user(request)
    require_developer(user)
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    if not db.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    db.update_user_password(user_id, hash_password(password))
    return RedirectResponse("/users", status_code=303)


@app.get("/batches/{batch_id}")
def batch_detail(request: Request, batch_id: int, tab: str = ""):
    user = require_user(request)
    batch = enrich_batch_identity(require_batch_access(batch_id, user))
    can_operate = can_operate_batch(user, batch)
    stale_recovered_count = (
        recover_stale_extension_downloads(batch_id) if can_operate else 0
    )
    if stale_recovered_count:
        batch = enrich_batch_identity(require_batch_access(batch_id, user))
        can_operate = can_operate_batch(user, batch)
    orders = db.get_batch_orders(batch_id)
    enrich_order_download_durations(orders)
    display_rows = []
    failed_rows = []
    for group_index, order in enumerate(orders):
        item_count = len(order["items"])
        for item_index, item in enumerate(order["items"]):
            row = {
                "order": order,
                "item": item,
                "is_multi_order": item_count > 1,
                "is_first_in_order": item_index == 0,
                "order_item_count": item_count,
                "order_group_index": group_index % 2,
            }
            display_rows.append(row)
            if item.get("download_status") == "failed":
                for download_item in item.get("download_items", []):
                    if download_item.get("download_status") == "failed":
                        failed_rows.append({**row, "download_item": download_item})
    display_rows = sort_rows_by_excel_row(display_rows)
    failed_rows = sort_rows_by_excel_row(failed_rows)
    current_task = current_download_task(display_rows)
    status_counts = db.get_batch_status_counts(batch_id)
    handled_count = int(batch["success_count"]) + int(status_counts["manual_done"])
    primary_action = batch_primary_action(batch, status_counts)
    server_controls = server_download_controls(batch, status_counts, handled_count)
    can_edit_download_name = (
        can_operate
        and batch["status"] != "processing"
        and not db.batch_has_downloading_items(batch_id)
    )
    if not can_operate:
        primary_action = {
            **primary_action,
            "title": "团队批次只读",
            "body": "你可以查看订单、下载情况和统计；操作需由创建人或开发者执行。",
            "cta": "查看订单明细",
        }
    filter_counts = {
        "all": len(display_rows),
        "failed": sum(1 for row in display_rows if row["item"].get("download_status") == "failed"),
        "downloading": sum(1 for row in display_rows if row["item"].get("download_status") == "downloading"),
        "pending": sum(1 for row in display_rows if row["item"].get("download_status") == "pending"),
        "completed": sum(1 for row in display_rows if row["item"].get("download_status") == "downloaded"),
    }
    progress_percent = 0
    if int(batch["link_count"]) > 0:
        progress_percent = round(
            handled_count * 100 / int(batch["link_count"])
        )
    archive_path = batch["archive_path"]
    batch_archive_ready = bool(archive_path and Path(archive_path).exists())
    import_summary = None
    if batch.get("import_summary_json"):
        try:
            import_summary = json.loads(batch["import_summary_json"])
        except json.JSONDecodeError:
            import_summary = None
    if import_summary is None:
        import_summary = build_import_summary(
            item for order in orders for item in order["items"]
        )
    tab_context = batch_detail_tab(batch, tab)
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context=template_context(
            user,
            batch=batch,
            display_rows=display_rows,
            failed_rows=failed_rows,
            current_task=current_task,
            status_counts=status_counts,
            status_labels=STATUS_LABELS,
            handled_count=handled_count,
            progress_percent=progress_percent,
            batch_archive_ready=batch_archive_ready,
            error_labels=ERROR_LABELS,
            import_summary=import_summary,
            stale_recovered_count=stale_recovered_count,
            primary_action=primary_action,
            server_controls=server_controls,
            filter_counts=filter_counts,
            can_operate=can_operate,
            can_edit_download_name=can_edit_download_name,
            **tab_context,
        ),
    )


@app.get("/batches/{batch_id}/speed-report")
def batch_speed_report(request: Request, batch_id: int):
    user = require_user(request)
    batch = enrich_batch_identity(require_batch_access(batch_id, user))
    report = build_speed_report(batch_id, db.DB_PATH, slow_limit=20)
    return templates.TemplateResponse(
        request=request,
        name="speed_report.html",
        context=template_context(
            user,
            batch=batch,
            report=report,
            format_seconds=format_seconds,
        ),
    )


@app.get("/batches/{batch_id}/status")
def batch_status(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    can_operate = can_operate_batch(user, batch)
    stale_recovered_count = (
        recover_stale_extension_downloads(batch_id) if can_operate else 0
    )
    if stale_recovered_count:
        batch = require_batch_access(batch_id, user)
        can_operate = can_operate_batch(user, batch)
    orders = db.get_batch_orders(batch_id)
    enrich_order_download_durations(orders)
    counts = db.get_batch_status_counts(batch_id)
    actions = batch_download_actions(batch, counts)
    if not can_operate:
        actions = {**actions, "can_start_extension": False, "can_retry_failed": False}
    return {
        "batch": batch,
        "status_counts": counts,
        "current_task": current_download_task_from_orders(orders),
        "stale_recovered_count": stale_recovered_count,
        "actions": actions,
    }


@app.post("/batches/{batch_id}/confirm")
def confirm_batch(
    request: Request,
    batch_id: int,
    download_name: Optional[str] = Form(None),
):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    if batch["status"] != "precheck_ready":
        raise HTTPException(status_code=400, detail="只有预检完成、待确认的上传记录可以确认导入。")
    if download_name is not None:
        if not db.update_batch_download_name(batch_id, cleaned_download_name(download_name)):
            raise HTTPException(status_code=404, detail="Batch not found")
    if not db.confirm_batch(batch_id):
        raise HTTPException(status_code=400, detail="只有预检完成、待确认的上传记录可以确认导入。")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/discard")
def discard_batch(request: Request, batch_id: int):
    user = require_user(request)
    require_batch_operation(batch_id, user)
    if not db.discard_batch(batch_id):
        raise HTTPException(status_code=400, detail="当前状态不能作废。下载中或已完成批次不能作废。")
    return RedirectResponse("/batches", status_code=303)


@app.post("/batches/{batch_id}/download-name")
def update_batch_download_name(
    request: Request,
    batch_id: int,
    download_name: str = Form(""),
):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    if batch["status"] == "processing" or db.batch_has_downloading_items(batch_id):
        raise HTTPException(status_code=400, detail="批次正在下载中，不能修改下载文件夹名。")
    if not db.update_batch_download_name(batch_id, cleaned_download_name(download_name)):
        raise HTTPException(status_code=404, detail="Batch not found")
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/delete")
def delete_batch(
    request: Request,
    batch_id: int,
    confirmation: str = Form(""),
    reason: str = Form(""),
):
    user = require_user(request)
    require_developer(user)
    batch = require_batch_access(batch_id, user)
    expected_confirmation = f"DELETE-BATCH-{batch_id}"
    if confirmation.strip() != expected_confirmation:
        raise HTTPException(status_code=400, detail="删除确认文本不正确。")
    if batch["status"] == "processing" or db.batch_has_downloading_items(batch_id):
        raise HTTPException(status_code=400, detail="批次正在下载中，不能删除。")
    delete_reason = reason.strip() or "开发者删除批次"
    if not db.delete_batch_with_audit(batch_id, int(user["id"]), delete_reason):
        raise HTTPException(status_code=404, detail="Batch not found")
    remove_batch_files(batch_id)
    return RedirectResponse("/", status_code=303)


@app.get("/api/extension/batches/{batch_id}/download-items")
def extension_download_items(request: Request, batch_id: int, limit: int = 50):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    if recover_stale_extension_downloads(batch_id):
        batch = require_batch_operation(batch_id, user)
    require_download_allowed(batch)
    items = db.get_extension_download_items(batch_id, limit)
    counts = db.get_batch_status_counts(batch_id)
    return {
        "batch": {
            "id": int(batch["id"]),
            "file_name": batch["file_name"],
            "download_name": batch["download_name"],
            "status": batch["status"],
        },
        "status_counts": counts,
        "items": [extension_download_item_payload(item) for item in items],
    }


@app.post("/api/extension/batches/{batch_id}/next-download-item")
def extension_next_download_item(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    if recover_stale_extension_downloads(batch_id):
        batch = require_batch_operation(batch_id, user)
    require_download_start_allowed(batch)

    item = db.get_next_extension_download_item(
        batch_id,
        EXTENSION_RETRYABLE_ERROR_CODES,
        extension_max_attempts(),
    )
    if not item:
        counts = db.get_batch_status_counts(batch_id)
        refresh_batch_status_after_extension_update(batch_id)
        return {
            "ok": True,
            "item": None,
            "status_counts": counts,
            "max_attempts": extension_max_attempts(),
        }

    dispatched = db.dispatch_download_item(int(item["id"]))
    if not dispatched:
        raise HTTPException(status_code=409, detail="Download item could not be dispatched")
    refresh_batch_status_after_extension_update(batch_id)
    db.record_extension_event(
        batch_id=batch_id,
        download_item_id=int(dispatched["id"]),
        event="item_dispatched",
        message=f"attempt {int(dispatched.get('attempt_count') or 0)}/{extension_max_attempts()}",
        detail={"download_item_id": int(dispatched["id"])},
    )
    return {
        "ok": True,
        "item": extension_download_item_payload(dispatched),
        "status_counts": db.get_batch_status_counts(batch_id),
        "max_attempts": extension_max_attempts(),
    }


@app.post("/api/extension/download-items/{download_item_id}/start")
def extension_start_download_item(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    batch = require_batch_operation(int(item["batch_id"]), user)
    require_download_start_allowed(batch)
    db.clear_downloaded_files(download_item_id)
    db.mark_download_started(download_item_id)
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {"ok": True, "download_item_id": download_item_id}


@app.post("/api/extension/download-items/{download_item_id}/heartbeat")
def extension_download_item_heartbeat(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    batch = require_batch_operation(int(item["batch_id"]), user)
    require_download_allowed(batch)
    updated = db.mark_download_heartbeat(download_item_id)
    return {
        "ok": True,
        "download_item_id": download_item_id,
        "updated": updated,
        "status": item["status"],
    }


@app.post("/api/extension/download-items/{download_item_id}/success")
def extension_download_item_success(
    request: Request,
    download_item_id: int,
    body: Any = Body(default=None),
):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    batch = require_batch_operation(int(item["batch_id"]), user)
    require_download_allowed(batch)
    data = request_json_dict(body)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    image_count = data.get("image_count")
    try:
        image_count = int(image_count)
    except (TypeError, ValueError):
        image_count = len(files)
    if image_count <= 0:
        image_count = 1
    db.replace_downloaded_files_for_item(download_item_id, files)
    db.mark_download_success(download_item_id, image_count)
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {"ok": True, "download_item_id": download_item_id, "image_count": image_count}


@app.post("/api/extension/download-items/{download_item_id}/failure")
def extension_download_item_failure(
    request: Request,
    download_item_id: int,
    body: Any = Body(default=None),
):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    batch = require_batch_operation(int(item["batch_id"]), user)
    require_download_allowed(batch)
    data = request_json_dict(body)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    partial_image_count = data.get("partial_image_count")
    try:
        partial_image_count = int(partial_image_count)
    except (TypeError, ValueError):
        partial_image_count = len(files)
    partial_image_count = max(0, partial_image_count)
    classified = classify_extension_failure(data)
    error_code = str(classified["code"])
    error_message = str(classified["message"])
    error_detail = str(classified["detail"] or "")
    if files:
        db.replace_downloaded_files_for_item(download_item_id, files)
    if should_retry_extension_failure(item, classified):
        db.mark_download_retry_pending(
            download_item_id,
            error_message,
            error_code=error_code,
            error_detail=error_detail,
            image_count=partial_image_count,
        )
        final_status = "pending"
    else:
        db.mark_download_failed(
            download_item_id,
            error_message,
            error_code=error_code,
            error_detail=error_detail,
            image_count=partial_image_count,
        )
        final_status = "failed"
    db.record_extension_event(
        batch_id=int(item["batch_id"]),
        download_item_id=download_item_id,
        event="item_failure",
        level="warning",
        message=error_message,
        detail={
            "raw": data,
            "classified_code": error_code,
            "final_status": final_status,
            "attempt_count": int(item.get("attempt_count") or 0),
            "max_attempts": extension_max_attempts(),
        },
    )
    refresh_batch_status_after_extension_update(int(item["batch_id"]))
    return {
        "ok": True,
        "download_item_id": download_item_id,
        "partial_image_count": partial_image_count,
        "status": final_status,
        "error_code": error_code,
        "retryable": final_status == "pending",
    }


@app.post("/api/extension/events")
def extension_events(request: Request, body: Any = Body(default=None)):
    user = require_user(request)
    data = request_json_dict(body)
    batch_id = int(data.get("batch_id") or 0)
    if batch_id <= 0:
        raise HTTPException(status_code=400, detail="batch_id is required")
    require_batch_operation(batch_id, user)
    download_item_id = data.get("download_item_id")
    try:
        download_item_id = int(download_item_id) if download_item_id else None
    except (TypeError, ValueError):
        download_item_id = None
    event_id = db.record_extension_event(
        batch_id=batch_id,
        download_item_id=download_item_id,
        event=str(data.get("event") or "event"),
        level=str(data.get("level") or "info"),
        message=str(data.get("message") or ""),
        detail=data.get("detail") if isinstance(data.get("detail"), dict) else None,
    )
    return {"ok": True, "event_id": event_id}


@app.post("/batches/{batch_id}/start-download")
def start_batch_download(request: Request, batch_id: int, limit: int = Form(0)):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    require_download_start_allowed(batch)
    health = local_drive_health()
    if not health["ok"]:
        raise HTTPException(
            status_code=400,
            detail=f"本机 Drive 下载环境不可用：{health.get('error') or '请先完成 Drive 设置。'}",
        )
    selected_limit = normalized_limit(limit)
    db.clear_server_download_stop(batch_id)
    if selected_limit:
        start_background(start_download_limited, batch_id, selected_limit)
    else:
        start_background(start_download, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/retry-failed")
def retry_failed_items(request: Request, batch_id: int, limit: int = Form(0)):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    require_download_start_allowed(batch)
    selected_limit = normalized_limit(limit)
    db.clear_server_download_stop(batch_id)
    if selected_limit:
        start_background(retry_failed_limited, batch_id, selected_limit)
    else:
        start_background(retry_failed, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/server-download/pause")
def pause_server_download(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_operation(batch_id, user)
    if batch["status"] == "processing" or db.batch_has_downloading_items(batch_id):
        db.request_server_download_stop(batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/download-items/{download_item_id}/retry")
def retry_one_download_item(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    batch = require_batch_operation(int(item["batch_id"]), user)
    require_download_start_allowed(batch)
    start_background(retry_download_item, download_item_id)
    return RedirectResponse(f"/batches/{item['batch_id']}", status_code=303)


@app.post("/download-items/{download_item_id}/manual-done")
def mark_one_download_item_manual_done(request: Request, download_item_id: int):
    user = require_user(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    require_batch_operation(int(item["batch_id"]), user)
    mark_download_item_manual_done(download_item_id)
    return RedirectResponse(f"/batches/{item['batch_id']}", status_code=303)


@app.get("/batches/{batch_id}/download.zip")
def download_archive(request: Request, batch_id: int):
    user = require_user(request)
    batch = require_batch_access(batch_id, user)
    archive_path = Path(batch["archive_path"] or ARCHIVES_DIR / f"batch-{batch_id}.zip")
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive is not ready")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{safe_filename(str(batch.get('download_name') or f'batch-{batch_id}'))}.zip",
    )


@app.get("/batches/{batch_id}/source-file")
def download_source_file(request: Request, batch_id: int):
    user = require_user(request)
    require_developer(user)
    batch = require_batch_access(batch_id, user)
    source_path = Path(batch["source_path"] or "")
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=404, detail="Source file is not ready")
    filename = batch["file_name"] or f"batch-{batch_id}-source.xlsx"
    return FileResponse(
        source_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
