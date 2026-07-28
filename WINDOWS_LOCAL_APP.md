# Windows 本地 App 打包说明

## 目标

发布给操作员的第一版 Windows 包应支持：

- 双击 `AutoDownload.exe` 启动本地服务。
- 自动打开浏览器访问本地页面。
- 不要求操作员安装 Python。
- 内置 `rclone.exe`，用于稳定下载 Google Drive 图片。
- Google Drive OAuth token 只保存在每台电脑本地，不进入发布包。

## 准备 rclone

下载 Windows 版 rclone 后，把 `rclone.exe` 放到：

```text
vendor/rclone/rclone.exe
```

应用查找顺序：

1. 环境变量 `RCLONE_BIN` 指定的路径。
2. 发布目录内置的 `vendor/rclone/rclone.exe`。
3. 系统 `PATH` 里的 `rclone`。

## 本地开发启动

```bat
start_auto_download.bat
```

脚本会优先使用 `.venv\Scripts\python.exe`，启动 FastAPI 服务，并打开浏览器。

## 构建 exe

在 Windows 开发机上优先运行一键构建脚本：

```bat
build_windows_app.bat
```

输出目录：

```text
dist\AutoDownload\
```

把整个 `dist\AutoDownload\` 目录交给操作员，不要只发单个 exe。

手动构建命令等价于：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\pyinstaller.exe --clean auto_download_local.spec
```

## 操作员首次使用

1. 双击 `AutoDownload.exe`。
2. 登录本地系统账号。
3. 打开“Drive 设置”。
4. 点击“登录 Google Drive”。
5. 浏览器打开后，登录能访问设计图的 Google 账号并允许只读权限。
6. 回到“Drive 设置”，确认 Drive 访问为“可访问”。

如果页面登录失败，再在发布目录双击或命令行执行 `configure_drive.bat` 做高级排障。

推荐配置：

- remote 名称：`gdrive`
- storage 类型：Google Drive
- scope：只读权限
- auto config：yes

完成后刷新“Drive 设置”，确认 Drive 访问为“可访问”。

## 数据位置

第一版数据保存在发布目录下：

```text
data\app.db
data\uploads\
data\cache\
data\orders\
data\archives\
```

每台电脑的数据相互独立。Google token 保存在该 Windows 用户自己的 rclone 配置位置，不随应用包分发。

## 发布前验收

- 在干净 Windows 电脑上启动 `AutoDownload.exe`。
- Drive 设置页能识别内置 `vendor\rclone\rclone.exe`。
- 未授权时能显示明确错误。
- 完成 `gdrive` 授权后显示可访问。
- 上传小批量 Excel 后能完成下载、失败重试和 ZIP 生成。

如果 `AutoDownload.exe` 启动失败，发布目录会写入：

```text
startup-error.log
```

优先查看这个文件，它会记录完整 Python traceback。

## 发布包应包含

```text
AutoDownload.exe
configure_drive.bat
templates\
static\
vendor\rclone\rclone.exe
```

`data\` 可以不存在，应用首次启动会自动创建。
