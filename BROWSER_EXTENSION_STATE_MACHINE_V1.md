# 浏览器插件状态机设计 v1

## 版本信息

- 文档版本：`v1.0-design`
- 日期：`2026-06-16`
- 状态：`Proposed`
- 适用范围：Web 批次详情页、Chrome 浏览器插件 popup、extension service worker、content script 桥接协议。
- 当前稳定点：`master` 已合并浏览器插件下载稳定性修复，包括 Drive API 下载、停止逻辑、`/open?id` 文件夹识别、heartbeat 恢复 stale downloading。
- 测试域名：`https://dev.waysing.cn`
- 固定插件 ID：`nodoinolmkijilpcgncdcglmplkleaie`

本设计是下一阶段状态管理重构的实施基线。实施前不得把这里的状态枚举当作当前代码已完全支持的行为。

## 设计目标

统一 Web 页面、插件 popup 和 service worker 的状态展示，避免各端分别拼接 `running`、`stopping`、中文 `state` 文案导致按钮互相打架。

核心原则：

- 插件运行状态由 service worker 产出统一 snapshot。
- Web 页面先判断当前浏览器插件是否安装、是否响应，再结合服务端下载项状态渲染按钮。
- 服务端 `download_items.status` 继续作为任务事实记录。
- `stale` 是 UI 和诊断状态，不新增为 `download_items.status` 落库值。
- 操作员只在 Web 批次页处理开始、停止、重试、刷新，不需要理解 Chrome 下载栏。

## 状态定义

### 插件运行状态 `phase`

| 状态 | 来源 | 含义 | 典型 UI |
|---|---|---|---|
| `disconnected` | Web 派生 | 页面未收到 content script 或 service worker 响应 | 未检测到插件，开始/停止禁用，可刷新 |
| `idle` | 插件 snapshot | 插件已安装并响应，但缺少有效 `baseUrl` 或 `batchId` | popup 可填写配置，Web 不直接启动 |
| `ready` | 插件 snapshot | 插件已安装、响应、配置完整、未运行 | 可开始当前批次 |
| `running` | 插件 snapshot | 插件正在拉取队列或处理下载项 | 开始禁用，同批次可停止 |
| `stopping` | 插件 snapshot | 已收到停止请求，正在取消当前下载或等待失败回写 | 开始/停止/重试禁用 |
| `stopped` | 插件 snapshot | 用户停止后进入终态，本轮不再继续拉取新项 | 可重试失败项或重新开始 |
| `completed` | 插件 snapshot | 本轮没有更多待处理项 | 可刷新；有失败项时可重试 |
| `failed` | 插件 snapshot | 插件运行级错误，例如未登录、Web API 失败、配置缺失 | 显示错误，可修复后重试 |

### 服务端任务状态

`download_items.status` 保持当前枚举：

| 状态 | 含义 |
|---|---|
| `pending` | 待下载 |
| `downloading` | 某个执行器已声明开始处理 |
| `downloaded` | 下载成功 |
| `failed` | 下载失败，可重试 |
| `skipped` | 已跳过 |
| `manual_done` | 人工标记已处理 |

`stale` 不落库为独立状态。服务端发现 `downloading` 项 heartbeat 过期后，恢复为：

```text
status = failed
error_code = interrupted
error_message = 插件中断或页面重新加载，下载结果未回写，请重试。
```

Web 页面可在本次响应中显示“发现上次中断项，已恢复为可重试”。

## 状态转换

| 当前状态 | 事件 | 下一状态 | 说明 |
|---|---|---|---|
| `disconnected` | 收到插件 status ACK | `idle` / `ready` / `running` / 其他 snapshot 状态 | Web 派生状态解除 |
| `idle` | 保存有效配置 | `ready` | `baseUrl` 和 `batchId` 均有效 |
| `ready` | start 成功 | `running` | 开始拉取服务端队列 |
| `stopped` | start 成功 | `running` | 重新开始 |
| `completed` | start 成功且仍有待处理项 | `running` | 常见于重试失败项 |
| `failed` | start 成功 | `running` | 修复登录、配置或权限后重试 |
| `running` | stop 请求 | `stopping` | 立即禁止重复操作 |
| `stopping` | 当前项取消并回写 failure | `stopped` | 不继续处理新项 |
| `running` | 队列为空 | `completed` | 没有更多待处理项 |
| `running` | 运行级异常 | `failed` | 例如 Web 登录失效或 API 整体失败 |
| `running` | 单项失败 | `running` | 只累加失败计数，继续后续项 |

## 统一 Snapshot 协议

service worker 对 popup 和 Web content script 返回统一结构：

