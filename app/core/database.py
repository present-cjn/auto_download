from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.core.excel_parser import OrderItemRow


DB_PATH = Path("data/app.db")
SQLITE_BUSY_TIMEOUT_MS = 30_000

LEGACY_BATCH_STATUS_MAP = {
    "pending": "uploaded",
    "review_ready": "confirmed",
    "needs_fix": "precheck_failed",
    "downloading": "processing",
    "failed": "precheck_failed",
}

DISCARDABLE_BATCH_STATUSES = {
    "uploaded",
    "parsing",
    "precheck_ready",
    "precheck_failed",
    "confirmed",
}

FORMAL_BATCH_STATUSES = {
    "confirmed",
    "processing",
    "completed",
    "completed_with_errors",
}


DOWNLOAD_ITEMS_SCHEMA = """
CREATE TABLE download_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    order_no TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    sku TEXT,
    design_link TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'design',
    status TEXT NOT NULL DEFAULT 'pending',
    image_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    error_code TEXT,
    error_detail TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    heartbeat_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(batch_id, order_item_id, design_link, source_type)
);
"""

DOWNLOADED_FILES_SCHEMA = """
CREATE TABLE downloaded_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    download_item_id INTEGER NOT NULL REFERENCES download_items(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    order_no TEXT NOT NULL,
    file_name TEXT NOT NULL,
    local_path TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

EXTENSION_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS extension_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    download_item_id INTEGER REFERENCES download_items(id) ON DELETE SET NULL,
    event TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

BATCH_DELETION_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_deletion_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    source_path TEXT,
    status TEXT NOT NULL,
    created_by_user_id INTEGER,
    created_by_username TEXT,
    order_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    link_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    archive_path TEXT,
    deleted_by_user_id INTEGER NOT NULL REFERENCES users(id),
    delete_reason TEXT,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

PRODUCTION_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    order_no TEXT NOT NULL,
    sku TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending_push'
        CHECK(status IN ('pending_push', 'pushing', 'pushed', 'push_failed', 'manual_pushed')),
    payload_json TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

RESOURCE_FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('header_template', 'browser_extension', 'guide')),
    original_file_name TEXT NOT NULL,
    storage_path TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    version_note TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
    uploaded_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

MONTHLY_TEAM_QUOTAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS monthly_team_quotas (
    quota_month TEXT PRIMARY KEY,
    total_quota INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    updated_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('developer', 'admin', 'operator')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


def ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_definition: str
) -> None:
    if column_name in table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")


def table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return str(row["sql"] or "") if row else ""


def migrate_download_items_schema(conn: sqlite3.Connection) -> None:
    existing_columns = table_columns(conn, "download_items")
    if not existing_columns:
        return

    if "sku" in existing_columns and "source_type" in existing_columns:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    downloaded_file_columns = table_columns(conn, "downloaded_files")
    source_type_expression = (
        "COALESCE(di.source_type, 'design')"
        if "source_type" in existing_columns
        else "'design'"
    )
    error_code_expression = "di.error_code" if "error_code" in existing_columns else "NULL"
    error_detail_expression = (
        "di.error_detail" if "error_detail" in existing_columns else "NULL"
    )
    conn.execute("ALTER TABLE download_items RENAME TO download_items_legacy")
    if downloaded_file_columns:
        conn.execute("ALTER TABLE downloaded_files RENAME TO downloaded_files_legacy")

    conn.executescript(DOWNLOAD_ITEMS_SCHEMA)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO download_items (
            id, batch_id, order_id, order_item_id, order_no, row_number, sku,
            design_link, source_type, status, image_count, error_message, error_code,
            error_detail, attempt_count, started_at, heartbeat_at, completed_at, created_at
        )
        SELECT
            di.id,
            di.batch_id,
            di.order_id,
            di.order_item_id,
            di.order_no,
            di.row_number,
            oi.sku,
            di.design_link,
            {source_type_expression},
            di.status,
            di.image_count,
            di.error_message,
            {error_code_expression},
            {error_detail_expression},
            0,
            di.started_at,
            NULL,
            di.completed_at,
            di.created_at
        FROM download_items_legacy di
        LEFT JOIN order_items oi ON oi.id = di.order_item_id
        """
    )

    conn.executescript(DOWNLOADED_FILES_SCHEMA)
    if downloaded_file_columns:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO downloaded_files (
                id, download_item_id, batch_id, order_id, order_no,
                file_name, local_path, file_size, created_at
            )
            SELECT
                id, download_item_id, batch_id, order_id, order_no,
                file_name, local_path, file_size, created_at
            FROM downloaded_files_legacy
            """
        )
    conn.execute("PRAGMA foreign_keys = ON")


def migrate_users_schema(conn: sqlite3.Connection) -> None:
    existing_columns = table_columns(conn, "users")
    if not existing_columns or "developer" in table_sql(conn, "users"):
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE users RENAME TO users_legacy")
        conn.executescript(USERS_SCHEMA)
        conn.execute(
            """
            INSERT OR IGNORE INTO users (
                id, username, password_hash, role, status, created_at, updated_at
            )
            SELECT
                id, username, password_hash, role, status, created_at, updated_at
            FROM users_legacy
            """
        )
        conn.execute("DROP TABLE users_legacy")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    if table_columns(conn, "import_batches"):
        ensure_column(
            conn,
            "import_batches",
            "download_name",
            "download_name TEXT",
        )
        ensure_column(
            conn,
            "import_batches",
            "business_date",
            "business_date TEXT",
        )
        ensure_column(
            conn,
            "import_batches",
            "user_daily_sequence",
            "user_daily_sequence INTEGER",
        )
        ensure_column(
            conn,
            "import_batches",
            "import_summary_json",
            "import_summary_json TEXT",
        )
        ensure_column(
            conn,
            "import_batches",
            "created_by_user_id",
            "created_by_user_id INTEGER REFERENCES users(id)",
        )
        ensure_column(
            conn,
            "import_batches",
            "server_stop_requested",
            "server_stop_requested INTEGER NOT NULL DEFAULT 0",
        )
    if table_columns(conn, "download_items"):
        ensure_column(conn, "download_items", "error_code", "error_code TEXT")
        ensure_column(conn, "download_items", "error_detail", "error_detail TEXT")
        ensure_column(conn, "download_items", "heartbeat_at", "heartbeat_at TEXT")
        ensure_column(
            conn,
            "download_items",
            "attempt_count",
            "attempt_count INTEGER NOT NULL DEFAULT 0",
        )
        if "source_type" not in table_columns(conn, "download_items"):
            migrate_download_items_schema(conn)
    if table_columns(conn, "order_items"):
        ensure_column(conn, "order_items", "mockup_link", "mockup_link TEXT")
        ensure_column(conn, "order_items", "carrier", "carrier TEXT")


def batch_business_date_from_created_at(created_at: Optional[str]) -> str:
    value = (created_at or "").strip()
    if len(value) >= 10:
        return value[:10].replace("-", "")
    return "unknown"


def safe_batch_download_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120].strip(" .") or "orders"


def batch_download_name_from_file_name(file_name: str) -> str:
    raw_name = str(file_name or "").strip()
    stem = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    return safe_batch_download_name(stem)


def unique_batch_download_name(
    conn: sqlite3.Connection,
    base_name: str,
    created_by_user_id: Optional[int],
    exclude_batch_id: Optional[int] = None,
) -> str:
    base = safe_batch_download_name(base_name)
    user_key = int(created_by_user_id) if created_by_user_id is not None else -1
    suffix = 1
    while True:
        candidate = base if suffix == 1 else f"{base}-{suffix}"
        params: list[Any] = [user_key, candidate]
        exclude_clause = ""
        if exclude_batch_id is not None:
            exclude_clause = "AND id != ?"
            params.append(int(exclude_batch_id))
        exists = conn.execute(
            f"""
            SELECT 1
            FROM import_batches
            WHERE COALESCE(created_by_user_id, -1) = ?
              AND download_name = ?
              {exclude_clause}
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not exists:
            return candidate
        suffix += 1


