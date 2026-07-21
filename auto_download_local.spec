# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


block_cipher = None
datas = [
    ("configure_drive.bat", "."),
    ("templates", "templates"),
    ("static", "static"),
    ("browser-extension", "browser-extension"),
]

if Path("vendor/rclone").exists():
    datas.append(("vendor/rclone", "vendor/rclone"))


a = Analysis(
    ["scripts/start_local_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "app",
        "app.main",
        "app.core",
        "app.core.database",
        "app.core.downloader",
        "app.core.excel_parser",
        "app.core.security",
        "app.core.tasks",
        "app.tools",
        "app.tools.printerval_session",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoDownload",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoDownload",
    contents_directory=".",
)
