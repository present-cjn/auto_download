from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.core.excel_parser import OrderItemRow


DB_PATH = Path("data/app.db")


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
    started_at TEXT,
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

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'operator')),
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
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
            error_detail, started_at, completed_at, created_at
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
            di.started_at,
            di.completed_at,
            di.created_at
        FROM download_items_legacy di
        LEFT JOIN order_items oi ON oi.id = di.order_item_id
        """
    )

    conn.executescript(DOWNLOADED_FILES_SCHEMA)
    if downloaded_file_columns:
        conn.execute(
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


def ensure_schema_columns(conn: sqlite3.Connection) -> None:
    if table_columns(conn, "import_batches"):
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
    if table_columns(conn, "download_items"):
        ensure_column(conn, "download_items", "error_code", "error_code TEXT")
        ensure_column(conn, "download_items", "error_detail", "error_detail TEXT")
        if "source_type" not in table_columns(conn, "download_items"):
            migrate_download_items_schema(conn)
    if table_columns(conn, "order_items"):
        ensure_column(conn, "order_items", "mockup_link", "mockup_link TEXT")
        ensure_column(conn, "order_items", "carrier", "carrier TEXT")


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
        WHERE status IN ('downloading', 'parsing', 'pending')
        """
    ).fetchall()
    for row in rows:
        batch_id = int(row["id"])
        item_count = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        if row["status"] == "pending" and item_count == 0:
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
            next_status = "pending"
        elif int(current_counts["failed_count"]) > 0:
            next_status = "completed_with_errors"
        elif int(current_counts["success_count"]) > 0:
            next_status = "completed"
        else:
            next_status = "review_ready"
        conn.execute(
            """
            UPDATE import_batches
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_status, batch_id),
        )


def init_db(db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        migrate_download_items_schema(conn)
        conn.executescript(USERS_SCHEMA)
        conn.executescript(SESSIONS_SCHEMA)
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                order_count INTEGER NOT NULL DEFAULT 0,
                item_count INTEGER NOT NULL DEFAULT 0,
                link_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                archive_path TEXT,
                error_message TEXT,
                import_summary_json TEXT,
                created_by_user_id INTEGER REFERENCES users(id),
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
                started_at TEXT,
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
        cursor = conn.execute(
            """
            INSERT INTO import_batches (
                file_name, source_path, status, created_by_user_id
            )
            VALUES (?, ?, 'pending', ?)
            """,
            (file_name, str(source_path), created_by_user_id),
        )
        return int(cursor.lastrowid)


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
            download_links = [
                ("design", item.design_link),
                ("mockup", item.mockup_link),
            ]
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
    if user["role"] == "admin":
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
                oi.sku AS item_sku
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
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
                oi.sku AS item_sku
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
            WHERE di.batch_id = ? AND di.status IN ('pending', 'failed')
            ORDER BY di.id
            LIMIT ?
            """,
            (batch_id, limit),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_failed_download_items(batch_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                di.*,
                oi.sku AS item_sku
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
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
                oi.sku AS item_sku
            FROM download_items di
            JOIN order_items oi ON oi.id = di.order_item_id
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
        conn.execute(
            """
            UPDATE download_items
            SET status = 'downloading', error_message = NULL,
                error_code = NULL, error_detail = NULL,
                started_at = CURRENT_TIMESTAMP, completed_at = NULL
            WHERE id = ?
            """,
            (download_item_id,),
        )


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
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'failed', error_message = ?,
                error_code = ?, error_detail = ?,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                error_message[:1000],
                error_code,
                error_detail[:4000] if error_detail else None,
                download_item_id,
            ),
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