def backfill_missing_batch_download_names(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "import_batches"):
        return
    rows = conn.execute(
        """
        SELECT id, file_name, created_by_user_id, download_name
        FROM import_batches
        ORDER BY COALESCE(created_by_user_id, -1), created_at, id
        """
    ).fetchall()
    for row in rows:
        current_name = str(row["download_name"] or "").strip()
        if current_name:
            normalized_name = safe_batch_download_name(current_name)
            if normalized_name != current_name:
                normalized_name = unique_batch_download_name(
                    conn,
                    normalized_name,
                    row["created_by_user_id"],
                    exclude_batch_id=int(row["id"]),
                )
                conn.execute(
                    """
                    UPDATE import_batches
                    SET download_name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (normalized_name, int(row["id"])),
                )
            continue
        download_name = unique_batch_download_name(
            conn,
            batch_download_name_from_file_name(row["file_name"]),
            row["created_by_user_id"],
            exclude_batch_id=int(row["id"]),
        )
        conn.execute(
            """
            UPDATE import_batches
            SET download_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (download_name, int(row["id"])),
        )


def assign_missing_batch_business_ids(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "import_batches"):
        return
    rows = conn.execute(
        """
        SELECT id, created_by_user_id, created_at, business_date, user_daily_sequence
        FROM import_batches
        ORDER BY COALESCE(created_by_user_id, -1), created_at, id
        """
    ).fetchall()
    next_sequence_by_key: dict[tuple[int, str], int] = {}
    for row in rows:
        batch_id = int(row["id"])
        user_key = int(row["created_by_user_id"] or -1)
        business_date = row["business_date"] or batch_business_date_from_created_at(
            row["created_at"]
        )
        key = (user_key, business_date)
        current_sequence = row["user_daily_sequence"]
        if current_sequence:
            next_sequence_by_key[key] = max(
                next_sequence_by_key.get(key, 1),
                int(current_sequence) + 1,
            )
            if not row["business_date"]:
                conn.execute(
                    """
                    UPDATE import_batches
                    SET business_date = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (business_date, batch_id),
                )
            continue
        next_sequence = next_sequence_by_key.get(key, 1)
        conn.execute(
            """
            UPDATE import_batches
            SET business_date = ?, user_daily_sequence = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (business_date, next_sequence, batch_id),
        )
        next_sequence_by_key[key] = next_sequence + 1


