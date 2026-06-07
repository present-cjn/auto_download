# 订单管理系统数据结构设计

本文档基于 `BT-6月1日订单总.xlsx` 的现有表头设计，目标是为后续统一开发订单导入、设计图下载、物流单号生成、物流跟踪、生产进度跟踪等功能提供稳定的数据模型。

## 1. 当前 Excel 数据来源

### 工作表 `12`

这是主订单明细表。每一行更接近一个“订单明细 / SKU 明细”，不是完整客户订单。

| Excel 列 | 表头 | 建议归属 | 说明 |
|---:|---|---|---|
| A | 订单号 | `Order.order_no` | 客户订单号，同一订单号可能出现多行 |
| B | 日期 | `Order.order_date` | Excel 日期序列，例如 `46174` = `2026-06-01` |
| C | Design Link | `Asset.source_url` | Google Drive 设计图链接，通常挂到订单明细 |
| D | sku | `OrderItem.sku` | 商品 SKU，可用于识别商品类型、供应商、生产规则 |
| E | 尺码 | `OrderItem.size` | 商品尺寸 |
| F | Color | `OrderItem.color` | 商品颜色 |
| G | 数量 | `OrderItem.quantity` | 购买数量 |
| H | 定制名 | `OrderItem.custom_name` | 个性化定制内容 |
| I | 单号 | `Shipment.tracking_no` 或 `Shipment.external_no` | 当前表中已有的物流/转单号 |
| J | Shipping Fullname | `ShippingAddress.full_name` | 收件人姓名 |
| K | Address | `ShippingAddress.address1` | 收件地址 |
| L | City | `ShippingAddress.city` | 城市 |
| M | Province | `ShippingAddress.province` | 州/省 |
| N | Zip | `ShippingAddress.postal_code` | 邮编 |
| O | Country | `ShippingAddress.country_code` | 国家 |
| P | Phone | `ShippingAddress.phone` | 电话 |
| Q | Mail | `Customer.email` / `Order.customer_email` | 客户邮箱 |
| R | Customer Note | `Order.customer_note` | 客户备注 |
| S | Product ID | `OrderItem.product_external_id` | 外部商品 ID |
| T | Week | `Order.week_no` | 业务周次 |
| U | Các mục mẹ | `OrderItem.parent_item_name_local` | 父级商品信息，保留原始字段 |
| V | Parent items | `OrderItem.parent_item_name` | 父级商品信息 |

### 工作表 `Sheet4`

这是订单号与转单号映射表。

| Excel 列 | 表头 | 建议归属 | 说明 |
|---:|---|---|---|
| A | 原单号 | `Order.order_no` | 对应主表订单号 |
| B | 转单号 | `Shipment.tracking_no` / `Shipment.external_no` | 可作为导入时校验或补充物流单号 |

## 2. 核心实体

### Order 客户订单

表示客户的一次下单。一个 `Order` 可以包含多个 `OrderItem`，也可以拆成多个履约分组和多个物流单。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `order_no` | string, unique | 客户订单号，例如 `BSK-1347504-US` |
| `order_date` | date | 下单日期 |
| `week_no` | int | Excel 的 `Week` |
| `customer_email` | string | 客户邮箱快照 |
| `customer_note` | text | 客户备注 |
| `source_file` | string | 来源文件名 |
| `source_batch_id` | string | 导入批次 |
| `status` | enum | `imported`, `processing`, `partially_shipped`, `shipped`, `cancelled` |
| `created_at` / `updated_at` | datetime | 系统时间 |

### OrderItem 订单明细

表示订单中的一个商品明细。当前 Excel 主表的一行通常对应一条 `OrderItem`。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `order_id` | FK | 所属客户订单 |
| `source_row_no` | int | Excel 原始行号，便于追溯 |
| `sku` | string | SKU |
| `product_external_id` | string | Excel 的 `Product ID` |
| `product_type` | string | 商品类型，通常由 SKU 或商品配置表推导 |
| `supplier_id` | FK, nullable | 供应商，通常由 SKU 或商品配置表推导 |
| `size` | string | 尺码 |
| `color` | string | 颜色 |
| `quantity` | int | 数量 |
| `custom_name` | string | 定制名 |
| `parent_item_name` | string | 父级商品 |
| `parent_item_name_local` | string | 原始本地语言父级商品 |
| `status` | enum | `imported`, `assigned`, `in_production`, `ready_to_ship`, `shipped`, `cancelled` |

### ShippingAddress 收货地址

建议作为订单的地址快照，而不是直接覆盖客户资料。客户以后改地址，不应影响历史订单。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `order_id` | FK | 所属订单 |
| `full_name` | string | 收件人 |
| `address1` | string | 地址 |
| `city` | string | 城市 |
| `province` | string | 州/省 |
| `postal_code` | string | 邮编 |
| `country_code` | string | 国家代码 |
| `phone` | string | 电话 |

