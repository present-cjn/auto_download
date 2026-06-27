from __future__ import annotations

from datetime import datetime

from app.main import (
    batch_detail_tab,
    batch_primary_action,
    batch_work_state,
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


def test_batch_work_state_prioritizes_failed_items() -> None:
    batch = {"status": "completed_with_errors"}
    counts = {
        "pending": 0,
        "failed": 2,
        "downloading": 0,
    }

    state = batch_work_state(batch, counts)
    action = batch_primary_action(batch, counts)

    assert state["code"] == "action_required"
    assert state["label"] == "需要处理"
    assert action["title"] == "有失败项需要处理"
    assert action["cta"] == "处理失败项"


def test_batch_primary_action_for_ready_batch() -> None:
    batch = {"status": "confirmed"}
    counts = {
        "pending": 3,
        "failed": 0,
        "downloading": 0,
    }

    action = batch_primary_action(batch, counts)

    assert action["code"] == "ready"
    assert action["title"] == "可以下载图片"
    assert action["cta"] == "开始下载图片"


def test_batch_primary_action_for_precheck_ready_batch() -> None:
    batch = {"status": "precheck_ready"}
    counts = {
        "pending": 3,
        "failed": 0,
        "downloading": 0,
    }

    action = batch_primary_action(batch, counts)

    assert action["code"] == "needs_confirm"
    assert action["title"] == "上传预检已完成"
    assert action["cta"] == "确认导入"


def test_batch_detail_tab_matches_batch_phase() -> None:
    precheck_context = batch_detail_tab({"status": "precheck_ready"})
    download_context = batch_detail_tab({"status": "confirmed"})
    forced_precheck_context = batch_detail_tab({"status": "precheck_ready"}, "download")
    discarded_context = batch_detail_tab({"status": "discarded"})

    assert precheck_context == {
        "active_tab": "precheck",
        "can_show_download_tab": False,
    }
    assert download_context == {
        "active_tab": "download",
        "can_show_download_tab": True,
    }
    assert forced_precheck_context["active_tab"] == "precheck"
    assert discarded_context["active_tab"] == "precheck"
