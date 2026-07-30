from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import requests

from app.core.downloader import (
    DriveDownloadError,
    DriveDownloadTimeout,
    PrintervalCloudflareChallengeError,
    PRINTERVAL_IMAGE_REFERER,
    cached_drive_folder,
    classify_download_failure,
    copy_images,
    detect_cloudflare_challenge,
    direct_url_cache_key,
    download_printerval_with_playwright,
    download_printerval_image_with_curl_cffi,
    download_drive_file_by_id,
    download_drive_folder_by_id,
    download_design_images,
    download_printerval_images_with_playwright_fetch,
    extract_drive_file_id,
    extract_drive_folder_id,
    image_extension_for_file,
    image_filename_with_extension,
    iter_image_files,
    is_google_drive_url,
    normalize_image_file_extensions,
    next_available_path,
    parse_drive_resource,
    parse_printerval_design_image_urls,
    parse_printerval_zip_metadata,
    printerval_download_url,
    run_download_with_timeout,
    safe_extract_printerval_zip,
    safe_filename,
    write_cache_manifest,
)


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"image",
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False
        self.text = body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size: int):
        yield self.body

    def close(self) -> None:
        self.closed = True


class FakeCurlCffiSession:
    def __init__(self, handler=None) -> None:
        self.calls = []
        self.post_calls = []
        self.handler = handler

    def get(self, url: str, timeout: int, headers: dict[str, str]):
        self.calls.append((url, timeout, headers))
        if self.handler:
            return self.handler(url, timeout, headers)
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        return FakeResponse(
            url.rsplit("/", 1)[-1].encode("utf-8"),
            headers={"Content-Type": "image/jpeg" if url.endswith(".jpg") else "image/png"},
        )

    def post(self, url: str, timeout: int, headers: dict[str, str], json: dict):
        self.post_calls.append((url, timeout, headers, json))
        if self.handler:
            return self.handler(url, timeout, headers, json)
        return FakeResponse(b"not implemented", status_code=404, headers={"Content-Type": "text/plain"})


PRINTERVAL_DESIGN_URL = (
    "https://printerval.com/fr/folder-design?product_id=15485898&product_sku_id=51232356&design_urls="
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091327829/"
    "149405ce7e4463de60a008db11be8b5f/Collar2.jpg,"
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091332849/"
    "149405ce7e4463de60a008db11be8b5f/Collar.jpg,"
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091324306/"
    "149405ce7e4463de60a008db11be8b5f/back.jpg,"
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091336894/"
    "149405ce7e4463de60a008db11be8b5f/Front.jpg,"
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091339241/"
    "149405ce7e4463de60a008db11be8b5f/Sleeves.jpg,"
    "/psd-renderer/2026-06-25/fr_15485898/psd-gen-design/20260625091421470/"
    "149405ce7e4463de60a008db11be8b5f/mk.jpg,"
    "https://storage.prtvstatic.com/2026/06/25/"
    "m3vwych5r6zc7psqmakcpjvtfalrgfzsiuogh5m8-92f078ea09ab8bfa223b28f7b2cd18ea.png"
    "&is_show_product_image=0"
)


def slow_download_for_timeout_test(resource_id: str, output_dir: Path) -> None:
    time.sleep(2)


def challenge_download_for_timeout_test(resource_id: str, output_dir: Path) -> None:
    raise PrintervalCloudflareChallengeError("Printerval Cloudflare challenge required")


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def printerval_zip_html(links: list[str] | None = None, csrf_token: str = "csrf-token") -> bytes:
    links = links or [
        "https://dl.printerval.com/psd-renderer/2026-06-25/item/back.jpg",
        "https://storage.prtvstatic.com/2026/06/25/mockup.png",
    ]
    encoded_links = json.dumps(links).replace("/", "\\/")
    return f"""
        <script>
        function downloadAllDesignImages() {{
            const links = {encoded_links};
            const productImage = "https:\\/\\/dl.printerval.com\\/product.jpg";
            const allLinks = links.concat([productImage]);
            const productId = 3431071559;
            fetch('/service/pod/download-images-from-links', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'X-CSRF-TOKEN': '{csrf_token}'
                }},
                body: JSON.stringify({{ links: allLinks }})
            }});
        }}
        </script>
    """.encode("utf-8")


def test_drive_url_detection_and_folder_id() -> None:
    url = "https://drive.google.com/drive/folders/abc123?usp=sharing&hl=en"
    profile_url = "https://drive.google.com/drive/u/0/folders/profileFolder123"

    assert is_google_drive_url(url)
    assert not is_google_drive_url("https://example.com/drive/folders/abc123")
    assert extract_drive_folder_id(url) == "abc123"
    assert extract_drive_folder_id(profile_url) == "profileFolder123"


def test_drive_resource_parses_folder_and_file_ids() -> None:
    folder = parse_drive_resource("https://drive.google.com/drive/folders/folder123?usp=sharing")
    file = parse_drive_resource("https://drive.google.com/file/d/file123/view?usp=sharing")
    open_file = parse_drive_resource("https://drive.google.com/open?id=openFile123&usp=drive_copy")
    uc_file = parse_drive_resource("https://drive.google.com/uc?id=ucFile123&export=download")

    assert folder.kind == "folder"
    assert folder.resource_id == "folder123"
    assert file.kind == "file"
    assert file.resource_id == "file123"
    assert open_file.kind == "file"
    assert open_file.resource_id == "openFile123"
    assert uc_file.kind == "file"
    assert uc_file.resource_id == "ucFile123"
    assert extract_drive_file_id("https://drive.google.com/file/d/file123/view") == "file123"
    assert extract_drive_file_id("https://drive.google.com/open?id=openFile123&usp=drive_copy") == "openFile123"


def test_drive_resource_rejects_unknown_drive_shape() -> None:
    try:
        parse_drive_resource("https://drive.google.com/open")
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


def test_copy_images_renames_with_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "a.png").write_bytes(b"png")
    (source / "b.JPG").write_bytes(b"jpg")

    copied = copy_images(source, target, name_prefix="design")

    assert [file.file_name for file in copied] == ["design-1.png", "design-2.jpg"]
    assert (target / "design-1.png").read_bytes() == b"png"
    assert (target / "design-2.jpg").read_bytes() == b"jpg"


def test_copy_images_renames_psd_with_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "artwork.psd").write_bytes(b"psd")

    copied = copy_images(source, target, name_prefix="design")

    assert [file.file_name for file in copied] == ["design-1.psd"]
    assert (target / "design-1.psd").read_bytes() == b"psd"