def backfill_missing_download_items(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO download_items (
            batch_id, order_id, order_item_id, order_no, row_number, sku,
            design_link, source_type
        )
        SELECT
            oi.batch_id,
            oi.order_id,
            oi.id,
            o.order_no,
            oi.row_number,
            oi.sku,
            oi.design_link,
            'design'
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.design_link IS NOT NULL
          AND oi.design_link != ''
        UNION ALL
        SELECT
            oi.batch_id,
            oi.order_id,
            oi.id,
            o.order_no,
            oi.row_number,
            oi.sku,
            oi.mockup_link,
            'mockup'
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.mockup_link IS NOT NULL
          AND oi.mockup_link != ''
        """
    )


def refresh_all_batch_counts(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id FROM import_batches").fetchall()
    for row in rows:
        refresh_batch_counts_with_conn(conn, int(row["id"]))


def refresh_batch_counts_with_conn(conn: sqlite3.Connection, batch_id: int) -> None:
    order_count = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    item_count = conn.execute(
        "SELECT COUNT(*) FROM order_items WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    link_count = conn.execute(
        "SELECT COUNT(*) FROM download_items WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    success_count = conn.execute(
        """
        SELECT COUNT(*) FROM download_items
        WHERE batch_id = ? AND status = 'downloaded'
        """,
        (batch_id,),
    ).fetchone()[0]
    failed_count = conn.execute(
        """
        SELECT COUNT(*) FROM download_items
        WHERE batch_id = ? AND status = 'failed'
        """,
        (batch_id,),
    ).fetchone()[0]
    skipped_count = conn.execute(
        """
        SELECT COUNT(*) FROM download_items
        WHERE batch_id = ? AND status = 'skipped'
        """,
        (batch_id,),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE import_batches
        SET order_count = ?, item_count = ?, link_count = ?,
            success_count = ?, failed_count = ?, skipped_count = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            order_count,
            item_count,
            link_count,
            success_count,
            failed_count,
            skipped_count,
            batch_id,
        ),
    )


def reconcile_interrupted_batches(conn: sqlite3.Connection) -> None:
    normalize_legacy_batch_statuses(conn)
    if "server_stop_requested" in table_columns(conn, "import_batches"):
        conn.execute("UPDATE import_batches SET server_stop_requested = 0")
    interrupted_rows = conn.execute(
        """
        SELECT DISTINCT batch_id
        FROM download_items
        WHERE status = 'downloading'
        """
    ).fetchall()
    interrupted_batch_ids = [int(row["batch_id"]) for row in interrupted_rows]
    if interrupted_batch_ids:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'failed',
                error_message = '服务中断或下载任务未正常结束，请重试。',
                error_code = 'interrupted',
                error_detail = 'Recovered from stale downloading status on startup.',
                completed_at = CURRENT_TIMESTAMP
            WHERE status = 'downloading'
            """
        )
        for batch_id in interrupted_batch_ids:
            refresh_batch_counts_with_conn(conn, batch_id)

    rows = conn.execute(
        """
        SELECT id, status, success_count, failed_count
        FROM import_batches
        WHERE status IN ('processing', 'parsing', 'uploaded')
        """
    ).fetchall()
    for row in rows:
        batch_id = int(row["id"])
        item_count = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        if row["status"] == "uploaded" and item_count == 0:
            continue
        current_counts = conn.execute(
            """
            SELECT success_count, failed_count
            FROM import_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()
        if item_count == 0:
            next_status = "uploaded"
        elif int(current_counts["failed_count"]) > 0:
            next_status = "completed_with_errors"
        elif int(current_counts["success_count"]) > 0:
            next_status = "completed"
        else:
            next_status = "precheck_ready" if row["status"] == "parsing" else "confirmed"
        conn.execute(
            """
            UPDATE import_batches
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_status, batch_id),
        )


def normalize_legacy_batch_statuses(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "import_batches"):
        return
    for old_status, new_status in LEGACY_BATCH_STATUS_MAP.items():
        conn.execute(
            """
            UPDATE import_batches
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE status = ?
            """,
            (new_status, old_status),
        )


def recover_stale_downloading_items(batch_id: int, stale_minutes: int = 30) -> int:
    stale_minutes = max(1, int(stale_minutes))
    stale_modifier = f"-{stale_minutes} minutes"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM download_items
            WHERE batch_id = ?
              AND status = 'downloading'
              AND datetime(COALESCE(heartbeat_at, started_at, created_at)) <= datetime('now', ?)
            """,
            (batch_id, stale_modifier),
        ).fetchall()
        if not rows:
            return 0
        item_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"""
            UPDATE download_items
            SET status = 'failed',
                error_message = '插件中断或页面重新加载，下载结果未回写，请重试。',
                error_code = 'interrupted',
                error_detail = 'Recovered stale extension downloading item after missing heartbeat.',
                completed_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            item_ids,
        )
        refresh_batch_counts_with_conn(conn, batch_id)
        return len(item_ids)


def init_db(db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        migrate_download_items_schema(conn)
        migrate_users_schema(conn)
        conn.executescript(USERS_SCHEMA)
        conn.executescript(RESOURCE_FILES_SCHEMA)
        conn.executescript(MONTHLY_TEAM_QUOTAS_SCHEMA)
        conn.executescript(SESSIONS_SCHEMA)
        conn.executescript(EXTENSION_EVENTS_SCHEMA)
        conn.executescript(BATCH_DELETION_AUDIT_SCHEMA)
        conn.executescript(PRODUCTION_OUTBOX_SCHEMA)
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                download_name TEXT,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'uploaded',
                order_count INTEGER NOT NULL DEFAULT 0,
                item_count INTEGER NOT NULL DEFAULT 0,
                link_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                archive_path TEXT,
                error_message TEXT,
                server_stop_requested INTEGER NOT NULL DEFAULT 0,
                import_summary_json TEXT,
                created_by_user_id INTEGER REFERENCES users(id),
                business_date TEXT,
                user_daily_sequence INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                order_no TEXT NOT NULL,
                order_date TEXT,
                order_date_raw TEXT,
                week INTEGER,
                shipping_fullname TEXT,
                address TEXT,
                city TEXT,
                province TEXT,
                zip_code TEXT,
                country TEXT,
                phone TEXT,
                mail TEXT,
                customer_note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, order_no)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                row_number INTEGER NOT NULL,
                sku TEXT,
                size TEXT,
                color TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                custom_name TEXT,
                tracking_no TEXT,
                carrier TEXT,
                product_id TEXT,
                design_link TEXT,
                mockup_link TEXT,
                parent_item_name_local TEXT,
                parent_item_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
                order_no TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                sku TEXT,
                design_link TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'design',
                status TEXT NOT NULL DEFAULT 'pending',
                image_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                error_code TEXT,
                error_detail TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                heartbeat_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, order_item_id, design_link, source_type)
            );

            CREATE TABLE IF NOT EXISTS downloaded_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_item_id INTEGER NOT NULL REFERENCES download_items(id) ON DELETE CASCADE,
                batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                order_no TEXT NOT NULL,
                file_name TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        ensure_schema_columns(conn)
        backfill_missing_batch_download_names(conn)
        assign_missing_batch_business_ids(conn)
        backfill_missing_download_items(conn)
        refresh_all_batch_counts(conn)
        reconcile_interrupted_batches(conn)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def user_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_user(
    username: str,
    password_hash: str,
    role: str = "operator",
    status: str = "active",
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, status)
            VALUES (?, ?, ?, ?)
            """,
            (username.strip(), password_hash, role, status),
        )
        return int(cursor.lastrowid)


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, role, status, created_at, updated_at
            FROM users
            ORDER BY id
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_user(user_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, role, status, created_at, updated_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, role, status, created_at, updated_at
            FROM users
            WHERE username = ?
            """,
            (username.strip(),),
        ).fetchone()
        return row_to_dict(row) if row else None


