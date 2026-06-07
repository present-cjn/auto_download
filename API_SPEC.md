# API 设计文档

当前项目仍是本地 CLI 阶段，以下 API 为未来云端服务占位。

## 1. 导入

### `POST /imports/orders`

上传订单 Excel，创建导入批次。

请求：

- multipart 文件。
- 可选参数：表格类型、sheet 名称。

返回：

- `import_batch_id`
- 导入状态
- 成功/失败行数

## 2. 订单查询

### `GET /orders`

查询订单列表。

常用筛选：

- 订单号
- 导入批次
- 日期
- 下载状态
- 物流状态

### `GET /orders/{id}`

查询订单详情，包括明细、设计图、履约分组、物流单。

## 3. 设计图下载

### `POST /orders/{id}/download-assets`

为指定订单创建设计图下载任务。

### `POST /imports/{batch_id}/download-assets`

为导入批次创建批量下载任务。

## 4. 履约分组

### `POST /fulfillment-groups/generate`

根据规则生成履约分组。

### `PATCH /fulfillment-groups/{id}`

人工调整供应商、物流公司或状态。

## 5. 物流

### `POST /shipments/batch-create`

根据选定的履约分组和物流公司批量创建物流单。

### `GET /shipments/{id}/tracking`

查询物流轨迹。

## 6. 导出

### `POST /exports/{template_id}`

按导出模板生成 Excel。

## 7. 待确认问题

- API 鉴权方式。
- 批量任务返回同步结果还是异步任务 ID。
- 物流公司 API 的统一抽象。
- 下载图片是否按订单、批次还是链接级别触发。