def test_normalize_image_file_extensions_adds_missing_suffix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mockup").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (source / "preview").write_bytes(b"RIFF\x08\x00\x00\x00WEBPwebp")
    (source / "already.png").write_bytes(b"\x89PNG\r\n\x1a\npng")
    (source / "note").write_text("not an image")

    normalize_image_file_extensions(source)

    assert image_extension_for_file(source / "mockup.jpg") == ".jpg"
    assert image_filename_with_extension("mockup", ".jpg") == "mockup.jpg"
    assert sorted(path.name for path in iter_image_files(source)) == [
        "already.png",
        "mockup.jpg",
        "preview.webp",
    ]
    assert (source / "note").exists()


def test_normalize_image_file_extensions_uniquifies_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mockup").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (source / "mockup.jpg").write_bytes(b"existing")

    normalize_image_file_extensions(source)

    assert sorted(path.name for path in iter_image_files(source)) == ["mockup(1).jpg", "mockup.jpg"]


def test_download_drive_file_by_id_uses_output_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DRIVE_DOWNLOAD_BACKEND", "gdown")
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
            "output": str(tmp_path) + os.sep,
            "quiet": False,
            "use_cookies": False,
        }
    ]


def test_download_drive_file_by_id_falls_back_to_uc_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DRIVE_DOWNLOAD_BACKEND", "gdown")
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
            "output": str(tmp_path) + os.sep,
            "quiet": False,
            "use_cookies": False,
        },
        {
            "id": None,
            "url": "https://drive.google.com/uc?id=file123",
            "output": str(tmp_path) + os.sep,
            "quiet": False,
            "use_cookies": False,
        },
    ]


def test_download_design_images_adds_extension_before_copy(tmp_path: Path, monkeypatch) -> None:
    def fake_download_drive_resource(url: str, output_dir: Path) -> None:
        assert url == "https://drive.google.com/file/d/file123/view?usp=drive_link"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup").write_bytes(b"\xff\xd8\xff\xe0jpeg")

    monkeypatch.setattr("app.core.downloader.download_drive_resource", fake_download_drive_resource)

    copied = download_design_images(
        "https://drive.google.com/file/d/file123/view?usp=drive_link",
        tmp_path,
    )

    assert [file.file_name for file in copied] == ["mockup.jpg"]
    assert (tmp_path / "mockup.jpg").exists()


def test_download_drive_file_by_id_reports_attempt_details(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DRIVE_DOWNLOAD_BACKEND", "gdown")

    class FakeGdown:
        @staticmethod
        def download(id=None, url=None, output="", quiet=False, use_cookies=False):
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / "downloaded-html").write_text("not an image")
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
        assert "downloaded-html" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_rclone_folder_download_uses_conservative_defaults(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.delenv("DRIVE_DOWNLOAD_BACKEND", raising=False)
    monkeypatch.setenv("RCLONE_BIN", "rclone-test")
    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)

    download_drive_folder_by_id("folder123", tmp_path)

    assert calls == [
        [
            "rclone-test",
            "copy",
            "gdrive:",
            str(tmp_path),
            "--transfers",
            "1",
            "--checkers",
            "1",
            "--local-encoding",
            "Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Del,Ctl,InvalidUtf8,Dot",
            "--drive-root-folder-id",
            "folder123",
            "--drive-pacer-min-sleep",
            "500ms",
            "--drive-pacer-burst",
            "5",
        ]
    ]


def test_rclone_bin_prefers_env_over_bundled(monkeypatch) -> None:
    from app.core import downloader

    monkeypatch.setenv("RCLONE_BIN", "custom-rclone")
    monkeypatch.setattr(downloader, "bundled_rclone_bin", lambda: Path("vendor/rclone/rclone.exe"))

    assert downloader.rclone_bin() == "custom-rclone"


def test_rclone_bin_uses_bundled_when_env_is_missing(monkeypatch) -> None:
    from app.core import downloader

    bundled = Path("vendor/rclone/rclone.exe")
    monkeypatch.delenv("RCLONE_BIN", raising=False)
    monkeypatch.setattr(downloader, "bundled_rclone_bin", lambda: bundled)

    assert downloader.rclone_bin() == str(bundled)


def test_rclone_bin_uses_pyinstaller_resource_root(tmp_path: Path, monkeypatch) -> None:
    from app.core import downloader

    executable_name = "rclone.exe" if os.name == "nt" else "rclone"
    bundled = tmp_path / "_internal" / "vendor" / "rclone" / executable_name
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    monkeypatch.delenv("RCLONE_BIN", raising=False)
    monkeypatch.setenv("APP_RESOURCE_ROOT", str(tmp_path / "_internal"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "AutoDownload.exe"))

    assert downloader.rclone_bin() == str(bundled)


def test_rclone_file_download_uses_copyid_directory_target(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.delenv("DRIVE_DOWNLOAD_BACKEND", raising=False)
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setenv("RCLONE_DRIVE_REMOTES", "gdrive_a")
    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)

    download_drive_file_by_id("file123", tmp_path)

    assert calls == [
        [
            "rclone",
            "backend",
            "copyid",
            "gdrive_a:",
            "file123",
            str(tmp_path) + os.sep,
            "--local-encoding",
            "Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Del,Ctl,InvalidUtf8,Dot",
            "--drive-pacer-min-sleep",
            "500ms",
            "--drive-pacer-burst",
            "5",
        ]
    ]


def test_rclone_remote_pool_retries_rate_limit_on_next_remote(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="rateLimitExceeded")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("RCLONE_DRIVE_REMOTES", "gdrive_a, gdrive_b")
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)

    download_drive_folder_by_id("folder123", tmp_path)

    assert [call[2] for call in calls] == ["gdrive_a:", "gdrive_b:"]


def test_rclone_remote_pool_stops_on_permission_error(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")

    monkeypatch.setenv("RCLONE_DRIVE_REMOTES", "gdrive_a, gdrive_b")
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)

    try:
        download_drive_folder_by_id("folder123", tmp_path)
    except DriveDownloadError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadError")

    assert [call[2] for call in calls] == ["gdrive_a:"]


def test_rclone_command_decodes_output_as_utf8_with_replacement(monkeypatch) -> None:
    from app.core import downloader

    kwargs_seen = {}

    def fake_run(command, **kwargs):
        kwargs_seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)

    completed = downloader.run_rclone_command(["rclone", "lsf", "gdrive:"])

    assert completed.stdout is None
    assert kwargs_seen["encoding"] == "utf-8"
    assert kwargs_seen["errors"] == "replace"
    assert kwargs_seen["text"] is True