def update_user_status(user_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, user_id),
        )


def update_user_password(user_id: int, password_hash: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (password_hash, user_id),
        )


def update_user_role(user_id: int, role: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET role = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (role, user_id),
        )


def create_resource_file(
    title: str,
    category: str,
    original_file_name: str,
    uploaded_by_user_id: int,
    version_note: str = "",
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO resource_files (
                title, category, original_file_name, uploaded_by_user_id, version_note
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                category,
                original_file_name,
                uploaded_by_user_id,
                version_note.strip(),
            ),
        )
        return int(cursor.lastrowid)


def update_resource_file_storage(
    resource_id: int, storage_path: Path, file_size: int
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE resource_files
            SET storage_path = ?, file_size = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(storage_path), file_size, resource_id),
        )


def list_resource_files(include_disabled: bool = False) -> list[dict[str, Any]]:
    status_clause = "" if include_disabled else "WHERE rf.status = 'active'"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                rf.*,
                u.username AS uploaded_by_username
            FROM resource_files rf
            LEFT JOIN users u ON u.id = rf.uploaded_by_user_id
            {status_clause}
            ORDER BY
                CASE rf.category
                    WHEN 'header_template' THEN 1
                    WHEN 'browser_extension' THEN 2
                    ELSE 3
                END,
                rf.created_at DESC,
                rf.id DESC
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_resource_file(
    resource_id: int, include_disabled: bool = False
) -> Optional[dict[str, Any]]:
    status_clause = "" if include_disabled else "AND rf.status = 'active'"
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                rf.*,
                u.username AS uploaded_by_username
            FROM resource_files rf
            LEFT JOIN users u ON u.id = rf.uploaded_by_user_id
            WHERE rf.id = ?
              {status_clause}
            """,
            (resource_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def get_resource_file_by_category_and_version_note(
    category: str, version_note: str, include_disabled: bool = False
) -> Optional[dict[str, Any]]:
    status_clause = "" if include_disabled else "AND rf.status = 'active'"
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                rf.*,
                u.username AS uploaded_by_username
            FROM resource_files rf
            LEFT JOIN users u ON u.id = rf.uploaded_by_user_id
            WHERE rf.category = ?
              AND rf.version_note = ?
              {status_clause}
            ORDER BY rf.id DESC
            LIMIT 1
            """,
            (category, version_note),
        ).fetchone()
        return row_to_dict(row) if row else None


def update_resource_file_status(resource_id: int, status: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE resource_files
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, resource_id),
        )
        return cursor.rowcount > 0


def set_resource_category_active_only(category: str, active_resource_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE resource_files
            SET status = CASE WHEN id = ? THEN 'active' ELSE 'disabled' END,
                updated_at = CURRENT_TIMESTAMP
            WHERE category = ?
            """,
            (active_resource_id, category),
        )


def create_session(session_token: str, user_id: int, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_token, user_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (session_token, user_id, expires_at),
        )


def delete_session(session_token: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE session_token = ?",
            (session_token,),
        )


def delete_expired_sessions(now: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))


def get_user_by_session(session_token: str, now: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                u.id,
                u.username,
                u.password_hash,
                u.role,
                u.status,
                u.created_at,
                u.updated_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = ?
              AND s.expires_at > ?
              AND u.status = 'active'
            """,
            (session_token, now),
        ).fetchone()
        return row_to_dict(row) if row else None


def create_batch(
    file_name: str, source_path: Path, created_by_user_id: Optional[int] = None
) -> int:
    with connect() as conn:
        business_date = conn.execute(
            "SELECT strftime('%Y%m%d', 'now')"
        ).fetchone()[0]
        user_key = int(created_by_user_id) if created_by_user_id is not None else -1
        next_sequence = int(
            conn.execute(
                """
                SELECT COALESCE(MAX(user_daily_sequence), 0) + 1
                FROM import_batches
                WHERE COALESCE(created_by_user_id, -1) = ?
                  AND business_date = ?
                """,
                (user_key, business_date),
            ).fetchone()[0]
        )
        download_name = unique_batch_download_name(
            conn,
            batch_download_name_from_file_name(file_name),
            created_by_user_id,
        )
        cursor = conn.execute(
            """
            INSERT INTO import_batches (
                file_name, download_name, source_path, status, created_by_user_id,
                business_date, user_daily_sequence
            )
            VALUES (?, ?, ?, 'uploaded', ?, ?, ?)
            """,
            (
                file_name,
                download_name,
                str(source_path),
                created_by_user_id,
                business_date,
                next_sequence,
            ),
        )
        return int(cursor.lastrowid)


