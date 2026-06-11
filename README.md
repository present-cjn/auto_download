# Order Design Image Downloader

本项目第一阶段是一个轻量订单设计图下载工具：读取客户每天提供的订单 Excel，根据订单号创建目录，并下载 `Design Link` / `Mockup Link` 中的 Google Drive 图片。

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
ADMIN_USERNAME=admin ADMIN_PASSWORD=change-me uvicorn app.main:app --host 0.0.0.0 --port 8000
```

服务器备用下载的单个 Google Drive 下载任务默认 180 秒超时；可用 `DRIVE_DOWNLOAD_TIMEOUT_SECONDS` 调整，例如测试时设为 `60`。

打开：

```text
http://localhost:8000
```

首次启动时如果数据库里还没有账号，系统会用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 创建初始管理员。未设置时默认是：

```text
用户名：admin
密码：change-me
```

`APP_PASSWORD` 仍可作为旧启动方式的密码兜底，但推荐改用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`。

Web 工具支持：

- 上传 `.xlsx` 订单表。
- 使用用户名和密码登录。
- 管理员可查看全部批次并管理账号；业务员只查看自己上传的批次。
- 后台解析订单和 SKU 明细。
- 展示导入预检摘要，包括空链接、非 Drive 链接、重复链接和多 SKU 订单。
- 进入批次详情页查看订单和 SKU 明细。
- 确认字段无误后使用浏览器插件下载设计图；服务器下载入口保留为备用。
- 查看批次、订单、SKU、下载状态、失败类型和失败原因。
- 批量重试失败项，或单独重试失败明细。
- 对无法自动下载的失败项标记为已手动处理。
- 打开原始 Google Drive 链接，便于人工预览或手动下载。
- 浏览器插件会把图片保存到本机 Downloads 的 SKU 文件夹。
- 服务器备用下载完成后，可单独下载某个订单 ZIP，或一键下载全部订单 ZIP。

当前内部测试推荐下载链路是：

```text
Google Drive -> 用户 Chrome 插件 -> 用户本机 Downloads/auto-download/batch-<id>/<sku>/
```

浏览器插件会复用 Web 登录会话，使用当前 Chrome Profile 授权的 Google Drive 只读权限下载文件，并把成功/失败状态回写到 Web。

服务器备用下载链路仍保留：

```text
Google Drive -> 服务器后台任务 -> 服务器保存图片 -> 服务器生成 ZIP -> 用户下载 ZIP 到本地
```

也就是说，点击“备用服务器下载”后，图片先下载到服务器，不依赖用户电脑持续联网或浏览器保持打开。由于 Google Drive 对服务器公开链接下载容易限流，当前只作为应急备用路径。

第一版数据保存在：

```text
data/app.db
data/uploads/<batch_id>/source.xlsx
data/cache/<batch_id>/<google_drive_folder_id>/
data/orders/<batch_id>/<sku>/
data/archives/batch-<batch_id>.zip
data/archives/batch-<batch_id>-order-<order_id>-<订单号>.zip
```

同一个 Google Drive 资源在同一批次内只会下载到缓存一次，多个 SKU 复用同一链接时会从缓存复制到各自目录，减少重复请求 Google Drive。链接支持 Drive 文件夹和 Drive 单文件图片。

页面展示上，单 SKU 和多 SKU 都在同一张明细表里显示；同一个订单下的多条 SKU 会用同一组底色和左侧标识区分。订单 ZIP 会收集该订单关联的 SKU 文件夹。

历史批次如果没有上传人，只对管理员可见；新上传批次会记录创建人。

`data/` 默认不进入 Git。

## CLI Dry Run

Preview the folders and Google Drive links without downloading:

```bash
. .venv/bin/activate
python download_orders.py --dry-run
```

## CLI Download

Download images into `orders/<sku>/`:

```bash
. .venv/bin/activate
python download_orders.py
```

For a small smoke test:

```bash
. .venv/bin/activate
python download_orders.py --limit 3
```

## Tests

运行自动化测试：

```bash
. .venv/bin/activate
python -m pytest
```

当前测试覆盖 Excel 解析、导入预检摘要、下载辅助逻辑、失败分类、下载状态字段和订单 ZIP 结构。测试不访问真实 Google Drive。

## Current Input

后续标准输入以 `IMPORT_EXPORT_SPEC.md` 中的《标准订单导入模板 v1》为准。模板要求每一行代表一条订单明细/SKU，同一个订单有多个 SKU 时，`订单号` 重复填写，多行分别记录不同 SKU。

当前下图阶段必填字段：

- `订单号`
- `日期`
- `SKU`
- `Design Link`
- `Shipping Fullname`
- `Address`
- `City`
- `Province`
- `Zip`
- `Country`

Web 上传要求 Excel 只有一个可见工作表；工作表名称不限。若文件包含多个可见工作表，系统会拒绝解析并提示保留订单明细表后重新上传。CLI 仍可通过 `--sheet` 指定工作表。

同一个订单号如果出现多个 SKU，数据库里会完整保留每一行 SKU 明细。订单号只在订单层去重，不会丢掉同一订单下的多个 SKU。图片下载目录按 SKU 创建，`Design Link` 和 `Mockup Link` 都会下载到对应 SKU 文件夹；两者都支持 Drive 文件夹链接和 `/file/d/.../view` 单文件图片链接。

代码兼容 `SKU`/`sku`、`Zip`/`Postcode`、`颜色`/`Color`。推荐正式模板使用 `SKU`、`Zip`、`颜色`。

## Output

默认输出到：

```text
orders/<sku>/
```

`.venv/`、`orders/`、缓存文件和下载报告文件默认不进入 Git。

## Design Docs

- `DATA_MODEL.md`：订单系统核心数据结构。
- `PRODUCT_REQUIREMENTS.md`：当前需求和后续产品边界。
- `WORKFLOW.md`：当前人工流程和目标系统流程。
- `IMPORT_EXPORT_SPEC.md`：Excel 导入导出字段映射与更新策略。
- `STATUS_MODEL.md`：订单、图片、履约、物流等状态定义。
- `BROWSER_EXTENSION_STABLE_TESTING.md`：当前浏览器插件内部测试稳定版手册。
- `ARCHITECTURE.md`：当前 CLI 到未来云端服务的架构演进。
- `API_SPEC.md`：未来 Web API 设计占位。
- `PERMISSIONS.md`：未来角色和权限设计。
- `DEPLOYMENT.md`：本地运行和未来云端部署说明。
- `AUDIT_LOG.md`：操作审计和追溯设计。
- `DEVELOPMENT_PLAN.md`：阶段性开发计划。
