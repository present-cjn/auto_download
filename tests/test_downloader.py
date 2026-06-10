from __future__ import annotations

from pathlib import Path
import time

import requests

from app.core.downloader import (
    DriveDownloadError,
    DriveDownloadTimeout,
    cached_drive_folder,
    classify_download_failure,
    copy_images,
    download_drive_file_by_id,
    extract_drive_file_id,
    extract_drive_folder_id,
    is_google_drive_url,
    next_available_path,
    parse_drive_resource,
    run_download_with_timeout,
    safe_filename,
)


def test_drive_url_detection_and_folder_id() -> None:
    url = "https://drive.google.com/drive/folders/abc123?usp=sharing&hl=en"

    assert is_google_drive_url(url)
    assert not is_google_drive_url("https://example.com/drive/folders/abc123")
    assert extract_drive_folder_id(url) == "abc123"


def test_drive_resource_parses_folder_and_file_ids() -> None:
    folder = parse_drive_resource("https://drive.google.com/drive/folders/folder123?usp=sharing")
    file = parse_drive_resource("https://drive.google.com/file/d/file123/view?usp=sharing")

    assert folder.kind == "folder"
    assert folder.resource_id == "folder123"
    assert file.kind == "file"
    assert file.resource_id == "file123"
    assert extract_drive_file_id("https://drive.google.com/file/d/file123/view") == "file123"


def test_drive_resource_rejects_unknown_drive_shape() -> None:
    try:
        parse_drive_resource("https://drive.google.com/open?id=abc123")
    except ValueError as exc:
        assert "文件夹或文件 ID" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_filename_and_copy_images(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "image.jpg").write_bytes(b"jpg")
    (source / "note.txt").write_text("ignore")
    (target).mkdir()
    (target / "image.jpg").write_bytes(b"existing")

    copied = copy_images(source, target)

    assert safe_filename('bad/name:*?.jpg') == "bad_name___.jpg"
    assert next_available_path(target, "image.jpg").name == "image(2).jpg"
    assert [file.file_name for file in copied] == ["image(1).jpg"]
    assert (target / "image(1).jpg").read_bytes() == b"jpg"


def test_download_drive_file_by_id_uses_output_directory(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class FakeGdown:
        @staticmethod
        def download(id, output, quiet, use_cookies):
            calls.append(
                {
                    "id": id,
                    "output": output,
                    "quiet": quiet,
                    "use_cookies": use_cookies,
                }
            )
            Path(output).mkdir(parents=True, exist_ok=True)
            return str(Path(output) / "mockup.jpg")

    monkeypatch.setattr("app.core.downloader.import_gdown", lambda: FakeGdown)

    download_drive_file_by_id("file123", tmp_path)

    assert calls == [
        {
            "id": "file123",
            "output": f"{tmp_path}/",
            "quiet": False,
            "use_cookies": False,
        }
    ]


def test_download_drive_file_by_id_falls_back_to_uc_url(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class FakeGdown:
        @staticmethod
        def download(id=None, url=None, output="", quiet=False, use_cookies=False):
            calls.append(
                {
                    "id": id,
                    "url": url,
                    "output": output,
                    "quiet": quiet,
                    "use_cookies": use_cookies,
                }
            )
            if id:
                raise RuntimeError("id mode failed")
            Path(output).mkdir(parents=True, exist_ok=True)
            return str(Path(output) / "mockup.jpg")

    monkeypatch.setattr("app.core.downloader.import_gdown", lambda: FakeGdown)

    download_drive_file_by_id("file123", tmp_path)

    assert calls == [
        {
            "id": "file123",
            "url": None,
            "output": f"{tmp_path}/",
            "quiet": False,
            "use_cookies": False,
        },
        {
            "id": None,
            "url": "https://drive.google.com/uc?id=file123",
            "output": f"{tmp_path}/",
            "quiet": False,
            "use_cookies": False,
        },
    ]


def test_download_drive_file_by_id_reports_attempt_details(tmp_path: Path, monkeypatch) -> None:
    class FakeGdown:
        @staticmethod
        def download(id=None, url=None, output="", quiet=False, use_cookies=False):
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / "view?usp=sharing").write_text("not an image")
            return None

    monkeypatch.setattr("app.core.downloader.import_gdown", lambda: FakeGdown)
    monkeypatch.setattr("app.core.downloader.time.sleep", lambda seconds: None)

    try:
        download_drive_file_by_id("file123", tmp_path)
    except DriveDownloadError as exc:
        message = str(exc)
        assert "resource=file:file123" in message
        assert "method=file_id" in message
        assert "method=uc_url" in message
        assert "gdown returned None" in message
        assert "view?usp=sharing" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_cached_drive_folder_uses_resource_kind_prefix(tmp_path: Path, monkeypatch) -> None:
    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)

    cache_dir = cached_drive_folder(
        "https://drive.google.com/file/d/file123/view?usp=sharing",
        tmp_path,
    )

    assert cache_dir == tmp_path / "file-file123"
    assert (cache_dir / "mockup.jpg").exists()


def test_cached_drive_folder_reports_non_image_files(tmp_path: Path, monkeypatch) -> None:
    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "view?usp=sharing").write_text("not an image")

    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)

    try:
        cached_drive_folder(
            "https://drive.google.com/file/d/file123/view?usp=sharing",
            tmp_path,
        )
    except DriveDownloadError as exc:
        message = str(exc)
        assert "no image files were found" in message
        assert "resource=file:file123" in message
        assert "view?usp=sharing" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_run_download_with_timeout_raises_timeout(tmp_path: Path) -> None:
    def slow_download(resource_id: str, output_dir: Path) -> None:
        time.sleep(2)

    try:
        run_download_with_timeout(
            slow_download,
            "folder123",
            tmp_path,
            timeout_seconds=1,
        )
    except DriveDownloadTimeout as exc:
        assert "超过 1 秒" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadTimeout")


def test_classify_download_timeout() -> None:
    failure = classify_download_failure(DriveDownloadTimeout("too slow"))

    assert failure.code == "download_timeout"
    assert "超时" in failure.message


def test_classify_download_failure() -> None:
    network = classify_download_failure(requests.exceptions.SSLError("ssl broke"))
    invalid = classify_download_failure(ValueError("不是 Google Drive 链接"))
    rate_limited = classify_download_failure(
        DriveDownloadError("FileURLRetrievalError: Cannot retrieve the public link")
    )
    empty = classify_download_failure(
        DriveDownloadError("Google Drive folder downloaded, but no image files were found")
    )
    unknown = classify_download_failure(RuntimeError("boom"))

    assert network.code == "network_error"
    assert invalid.code == "invalid_drive_url"
    assert rate_limited.code == "drive_rate_limited_or_permission"
    assert empty.code == "no_images_found"
    assert unknown.code == "unknown_error"
