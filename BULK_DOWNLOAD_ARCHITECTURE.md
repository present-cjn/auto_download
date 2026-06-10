# 大批量下载架构升级计划

## 现状判断

当前 gdown 下载器适合作为 demo 和过渡方案，但不适合作为每天几百到上千 SKU 的正式下载通道。主要风险是 Google Drive 对公开链接、无登录态、连续请求的访问方式容易限流，表现为 `FileURLRetrievalError`、无法获取 public link 或短时间内大量失败。

第一阶段目标不是追求最快速度，而是避免系统持续请求 Google 后扩大失败面。

## 第一阶段：稳定现有 demo

- 使用分批下载，默认每次处理 10 或 20 个链接。
- 连续出现 Drive 限流或权限类失败时自动暂停本轮下载。
- 页面显示当前下载项、失败原因和耗时。
- 保留 gdown 作为临时下载器，用于验证业务流程、Excel 模板、ZIP 结构和用户操作习惯。

## 第二阶段：正式任务模型

- 引入独立的下载任务模型，支持 queued、running、paused、rate_limited、succeeded、failed 等状态。
- 增加 Drive resource 缓存层，同一个文件或文件夹只下载一次。
- 失败资源在一个冷却时间内不重复请求，后续 SKU 复用同一失败状态。
- 后台 worker 单独处理下载，页面只负责创建任务和查看进度。

## 第三阶段：Google Drive API

- 使用 Google OAuth 授权用户访问 Drive。
- 使用 Drive API 列文件和下载文件，替代 gdown 解析公开分享页。
- 对 403、429、权限不足、不可下载文件等 API 错误做明确分类。
- 按 Google 账号、批次和项目维度做限速与指数退避。
- 如果客户使用 Google Workspace，再评估服务账号和 domain-wide delegation。

## 存储和运维

- 短期继续使用服务器本地 `data/`。
- 中期增加自动清理、批次归档和数据库备份。
- 大规模稳定后再评估对象存储和 PostgreSQL。