```json
{
  "ok": true,
  "protocolVersion": 2,
  "snapshot": {
    "phase": "ready",
    "baseUrl": "https://dev.waysing.cn",
    "batchId": "12",
    "batchRelation": "same",
    "isRunning": false,
    "isStopping": false,
    "processed": 0,
    "done": 0,
    "failed": 0,
    "currentTask": {
      "downloadItemId": 123,
      "sku": "SKU-A",
      "sourceType": "design"
    },
    "lastError": {
      "code": "extension_download_failed",
      "message": "Drive API 403"
    },
    "message": "插件已就绪。",
    "updatedAt": "2026-06-16T00:00:00Z"
  }
}
```

字段约定：

- `phase` 是 UI 唯一可信的插件运行状态。
- `batchRelation` 由 Web 页面根据当前页面批次与 `snapshot.batchId` 派生，可取 `same`、`other`、`none`。
- `currentTask` 为空表示没有当前项。
- `lastError` 只表示插件运行级错误或最近一次失败摘要，不替代服务端失败详情。
- 为兼容当前代码，实施初期可以保留旧字段 `running`、`stopping`、`state`，但新 UI 不再依赖它们。

## Web 消息协议

保留现有窗口消息类型，升级 payload：

| 方向 | 类型 | 说明 |
|---|---|---|
| Web -> content script | `autoDownloadExtensionStatus` | ping 插件并获取 snapshot |
| Web -> content script | `autoDownloadExtensionStart` | 传入 `baseUrl`、`batchId` 并启动 |
| Web -> content script | `autoDownloadExtensionStop` | 请求停止当前插件运行 |
| content script -> Web | `autoDownloadExtensionAck` | 返回 `ok`、`action`、`snapshot` 或 `error` |

content script 到 service worker 的消息：

| 类型 | 输入 | 输出 |
|---|---|---|
| `status` | 无 | `{ ok, protocolVersion, snapshot }` |
| `configureAndStart` | `{ baseUrl, batchId }` | `{ ok, protocolVersion, snapshot, error? }` |
| `stop` | 无 | `{ ok, protocolVersion, snapshot, error? }` |

Web 页面超时策略：

- 发出 status/start/stop 后 2500ms 未收到 ACK，派生 `phase=disconnected`。
- 收到 ACK 但 `ok=false`，显示错误并刷新按钮状态。

## 服务端接口设计

保留现有插件下载 API：

- `GET /api/extension/batches/{batch_id}/download-items?limit=20`
- `POST /api/extension/download-items/{download_item_id}/start`
- `POST /api/extension/download-items/{download_item_id}/heartbeat`
- `POST /api/extension/download-items/{download_item_id}/success`
- `POST /api/extension/download-items/{download_item_id}/failure`

增强 Web 状态接口：

```text
GET /batches/{batch_id}/status
```

建议返回：

```json
{
  "batch": {
    "id": 12,
    "status": "downloading",
    "file_name": "orders.xlsx"
  },
  "status_counts": {
    "pending": 10,
    "downloading": 1,
    "downloaded": 3,
    "failed": 2,
    "skipped": 0,
    "manual_done": 0
  },
  "current_task": {
    "download_item_id": 123,
    "sku": "SKU-A",
    "source_type": "design",
    "duration_label": "已用时 1m 20s"
  },
  "stale_recovered_count": 0,
  "actions": {
    "can_start_extension": true,
    "can_retry_failed": true,
    "can_refresh": true
  }
}
```

说明：

- Web 最终按钮状态仍需同时考虑插件 snapshot。
- `actions` 只表示服务端角度是否允许，不代表当前浏览器插件可用。
- stale 恢复继续在读取批次页、读取状态接口、插件拉取队列前执行。

## Web UI 行为

批次详情页显示三个层次：

1. 插件连接：已安装并响应、未检测到插件、插件正在处理其他批次。
2. 插件状态：空闲、就绪、运行中、正在停止、已停止、已完成、错误、未连接。
3. 服务端进度：待处理、处理中、成功、失败、手动完成。

按钮规则：

| 按钮 | 启用条件 | 禁用条件 |
|---|---|---|
| 开始 | 插件 `ready/stopped/completed/failed`，当前批次有 `pending+failed`，批次不是 `needs_fix`，没有其他批次运行 | `disconnected`、`running`、`stopping`、其他批次运行、无待处理项 |
| 停止 | 插件 `running` 且正在处理当前批次 | 未连接、未运行、正在停止、运行其他批次 |
| 重试失败项 | 有 failed 项，当前批次没有 fresh `downloading`，插件不是 `running/stopping` | 当前批次处理中、正在停止、没有失败项 |
| 刷新 | 始终启用 | 无 |
| 单项重试/标记已处理 | 该项 `failed` 且当前批次没有运行冲突 | 当前批次 `downloading` 或插件 `running/stopping` |