def update_batch_download_name(batch_id: int, download_name: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, created_by_user_id
            FROM import_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()
        if not row:
            return False
        unique_name = unique_batch_download_name(
            conn,
            download_name,
            row["created_by_user_id"],
            exclude_batch_id=batch_id,
        )
        cursor = conn.execute(
            """
            UPDATE import_batches
            SET download_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (unique_name, batch_id),
        )
        return cursor.rowcount > 0


def confirm_batch(batch_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE import_batches
            SET status = 'confirmed', error_message = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'precheck_ready'
            """,
            (batch_id,),
        )
        return cursor.rowcount > 0


def discard_batch(batch_id: int) -> bool:
    placeholders = ",".join("?" for _ in DISCARDABLE_BATCH_STATUSES)
    with connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE import_batches
            SET status = 'discarded', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ({placeholders})
            """,
            [batch_id, *sorted(DISCARDABLE_BATCH_STATUSES)],
        )
        return cursor.rowcount > 0


def batch_has_downloading_items(batch_id: int) -> bool:
    with connect() as conn:
        return (
            int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM download_items
                    WHERE batch_id = ? AND status = 'downloading'
                    """,
                    (batch_id,),
                ).fetchone()[0]
            )
            > 0
        )


def delete_batch_with_audit(
    batch_id: int, deleted_by_user_id: int, delete_reason: str
) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                ib.*,
                u.username AS created_by_username
            FROM import_batches ib
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            WHERE ib.id = ?
            """,
            (batch_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """
            INSERT INTO batch_deletion_audit (
                batch_id, file_name, source_path, status, created_by_user_id,
                created_by_username, order_count, item_count, link_count,
                success_count, failed_count, skipped_count, archive_path,
                deleted_by_user_id, delete_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                row["file_name"],
                row["source_path"],
                row["status"],
                row["created_by_user_id"],
                row["created_by_username"],
                int(row["order_count"] or 0),
                int(row["item_count"] or 0),
                int(row["link_count"] or 0),
                int(row["success_count"] or 0),
                int(row["failed_count"] or 0),
                int(row["skipped_count"] or 0),
                row["archive_path"],
                deleted_by_user_id,
                delete_reason[:1000],
            ),
        )
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
        return True


def list_batch_deletion_audit() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                bda.*,
                u.username AS deleted_by_username
            FROM batch_deletion_audit bda
            LEFT JOIN users u ON u.id = bda.deleted_by_user_id
            ORDER BY bda.id DESC
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def update_batch_source(batch_id: int, source_path: Path) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_batches
            SET source_path = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(source_path), batch_id),
        )


def update_batch_status(
    batch_id: int, status: str, error_message: Optional[str] = None
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_batches
            SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, error_message, batch_id),
        )


def claim_batch_processing(batch_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE import_batches
            SET status = 'processing', error_message = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN ('confirmed', 'completed_with_errors')
              AND NOT EXISTS (
                  SELECT 1
                  FROM download_items
                  WHERE batch_id = ? AND status = 'downloading'
              )
            """,
            (batch_id, batch_id),
        )
        return cursor.rowcount > 0


def set_batch_archive(batch_id: int, archive_path: Path) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_batches
            SET archive_path = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(archive_path), batch_id),
        )


def set_batch_import_summary(batch_id: int, summary: dict[str, object]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_batches
            SET import_summary_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(summary, ensure_ascii=False), batch_id),
        )


def refresh_batch_counts(batch_id: int) -> None:
    with connect() as conn:
        order_count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        link_count = conn.execute(
            "SELECT COUNT(*) FROM download_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        success_count = conn.execute(
            """
            SELECT COUNT(*) FROM download_items
            WHERE batch_id = ? AND status = 'downloaded'
            """,
            (batch_id,),
        ).fetchone()[0]
        failed_count = conn.execute(
            """
            SELECT COUNT(*) FROM download_items
            WHERE batch_id = ? AND status = 'failed'
            """,
            (batch_id,),
        ).fetchone()[0]
        skipped_count = conn.execute(
            """
            SELECT COUNT(*) FROM download_items
            WHERE batch_id = ? AND status = 'skipped'
            """,
            (batch_id,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE import_batches
            SET order_count = ?, item_count = ?, link_count = ?,
                success_count = ?, failed_count = ?, skipped_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                order_count,
                item_count,
                link_count,
                success_count,
                failed_count,
                skipped_count,
                batch_id,
            ),
        )


def insert_import_items(batch_id: int, items: list[OrderItemRow]) -> None:
    with connect() as conn:
        order_ids: dict[str, int] = {}
        for item in items:
            if item.order_no not in order_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO orders (
                        batch_id, order_no, order_date, order_date_raw, week,
                        shipping_fullname, address, city, province, zip_code,
                        country, phone, mail, customer_note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        item.order_no,
                        item.order_date,
                        item.order_date_raw,
                        item.week,
                        item.shipping_fullname,
                        item.address,
                        item.city,
                        item.province,
                        item.zip_code,
                        item.country,
                        item.phone,
                        item.mail,
                        item.customer_note,
                    ),
                )
                order_row = conn.execute(
                    """
                    SELECT id FROM orders
                    WHERE batch_id = ? AND order_no = ?
                    """,
                    (batch_id, item.order_no),
                ).fetchone()
                order_ids[item.order_no] = int(order_row["id"])

            order_id = order_ids[item.order_no]
            cursor = conn.execute(
                """
                INSERT INTO order_items (
                    batch_id, order_id, row_number, sku, size, color, quantity,
                    custom_name, tracking_no, carrier, product_id, design_link,
                    mockup_link, parent_item_name_local, parent_item_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    order_id,
                    item.row_number,
                    item.sku,
                    item.size,
                    item.color,
                    item.quantity,
                    item.custom_name,
                    item.tracking_no,
                    item.carrier,
                    item.product_id,
                    item.design_link,
                    item.mockup_link,
                    item.parent_item_name_local,
                    item.parent_item_name,
                ),
            )
            order_item_id = int(cursor.lastrowid)
            design_link = (item.design_link or "").strip()
            mockup_link = (item.mockup_link or "").strip()
            download_links = [("design", design_link)]
            if mockup_link and mockup_link != design_link:
                download_links.append(("mockup", mockup_link))
            for source_type, source_url in download_links:
                if not source_url:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO download_items (
                        batch_id, order_id, order_item_id, order_no, row_number, sku,
                        design_link, source_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        order_id,
                        order_item_id,
                        item.order_no,
                        item.row_number,
                        item.sku,
                        source_url,
                        source_type,
                    ),
                )
    refresh_batch_counts(batch_id)


