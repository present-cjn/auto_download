# 导入导出规范

## 1. 设计原则

Excel 表格是外部工作流格式，系统内部使用稳定的数据结构。导入导出通过字段映射适配，避免外部表头变化直接影响核心数据模型。

```text
Excel 表格
  -> 字段映射
      -> 系统标准数据结构
          -> 导出模板
              -> 新 Excel 表格
```

## 2. 当前导入表：BT-6月1日订单总.xlsx

### 工作表 `12`

主订单明细表。每行对应一个订单明细。

| Excel 列 | 表头 | 系统字段 | 必填 | 更新策略 |
|---:|---|---|---|---|
| A | 订单号 | `Order.order_no` | 是 | upsert key，不允许空值覆盖 |
| B | 日期 | `Order.order_date` | 否 | 订单新建时写入，后续按规则覆盖 |
| C | Design Link | `Asset.source_url` | 是 | 同订单明细下去重追加 |
| D | sku | `OrderItem.sku` | 是 | 按导入行写入 |
| E | 尺码 | `OrderItem.size` | 否 | 按导入行写入 |
| F | Color | `OrderItem.color` | 否 | 按导入行写入 |
| G | 数量 | `OrderItem.quantity` | 是 | 按导入行写入 |
| H | 定制名 | `OrderItem.custom_name` | 否 | 按导入行写入 |
| I | 单号 | `Shipment.tracking_no` | 否 | 有值则写入；空值不清空旧值 |
| J | Shipping Fullname | `ShippingAddress.full_name` | 否 | 地址快照字段 |
| K | Address | `ShippingAddress.address1` | 否 | 地址快照字段 |
| L | City | `ShippingAddress.city` | 否 | 地址快照字段 |
| M | Province | `ShippingAddress.province` | 否 | 地址快照字段 |
| N | Zip | `ShippingAddress.postal_code` | 否 | 地址快照字段 |
| O | Country | `ShippingAddress.country_code` | 否 | 地址快照字段 |
| P | Phone | `ShippingAddress.phone` | 否 | 地址快照字段 |
| Q | Mail | `Order.customer_email` | 否 | 订单快照字段 |
| R | Customer Note | `Order.customer_note` | 否 | 空值不清空旧值 |
| S | Product ID | `OrderItem.product_external_id` | 否 | 按导入行写入 |
| T | Week | `Order.week_no` | 否 | 订单字段 |
| U | Các mục mẹ | `OrderItem.parent_item_name_local` | 否 | 保留原始字段 |
| V | Parent items | `OrderItem.parent_item_name` | 否 | 保留原始字段 |

### 工作表 `Sheet4`

订单号与转单号映射表。

| Excel 列 | 表头 | 系统字段 | 必填 | 更新策略 |
|---:|---|---|---|---|
| A | 原单号 | `Order.order_no` | 是 | 匹配已有订单 |
| B | 转单号 | `Shipment.tracking_no` | 是 | 有值则写入；空值不清空旧值 |

## 3. 当前导入规则

- 每次导入创建一个 `ImportBatch`。
- 每一行保存为 `ImportRow.raw_data`，用于排查和对账。
- `订单号` 是订单层面的业务键。
- 主表每一行创建或更新一条订单明细。
- `Design Link` 创建 `Asset`，同一订单同一链接不重复创建。
- `单号` 或 `Sheet4.转单号` 有值时创建或更新 `Shipment`。
- 空值默认不覆盖已有关键业务字段。

## 4. 当前下载导出

当前 CLI 工具不导出业务 Excel，只生成文件目录：

```text
orders/<订单号>/
```

轻量 Web 工具会生成批次目录和 ZIP：

```text
data/orders/<batch_id>/<订单号>/
data/archives/batch-<batch_id>.zip
```

后续建议增加：

- `download_report.csv`：订单号、链接、状态、文件数量、错误原因。
- `failed.csv`：失败订单号、链接、错误原因、可重试标记。

## 5. 后续导出模板占位

### 供应商生产表

待确认字段：

- 订单号
- SKU
- 尺码
- 颜色
- 数量
- 定制名
- 设计图路径
- 供应商
- 生产状态

### 物流创建表

待确认字段：

- 收件人
- 地址
- 城市
- 州/省
- 邮编
- 国家
- 电话
- 物流公司
- 商品数量
- 履约分组号

### 客户对账表

待确认字段：

- 原单号
- 转单号
- 物流状态
- 发货时间
- 签收时间
- 异常说明

## 6. 待确认问题

- 是否存在其他每日导入表。
- 现有导出表的完整模板。
- 地址字段变更时是否允许覆盖。
- 物流单号是否允许人工修改。
- 订单明细的稳定唯一键是否只能依赖 Excel 行号，还是可以从 SKU 后缀推导。
