from __future__ import annotations

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
    status TEXT NOT NULL DEFAULT 'pending',
    image_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(batch_id, order_item_id, design_link)
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


def migrate_download_items_schema(conn: sqlite3.Connection) -> None:
    existing_columns = table_columns(conn, "download_items")
    if not existing_columns or "sku" in existing_columns:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    downloaded_file_columns = table_columns(conn, "downloaded_files")
    conn.execute("ALTER TABLE download_items RENAME TO download_items_legacy")
    if downloaded_file_columns:
        conn.execute("ALTER TABLE downloaded_files RENAME TO downloaded_files_legacy")

    conn.executescript(DOWNLOAD_ITEMS_SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO download_items (
            id, batch_id, order_id, order_item_id, order_no, row_number, sku,
            design_link, status, image_count, error_message, started_at,
            completed_at, created_at
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
            di.status,
            di.image_count,
            di.error_message,
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


def backfill_missing_download_items(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO download_items (
            batch_id, order_id, order_item_id, order_no, row_number, sku, design_link
        )
        SELECT
            oi.batch_id,
            oi.order_id,
            oi.id,
            o.order_no,
            oi.row_number,
            oi.sku,
            oi.design_link
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.design_link IS NOT NULL
          AND oi.design_link != ''
        """
    )


def refresh_all_batch_counts(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id FROM import_batches").fetchall()
    for row in rows:
        batch_id = int(row["id"])
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


def init_db(db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        migrate_download_items_schema(conn)
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
                product_id TEXT,
                design_link TEXT,
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
                status TEXT NOT NULL DEFAULT 'pending',
                image_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(batch_id, order_item_id, design_link)
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
        backfill_missing_download_items(conn)
        refresh_all_batch_counts(conn)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create_batch(file_name: str, source_path: Path) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO import_batches (file_name, source_path, status)
            VALUES (?, ?, 'pending')
            """,
            (file_name, str(source_path)),
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
                    custom_name, tracking_no, product_id, design_link,
                    parent_item_name_local, parent_item_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item.product_id,
                    item.design_link,
                    item.parent_item_name_local,
                    item.parent_item_name,
                ),
            )
            order_item_id = int(cursor.lastrowid)
            if item.design_link:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO download_items (
                        batch_id, order_id, order_item_id, order_no, row_number, sku, design_link
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        order_id,
                        order_item_id,
                        item.order_no,
                        item.row_number,
                        item.sku,
                        item.design_link,
                    ),
                )
    refresh_batch_counts(batch_id)


def list_batches() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM import_batches
            ORDER BY id DESC
            """
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_batch(batch_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
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
                SELECT
                    oi.*,
                    di.id AS download_item_id,
                    di.status AS download_status,
                    di.image_count AS download_image_count,
                    di.error_message AS download_error
                FROM order_items oi
                LEFT JOIN download_items di
                    ON di.batch_id = oi.batch_id
                    AND di.order_item_id = oi.id
                WHERE oi.order_id = ?
                ORDER BY oi.row_number
                """,
                (order["id"],),
            ).fetchall()
            order["items"] = [row_to_dict(row) for row in item_rows]
        return orders


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


def mark_download_started(download_item_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'downloading', error_message = NULL,
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
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (image_count, download_item_id),
        )


def mark_download_failed(download_item_id: int, error_message: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE download_items
            SET status = 'failed', error_message = ?,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error_message[:1000], download_item_id),
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
