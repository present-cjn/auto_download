from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Optional


DEFAULT_DB_PATH = Path("data/app.db")
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Event:
    id: int
    batch_id: int
    download_item_id: Optional[int]
    event: str
    level: str
    message: str
    detail: dict[str, Any]
    created_at: datetime


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, DATETIME_FORMAT)


def parse_detail(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"raw_detail_json": value}
    return parsed if isinstance(parsed, dict) else {"detail": parsed}


def seconds_between(start: Event, end: Event) -> float:
    return max(0.0, (end.created_at - start.created_at).total_seconds())


def percentile(values: list[float], percent: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * percent))
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarize_values(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "avg": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 3),
        "median": round(float(median(values)), 3),
        "p95": round(float(percentile(values, 0.95)), 3),
        "max": round(max(values), 3),
    }


def first_event(events: list[Event], name: str) -> Optional[Event]:
    return next((event for event in events if event.event == name), None)


def last_event(events: list[Event], names: set[str]) -> Optional[Event]:
    for event in reversed(events):
        if event.event in names:
            return event
    return None


def paired_duration(events: list[Event], start_name: str, end_name: str) -> Optional[float]:
    start = first_event(events, start_name)
    end = last_event(events, {end_name})
    if not start or not end or end.created_at < start.created_at:
        return None
    return seconds_between(start, end)


def event_elapsed_seconds(event: Event) -> Optional[float]:
    raw = event.detail.get("elapsedMs")
    try:
        return round(float(raw) / 1000, 3)
    except (TypeError, ValueError):
        return None


def load_events(conn: sqlite3.Connection, batch_id: int) -> list[Event]:
    rows = conn.execute(
        """
        SELECT id, batch_id, download_item_id, event, level, message, detail_json, created_at
        FROM extension_events
        WHERE batch_id = ?
        ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
    return [
        Event(
            id=int(row["id"]),
            batch_id=int(row["batch_id"]),
            download_item_id=(
                int(row["download_item_id"]) if row["download_item_id"] is not None else None
            ),
            event=str(row["event"]),
            level=str(row["level"] or "info"),
            message=str(row["message"] or ""),
            detail=parse_detail(row["detail_json"]),
            created_at=parse_timestamp(str(row["created_at"])),
        )
        for row in rows
    ]


def load_download_items(conn: sqlite3.Connection, batch_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, sku, source_type, status, image_count, attempt_count, error_code, error_message
        FROM download_items
        WHERE batch_id = ?
        ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def load_downloaded_file_counts(conn: sqlite3.Connection, batch_id: int) -> dict[int, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT download_item_id, COUNT(*) AS file_count, COALESCE(SUM(file_size), 0) AS total_bytes
        FROM downloaded_files
        WHERE batch_id = ?
        GROUP BY download_item_id
        """,
        (batch_id,),
    ).fetchall()
    return {
        int(row["download_item_id"]): {
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"] or 0),
        }
        for row in rows
    }


def group_events_by_item(events: list[Event]) -> dict[int, list[Event]]:
    grouped: dict[int, list[Event]] = {}
    for event in events:
        if event.download_item_id is None:
            continue
        grouped.setdefault(event.download_item_id, []).append(event)
    return grouped


def build_item_report(
    item_id: int,
    item: dict[str, Any],
    events: list[Event],
    file_summary: dict[str, int],
) -> dict[str, Any]:
    finish = last_event(events, {"task_success", "task_failure"})
    task_start = first_event(events, "task_start")
    folder_list_duration = paired_duration(events, "folder_list_start", "folder_list_done")
    success_post_duration = paired_duration(events, "success_post_start", "success_post_done")
    failure_post_duration = paired_duration(events, "failure_post_start", "failure_post_done")
    download_complete_durations = [
        duration
        for duration in (event_elapsed_seconds(event) for event in events if event.event == "download_complete")
        if duration is not None
    ]
    retry_wait_ms = 0
    for event in events:
        if event.event != "retry_wait":
            continue
        try:
            retry_wait_ms += int(event.detail.get("waitMs") or 0)
        except (TypeError, ValueError):
            continue
    total_duration = (
        seconds_between(task_start, finish)
        if task_start and finish and finish.created_at >= task_start.created_at
        else None
    )
    return {
        "download_item_id": item_id,
        "sku": item.get("sku") or "",
        "source_type": item.get("source_type") or "",
        "status": item.get("status") or "",
        "attempt_count": int(item.get("attempt_count") or 0),
        "image_count": int(item.get("image_count") or 0),
        "file_count": int(file_summary.get("file_count", 0)),
        "total_bytes": int(file_summary.get("total_bytes", 0)),
        "error_code": item.get("error_code") or "",
        "total_seconds": round(total_duration, 3) if total_duration is not None else None,
        "folder_list_seconds": round(folder_list_duration, 3) if folder_list_duration is not None else None,
        "download_seconds": round(sum(download_complete_durations), 3),
        "download_file_count": len(download_complete_durations),
        "success_post_seconds": round(success_post_duration, 3) if success_post_duration is not None else None,
        "failure_post_seconds": round(failure_post_duration, 3) if failure_post_duration is not None else None,
        "retry_wait_seconds": round(retry_wait_ms / 1000, 3),
        "retry_count": sum(1 for event in events if event.event == "retry_wait"),
        "event_count": len(events),
        "complete": finish is not None,
    }