### Asset 设计图 / 附件

用于管理 `Design Link` 下载出的图片和后续可能出现的文件。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `order_id` | FK | 所属订单 |
| `order_item_id` | FK, nullable | 所属订单明细；当前建议优先挂到明细 |
| `source_url` | text | Google Drive 原始链接 |
| `drive_folder_id` | string | 从链接解析出的 Drive folder id |
| `local_dir` | string | 本地订单目录，例如 `orders/BSK-1347504-US/` |
| `local_path` | string, nullable | 下载后的具体文件路径 |
| `file_name` | string, nullable | 文件名 |
| `file_ext` | string, nullable | 文件扩展名 |
| `asset_type` | enum | `design_image`, `reference`, `other` |
| `download_status` | enum | `pending`, `downloaded`, `failed`, `skipped` |
| `error_message` | text, nullable | 下载失败原因 |

### FulfillmentGroup 履约分组

这是后续系统最关键的中间层。它表示“哪些订单明细可以一起生产、一起发货、共用一个物流单号”。

一个客户订单可能拆成多个 `FulfillmentGroup`。拆分依据通常包括供应商、商品类型、仓库、物流公司、是否允许合并发货等。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `order_id` | FK | 所属订单 |
| `supplier_id` | FK, nullable | 供应商 |
| `product_type` | string, nullable | 商品类型 |
| `carrier_id` | FK, nullable | 指定物流公司 |
| `group_key` | string | 系统生成的分组键，例如 `order_id + supplier_id + product_type` |
| `status` | enum | `pending`, `in_production`, `ready_to_ship`, `shipment_created`, `shipped`, `cancelled` |
| `created_by_rule` | string | 记录使用了哪条分组规则 |

### FulfillmentGroupItem 履约分组明细

连接 `FulfillmentGroup` 和 `OrderItem`。如果未来需要拆分数量，这层可以支持一条订单明细拆到多个分组。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `fulfillment_group_id` | FK | 履约分组 |
| `order_item_id` | FK | 订单明细 |
| `quantity` | int | 分配到该分组的数量 |

### Shipment 物流单

表示一个实际物流单。简单情况下，一个 `FulfillmentGroup` 对应一个 `Shipment`；复杂情况下，一个履约分组也可以拆成多个物流单。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `fulfillment_group_id` | FK | 所属履约分组 |
| `carrier_id` | FK | 物流公司 |
| `tracking_no` | string | 物流单号 / 转单号 |
| `external_no` | string, nullable | 第三方系统返回的外部编号 |
| `label_url` | text, nullable | 面单地址 |
| `status` | enum | `created`, `in_transit`, `delivered`, `exception`, `cancelled` |
| `created_at` | datetime | 创建时间 |
| `shipped_at` | datetime, nullable | 发货时间 |
| `delivered_at` | datetime, nullable | 签收时间 |

### ShipmentTrackingEvent 物流跟踪事件

物流状态不要只覆盖 `Shipment.status`，还应保留事件流水。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `shipment_id` | FK | 所属物流单 |
| `event_time` | datetime | 物流事件时间 |
| `status` | string | 物流公司原始状态 |
| `normalized_status` | enum | 系统归一状态 |
| `location` | string, nullable | 事件地点 |
| `description` | text | 事件描述 |
| `raw_payload` | json | 物流接口原始返回 |

### ProductionProgressEvent 生产进度事件

生产进度建议用事件表记录，而不是只在订单或明细上覆盖一个状态。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `target_type` | enum | `order_item` 或 `fulfillment_group` |
| `target_id` | string / bigint | 对应目标 ID |
| `status` | enum | `pending`, `assigned`, `printing`, `sewing`, `qc`, `packed`, `ready_to_ship`, `blocked` |
| `note` | text, nullable | 备注 |
| `operator_id` | FK, nullable | 操作人 |
| `created_at` | datetime | 记录时间 |

### ProductCatalog 商品配置

当前 Excel 没有明确的“商品类型”和“供应商”字段，因此后续需要通过 SKU 或 Product ID 维护商品配置。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `sku_pattern` | string | SKU 匹配规则 |
| `product_external_id` | string, nullable | 外部商品 ID |
| `product_type` | string | 商品类型 |
| `supplier_id` | FK | 默认供应商 |
| `default_carrier_id` | FK, nullable | 默认物流公司 |
| `fulfillment_rule` | string | 默认履约分组规则 |

### Supplier 供应商

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `name` | string | 供应商名称 |
| `code` | string, unique | 供应商代码 |
| `contact_info` | json | 联系方式 |
| `status` | enum | `active`, `disabled` |

### Carrier 物流公司

