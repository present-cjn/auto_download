#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PACER_MIN_SLEEP = "500ms"
DEFAULT_PACER_BURST = "5"
DEFAULT_TRANSFERS = "1"
DEFAULT_CHECKERS = "1"


DRIVE_FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
DRIVE_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
DRIVE_ID_RE = re.compile(r"(?:[?&]id=)([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class CaseResult:
    name: str
    case_type: str
    status: str
    elapsed_seconds: float
    file_count: int
    total_bytes: int
    log_path: str
    error_summary: str


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return cleaned or "case"


def extract_drive_id(value: str, expected_type: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Drive ID or URL is empty.")
    if "drive.google.com" not in value and "docs.google.com" not in value:
        return value

    patterns = [DRIVE_FILE_RE, DRIVE_FOLDER_RE, DRIVE_ID_RE]
    for pattern in patterns:
        match = pattern.search(value)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract {expected_type} ID from Drive URL: {value}")


def load_manifest(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON list of test cases.")
    for index, case in enumerate(data, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"Case #{index} must be a JSON object.")
        if not case.get("name"):
            raise ValueError(f"Case #{index} is missing name.")
        if case.get("type") not in {"file_id", "folder_id", "path"}:
            raise ValueError(
                f"Case {case.get('name')!r} type must be file_id, folder_id, or path."
            )
    return data


def run_command(command: list[str], log_path: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n===== {started_at} =====\n")
        log_file.write("$ " + " ".join(command) + "\n\n")

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    with log_path.open("a", encoding="utf-8") as log_file:
        if completed.stdout:
            log_file.write("--- stdout ---\n")
            log_file.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                log_file.write("\n")
        if completed.stderr:
            log_file.write("--- stderr ---\n")
            log_file.write(completed.stderr)
            if not completed.stderr.endswith("\n"):
                log_file.write("\n")
        log_file.write(f"--- exit_code: {completed.returncode} ---\n")

    return completed


def file_stats(directory: Path) -> tuple[int, int]:
    if not directory.exists():
        return 0, 0
    files = [path for path in directory.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def summarize_error(log_path: Path, completed: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(part for part in [completed.stderr, completed.stdout] if part).strip()
    if not combined and log_path.exists():
        combined = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return ""
    interesting = [
        line
        for line in lines
        if any(
            token in line.lower()
            for token in ["error", "failed", "denied", "rate", "quota", "not found", "cannot"]
        )
    ]
    selected = interesting[-3:] if interesting else lines[-3:]
    return " | ".join(selected)[:1000]


def common_rclone_flags(args: argparse.Namespace, log_path: Path) -> list[str]:
    return [
        "--drive-pacer-min-sleep",
        args.drive_pacer_min_sleep,
        "--drive-pacer-burst",
        args.drive_pacer_burst,
        "-vv",
        "--log-file",
        str(log_path),
    ]


def copy_flags(args: argparse.Namespace) -> list[str]:
    return ["--transfers", args.transfers, "--checkers", args.checkers]


def build_command(
    case: dict[str, Any],
    args: argparse.Namespace,
    case_output_dir: Path,
    log_path: Path,
) -> list[str]:
    case_type = case["type"]
    remote = args.remote.rstrip(":") + ":"
    base = [args.rclone_bin]
    common = common_rclone_flags(args, log_path)

    if case_type == "file_id":
        file_id = case.get("id") or case.get("url")
        if not file_id:
            raise ValueError(f"Case {case['name']!r} missing id or url.")
        file_id = extract_drive_id(str(file_id), "file")
        target_name = safe_name(case.get("target_name") or case["name"])
        target = case_output_dir / target_name
        return base + ["backend", "copyid", remote, file_id, str(target)] + common

    if case_type == "folder_id":
        folder_id = case.get("id") or case.get("url")
        if not folder_id:
            raise ValueError(f"Case {case['name']!r} missing id or url.")
        folder_id = extract_drive_id(str(folder_id), "folder")
        return (
            base
            + ["copy", remote, str(case_output_dir)]
            + copy_flags(args)
            + ["--drive-root-folder-id", folder_id]
            + common
        )

    path = case.get("path")
    if not path:
        raise ValueError(f"Case {case['name']!r} missing path.")
    source = remote + str(path).lstrip(":")
    mode = case.get("mode", "copy")
    if mode == "copyto":
        target_name = safe_name(case.get("target_name") or Path(str(path)).name or case["name"])
        return base + ["copyto", source, str(case_output_dir / target_name)] + copy_flags(args) + common
    if mode == "copy":
        return base + ["copy", source, str(case_output_dir)] + copy_flags(args) + common
    raise ValueError(f"Case {case['name']!r} path mode must be copy or copyto.")


def run_case(case: dict[str, Any], args: argparse.Namespace) -> CaseResult:
    name = safe_name(str(case["name"]))
    case_output_dir = args.output_dir / name
    log_path = args.log_dir / f"{name}.log"
    if args.clean and case_output_dir.exists():
        shutil.rmtree(case_output_dir)
    case_output_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and args.clean_logs:
        log_path.unlink()

    command = build_command(case, args, case_output_dir, log_path)
    started = time.monotonic()
    try:
        completed = run_command(command, log_path, timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"--- timeout after {args.timeout}s ---\n")
            if exc.stdout:
                log_file.write(str(exc.stdout))
            if exc.stderr:
                log_file.write(str(exc.stderr))
        file_count, total_bytes = file_stats(case_output_dir)
        return CaseResult(
            name=name,
            case_type=case["type"],
            status="timeout",
            elapsed_seconds=elapsed,
            file_count=file_count,
            total_bytes=total_bytes,
            log_path=str(log_path),
            error_summary=f"timeout after {args.timeout}s",
        )

    elapsed = time.monotonic() - started
    file_count, total_bytes = file_stats(case_output_dir)
    status = "success" if completed.returncode == 0 and file_count > 0 else "failed"
    return CaseResult(
        name=name,
        case_type=case["type"],
        status=status,
        elapsed_seconds=elapsed,
        file_count=file_count,
        total_bytes=total_bytes,
        log_path=str(log_path),
        error_summary="" if status == "success" else summarize_error(log_path, completed),
    )


def print_summary(results: list[CaseResult]) -> None:
    print("\nRclone PoC summary")
    print("==================")
    for result in results:
        print(
            f"{result.status.upper():7} {result.name} "
            f"type={result.case_type} files={result.file_count} "
            f"bytes={result.total_bytes} elapsed={result.elapsed_seconds:.1f}s"
        )
        if result.error_summary:
            print(f"  error: {result.error_summary}")
        print(f"  log: {result.log_path}")

    success_count = sum(1 for result in results if result.status == "success")
    print(f"\nTotal: {len(results)}, success: {success_count}, failed: {len(results) - success_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run rclone Google Drive download PoC cases.")
    parser.add_argument("--manifest", required=True, type=Path, help="JSON manifest with PoC cases.")
    parser.add_argument("--remote", default="gdrive", help="rclone remote name, default: gdrive")
    parser.add_argument("--output-dir", default=Path("rclone-test"), type=Path)
    parser.add_argument("--log-dir", default=Path("rclone-results"), type=Path)
    parser.add_argument("--rclone-bin", default="rclone")
    parser.add_argument("--limit", type=int, default=0, help="Run only the first N cases.")
    parser.add_argument("--timeout", type=int, default=900, help="Per-case timeout seconds.")
    parser.add_argument("--transfers", default=DEFAULT_TRANSFERS)
    parser.add_argument("--checkers", default=DEFAULT_CHECKERS)
    parser.add_argument("--drive-pacer-min-sleep", default=DEFAULT_PACER_MIN_SLEEP)
    parser.add_argument("--drive-pacer-burst", default=DEFAULT_PACER_BURST)
    parser.add_argument("--clean", action="store_true", help="Delete each case output directory before running.")
    parser.add_argument("--clean-logs", action="store_true", help="Delete each case log before running.")
    args = parser.parse_args()

    cases = load_manifest(args.manifest)
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("No cases to run.", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)

    preflight_log = args.log_dir / "preflight.log"
    for command in ([args.rclone_bin, "version"], [args.rclone_bin, "listremotes"]):
        completed = run_command(command, preflight_log, timeout=60)
        if completed.returncode != 0:
            print(f"Preflight failed: {' '.join(command)}", file=sys.stderr)
            print(summarize_error(preflight_log, completed), file=sys.stderr)
            return completed.returncode or 1

    results = [run_case(case, args) for case in cases]
    print_summary(results)

    summary_path = args.log_dir / "summary.json"
    summary_path.write_text(
        json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote summary: {summary_path}")
    return 0 if all(result.status == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
