# 部署运维文档

## 1. 当前本地运行

创建虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

dry-run：

```bash
python download_orders.py --dry-run
```

正式下载：

```bash
python download_orders.py
```

## 2. 当前目录约定

- 输入 Excel 放在项目目录。
- 下载文件输出到 `orders/<订单号>/`。
- `.venv/`、`orders/`、缓存文件不提交 Git。

## 3. 未来云端部署候选

建议组件：

- Web 前端。
- 后端 API。
- PostgreSQL。
- 对象存储。
- 后台 worker。
- 定时任务。
- 日志和监控。

## 4. 运维要求

未来生产环境需要：

- 数据库定时备份。
- 对象存储备份策略。
- 后台任务失败重试。
- 错误日志和报警。
- API 密钥加密保存。
- HTTPS。
- 测试环境和生产环境分离。

## 5. 待确认问题

- 云服务器或云平台选择。
- 预计每天订单量和图片量。
- 图片保留周期。
- 是否需要公网访问供应商。