def list_batches() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                ib.*,
                u.username AS created_by_username
            FROM import_batches ib
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            ORDER BY ib.id DESC
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def list_batches_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    if user["role"] in {"developer", "admin"}:
        return list_batches()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                ib.*,
                u.username AS created_by_username
            FROM import_batches ib
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            WHERE ib.created_by_user_id = ?
            ORDER BY ib.id DESC
            """,
            (int(user["id"]),),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def list_download_usage_by_user() -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in FORMAL_BATCH_STATUSES)
    formal_statuses = sorted(FORMAL_BATCH_STATUSES)
    with connect() as conn:
        batch_rows = conn.execute(
            f"""
            SELECT
                ib.created_by_user_id AS user_id,
                COALESCE(u.username, '历史批次') AS username,
                COALESCE(u.role, 'legacy') AS role,
                COUNT(*) AS formal_batch_count,
                COALESCE(SUM(ib.order_count), 0) AS order_count,
                COALESCE(SUM(ib.item_count), 0) AS item_count,
                COALESCE(SUM(ib.link_count), 0) AS download_item_count,
                COALESCE(SUM(ib.success_count), 0) AS downloaded_count,
                COALESCE(SUM(ib.failed_count), 0) AS failed_count
            FROM import_batches ib
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            WHERE ib.status IN ({placeholders})
            GROUP BY ib.created_by_user_id, u.username, u.role
            ORDER BY downloaded_count DESC, formal_batch_count DESC, username
            """,
            formal_statuses,
        ).fetchall()
        stats = {}
        for row in batch_rows:
            key = row["user_id"]
            stats[key] = {
                **row_to_dict(row),
                "manual_done_count": 0,
                "pending_count": 0,
                "processing_count": 0,
            }
        download_rows = conn.execute(
            f"""
            SELECT
                ib.created_by_user_id AS user_id,
                di.status,
                COUNT(*) AS count
            FROM import_batches ib
            JOIN download_items di ON di.batch_id = ib.id
            WHERE ib.status IN ({placeholders})
            GROUP BY ib.created_by_user_id, di.status
            """,
            formal_statuses,
        ).fetchall()
        for row in download_rows:
            key = row["user_id"]
            if key not in stats:
                continue
            status = row["status"]
            count = int(row["count"])
            if status == "manual_done":
                stats[key]["manual_done_count"] = count
            elif status == "pending":
                stats[key]["pending_count"] = count
            elif status == "downloading":
                stats[key]["processing_count"] = count
        return list(stats.values())


def download_usage_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    fields = [
        "formal_batch_count",
        "order_count",
        "item_count",
        "download_item_count",
        "downloaded_count",
        "failed_count",
        "manual_done_count",
        "pending_count",
        "processing_count",
    ]
    return {field: sum(int(row.get(field) or 0) for row in rows) for field in fields}


def get_monthly_team_quota(quota_month: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                mtq.*,
                u.username AS updated_by_username
            FROM monthly_team_quotas mtq
            LEFT JOIN users u ON u.id = mtq.updated_by_user_id
            WHERE mtq.quota_month = ?
            """,
            (quota_month,),
        ).fetchone()
        if row:
            return row_to_dict(row)
        return {
            "quota_month": quota_month,
            "total_quota": 0,
            "note": "",
            "updated_by_user_id": None,
            "updated_by_username": None,
            "created_at": None,
            "updated_at": None,
        }


def set_monthly_team_quota(
    quota_month: str, total_quota: int, updated_by_user_id: int, note: str = ""
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO monthly_team_quotas (
                quota_month, total_quota, note, updated_by_user_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(quota_month) DO UPDATE SET
                total_quota = excluded.total_quota,
                note = excluded.note,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (quota_month, max(0, int(total_quota)), note.strip(), updated_by_user_id),
        )


def list_monthly_quota_usage_by_user(quota_month: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                u.id AS user_id,
                u.username,
                u.role,
                u.status,
                COALESCE(usage.downloaded_count, 0) AS downloaded_count
            FROM users u
            LEFT JOIN (
                SELECT
                    ib.created_by_user_id AS user_id,
                    COUNT(*) AS downloaded_count
                FROM download_items di
                JOIN import_batches ib ON ib.id = di.batch_id
                JOIN users owner ON owner.id = ib.created_by_user_id
                WHERE di.status = 'downloaded'
                  AND di.completed_at IS NOT NULL
                  AND substr(di.completed_at, 1, 7) = ?
                  AND owner.role IN ('admin', 'operator')
                GROUP BY ib.created_by_user_id
            ) usage ON usage.user_id = u.id
            WHERE u.role IN ('admin', 'operator')
            ORDER BY downloaded_count DESC, u.role, u.username
            """,
            (quota_month,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def count_unassigned_successful_downloads() -> int:
    with connect() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM download_items di
                JOIN import_batches ib ON ib.id = di.batch_id
                JOIN users owner ON owner.id = ib.created_by_user_id
                WHERE di.status = 'downloaded'
                  AND di.completed_at IS NULL
                  AND owner.role IN ('admin', 'operator')
                """
            ).fetchone()[0]
        )