用于用户自定义物流公司及后续批量创建物流单。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 系统内部主键 |
| `name` | string | 物流公司名称 |
| `code` | string, unique | 系统内部代码 |
| `api_provider` | string | 接口提供方 |
| `api_config` | json | API 配置，密钥应加密保存 |
| `tracking_url_template` | text | 查询链接模板 |
| `status` | enum | `active`, `disabled` |

### ImportBatch / ImportRow 导入批次

建议保留原始导入记录，方便排查 Excel 数据问题。

`ImportBatch` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 导入批次 ID |
| `file_name` | string | 文件名 |
| `sheet_name` | string | 工作表名 |
| `row_count` | int | 行数 |
| `status` | enum | `pending`, `completed`, `failed` |
| `created_at` | datetime | 导入时间 |

`ImportRow` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string / bigint | 原始行 ID |
| `batch_id` | FK | 导入批次 |
| `row_no` | int | Excel 行号 |
| `order_no` | string | 原始订单号 |
| `raw_data` | json | 原始行完整字段 |
| `error_message` | text, nullable | 解析失败原因 |

## 3. 推荐关系

```text
Order
  -> ShippingAddress
  -> OrderItem
      -> Asset
      -> FulfillmentGroupItem
          -> FulfillmentGroup
              -> Shipment
                  -> ShipmentTrackingEvent
              -> ProductionProgressEvent

ProductCatalog
  -> Supplier
  -> Carrier

ImportBatch
  -> ImportRow
```

核心原则：

- `Order` 是客户订单，不直接等于物流单。
- `OrderItem` 是客户买的每个商品明细。
- `FulfillmentGroup` 决定哪些明细可以一起生产、一起发货。
- `Shipment` 是实际物流单号。
- `Asset` 管理设计图下载结果。
- `ProgressEvent` 和 `TrackingEvent` 保留历史，不只覆盖当前状态。

## 4. 导入和分组规则

### Excel 导入

1. 创建 `ImportBatch`。
2. 逐行保存 `ImportRow.raw_data`。
3. 按 `订单号` upsert `Order`。
4. 每一行创建一条 `OrderItem`。
5. 根据 `Design Link` 创建 `Asset`。
6. 根据收货字段创建或更新该订单的 `ShippingAddress` 快照。
7. 若 `单号` 或 `Sheet4.转单号` 已存在，则创建对应 `Shipment`。

### 履约分组

第一版建议使用以下优先级：

1. 如果 Excel 行已有 `单号`，同一订单下相同 `单号` 的明细进入同一个 `FulfillmentGroup`。
2. 如果没有 `单号`，通过 `ProductCatalog` 从 SKU / Product ID 推导 `supplier_id` 和 `product_type`。
3. 默认分组键：`order_id + supplier_id + product_type + carrier_id`。
4. 用户手动指定物流公司时，更新 `FulfillmentGroup.carrier_id`，再创建 `Shipment`。

这样可以支持：

- 一个订单多个商品共用一个物流单；
- 一个订单拆成多个物流单；
- 同一订单中不同供应商、不同商品类型分开履约；
- 后续手动调整分组。

## 5. 后续功能如何落到数据结构

### 设计图下载

当前脚本可以先按 `orders/<订单号>/` 下载。系统化后建议：

- 从 `Asset.source_url` 读取 Drive 链接；
- 下载成功后写入 `Asset.local_path`；
- 失败时写入 `Asset.download_status = failed` 和 `error_message`；
- 下载动作可以按订单、按订单明细、按导入批次触发。

### 批量生成物流单号

建议入口是 `FulfillmentGroup`，不是 `Order`。

流程：

1. 用户选择物流公司 `Carrier`。
2. 系统筛选 `ready_to_ship` 的 `FulfillmentGroup`。
3. 调用物流公司 API。
4. 返回结果写入 `Shipment`。
5. 更新 `FulfillmentGroup.status = shipment_created`。

### 物流跟踪

建议定时任务按 `Shipment.tracking_no` 查询。

每次查询：

- 更新 `Shipment.status` 为最新归一状态；
- 追加 `ShipmentTrackingEvent`；
- 保存物流接口原始返回，便于排错。

### 生产进度

如果生产是按明细推进，事件挂到 `OrderItem`。

如果生产是按供应商或批次推进，事件挂到 `FulfillmentGroup`。

建议第一版以 `FulfillmentGroup` 为主，因为它更接近供应商生产和发货的单位。

## 6. 第一阶段建议落地范围

第一阶段不要直接做完整订单管理系统，建议先完成这些基础能力：

1. Excel 导入为结构化数据。
2. 订单、明细、设计图、物流单号能正确关联。
3. 保留原始导入行，便于对账。
4. 根据 `订单号` 下载图片，并把下载结果记录到 `Asset`。
5. 支持按 `单号` 或规则生成 `FulfillmentGroup`。

这套结构稳定后，后续新增物流公司、物流跟踪、生产进度、批量操作，主要是在现有实体上增加配置、状态和事件记录，不需要重做订单主结构。
