# 审计日志设计文档

## 1. 设计目标

订单、地址、物流单号、设计图和导出文件都需要可追溯。后续云端服务应记录关键操作，方便排错、对账和责任追踪。

## 2. 需要记录的操作

- 上传并导入 Excel。
- 创建或更新订单。
- 创建订单明细。
- 新增或下载设计图。
- 下载失败和重试。
- 创建或修改履约分组。
- 获取或修改物流单号。
- 查询或更新物流状态。
- 修改生产进度。
- 导出表格。
- 修改系统配置。

## 3. AuditLog 建议字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 日志 ID |
| `actor_id` | FK, nullable | 操作人 |
| `actor_name` | string | 操作人快照 |
| `action` | string | 操作类型 |
| `target_type` | string | 目标对象类型 |
| `target_id` | string | 目标对象 ID |
| `before_data` | json, nullable | 修改前 |
| `after_data` | json, nullable | 修改后 |
| `metadata` | json | 附加信息 |
| `created_at` | datetime | 操作时间 |

## 4. 当前 CLI 阶段过渡方案

当前暂不实现数据库审计日志。第一阶段可通过以下方式过渡：

- 控制台输出处理进度。
- 后续增加 `download_report.csv`。
- 后续增加 `failed.csv`。
- 保留导入源文件。

## 5. 待确认问题

- 审计日志保留时间。
- 是否需要导出审计日志。
- 是否需要记录敏感字段修改前后的完整值。
