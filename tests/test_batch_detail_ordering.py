from __future__ import annotations

from datetime import datetime

from app.main import (
    enrich_download_task_duration,
    format_duration_seconds,
    sort_rows_by_excel_row,
)


def test_sort_rows_by_excel_row() -> None:
    rows = [
        {"item": {"row_number": 5}},
        {"item": {"row_number": 2}},
        {"item": {"row_number": 9}},
    ]

    sorted_rows = sort_rows_by_excel_row(rows)

    assert [row["item"]["row_number"] for row in sorted_rows] == [2, 5, 9]


def test_download_duration_labels() -> None:
    assert format_duration_seconds(7) == "7s"
    assert format_duration_seconds(67) == "1m 7s"
    assert format_duration_seconds(3667) == "1h 1m 7s"

    downloaded = {
        "download_status": "downloaded",
        "download_started_at": "2026-06-10 01:00:00",
        "download_completed_at": "2026-06-10 01:01:07",
    }
    downloading = {
        "download_status": "downloading",
        "download_started_at": "2026-06-10 01:00:00",
        "download_completed_at": None,
    }

    enrich_download_task_duration(downloaded)
    enrich_download_task_duration(downloading, datetime(2026, 6, 10, 1, 0, 9))

    assert downloaded["download_duration_label"] == "耗时 1m 7s"
    assert downloading["download_duration_label"] == "已用时 9s"
