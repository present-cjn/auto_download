# 标准订单导入模板 v1

## 1. 设计原则

Excel 是外部协作格式，系统内部使用稳定的数据结构。后续不再以每个人自己的表头习惯为准，而是由系统定义标准导入模板，业务人员按模板填写。

```text
标准订单导入模板
  -> 字段映射
      -> 系统订单 / SKU / 图片 / 物流数据
          -> 下载图片、生成 ZIP、创建物流单、进度跟踪
              -> 后续导出表
```

标准模板 v1 的目标是先稳定支持下图，同时给后续物流、供应商生产和进度跟踪留下字段位置。

## 2. Excel 文件规则

- 文件格式：`.xlsx`。
- Web 上传要求一个 Excel 文件只有一个可见工作表，工作表名称不限。
- 第一行必须是标准表头。
- 从第二行开始，每行代表一条订单明细，也就是一个 SKU 明细。
- 同一个订单有多个 SKU 时，`订单号` 重复填写，多行分别记录不同 SKU。
- 表头名称和顺序应保持固定；暂时不用的字段可以留空，但不建议删除列或改表头。

## 3. 标准表头

| 顺序 | 表头 | 必填阶段 | 系统字段 | 用途 |
|---:|---|---|---|---|
| 1 | 订单号 | 当前必填 | `Order.order_no` | 订单层业务键；同一订单多 SKU 时重复填写。 |
| 2 | 日期 | 当前必填 | `Order.order_date` | 订单日期，用于批次核对和后续统计。 |
| 3 | SKU | 当前必填 | `OrderItem.sku` | 订单明细标识；用于 SKU 文件夹、生产和物流拆分。 |
| 4 | Design Link | 当前必填 | `Asset.source_url` | Google Drive 设计图链接，可填写文件夹或单文件图片链接。 |
| 5 | Mockup Link | 选填 | `Asset.source_url` | Google Drive mockup 图片链接，可填写文件夹或单文件图片链接；有值时也会下载。 |
| 6 | 尺码 | 建议填写 | `OrderItem.size` | 商品规格。 |
| 7 | 颜色 | 建议填写 | `OrderItem.color` | 商品规格。 |
| 8 | 数量 | 建议填写 | `OrderItem.quantity` | 该 SKU 数量。 |
| 9 | 定制名 | 建议填写 | `OrderItem.custom_name` | 定制内容或客户指定名称。 |
| 10 | 物流公司 | 后续启用 | `Shipment.carrier` | 后续创建物流单和物流跟踪使用。 |
| 11 | Shipping Fullname | 物流阶段必填 | `ShippingAddress.full_name` | 收件人姓名。 |
| 12 | Address | 物流阶段必填 | `ShippingAddress.address1` | 收件地址。 |
| 13 | City | 物流阶段必填 | `ShippingAddress.city` | 城市。 |
| 14 | Province | 物流阶段必填 | `ShippingAddress.province` | 州、省或地区。 |
| 15 | Zip | 物流阶段必填 | `ShippingAddress.postal_code` | 邮编。 |
| 16 | Country | 物流阶段必填 | `ShippingAddress.country_code` | 国家或国家代码。 |
| 17 | Phone | 物流阶段建议填写 | `ShippingAddress.phone` | 收件电话。 |
| 18 | Mail | 建议填写 | `Order.customer_email` | 客户邮箱。 |
| 19 | Customer Note | 建议填写 | `Order.customer_note` | 客户备注。 |
| 20 | Product ID | 建议填写 | `OrderItem.product_external_id` | 外部商品 ID，用于对账或追溯。 |

## 4. 字段分组

### 当前下图必填字段

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

这些字段足够支持当前功能：按 SKU 创建目录，下载 Google Drive 图片，并生成 ZIP。

### 当前建议保留字段

- `尺码`
- `颜色`
- `数量`
- `定制名`
- `Product ID`
- `Customer Note`
- `Phone`
- `Mail`

这些字段当前不一定影响下图，但对人工核对、后续物流创建、客户对账和异常排查有价值。

