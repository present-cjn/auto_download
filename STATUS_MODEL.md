# 状态模型文档

## 1. 设计原则

状态字段用于快速查询当前状态，事件表用于保留历史。重要状态变化不能只覆盖字段，后续应追加事件记录。

## 2. Order.status

| 状态 | 说明 |
|---|---|
| `imported` | 已从 Excel 导入 |
| `processing` | 已进入处理流程 |
| `partially_shipped` | 部分履约分组已发货 |
| `shipped` | 全部履约分组已发货 |
| `completed` | 订单流程完成 |
| `cancelled` | 订单取消 |

第一阶段默认只使用 `imported`。

## 3. OrderItem.status

| 状态 | 说明 |
|---|---|
| `imported` | 明细已导入 |
| `assigned` | 已分配供应商或履约分组 |
| `in_production` | 生产中 |
| `ready_to_ship` | 可发货 |
| `shipped` | 已发货 |
| `cancelled` | 已取消 |

## 4. Asset.download_status

| 状态 | 说明 |
|---|---|
| `pending` | 待下载 |
| `downloaded` | 下载成功 |
| `failed` | 下载失败 |
| `skipped` | 已跳过，例如非图片文件或重复链接 |

当前下图工具主要围绕这个状态演进。

## 5. FulfillmentGroup.status

| 状态 | 说明 |
|---|---|
| `pending` | 已生成履约分组，待处理 |
| `in_production` | 生产中 |
| `ready_to_ship` | 可创建物流单 |
| `shipment_created` | 已创建物流单 |
| `shipped` | 已发货 |
| `cancelled` | 已取消 |

## 6. Shipment.status

| 状态 | 说明 |
|---|---|
| `created` | 已创建物流单 |
| `in_transit` | 运输中 |
| `delivered` | 已签收 |
| `exception` | 物流异常 |
| `cancelled` | 物流单取消 |

## 7. 状态变更原则

- 导入 Excel 不应自动覆盖人工确认后的终态。
- 下载失败可以重试，重试成功后可从 `failed` 变为 `downloaded`。
- 物流状态以后由物流接口事件归一化更新。
- 生产状态以后由人工或供应商操作写入事件。

## 8. 待确认问题

- 是否需要订单关闭或归档状态。
- 是否允许物流状态人工修正。
- 生产进度是否按订单明细还是履约分组维护。
