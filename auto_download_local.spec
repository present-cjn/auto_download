# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


block_cipher = None
datas = [
    ("configure_drive.bat", "."),
    ("local_settings.example.json", "."),
    ("templates", "templates"),
    ("static", "static"),
    ("browser-extension", "browser-extension"),
]

if Path("vendor/rclone").exists():
    datas.append(("vendor/rclone", "vendor/rclone"))


a = Analysis(
    ["scripts/start_local_app.py"],
    pathex=[str(Path.cwd())],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("app")
    + collect_submodules("uvicorn")
    + [
        "multipart",
        "python_multipart",
        "python_multipart.multipart",
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