def test_downloader_config_reads_local_settings(tmp_path: Path, monkeypatch) -> None:
    from app.core import downloader, local_settings

    monkeypatch.setattr(local_settings, "LOCAL_SETTINGS_PATH", tmp_path / "local_settings.json")
    local_settings.save_local_settings(
        {
            "drive_download_timeout_seconds": 321,
            "max_image_file_size_mb": 77,
            "min_free_disk_space_mb": 2048,
            "rclone_transfers": "2",
            "rclone_checkers": "3",
            "printerval_curl_timeout_seconds": 44,
            "printerval_playwright_timeout_seconds": 155,
            "printerval_playwright_enabled": False,
        }
    )

    assert downloader.drive_download_timeout_seconds() == 321
    assert downloader.max_image_file_size_mb() == 77
    assert downloader.min_free_disk_space_mb() == 2048
    assert downloader.rclone_transfers() == "2"
    assert downloader.rclone_checkers() == "3"
    assert downloader.printerval_curl_timeout_seconds() == 44
    assert downloader.printerval_playwright_timeout_seconds() == 155
    assert downloader.printerval_playwright_enabled() is False


def test_downloader_config_prefers_environment_over_local_settings(tmp_path: Path, monkeypatch) -> None:
    from app.core import downloader, local_settings

    monkeypatch.setattr(local_settings, "LOCAL_SETTINGS_PATH", tmp_path / "local_settings.json")
    local_settings.save_local_settings({"drive_download_timeout_seconds": 321})
    monkeypatch.setenv("DRIVE_DOWNLOAD_TIMEOUT_SECONDS", "456")

    assert downloader.drive_download_timeout_seconds() == 456


def test_cached_drive_folder_uses_resource_kind_prefix(tmp_path: Path, monkeypatch) -> None:
    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(
        "https://drive.google.com/file/d/file123/view?usp=sharing",
        tmp_path,
    )

    assert cache_dir == tmp_path / "file-file123"
    assert (cache_dir / "mockup.jpg").exists()


def test_cached_drive_file_adds_extension_to_downloaded_image(tmp_path: Path, monkeypatch) -> None:
    def fake_run_download_with_timeout(download_func, resource_id: str, output_dir: Path, timeout_seconds=None) -> None:
        assert resource_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup").write_bytes(b"\xff\xd8\xff\xe0jpeg")

    monkeypatch.setattr("app.core.downloader.run_download_with_timeout", fake_run_download_with_timeout)

    cache_dir = cached_drive_folder(
        "https://drive.google.com/file/d/file123/view?usp=drive_link",
        tmp_path,
    )

    assert cache_dir == tmp_path / "file-file123"
    assert sorted(path.name for path in iter_image_files(cache_dir)) == ["mockup.jpg"]


def test_open_id_folder_resolves_with_rclone_folder_probe(tmp_path: Path, monkeypatch) -> None:
    commands = []

    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="design.jpg\n", stderr="")

    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        assert folder_id == "folder123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "design.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder("https://drive.google.com/open?id=folder123", tmp_path)

    assert cache_dir == tmp_path / "folder-folder123"
    assert commands[0] == [
        "rclone",
        "lsf",
        "gdrive:",
        "--drive-root-folder-id",
        "folder123",
        "--max-depth",
        "1",
        "--drive-pacer-min-sleep",
        "500ms",
        "--drive-pacer-burst",
        "5",
    ]
    assert (cache_dir / "design.jpg").exists()


def test_open_id_file_resolves_with_rclone_folder_probe(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="can't use file as root folder",
        )

    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )
    cache_dir = cached_drive_folder("https://drive.google.com/open?id=file123", tmp_path)

    assert cache_dir == tmp_path / "file-file123"
    assert (cache_dir / "mockup.jpg").exists()


def test_open_id_empty_folder_probe_falls_back_to_file_download(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, check, capture_output, text, timeout=None, env=None, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "mockup.jpg").write_bytes(b"jpg")

    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        raise AssertionError("open?id= file should not be downloaded as a folder")

    monkeypatch.setattr("app.core.downloader.subprocess.run", fake_run)
    monkeypatch.setenv("RCLONE_BIN", "rclone")
    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)
    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder("https://drive.google.com/open?id=file123", tmp_path)

    assert cache_dir == tmp_path / "file-file123"
    assert (cache_dir / "mockup.jpg").exists()