def monthly_quota_totals(
    quota: dict[str, Any], usage_rows: list[dict[str, Any]]
) -> dict[str, int]:
    total_quota = max(0, int(quota.get("total_quota") or 0))
    used_count = sum(int(row.get("downloaded_count") or 0) for row in usage_rows)
    return {
        "total_quota": total_quota,
        "used_count": used_count,
        "remaining_count": max(0, total_quota - used_count),
        "overage_count": max(0, used_count - total_quota),
    }


def get_batch(batch_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                ib.*,
                u.username AS created_by_username
            FROM import_batches ib
            LEFT JOIN users u ON u.id = ib.created_by_user_id
            WHERE ib.id = ?
            """,
            (batch_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def get_order(order_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return row_to_dict(row) if row else None


def get_batch_orders(batch_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        orders = [
            row_to_dict(row)
            for row in conn.execute(
                """
                SELECT * FROM orders
                WHERE batch_id = ?
                ORDER BY order_no
                """,
                (batch_id,),
            ).fetchall()
        ]
        for order in orders:
            item_rows = conn.execute(
                """
                SELECT oi.*
                FROM order_items oi
                WHERE oi.order_id = ?
                ORDER BY oi.row_number
                """,
                (order["id"],),
            ).fetchall()
            items = [row_to_dict(row) for row in item_rows]
            for item in items:
                download_rows = conn.execute(
                    """
                    SELECT
                        id AS download_item_id,
                        design_link,
                        source_type,
                        status AS download_status,
                        image_count AS download_image_count,
                        error_message AS download_error,
                        error_code AS download_error_code,
                        error_detail AS download_error_detail,
                        started_at AS download_started_at,
                        completed_at AS download_completed_at
                    FROM download_items
                    WHERE order_item_id = ?
                    ORDER BY CASE source_type WHEN 'design' THEN 1 WHEN 'mockup' THEN 2 ELSE 3 END, id
                    """,
                    (item["id"],),
                ).fetchall()
                item["download_items"] = [row_to_dict(row) for row in download_rows]
                item["download_status"] = combined_download_status(item["download_items"])
                item["download_image_count"] = sum(
                    int(row["download_image_count"] or 0)
                    for row in item["download_items"]
                )
                failed = next(
                    (
                        row
                        for row in item["download_items"]
                        if row["download_status"] == "failed"
                    ),
                    None,
                )
                item["download_error"] = failed["download_error"] if failed else None
                item["download_error_code"] = failed["download_error_code"] if failed else None
                item["download_error_detail"] = failed["download_error_detail"] if failed else None
                item["download_item_id"] = (
                    item["download_items"][0]["download_item_id"]
                    if item["download_items"]
                    else None
                )
            order["items"] = items
        return orders


def combined_download_status(download_items: list[dict[str, Any]]) -> str:
    if not download_items:
        return "pending"
    statuses = {row["download_status"] for row in download_items}
    if "downloading" in statuses:
        return "downloading"
    if "failed" in statuses:
        return "failed"
    if statuses <= {"downloaded", "manual_done"}:
        return "downloaded"
    if "pending" in statuses:
        return "pending"
    return next(iter(statuses))


def get_pending_download_items(batch_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.batch_id = ? AND di.status IN ('pending', 'failed')
            ORDER BY di.id
            """,
            (batch_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_extension_download_items(batch_id: int, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.batch_id = ? AND di.status IN ('pending', 'failed')
            ORDER BY CASE di.status WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, di.id
            LIMIT ?
            """,
            (batch_id, limit),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_next_extension_download_item(
    batch_id: int,
    retryable_error_codes: set[str],
    max_attempts: int,
) -> Optional[dict[str, Any]]:
    max_attempts = max(1, int(max_attempts))
    with connect() as conn:
        retryable_codes = sorted(code for code in retryable_error_codes if code)
        failed_clause = ""
        params: list[Any] = [batch_id]
        if retryable_codes:
            placeholders = ",".join("?" for _ in retryable_codes)
            failed_clause = f"""
                OR (
                    di.status = 'failed'
                    AND COALESCE(di.attempt_count, 0) < ?
                    AND di.error_code IN ({placeholders})
                )
            """
            params.append(max_attempts)
            params.extend(retryable_codes)
        row = conn.execute(
            f"""
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.batch_id = ?
              AND (
                di.status = 'pending'
                {failed_clause}
              )
            ORDER BY
                CASE di.status WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,
                di.id
            LIMIT 1
            """,
            params,
        ).fetchone()
        return row_to_dict(row) if row else None


def get_failed_download_items(batch_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.batch_id = ? AND di.status = 'failed'
            ORDER BY di.id
            """,
            (batch_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_download_item(download_item_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.id = ?
            """,
            (download_item_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def get_batch_status_counts(batch_id: int) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM download_items
            WHERE batch_id = ?
            GROUP BY status
            """,
            (batch_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        for status in [
            "pending",
            "downloading",
            "downloaded",
            "failed",
            "skipped",
            "manual_done",
        ]:
            counts.setdefault(status, 0)
        return counts


def mark_download_started(download_item_id: int) -> None:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE download_items
            SET status = 'downloading', error_message = NULL,
                error_code = NULL, error_detail = NULL, image_count = 0,
                started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP,
                completed_at = NULL
            WHERE id = ?
            """,
            (download_item_id,),
        )


def dispatch_download_item(download_item_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE download_items
            SET status = 'downloading', error_message = NULL,
                error_code = NULL, error_detail = NULL, image_count = 0,
                attempt_count = COALESCE(attempt_count, 0) + 1,
                started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP,
                completed_at = NULL
            WHERE id = ? AND status IN ('pending', 'failed')
            """,
            (download_item_id,),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku,
                ib.download_name AS batch_download_name
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            JOIN import_batches ib ON ib.id = di.batch_id
            WHERE di.id = ?
            """,
            (download_item_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def mark_download_heartbeat(download_item_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE download_items
            SET heartbeat_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'downloading'
            """,
            (download_item_id,),
        )
        return cursor.rowcount > 0


def mark_download_success(download_item_id: int, image_count: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'downloaded', image_count = ?, error_message = NULL,
                error_code = NULL, error_detail = NULL,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (image_count, download_item_id),
        )


def mark_download_failed(
    download_item_id: int,
    error_message: str,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    image_count: Optional[int] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'failed', error_message = ?,
                error_code = ?, error_detail = ?, image_count = COALESCE(?, image_count),
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                error_message[:1000],
                error_code,
                error_detail[:4000] if error_detail else None,
                image_count,
                download_item_id,
            ),
        )


def mark_download_retry_pending(
    download_item_id: int,
    error_message: str,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    image_count: Optional[int] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'pending', error_message = ?,
                error_code = ?, error_detail = ?, image_count = COALESCE(?, image_count),
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                error_message[:1000],
                error_code,
                error_detail[:4000] if error_detail else None,
                image_count,
                download_item_id,
            ),
        )


def request_server_download_stop(batch_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE import_batches
            SET server_stop_requested = 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (batch_id,),
        )
        return cursor.rowcount > 0


def clear_server_download_stop(batch_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE import_batches
            SET server_stop_requested = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (batch_id,),
        )


def server_download_stop_requested(batch_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT server_stop_requested
            FROM import_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()
        return bool(row and int(row["server_stop_requested"] or 0))


def reset_download_attempts(download_item_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE download_items SET attempt_count = 0 WHERE id = ?",
            (download_item_id,),
        )


def reset_failed_attempts(batch_id: int, limit: Optional[int] = None) -> None:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM download_items
            WHERE batch_id = ? AND status = 'failed'
            ORDER BY id
            """,
            (batch_id,),
        ).fetchall()
        item_ids = [int(row["id"]) for row in rows]
        if limit is not None and limit > 0:
            item_ids = item_ids[:limit]
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"UPDATE download_items SET attempt_count = 0 WHERE id IN ({placeholders})",
            item_ids,
        )


def mark_download_manual_done(download_item_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'manual_done', error_message = NULL,
                error_code = NULL, error_detail = NULL,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (download_item_id,),
        )


def enqueue_production_outbox_for_batch(batch_id: int) -> int:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                oi.id AS order_item_id,
                oi.order_id,
                o.order_no,
                oi.sku,
                COUNT(di.id) AS download_count,
                SUM(CASE WHEN di.status IN ('downloaded', 'manual_done') THEN 1 ELSE 0 END) AS ready_count
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            LEFT JOIN download_items di ON di.order_item_id = oi.id
            WHERE oi.batch_id = ?
            GROUP BY oi.id
            HAVING download_count > 0 AND download_count = ready_count
            """,
            (batch_id,),
        ).fetchall()
        inserted = 0
        for row in rows:
            sku = row["sku"] or f"item-{row['order_item_id']}"
            idempotency_key = f"batch:{batch_id}:order:{row['order_id']}:sku:{sku}"
            payload = {
                "batch_id": batch_id,
                "order_id": int(row["order_id"]),
                "order_no": row["order_no"],
                "sku": sku,
            }
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO production_outbox (
                    batch_id, order_id, order_no, sku, idempotency_key, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    int(row["order_id"]),
                    row["order_no"],
                    sku,
                    idempotency_key,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            inserted += cursor.rowcount
        return inserted


def add_downloaded_file(
    download_item_id: int,
    batch_id: int,
    order_id: int,
    order_no: str,
    file_name: str,
    local_path: Path,
    file_size: int,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO downloaded_files (
                download_item_id, batch_id, order_id, order_no,
                file_name, local_path, file_size
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                download_item_id,
                batch_id,
                order_id,
                order_no,
                file_name,
                str(local_path),
                file_size,
            ),
        )


def clear_downloaded_files(download_item_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM downloaded_files WHERE download_item_id = ?",
            (download_item_id,),
        )


def replace_downloaded_files_for_item(
    download_item_id: int, files: list[dict[str, Any]]
) -> None:
    item = get_download_item(download_item_id)
    if not item:
        return
    with connect() as conn:
        conn.execute(
            "DELETE FROM downloaded_files WHERE download_item_id = ?",
            (download_item_id,),
        )
        for file_info in files:
            file_name = str(file_info.get("file_name") or "").strip()
            local_path = str(file_info.get("local_path") or file_name).strip()
            if not file_name:
                continue
            try:
                file_size = int(file_info.get("file_size") or 0)
            except (TypeError, ValueError):
                file_size = 0
            conn.execute(
                """
                INSERT INTO downloaded_files (
                    download_item_id, batch_id, order_id, order_no,
                    file_name, local_path, file_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    download_item_id,
                    int(item["batch_id"]),
                    int(item["order_id"]),
                    item["order_no"],
                    file_name,
                    local_path,
                    max(0, file_size),
                ),
            )


def record_extension_event(
    batch_id: int,
    download_item_id: Optional[int],
    event: str,
    level: str = "info",
    message: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> int:
    safe_level = level if level in {"debug", "info", "warning", "error"} else "info"
    detail_json = None
    if detail is not None:
        detail_json = json.dumps(detail, ensure_ascii=False, default=str)[:8000]
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO extension_events (
                batch_id, download_item_id, event, level, message, detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                download_item_id,
                str(event or "event")[:120],
                safe_level,
                str(message or "")[:1000],
                detail_json,
            ),
        )
        return int(cursor.lastrowid)


def list_extension_events(batch_id: int, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM extension_events
            WHERE batch_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (batch_id, limit),
        ).fetchall()
        return [row_to_dict(row) for row in rows]