### 后续流程字段

- `物流公司`
- `物流单号`

这些字段先保留在模板里，后续启用供应商分配、批量创建物流单、物流跟踪时使用。

## 5. 导入规则

- 每次上传创建一个 `ImportBatch`，用于保留来源文件、上传人、导入统计和后续下载状态。
- `订单号` 是订单层业务键，但不是行唯一键。
- 每一行创建一条 `OrderItem`，保留 SKU、数量、规格、设计图链接等明细字段。
- 同一 `订单号` 下可以有多条 SKU 明细，系统必须完整保留，不能因为订单号重复而丢行。
- `Design Link` 创建 design 下载任务；`Mockup Link` 有值时创建 mockup 下载任务。
- Google Drive 链接支持文件夹格式 `/drive/folders/<id>` 和单文件格式 `/file/d/<id>/view`。
- 如果 `Design Link` 和 `Mockup Link` 下载出同名图片，系统用 `(1)` 后缀保留重复文件，避免漏下载。
- 同一批次内相同 Google Drive 资源可以复用缓存，避免重复请求 Google Drive。
- 空字段默认不应覆盖已有关键业务字段，尤其是物流单号、地址、备注等需要保留历史判断的字段。

## 6. 当前下载导出

轻量 Web 工具会生成批次目录和 ZIP：

```text
data/orders/<batch_id>/<SKU>/
data/archives/batch-<batch_id>.zip
data/archives/batch-<batch_id>-order-<order_id>-<订单号>.zip
```

批次 ZIP 内按 SKU 分文件夹存放图片。订单 ZIP 会包含该订单关联的 SKU 文件夹。

## 7. 历史样表兼容说明

当前早期样表 `BT-6月1日订单总.xlsx` 使用过以下表头和列顺序：

| 旧表头 | 标准模板 v1 字段 | 说明 |
|---|---|---|
| 订单号 | 订单号 | 保持一致。 |
| 日期 | 日期 | 保持一致。 |
| Design Link | Design Link | 保持一致。 |
| sku | SKU | 推荐统一为大写 `SKU`；当前代码兼容小写 `sku`。 |
| 尺码 | 尺码 | 保持一致。 |
| Color | 颜色 | 推荐统一为中文 `颜色`；当前代码兼容 `Color`。 |
| 数量 | 数量 | 保持一致。 |
| 定制名 | 定制名 | 保持一致。 |
| 单号 | 物流单号 | 后续物流单号字段；当前新模板可暂不填写。 |
| Shipping Fullname | Shipping Fullname | 保持一致。 |
| Address | Address | 保持一致。 |
| City | City | 保持一致。 |
| Province | Province | 保持一致。 |
| Zip | Zip | 保持一致。 |
| Country | Country | 保持一致。 |
| Phone | Phone | 保持一致。 |
| Mail | Mail | 保持一致。 |
| Customer Note | Customer Note | 保持一致。 |
| Product ID | Product ID | 保持一致。 |
| Week | 暂不纳入 v1 | 如后续需要周次统计再恢复。 |
| Các mục mẹ | 暂不纳入 v1 | 历史来源字段，暂不作为标准模板字段。 |
| Parent items | 暂不纳入 v1 | 历史来源字段，暂不作为标准模板字段。 |

当前代码已经支持标准模板 v1 的主表头，并兼容 `sku`/`SKU`、`Color`/`颜色`、`Postcode`/`Zip`。

## 8. 后续导出模板占位

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

- 原订单号
- 物流单号
- 物流状态
- 发货时间
- 签收时间
- 异常说明

## 9. 待确认问题

- 标准模板 v1 是否需要生成一个空白 Excel 模板供业务人员下载。
- 地址字段变更时是否允许覆盖已有订单地址。
- 物流单号是否允许业务员人工修改。
- 后续是否需要为 `SKU + 供应商 + 物流公司` 定义履约分组规则。
- 是否需要恢复 `Week` 作为标准字段。