def test_copyid_directory_error_falls_back_to_folder_download(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_download_file(file_id: str, output_dir: Path) -> None:
        calls.append(("file", file_id, output_dir.name))
        output_dir.mkdir(parents=True, exist_ok=True)
        raise DriveDownloadError("can't copyid directory use: rclone copy --drive-root-folder-id file123")

    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        calls.append(("folder", folder_id, output_dir.name))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "design.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)
    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr("app.core.downloader.rclone_drive_folder_image_paths", lambda folder_id: ["design.jpg"])
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder("https://drive.google.com/file/d/file123/view", tmp_path)

    assert cache_dir == tmp_path / "folder-file123"
    assert [call[:2] for call in calls] == [
        ("file", "file123"),
        ("folder", "file123"),
    ]
    assert calls[0][2].startswith(".file-file123.staging-")
    assert calls[1][2].startswith(".folder-file123.staging-")
    assert (cache_dir / "design.jpg").exists()


def test_cached_drive_folder_reports_non_image_files(tmp_path: Path, monkeypatch) -> None:
    def fake_download_file(file_id: str, output_dir: Path) -> None:
        assert file_id == "file123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "downloaded-html").write_text("not an image")

    monkeypatch.setattr("app.core.downloader.download_drive_file_by_id", fake_download_file)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    try:
        cached_drive_folder(
            "https://drive.google.com/file/d/file123/view?usp=sharing",
            tmp_path,
        )
    except DriveDownloadError as exc:
        message = str(exc)
        assert "no image files were found" in message
        assert "resource=file:file123" in message
        assert "downloaded-html" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_cached_drive_folder_reuses_existing_images(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        calls.append(folder_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "design.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr("app.core.downloader.rclone_drive_folder_image_paths", lambda folder_id: ["design.jpg"])
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    url = "https://drive.google.com/drive/folders/folder123"
    first_dir = cached_drive_folder(url, tmp_path)
    second_dir = cached_drive_folder(url, tmp_path)

    assert first_dir == second_dir == tmp_path / "folder-folder123"
    assert calls == ["folder123"]
    assert (second_dir / "design.jpg").read_bytes() == b"jpg"


def test_cached_drive_folder_accepts_psd_design_files(tmp_path: Path, monkeypatch) -> None:
    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        assert folder_id == "folder123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "artwork.psd").write_bytes(b"psd")

    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr("app.core.downloader.rclone_drive_folder_image_paths", lambda folder_id: ["artwork.psd"])
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder("https://drive.google.com/drive/folders/folder123", tmp_path)

    assert sorted(path.name for path in iter_image_files(cache_dir)) == ["artwork.psd"]


def test_cached_drive_folder_allows_duplicate_source_names(tmp_path: Path, monkeypatch) -> None:
    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        assert folder_id == "folder123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "dup.png").write_bytes(b"dup")
        (output_dir / "other.png").write_bytes(b"other")

    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr(
        "app.core.downloader.rclone_drive_folder_image_paths",
        lambda folder_id: ["dup.png", "dup.png", "other.png"],
    )
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder("https://drive.google.com/drive/folders/folder123", tmp_path)

    assert sorted(path.name for path in iter_image_files(cache_dir)) == ["dup.png", "other.png"]


def test_cached_drive_folder_still_reports_incomplete_unique_images(tmp_path: Path, monkeypatch) -> None:
    def fake_download_folder(folder_id: str, output_dir: Path) -> None:
        assert folder_id == "folder123"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "one.png").write_bytes(b"one")
        (output_dir / "two.png").write_bytes(b"two")

    monkeypatch.setattr("app.core.downloader.download_drive_folder_by_id", fake_download_folder)
    monkeypatch.setattr(
        "app.core.downloader.rclone_drive_folder_image_paths",
        lambda folder_id: ["one.png", "two.png", "three.png"],
    )
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    try:
        cached_drive_folder("https://drive.google.com/drive/folders/folder123", tmp_path)
    except DriveDownloadError as exc:
        message = str(exc)
        assert "expected_images=3" in message
        assert "downloaded_images=2" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_cached_drive_folder_downloads_plain_image_url(tmp_path: Path, monkeypatch) -> None:
    responses = []

    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        responses.append((url, stream, timeout, headers))
        return FakeResponse(
            b"jpg",
            headers={"Content-Type": "image/jpeg"},
        )

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    url = "https://cdn.example.com/products/front-view?token=secret"
    cache_dir = cached_drive_folder(url, tmp_path)

    assert cache_dir == tmp_path / f"url-{direct_url_cache_key(url)}"
    assert responses[0][0:3] == (url, True, 180)
    assert "Mozilla/5.0" in responses[0][3]["User-Agent"]
    assert responses[0][3]["Accept"].startswith("image/")
    assert "Referer" not in responses[0][3]
    assert (cache_dir / "front-view.jpg").read_bytes() == b"jpg"
    assert "secret" not in str(cache_dir)


def test_printerval_design_url_parses_all_image_urls() -> None:
    image_urls = parse_printerval_design_image_urls(PRINTERVAL_DESIGN_URL)

    assert len(image_urls) == 7
    assert image_urls[0] == (
        "https://assets.printerval.com/psd-renderer/2026-06-25/fr_15485898/"
        "psd-gen-design/20260625091327829/149405ce7e4463de60a008db11be8b5f/Collar2.jpg"
    )
    assert image_urls[-1] == (
        "https://storage.prtvstatic.com/2026/06/25/"
        "m3vwych5r6zc7psqmakcpjvtfalrgfzsiuogh5m8-92f078ea09ab8bfa223b28f7b2cd18ea.png"
    )


def test_printerval_design_url_accepts_single_design_url_parameter() -> None:
    image_urls = parse_printerval_design_image_urls(
        "https://printerval.com/us/folder-design?product_id=2163122603"
        "&design_url=https://assets.printerval.com/2026/07/24/1784878190.848021_3000x3000.png"
    )

    assert image_urls == [
        "https://assets.printerval.com/2026/07/24/1784878190.848021_3000x3000.png"
    ]


def test_printerval_download_url_prefers_dl_domain_for_psd_renderer() -> None:
    assert printerval_download_url(
        "https://assets.printerval.com/psd-renderer/2026-06-25/item/back.jpg"
    ) == "https://dl.printerval.com/psd-renderer/2026-06-25/item/back.jpg"
    assert printerval_download_url(
        "https://dl.printerval.com/psd-renderer/2026-06-25/item/back.jpg"
    ) == "https://dl.printerval.com/psd-renderer/2026-06-25/item/back.jpg"
    storage_url = "https://storage.prtvstatic.com/2026/06/25/mockup.png"
    assert printerval_download_url(storage_url) == storage_url


def test_printerval_zip_metadata_parses_page_links_without_product_image() -> None:
    metadata = parse_printerval_zip_metadata(printerval_zip_html().decode("utf-8"))

    assert metadata is not None
    assert metadata.csrf_token == "csrf-token"
    assert metadata.product_id == "3431071559"
    assert metadata.links == [
        "https://dl.printerval.com/psd-renderer/2026-06-25/item/back.jpg",
        "https://storage.prtvstatic.com/2026/06/25/mockup.png",
    ]


def test_printerval_zip_extract_rejects_unsafe_paths(tmp_path: Path) -> None:
    try:
        safe_extract_printerval_zip(make_zip({"../evil.jpg": b"jpg"}), tmp_path, expected_count=1)
    except DriveDownloadError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_cached_drive_folder_prefers_printerval_zip_endpoint(tmp_path: Path, monkeypatch, capsys) -> None:
    image_urls = parse_printerval_design_image_urls(PRINTERVAL_DESIGN_URL)
    zip_links = [printerval_download_url(url) for url in image_urls]

    def fake_handler(url: str, timeout: int, headers: dict[str, str], payload=None):
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(printerval_zip_html(zip_links), headers={"Content-Type": "text/html"})
        if url.endswith("/service/pod/download-images-from-links"):
            assert headers["X-CSRF-TOKEN"] == "csrf-token"
            assert headers["Referer"] == PRINTERVAL_DESIGN_URL
            assert payload == {"links": zip_links}
            return FakeResponse(
                make_zip({Path(link).name: Path(link).name.encode("utf-8") for link in zip_links}),
                headers={"Content-Type": "application/zip"},
            )
        raise AssertionError(f"Unexpected request: {url}")

    session = FakeCurlCffiSession(fake_handler)
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert len(session.calls) == 1
    assert len(session.post_calls) == 1
    assert sorted(path.name for path in iter_image_files(cache_dir)) == sorted(Path(link).name for link in zip_links)
    output = capsys.readouterr().out
    assert "Printerval ZIP endpoint start: product_id=3431071559 links=7" in output
    assert "Printerval ZIP endpoint extracted 7 images" in output


def test_cached_drive_folder_downloads_printerval_design_urls(tmp_path: Path, monkeypatch, capsys) -> None:
    session = FakeCurlCffiSession()

    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert cache_dir == tmp_path / f"printerval-{direct_url_cache_key(PRINTERVAL_DESIGN_URL)}"
    assert len(session.calls) == 8
    assert session.calls[0][0] == PRINTERVAL_DESIGN_URL
    image_calls = session.calls[1:]
    assert image_calls[0][0].startswith("https://dl.printerval.com/psd-renderer/")
    assert image_calls[-1][0].startswith("https://storage.prtvstatic.com/")
    assert all(headers["Referer"] == PRINTERVAL_IMAGE_REFERER for _, _, headers in image_calls)
    assert all("Mozilla/5.0" in headers["User-Agent"] for _, _, headers in session.calls)
    output = capsys.readouterr().out
    assert "Printerval image 1/7 strategy=short_referer variant=dl start" in output
    assert "download_url=https://dl.printerval.com/psd-renderer/" in output
    assert "Printerval image 1/7 saved Collar2.jpg size=" in output
    assert sorted(path.name for path in iter_image_files(cache_dir)) == [
        "Collar.jpg",
        "Collar2.jpg",
        "Front.jpg",
        "Sleeves.jpg",
        "back.jpg",
        "m3vwych5r6zc7psqmakcpjvtfalrgfzsiuogh5m8-92f078ea09ab8bfa223b28f7b2cd18ea.png",
        "mk.jpg",
    ]


def test_printerval_incomplete_cache_is_rebuilt(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / f"printerval-{direct_url_cache_key(PRINTERVAL_DESIGN_URL)}"
    cache_dir.mkdir()
    (cache_dir / "stale.jpg").write_bytes(b"old")
    session = FakeCurlCffiSession()

    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    rebuilt_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert rebuilt_dir == cache_dir
    assert session.calls[0][0] == PRINTERVAL_DESIGN_URL
    assert len(session.calls) == 8
    assert not (cache_dir / "stale.jpg").exists()
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_complete_cache_is_reused(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / f"printerval-{direct_url_cache_key(PRINTERVAL_DESIGN_URL)}"
    cache_dir.mkdir()
    for index in range(7):
        (cache_dir / f"cached-{index}.jpg").write_bytes(b"jpg")
    write_cache_manifest(cache_dir, PRINTERVAL_DESIGN_URL, f"printerval-{direct_url_cache_key(PRINTERVAL_DESIGN_URL)}", 7)

    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        raise AssertionError("Complete Printerval cache should not be downloaded again")

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)
    monkeypatch.setattr(
        "app.core.downloader.create_printerval_session",
        lambda: (_ for _ in ()).throw(AssertionError("Complete Printerval cache should not be downloaded again")),
    )

    reused_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert reused_dir == cache_dir
    assert sorted(path.name for path in iter_image_files(cache_dir)) == [f"cached-{index}.jpg" for index in range(7)]


def test_google_drive_incomplete_folder_cache_is_rebuilt(tmp_path: Path, monkeypatch) -> None:
    url = "https://drive.google.com/drive/folders/folder123"
    cache_dir = tmp_path / "folder-folder123"
    cache_dir.mkdir()
    (cache_dir / "old-a.jpg").write_bytes(b"old")
    (cache_dir / "old-b.jpg").write_bytes(b"old")
    run_calls = []

    monkeypatch.setattr(
        "app.core.downloader.rclone_drive_folder_image_paths",
        lambda folder_id: ["a.jpg", "b.jpg", "c.jpg"],
    )

    def fake_run(download_func, resource_id, output_dir, timeout_seconds=None):
        run_calls.append((download_func, resource_id, output_dir, timeout_seconds))
        for name in ["a.jpg", "b.jpg", "c.jpg"]:
            (output_dir / name).write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.run_download_with_timeout", fake_run)

    rebuilt_dir = cached_drive_folder(url, tmp_path)

    assert rebuilt_dir == cache_dir
    assert len(run_calls) == 1
    assert run_calls[0][1] == "folder123"
    assert sorted(path.name for path in iter_image_files(cache_dir)) == ["a.jpg", "b.jpg", "c.jpg"]


def test_google_drive_incomplete_folder_download_fails(tmp_path: Path, monkeypatch) -> None:
    url = "https://drive.google.com/drive/folders/folder123"

    monkeypatch.setattr(
        "app.core.downloader.rclone_drive_folder_image_paths",
        lambda folder_id: ["a.jpg", "b.jpg", "c.jpg"],
    )

    def fake_run(download_func, resource_id, output_dir, timeout_seconds=None):
        (output_dir / "a.jpg").write_bytes(b"jpg")
        (output_dir / "b.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.run_download_with_timeout", fake_run)

    try:
        cached_drive_folder(url, tmp_path)
    except DriveDownloadError as exc:
        message = str(exc)
        assert "download incomplete" in message
        assert "expected_images=3" in message
        assert "downloaded_images=2" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_google_drive_complete_folder_cache_is_reused(tmp_path: Path, monkeypatch) -> None:
    url = "https://drive.google.com/drive/folders/folder123"
    cache_dir = tmp_path / "folder-folder123"
    cache_dir.mkdir()
    for name in ["a.jpg", "b.jpg", "c.jpg"]:
        (cache_dir / name).write_bytes(b"jpg")
    write_cache_manifest(cache_dir, url, "folder:folder123", 3)

    monkeypatch.setattr(
        "app.core.downloader.rclone_drive_folder_image_paths",
        lambda folder_id: ["a.jpg", "b.jpg", "c.jpg"],
    )
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: (
            (_ for _ in ()).throw(AssertionError("Complete Drive cache should not be downloaded again"))
        ),
    )

    reused_dir = cached_drive_folder(url, tmp_path)

    assert reused_dir == cache_dir
    assert sorted(path.name for path in iter_image_files(cache_dir)) == ["a.jpg", "b.jpg", "c.jpg"]


def test_copy_images_rejects_large_image(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAX_IMAGE_FILE_SIZE_MB", "1")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "large.jpg").write_bytes(b"x" * (1024 * 1024 + 1))

    try:
        copy_images(source, target)
    except DriveDownloadError as exc:
        failure = classify_download_failure(exc)
        assert failure.code == "image_too_large"
        assert "large.jpg" in failure.detail
        assert "limit_mb=1" in failure.detail
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_printerval_warm_page_failure_still_downloads_images(tmp_path: Path, monkeypatch) -> None:
    session = FakeCurlCffiSession(
        lambda url, timeout, headers, payload=None: (
            (_ for _ in ()).throw(RuntimeError("page blocked"))
            if url == PRINTERVAL_DESIGN_URL
            else FakeResponse(
                url.rsplit("/", 1)[-1].encode("utf-8"),
                headers={"Content-Type": "image/jpeg" if url.endswith(".jpg") else "image/png"},
            )
        )
    )

    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert session.calls[0][0] == PRINTERVAL_DESIGN_URL
    assert len(session.calls) == 8
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_cache_uses_dynamic_outer_timeout(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run_download_with_timeout(download_func, resource_id, output_dir, timeout_seconds=None):
        calls.append((download_func, resource_id, output_dir, timeout_seconds))
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in range(7):
            (output_dir / f"done-{index}.jpg").write_bytes(b"jpg")

    monkeypatch.setattr("app.core.downloader.run_download_with_timeout", fake_run_download_with_timeout)

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert cache_dir == tmp_path / f"printerval-{direct_url_cache_key(PRINTERVAL_DESIGN_URL)}"
    assert calls[0][3] == 7 * 90


def test_printerval_curl_cffi_timeout_is_configurable(tmp_path: Path, monkeypatch) -> None:
    session = FakeCurlCffiSession()

    monkeypatch.setenv("PRINTERVAL_CURL_TIMEOUT_SECONDS", "12")

    saved_path = download_printerval_image_with_curl_cffi(
        "https://assets.printerval.com/design/back.jpg",
        tmp_path,
        session,
        referer_url=PRINTERVAL_IMAGE_REFERER,
        strategy="short_referer",
    )

    assert session.calls[0][1] == 12
    assert saved_path == tmp_path / "back.jpg"


def test_printerval_image_retries_transient_403(tmp_path: Path, monkeypatch) -> None:
    image_attempts: dict[str, int] = {}
    session = FakeCurlCffiSession()

    def fake_get(url: str, timeout: int, headers: dict[str, str]):
        session.calls.append((url, timeout, headers))
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        image_url = url
        image_attempts[image_url] = image_attempts.get(image_url, 0) + 1
        if image_url.endswith("/Collar2.jpg") and image_attempts[image_url] < 5:
            return FakeResponse(
                b"<html>blocked</html>",
                status_code=403,
                headers={"Content-Type": "text/html", "Server": "cloudflare"},
            )
        return FakeResponse(
            image_url.rsplit("/", 1)[-1].encode("utf-8"),
            headers={"Content-Type": "image/jpeg" if image_url.endswith(".jpg") else "image/png"},
        )

    session.get = fake_get
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr("app.core.downloader.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    first_url = printerval_download_url(parse_printerval_design_image_urls(PRINTERVAL_DESIGN_URL)[0])
    assert image_attempts[first_url] == 5
    image_calls = [call for call in session.calls if "Referer" in call[2]]
    assert any(headers["Referer"] == PRINTERVAL_IMAGE_REFERER for _, _, headers in image_calls)
    assert any(headers["Referer"] == PRINTERVAL_DESIGN_URL for _, _, headers in image_calls)
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_image_falls_back_to_page_referer(tmp_path: Path, monkeypatch) -> None:
    def fake_handler(url: str, timeout: int, headers: dict[str, str], payload=None):
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        if url.endswith("/Collar2.jpg") and headers["Referer"] == PRINTERVAL_IMAGE_REFERER:
            return FakeResponse(b"blocked", status_code=403, headers={"Content-Type": "text/html"})
        return FakeResponse(
            url.rsplit("/", 1)[-1].encode("utf-8"),
            headers={"Content-Type": "image/jpeg" if url.endswith(".jpg") else "image/png"},
        )

    session = FakeCurlCffiSession(fake_handler)
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    first_image_calls = [call for call in session.calls if call[0].endswith("/Collar2.jpg")]
    assert len(first_image_calls) == 3
    assert first_image_calls[0][2]["Referer"] == PRINTERVAL_IMAGE_REFERER
    assert first_image_calls[1][2]["Referer"] == PRINTERVAL_IMAGE_REFERER
    assert first_image_calls[2][2]["Referer"] == PRINTERVAL_DESIGN_URL
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_image_falls_back_from_dl_to_original_url(tmp_path: Path, monkeypatch) -> None:
    def fake_handler(url: str, timeout: int, headers: dict[str, str], payload=None):
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        if url.startswith("https://dl.printerval.com/psd-renderer/") and url.endswith("/Collar2.jpg"):
            return FakeResponse(b"blocked", status_code=403, headers={"Content-Type": "text/html"})
        return FakeResponse(
            url.rsplit("/", 1)[-1].encode("utf-8"),
            headers={"Content-Type": "image/jpeg" if url.endswith(".jpg") else "image/png"},
        )

    session = FakeCurlCffiSession(fake_handler)
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    first_image_calls = [call for call in session.calls if call[0].endswith("/Collar2.jpg")]
    assert first_image_calls[0][0].startswith("https://dl.printerval.com/psd-renderer/")
    assert first_image_calls[1][0].startswith("https://assets.printerval.com/psd-renderer/")
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_uses_one_curl_cffi_session_for_warmup_and_images(tmp_path: Path, monkeypatch) -> None:
    session = FakeCurlCffiSession()
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert session.calls[0][0] == PRINTERVAL_DESIGN_URL
    assert [call[0] for call in session.calls[1:]] == [
        printerval_download_url(url) for url in parse_printerval_design_image_urls(PRINTERVAL_DESIGN_URL)
    ]
    assert len(list(iter_image_files(cache_dir))) == 7


def test_printerval_failure_detail_includes_warmup_and_attempt_diagnostics(tmp_path: Path, monkeypatch) -> None:
    def fake_handler(url: str, timeout: int, headers: dict[str, str], payload=None):
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        return FakeResponse(
            b"<html>blocked by cloudflare</html>",
            status_code=403,
            headers={"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare"},
        )

    session = FakeCurlCffiSession(fake_handler)

    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr("app.core.downloader.time.sleep", lambda seconds: None)
    monkeypatch.setenv("PRINTERVAL_PLAYWRIGHT_ENABLED", "0")
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    try:
        cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)
    except DriveDownloadError as exc:
        message = str(exc)
        assert "warmup=strategy=curl_cffi_warmup status=200" in message
        assert "attempt=1" in message
        assert "attempt=2" in message
        assert "attempt=3" in message
        assert "strategy=short_referer" in message
        assert "strategy=page_referer" in message
        assert "curl_cffi returned HTTP 403" in message
        assert "content-type=text/html; charset=UTF-8" in message
        assert "server=cloudflare" in message
        assert "body=<html>blocked by cloudflare</html>" in message
        assert "playwright=DriveDownloadError: Printerval Playwright fallback is disabled" in message
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_printerval_falls_back_to_playwright_after_image_failures(tmp_path: Path, monkeypatch) -> None:
    def fake_handler(url: str, timeout: int, headers: dict[str, str], payload=None):
        if url == PRINTERVAL_DESIGN_URL:
            return FakeResponse(b"html", headers={"Content-Type": "text/html"})
        return FakeResponse(
            b"<html>blocked by cloudflare</html>",
            status_code=403,
            headers={"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare"},
        )

    playwright_calls = []
    session = FakeCurlCffiSession(fake_handler)

    def fake_playwright(url: str, output_dir: Path, expected_count: int) -> int:
        playwright_calls.append((url, output_dir, expected_count))
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in range(expected_count):
            (output_dir / f"playwright-{index}.jpg").write_bytes(b"jpg")
        return expected_count

    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr("app.core.downloader.time.sleep", lambda seconds: None)
    monkeypatch.setattr("app.core.downloader.download_printerval_with_playwright", fake_playwright)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert playwright_calls
    assert playwright_calls[0][0] == PRINTERVAL_DESIGN_URL
    assert playwright_calls[0][1].parent == tmp_path
    assert playwright_calls[0][1].name.startswith(f".{cache_dir.name}.staging-")
    assert playwright_calls[0][2] == 7
    assert len(list(cache_dir.glob("playwright-*.jpg"))) == 7


def test_printerval_skips_direct_images_when_playwright_profile_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    session = FakeCurlCffiSession(
        lambda url, timeout, headers, payload=None: FakeResponse(
            b"html",
            headers={"Content-Type": "text/html"},
        )
    )
    playwright_calls = []

    def fake_playwright(url: str, output_dir: Path, expected_count: int) -> int:
        playwright_calls.append((url, output_dir, expected_count))
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in range(expected_count):
            (output_dir / f"playwright-{index}.jpg").write_bytes(b"jpg")
        return expected_count

    monkeypatch.setenv("PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("app.core.downloader.create_printerval_session", lambda: session)
    monkeypatch.setattr("app.core.downloader.download_printerval_with_playwright", fake_playwright)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    cache_dir = cached_drive_folder(PRINTERVAL_DESIGN_URL, tmp_path)

    assert playwright_calls
    assert playwright_calls[0][0] == PRINTERVAL_DESIGN_URL
    assert playwright_calls[0][1].parent == tmp_path
    assert playwright_calls[0][1].name.startswith(f".{cache_dir.name}.staging-")
    assert playwright_calls[0][2] == 7
    assert len(session.calls) == 1
    assert session.calls[0][0] == PRINTERVAL_DESIGN_URL
    assert len(list(cache_dir.glob("playwright-*.jpg"))) == 7


def test_printerval_playwright_disabled_reports_clear_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRINTERVAL_PLAYWRIGHT_ENABLED", "0")

    try:
        download_printerval_with_playwright(PRINTERVAL_DESIGN_URL, tmp_path, expected_count=7)
    except DriveDownloadError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_detect_cloudflare_challenge() -> None:
    assert detect_cloudflare_challenge("<html><title>Just a moment...</title><span>Cloudflare</span></html>")
    assert detect_cloudflare_challenge(
        "<input type='hidden' name='cf-turnstile-response'>"
        "<span>Enable JavaScript and cookies to continue</span>"
    )
    assert not detect_cloudflare_challenge("<html><title>Printerval download</title></html>")
    assert not detect_cloudflare_challenge(
        """
        <html>
          <head><title>Ảnh Design sản phẩm Custom Shirt</title></head>
          <body>
            <script>
              function downloadAllDesignImages() {}
              const links = ["https:\\/\\/dl.printerval.com\\/psd-renderer\\/back.jpg"];
              window.__CF$cv$params = {};
              var a = document.createElement('script');
              a.src = '/cdn-cgi/challenge-platform/scripts/jsd/main.js';
            </script>
          </body>
        </html>
        """
    )


class FakePlaywrightDownloadError(Exception):
    pass


class FakePlaywrightTimeoutError(Exception):
    pass


class FakePlaywrightManager:
    def __enter__(self):
        return FakePlaywright()

    def __exit__(self, exc_type, exc, tb):
        return False


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class FakeChromium:
    def launch(self, **kwargs):
        return FakeBrowser()


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def new_context(self, **kwargs):
        return FakeContext()

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def new_page(self):
        return FakePage()

    def close(self) -> None:
        self.closed = True


class FakeResponseForPage:
    status = 200


class FakeLocator:
    def wait_for(self, state: str, timeout: int) -> None:
        raise FakePlaywrightDownloadError("button not found")


class FakePage:
    url = PRINTERVAL_DESIGN_URL

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def goto(self, url: str, wait_until: str, timeout: int):
        return FakeResponseForPage()

    def title(self) -> str:
        return "Printerval fake page"

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    def content(self) -> str:
        return "<html><title>Just a moment...</title><span>Cloudflare</span></html>"

    def screenshot(self, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"png")

    def evaluate(self, script: str):
        return False

    def locator(self, selector: str):
        return type("FakeLocatorHandle", (), {"first": FakeLocator()})()


class FakeBrowserFetchPage:
    def __init__(self) -> None:
        self.calls = []

    def evaluate(self, script: str, payload: dict[str, str]):
        self.calls.append(payload)
        return {
            "imageUrl": payload["imageUrl"],
            "downloadUrl": payload["downloadUrl"],
            "status": "200",
            "contentType": "image/jpeg",
            "bodyBase64": base64.b64encode(b"jpg").decode("ascii"),
            "sample": "",
        }


class FakeBrowserRequestResponse:
    status = 200
    headers = {"content-type": "image/png"}

    def body(self) -> bytes:
        return b"png"


class FakeBrowserRequestContext:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url: str, headers: dict[str, str], timeout: int):
        self.calls.append((url, headers, timeout))
        return FakeBrowserRequestResponse()


class FakeBrowserFetchContext:
    def __init__(self) -> None:
        self.request = FakeBrowserRequestContext()


class FakeBrowserRequestPage:
    url = PRINTERVAL_DESIGN_URL

    def __init__(self) -> None:
        self.context = FakeBrowserFetchContext()

    def evaluate(self, script: str, payload: dict[str, str]):
        raise AssertionError("page.evaluate should not be used when context.request is available")


def test_printerval_playwright_browser_fetch_saves_images(tmp_path: Path) -> None:
    page = FakeBrowserFetchPage()
    image_urls = [
        "https://assets.printerval.com/psd-renderer/2026-06-25/us_1/front.jpg",
        "https://storage.prtvstatic.com/2026/06/25/mockup.png",
    ]

    count = download_printerval_images_with_playwright_fetch(page, image_urls, tmp_path)

    assert count == 2
    assert (tmp_path / "front.jpg").read_bytes() == b"jpg"
    assert (tmp_path / "mockup.png").read_bytes() == b"jpg"
    assert [call["downloadUrl"] for call in page.calls] == image_urls


def test_printerval_playwright_browser_fetch_prefers_context_request(tmp_path: Path) -> None:
    page = FakeBrowserRequestPage()
    image_urls = ["https://assets.printerval.com/psd-renderer/2026-06-25/us_1/front.png"]

    count = download_printerval_images_with_playwright_fetch(page, image_urls, tmp_path)

    assert count == 1
    assert (tmp_path / "front.png").read_bytes() == b"png"
    assert page.context.request.calls[0][0] == image_urls[0]
    assert page.context.request.calls[0][1]["Referer"] == PRINTERVAL_DESIGN_URL


def test_printerval_playwright_cloudflare_challenge_fails_fast(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.downloader.import_playwright_sync_api",
        lambda: (lambda: FakePlaywrightManager(), FakePlaywrightDownloadError, FakePlaywrightTimeoutError),
    )
    monkeypatch.setenv("PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS", "1")

    try:
        download_printerval_with_playwright(PRINTERVAL_DESIGN_URL, tmp_path, expected_count=7)
    except PrintervalCloudflareChallengeError as exc:
        message = str(exc)
        assert "Cloudflare challenge required" in message
        assert "stage=diagnostics_before" in message
        assert "diagnostics=" in message
        diagnostics_dir = tmp_path / ".printerval-playwright-diagnostics"
        assert (diagnostics_dir / "before-download.html").exists()
        assert (diagnostics_dir / "failure.html").exists()
    else:
        raise AssertionError("Expected PrintervalCloudflareChallengeError")


def test_download_design_images_supports_plain_image_url(tmp_path: Path, monkeypatch) -> None:
    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        return FakeResponse(
            b"png",
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="mockup"',
            },
        )

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)

    copied = download_design_images("https://cdn.example.com/download?id=123", tmp_path)

    assert [file.file_name for file in copied] == ["mockup.png"]
    assert (tmp_path / "mockup.png").read_bytes() == b"png"


def test_plain_image_url_rejects_non_image_response(tmp_path: Path, monkeypatch) -> None:
    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        return FakeResponse(
            b"<html></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    try:
        cached_drive_folder("https://cdn.example.com/image-page", tmp_path)
    except DriveDownloadError as exc:
        failure = classify_download_failure(exc)
        assert failure.code == "extension_non_image_download"
        assert "content-type=text/html" in failure.detail
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_plain_image_url_reports_http_status(tmp_path: Path, monkeypatch) -> None:
    def fake_get(url: str, stream: bool, timeout: int, headers: dict[str, str]):
        return FakeResponse(
            b"not found",
            status_code=404,
            headers={"Content-Type": "text/plain"},
        )

    monkeypatch.setattr("app.core.downloader.requests.get", fake_get)
    monkeypatch.setattr(
        "app.core.downloader.run_download_with_timeout",
        lambda download_func, resource_id, output_dir, timeout_seconds=None: download_func(resource_id, output_dir),
    )

    try:
        cached_drive_folder("https://cdn.example.com/missing.jpg", tmp_path)
    except DriveDownloadError as exc:
        assert "HTTP 404" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadError")


def test_run_download_with_timeout_raises_timeout(tmp_path: Path) -> None:
    try:
        run_download_with_timeout(
            slow_download_for_timeout_test,
            "folder123",
            tmp_path,
            timeout_seconds=1,
        )
    except DriveDownloadTimeout as exc:
        assert "超过 1 秒" in str(exc)
    else:
        raise AssertionError("Expected DriveDownloadTimeout")


def test_run_download_with_timeout_restores_printerval_challenge_error(tmp_path: Path) -> None:
    try:
        run_download_with_timeout(
            challenge_download_for_timeout_test,
            "https://printerval.com/folder-design",
            tmp_path,
            timeout_seconds=1,
        )
    except PrintervalCloudflareChallengeError as exc:
        assert "Cloudflare challenge" in str(exc)
    else:
        raise AssertionError("Expected PrintervalCloudflareChallengeError")


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
    direct_image = classify_download_failure(
        DriveDownloadError("Direct image URL curl failed: curl: (22) HTTP 403")
    )
    printerval = classify_download_failure(
        DriveDownloadError("Printerval image download failed: curl: (22) HTTP 403 Forbidden")
    )
    challenge = classify_download_failure(
        PrintervalCloudflareChallengeError("Printerval Cloudflare challenge required")
    )
    large = classify_download_failure(
        DriveDownloadError("Image file too large; file=large.jpg; size=1048577; limit_mb=1")
    )
    empty = classify_download_failure(
        DriveDownloadError("Google Drive folder downloaded, but no image files were found")
    )
    unknown = classify_download_failure(RuntimeError("boom"))

    assert network.code == "network_error"
    assert invalid.code == "invalid_drive_url"
    assert rate_limited.code == "drive_rate_limited_or_permission"
    assert direct_image.code == "direct_image_download_failed"
    assert printerval.code == "direct_image_download_failed"
    assert challenge.code == "printerval_challenge_required"
    assert large.code == "image_too_large"
    assert empty.code == "no_images_found"
    assert unknown.code == "unknown_error"