Web 文案原则：

- 不再提示“点击 Chrome 下载栏继续”。
- 下载中断统一提示“请在本页重试失败项”。
- 插件未响应时提示“未检测到插件，请确认已安装并启用后刷新页面”。
- 其他批次运行时显示“插件正在处理批次 X，请等待完成后再启动本批次”。

## Popup UI 行为

popup 使用同一 snapshot：

- 状态徽标显示 `phase` 的中文标签。
- 计数显示 `processed`、`done`、`failed`。
- 当前项显示 `currentTask.sku` 和 `sourceType`。
- 最近错误显示 `lastError.message`。
- 开始按钮：仅在 `ready/stopped/completed/failed` 时启用。
- 停止按钮：仅在 `running` 时启用，`stopping` 时禁用。

popup 保留手动输入 `baseUrl` 和 `batchId`，作为调试和应急入口。

## Service Worker 行为

service worker 负责产出唯一 snapshot：

- 启动时从 `chrome.storage.local` 读取持久字段。
- 内存中的 `workerRunning`、`stopInProgress`、`currentTask` 只用于修正当前生命周期状态。
- 每次状态变化写入 `phase`、`message`、`updatedAt`。
- 单项失败不把 `phase` 置为 `failed`，只更新计数和 `lastError`。
- 运行级异常才置为 `failed`。
- stop 请求必须尽快进入 `stopping`，并尝试 `chrome.downloads.cancel(activeDownloadId)`。

## 边界情况

### 插件未安装或未响应

Web 发出 status 后 2500ms 无 ACK，派生 `disconnected`。开始和停止禁用，只显示安装/启用/刷新提示。

### 插件处理其他批次

如果 `snapshot.batchId` 与页面批次不同，Web 显示其他批次 ID。当前页面开始禁用，停止默认禁用，避免误停其他批次。

### 用户停止

插件进入 `stopping`，取消当前 Chrome download，并把当前项以 `extension_stopped_by_user` 回写 failure。完成后进入 `stopped`，Web 失败项可重试。

### 插件 reload 或 service worker 重启

内存运行状态会丢失。插件从 storage 派生 `stopped` 或 `failed`。服务端仍为 fresh `downloading` 的项等待 heartbeat 超时；超过阈值后恢复为 failed/interrupted。

### 浏览器关闭

浏览器关闭不会主动回写。服务端在读取批次页、读取状态接口或插件拉取队列时执行 stale 恢复。操作员看到的是“上次中断项已恢复为可重试”。

### Web 登录失效

插件 API 调用失败时进入 `failed`，提示重新登录 Web 后刷新并重试。

### Drive OAuth 或权限失败

单项标记 failed，错误详情落库，插件继续后续项。UI 提示在本页重试或检查 Drive 权限。

## 测试计划

### Python 测试

- 原有扩展 API 测试继续通过。
- `/batches/{batch_id}/status` 返回 `status_counts`、`current_task`、`actions`。
- stale downloading 恢复后返回 `stale_recovered_count`，对应项为 `failed/interrupted`。
- 有 `pending`、`failed`、`downloading`、`needs_fix` 时 actions 派生正确。
- 非批次创建者 operator 不能读取状态或下载队列。

### 插件手工测试

- popup 覆盖 `idle/ready/running/stopping/stopped/completed/failed`。
- Web 未安装插件时显示 `disconnected`。
- 当前插件运行本批次时，开始禁用、停止启用。
- 当前插件运行其他批次时，开始禁用、停止禁用。
- 点击停止后当前项变 failed，可在 Web 重试。
- 单项失败后插件继续处理后续项。

### 浏览器端验收

- 在 `https://dev.waysing.cn` 用固定插件 ID 测试。
- 正常下载 Drive file 和 Drive folder。
- 停止、重试、刷新按钮不会互相打架。
- 关闭浏览器或 reload 插件后，stale 项能恢复并给出清晰提示。
- 页面和 popup 不引导操作员点击 Chrome 下载栏。

## 安全注意事项

- 不提交生成固定扩展 ID 的 `.pem` 私钥。
- `browser-extension/manifest.json` 中的 public `key` 和 OAuth client ID 谨慎处理，实施状态机时不随意改动。
- 不提交 Google token、cookie、客户 Excel、下载结果或 `data/` 目录。

## 后续版本规则

- 实施完成并验证后，将本文档状态更新为 `Implemented`，并记录实施 commit。
- 如果实施中改变协议字段或状态语义，新增 `v1.1` 小版本记录，不直接覆盖历史含义。
- 如果引入服务器端插件运行会话表，另起 `v2` 设计，因为运行归属语义会从“当前浏览器”变为“服务器租约”。
