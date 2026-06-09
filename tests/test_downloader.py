from __future__ import annotations

from pathlib import Path

import requests

from app.core.downloader import (
    DriveDownloadError,
    classify_download_failure,
    copy_images,
    extract_drive_folder_id,
    is_google_drive_url,
    next_available_path,
    safe_filename,
)


def test_drive_url_detection_and_folder_id() -> None:
    url = "https://drive.google.com/drive/folders/abc123?usp=sharing&hl=en"

    assert is_google_drive_url(url)
    assert not is_google_drive_url("https://example.com/drive/folders/abc123")
    assert extract_drive_folder_id(url) == "abc123"


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


def test_classify_download_failure() -> None:
    network = classify_download_failure(requests.exceptions.SSLError("ssl broke"))
    invalid = classify_download_failure(ValueError("不是 Google Drive 链接"))
    empty = classify_download_failure(
        DriveDownloadError("Google Drive folder downloaded, but no image files were found")
    )
    unknown = classify_download_failure(RuntimeError("boom"))

    assert network.code == "network_error"
    assert invalid.code == "invalid_drive_url"
    assert empty.code == "no_images_found"
    assert unknown.code == "unknown_error"
