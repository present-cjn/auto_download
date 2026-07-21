from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.request import urlopen


if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def choose_port(host: str, preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 50):
        if port_is_free(host, port):
            return port
    raise RuntimeError("No free local port found.")


def wait_for_health(url: str, timeout_seconds: int = 30) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                return response.status == 200
        except OSError:
            time.sleep(0.5)
    return False


def run_packaged_server(host: str, port: int, env: dict[str, str]) -> int:
    os.environ.update(env)
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import uvicorn
    from app.main import app as fastapi_app

    config = uvicorn.Config(fastapi_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://{host}:{port}"
    if wait_for_health(f"{url}/health"):
        webbrowser.open(url)
        print(f"Auto Download is running at {url}")
        print("Close this window to stop the local app.")
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
            return 0
        return 0

    server.should_exit = True
    print("Local app did not become ready in time.", file=sys.stderr)
    return 1


def main() -> int:
    host = os.getenv("AUTO_DOWNLOAD_HOST", DEFAULT_HOST)
    preferred_port = int(os.getenv("AUTO_DOWNLOAD_PORT", str(DEFAULT_PORT)))
    port = choose_port(host, preferred_port)
    url = f"http://{host}:{port}"
    env = {
        **os.environ,
        "DRIVE_DOWNLOAD_BACKEND": os.getenv("DRIVE_DOWNLOAD_BACKEND", "rclone"),
    }
    if getattr(sys, "frozen", False):
        return run_packaged_server(host, port, env)

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    process = subprocess.Popen(command, cwd=ROOT, env=env)
    try:
        if wait_for_health(f"{url}/health"):
            webbrowser.open(url)
            print(f"Auto Download is running at {url}")
            print("Close this window to stop the local app.")
            return process.wait()
        print("Local app did not become ready in time.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()


def write_startup_error(exc: BaseException) -> Path:
    log_path = ROOT / "startup-error.log"
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path.write_text(detail, encoding="utf-8")
    return log_path


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        log_path = write_startup_error(exc)
        print("Auto Download failed to start.", file=sys.stderr)
        print(f"Startup error log: {log_path}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        if getattr(sys, "frozen", False):
            input("Press Enter to close this window...")
        raise
    raise SystemExit(exit_code)
