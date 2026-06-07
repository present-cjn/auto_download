# Order Design Image Downloader

本项目第一阶段是一个轻量订单设计图下载工具：读取客户每天提供的订单 Excel，根据订单号创建目录，并下载 `Design Link` 中的 Google Drive 图片。

当前支持 CLI 和轻量 Web 两种运行方式。后续方向是演进成云端订单管理服务，逐步支持订单维护、物流运单号获取/生成、物流跟踪、生产进度跟踪和导入导出自动化。当前代码先服务最重要的交付目标：稳定下图。

## Setup

Create and use the local virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Web Tool

启动轻量 Web 工具：

```bash
. .venv/bin/activate
APP_PASSWORD=change-me uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开：

```text
http://localhost:8000
```

Web 工具支持：

- 上传 `.xlsx` 订单表。
- 后台解析订单和 SKU 明细。
- 进入批次详情页人工核对。
- 确认后手动开始下载设计图。
- 查看批次、订单、SKU、下载状态和失败原因。
- 批量重试失败项，或单独重试失败明细。
- 打开原始 Google Drive 链接，便于人工预览或手动下载。
- 下载批次 ZIP。

云端部署时的下载链路是：

```text
Google Drive -> 服务器后台任务 -> 服务器保存图片 -> 服务器生成 ZIP -> 用户下载 ZIP 到本地
```

也就是说，点击“开始/继续下载”后，图片先下载到服务器，不依赖用户电脑持续联网或浏览器保持打开。用户最后通过“下载 ZIP”把服务器整理好的结果包下载到本地。

第一版数据保存在：

```text
data/app.db
data/uploads/<batch_id>/source.xlsx
data/cache/<batch_id>/<google_drive_folder_id>/
data/orders/<batch_id>/<订单号>/<sku>/
data/archives/batch-<batch_id>.zip
```

同一个 Google Drive folder 在同一批次内只会下载到缓存一次，多个 SKU 复用同一链接时会从缓存复制到各自目录，减少重复请求 Google Drive。

`data/` 默认不进入 Git。

## CLI Dry Run

Preview the folders and Google Drive links without downloading:

```bash
. .venv/bin/activate
python download_orders.py --dry-run
```

## CLI Download

Download images into `orders/<订单号>/<sku>/`:

```bash
. .venv/bin/activate
python download_orders.py
```

For a small smoke test:

```bash
. .venv/bin/activate
python download_orders.py --limit 3
```

## Current Input

默认读取：

- 文件：`BT-6月1日订单总.xlsx`
- 工作表：`12`
- 订单号列：`订单号`
- 图片链接列：`Design Link`

同一个订单号如果出现多个 SKU，会在订单目录下继续按 SKU 创建子文件夹，并把该 SKU 对应的图片放进去。

数据库里会完整保留每一行 SKU 明细。订单号只在订单层去重，不会丢掉同一订单下的多个 SKU。

## Output

默认输出到：

```text
orders/<订单号>/<sku>/
```

`.venv/`、`orders/`、缓存文件和下载报告文件默认不进入 Git。

## Design Docs

- `DATA_MODEL.md`：订单系统核心数据结构。
- `PRODUCT_REQUIREMENTS.md`：当前需求和后续产品边界。
- `WORKFLOW.md`：当前人工流程和目标系统流程。
- `IMPORT_EXPORT_SPEC.md`：Excel 导入导出字段映射与更新策略。
- `STATUS_MODEL.md`：订单、图片、履约、物流等状态定义。
- `ARCHITECTURE.md`：当前 CLI 到未来云端服务的架构演进。
- `API_SPEC.md`：未来 Web API 设计占位。
- `PERMISSIONS.md`：未来角色和权限设计。
- `DEPLOYMENT.md`：本地运行和未来云端部署说明。
- `AUDIT_LOG.md`：操作审计和追溯设计。
- `DEVELOPMENT_PLAN.md`：阶段性开发计划。
