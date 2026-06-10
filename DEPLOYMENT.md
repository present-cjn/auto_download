# 部署运维文档

## 1. 第一版部署目标

当前阶段建议使用一台香港 Ubuntu 服务器做轻量部署：

```text
Nginx -> 127.0.0.1:8000 Uvicorn -> SQLite + 本地 data/ 文件目录
```

暂不引入 Docker、PostgreSQL、Redis、Celery 或对象存储。目标是先稳定跑通：上传 Excel、服务器下载 Google Drive 图片、生成 ZIP、用户下载 ZIP。

推荐服务器：

- 地区：香港。
- 系统：Ubuntu 22.04 LTS。
- 配置：2 vCPU / 4GB RAM 起步。
- 硬盘：80GB 起步，建议 100GB+。
- 安全组：只开放 `22`、`80`、`443`。

## 2. 目录约定

推荐部署目录：

```text
/opt/auto_download
```

第一版数据保存在：

```text
/opt/auto_download/data/app.db
/opt/auto_download/data/uploads/<batch_id>/source.xlsx
/opt/auto_download/data/cache/<batch_id>/<drive_resource_id>/
/opt/auto_download/data/orders/<batch_id>/<SKU>/
/opt/auto_download/data/archives/*.zip
```

`data/` 是运行数据，不提交 Git。上线后至少需要备份 `data/app.db`。

## 3. 初始化服务器

安装系统依赖：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx sqlite3
```

创建服务用户：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin auto-download
```

拉取代码：

```bash
sudo git clone <YOUR_REPO_URL> /opt/auto_download
sudo chown -R auto-download:auto-download /opt/auto_download
```

创建虚拟环境并安装依赖：

```bash
sudo -u auto-download python3 -m venv /opt/auto_download/.venv
sudo -u auto-download /opt/auto_download/.venv/bin/python -m pip install -r /opt/auto_download/requirements.txt
```

## 4. 配置环境变量

复制示例环境文件：

```bash
sudo cp /opt/auto_download/deploy/env/auto-download.env.example /opt/auto_download/.env
sudo nano /opt/auto_download/.env
sudo chown auto-download:auto-download /opt/auto_download/.env
sudo chmod 600 /opt/auto_download/.env
```

至少修改：

```text
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
DRIVE_DOWNLOAD_TIMEOUT_SECONDS=900
```

首次启动时，如果数据库里还没有账号，系统会用这里的管理员账号初始化。

## 5. systemd 服务

安装服务文件：

```bash
sudo cp /opt/auto_download/deploy/systemd/auto-download.service /etc/systemd/system/auto-download.service
sudo systemctl daemon-reload
sudo systemctl enable auto-download
sudo systemctl start auto-download
```

检查状态：

```bash
sudo systemctl status auto-download
curl http://127.0.0.1:8000/health
```

查看实时日志：

```bash
sudo journalctl -u auto-download -f
```

重启服务：

```bash
sudo systemctl restart auto-download
```

## 6. Nginx 反向代理

复制 Nginx 示例配置：

```bash
sudo cp /opt/auto_download/deploy/nginx/auto-download.conf /etc/nginx/sites-available/auto-download.conf
sudo nano /etc/nginx/sites-available/auto-download.conf
```

把 `server_name example.com;` 改成真实域名；如果暂时没有域名，可以先填服务器公网 IP。

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/auto-download.conf /etc/nginx/sites-enabled/auto-download.conf
sudo nginx -t
sudo systemctl reload nginx
```

浏览器访问：

```text
http://<服务器IP或域名>
```

生产试用时建议配置 HTTPS。可以使用云厂商证书或 Certbot，确认域名解析完成后再启用。

## 7. 验证流程

部署完成后按这个顺序验证：

1. 打开 `/health`，确认返回 `{"status":"ok"}`。
2. 使用 `.env` 里的管理员账号登录。
3. 上传标准 Excel。
4. 确认批次解析成功，并生成 `data/orders/<batch_id>/`。
5. 点击开始下载。
6. 使用 `sudo journalctl -u auto-download -f` 观察下载日志。
7. 确认 `data/cache/<batch_id>/` 和 `data/orders/<batch_id>/<SKU>/` 有文件。
8. 下载订单 ZIP 和全部 ZIP，检查目录结构。

## 8. 日常运维

备份数据库：

```bash
sudo mkdir -p /opt/auto_download/backups
sudo sqlite3 /opt/auto_download/data/app.db ".backup '/opt/auto_download/backups/app-$(date +%F).db'"
```

查看磁盘：

```bash
df -h
du -sh /opt/auto_download/data/*
```

第一版可以人工清理旧批次文件；后续再做自动过期清理。

## 9. 更新代码

```bash
cd /opt/auto_download
sudo -u auto-download git pull
sudo -u auto-download /opt/auto_download/.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart auto-download
```

更新后检查：

```bash
sudo systemctl status auto-download
sudo journalctl -u auto-download -n 100 --no-pager
```

## 10. 暂不处理事项

以下内容等业务流程稳定后再升级：

- Google OAuth 登录态下载。
- PostgreSQL。
- Redis/Celery 后台队列。
- 对象存储。
- Docker 化部署。
- 自动清理和定时任务。