def build_speed_report(
    batch_id: int,
    db_path: Path = DEFAULT_DB_PATH,
    slow_limit: int = 20,
) -> dict[str, Any]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        events = load_events(conn, batch_id)
        items = load_download_items(conn, batch_id)
        file_counts = load_downloaded_file_counts(conn, batch_id)

    queue_start = first_event(events, "queue_start")
    queue_stop = last_event(events, {"queue_stop"})
    grouped_events = group_events_by_item(events)
    item_reports = [
        build_item_report(
            item_id,
            item,
            grouped_events.get(item_id, []),
            file_counts.get(item_id, {}),
        )
        for item_id, item in sorted(items.items())
    ]
    item_durations = [
        float(row["total_seconds"])
        for row in item_reports
        if row["total_seconds"] is not None
    ]
    folder_durations = [
        float(row["folder_list_seconds"])
        for row in item_reports
        if row["folder_list_seconds"] is not None
    ]
    download_durations = [
        float(row["download_seconds"])
        for row in item_reports
        if row["download_file_count"] > 0
    ]
    queue_duration = (
        seconds_between(queue_start, queue_stop)
        if queue_start and queue_stop and queue_stop.created_at >= queue_start.created_at
        else None
    )
    slowest = sorted(
        item_reports,
        key=lambda row: float(row["total_seconds"] or 0),
        reverse=True,
    )[: max(1, slow_limit)]
    return {
        "batch_id": batch_id,
        "event_count": len(events),
        "download_item_count": len(items),
        "queue_seconds": round(queue_duration, 3) if queue_duration is not None else None,
        "status_counts": {
            status: sum(1 for item in items.values() if item["status"] == status)
            for status in sorted({str(item["status"]) for item in items.values()})
        },
        "item_seconds": summarize_values(item_durations),
        "folder_list_seconds": summarize_values(folder_durations),
        "download_seconds": summarize_values(download_durations),
        "retry_count": sum(int(row["retry_count"]) for row in item_reports),
        "retry_wait_seconds": round(sum(float(row["retry_wait_seconds"]) for row in item_reports), 3),
        "incomplete_item_count": sum(1 for row in item_reports if not row["complete"]),
        "slowest_items": slowest,
    }


def format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}s"


def format_summary_block(label: str, summary: dict[str, Optional[float]]) -> str:
    if not summary["count"]:
        return f"{label}: no data"
    return (
        f"{label}: count={summary['count']} "
        f"avg={format_seconds(summary['avg'])} "
        f"median={format_seconds(summary['median'])} "
        f"p95={format_seconds(summary['p95'])} "
        f"max={format_seconds(summary['max'])}"
    )


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"Download speed report for batch #{report['batch_id']}",
        f"Events: {report['event_count']} | Download items: {report['download_item_count']}",
        f"Queue duration: {format_seconds(report['queue_seconds'])}",
        f"Status counts: {json.dumps(report['status_counts'], ensure_ascii=False, sort_keys=True)}",
        format_summary_block("Item duration", report["item_seconds"]),
        format_summary_block("Folder list duration", report["folder_list_seconds"]),
        format_summary_block("Chrome download duration", report["download_seconds"]),
        (
            f"Retries: count={report['retry_count']} "
            f"wait={format_seconds(report['retry_wait_seconds'])}"
        ),
        f"Incomplete items: {report['incomplete_item_count']}",
        "",
        "Slowest items:",
    ]
    for row in report["slowest_items"]:
        lines.append(
            "  "
            f"#{row['download_item_id']} {row['sku']} {row['source_type']} "
            f"status={row['status']} total={format_seconds(row['total_seconds'])} "
            f"folder_list={format_seconds(row['folder_list_seconds'])} "
            f"download={format_seconds(row['download_seconds'])} "
            f"files={row['file_count']} retries={row['retry_count']} "
            f"error={row['error_code'] or '-'}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report browser extension download speed by batch.")
    parser.add_argument("--batch-id", type=int, required=True, help="Import/download batch ID.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--limit-slow", type=int, default=20, help="Number of slow items to show.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_speed_report(args.batch_id, args.db_path, args.limit_slow)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text_report(report))


if __name__ == "__main__":
    main()
