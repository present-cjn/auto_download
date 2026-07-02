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
DRIVE_DOWNLOAD_BACKEND=rclone
DRIVE_DOWNLOAD_TIMEOUT_SECONDS=900
DRIVE_DOWNLOAD_DELAY_SECONDS=8
DRIVE_ITEM_RETRY_BACKOFF_SECONDS=30,90
RCLONE_DRIVE_REMOTES=gdrive
RCLONE_LOCAL_ENCODING=Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Del,Ctl,InvalidUtf8,Dot
MAX_IMAGE_FILE_SIZE_MB=100
MIN_FREE_DISK_SPACE_MB=1024
PRINTERVAL_CURL_BIN=curl
PRINTERVAL_CURL_TIMEOUT_SECONDS=30
PRINTERVAL_PLAYWRIGHT_ENABLED=1
PRINTERVAL_PLAYWRIGHT_HEADLESS=0
PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS=120
PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR=data/browser-profiles/printerval-main
```

首次启动时，如果数据库里还没有账号，系统会用这里的管理员账号初始化。
下载相关配置含义：

- `DRIVE_DOWNLOAD_BACKEND`：服务器备用下载后端，默认 `rclone`；临时回退旧逻辑时可设为 `gdown`。
- `DRIVE_DOWNLOAD_TIMEOUT_SECONDS`：单个 Drive 资源最长下载时间。
- `DRIVE_DOWNLOAD_DELAY_SECONDS`：批量下载时，每个链接之间等待的秒数，用于降低 Google 风控概率。
- `DRIVE_ITEM_RETRY_BACKOFF_SECONDS`：遇到网络、超时、Drive 限流或权限类错误时，单个链接额外重试前的等待秒数列表。
- `RCLONE_DRIVE_REMOTES`：rclone Google Drive remote 名称，支持逗号分隔的后备池，例如 `gdrive_a,gdrive_b`。
- `RCLONE_TRANSFERS` / `RCLONE_CHECKERS`：rclone 并发数，正式下载默认保守设置为 `1`。
- `RCLONE_DRIVE_PACER_MIN_SLEEP` / `RCLONE_DRIVE_PACER_BURST`：Drive API pacer 参数，默认 `500ms` 和 `5`。
- `RCLONE_LOCAL_ENCODING`：rclone 写入本地缓存时的文件名转义规则，用于兼容 NTFS/exFAT 等不支持 `|`、`:`、`?` 的文件系统。
- `MAX_IMAGE_FILE_SIZE_MB`：单张图片自动处理上限，默认 `100`；超过后提示人工判断或手动下载。
- `MIN_FREE_DISK_SPACE_MB`：开始下载前要求服务器缓存盘至少保留的空间，默认 `1024`。
- `PRINTERVAL_CURL_BIN`：系统 curl 兼容配置；Printerval 主路径优先使用 `curl_cffi` 和 `dl.printerval.com` 下载域。
- `PRINTERVAL_CURL_TIMEOUT_SECONDS`：Printerval 单张子图单次请求超时，默认 `30` 秒；Printerval 多图整体超时会按图片数量自动放宽。
- `PRINTERVAL_PLAYWRIGHT_ENABLED`：Printerval ZIP 接口和逐图下载都失败后，是否启用服务器浏览器兜底，默认启用。
- `PRINTERVAL_PLAYWRIGHT_HEADLESS`：Playwright Chromium 是否无头运行。Printerval 当前对 `HEADLESS=1` 容易触发 Cloudflare 403，推荐服务器也使用 `0`，并通过 Xvfb 提供虚拟显示。
- `PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS`：浏览器打开页面、点击下载和等待文件的最长时间，默认 `120` 秒。
- `PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR`：Printerval 持久化 Chromium profile 目录，Cloudflare 验证后的 cookie/session 会保存在这里。

服务器下载使用服务用户自己的 rclone 配置，不把 OAuth token、client secret 写入
Git 或数据库。建议先创建自建 Google OAuth client，然后以 `auto-download` 用户配置
一个或多个 remote：

```bash
sudo -u auto-download rclone config
sudo -u auto-download rclone listremotes
sudo -u auto-download rclone about gdrive:
```

如果配置多个 Google 账号，给每个账号创建独立 remote，并写入
`RCLONE_DRIVE_REMOTES=gdrive_a,gdrive_b,gdrive_c`。限流、网络或未知下载失败时会按
顺序尝试后备 remote；权限不足或文件不存在不会继续消耗其他账号。

Printerval 浏览器兜底依赖 Playwright Chromium。安装或升级依赖后执行：

```bash
sudo -u auto-download /opt/auto_download/.venv/bin/python -m playwright install chromium
```

如果服务器缺少 Chromium 系统依赖，按命令输出补齐系统包，或先设置
`PRINTERVAL_PLAYWRIGHT_ENABLED=0` 只使用 ZIP 接口和逐图下载。

建议用有头 Chromium 配合虚拟显示运行 Printerval 下载，而不是纯 headless。Ubuntu/Debian 可安装 Xvfb：

```bash
sudo apt-get update
sudo apt-get install -y xvfb
```

本地模拟无屏幕服务器时也使用同一方式：

```bash
scripts/run_printerval_local.sh
```

该脚本默认要求 `xvfb-run` 存在，并以 `PRINTERVAL_PLAYWRIGHT_HEADLESS=0` 在虚拟显示中启动服务。如果需要本地真实弹窗调试，可临时设置 `PRINTERVAL_USE_XVFB=0`。

如果下载日志出现 `printerval_challenge_required`，用同一个 profile 在服务器上刷新会话：

```bash
cd /opt/auto_download
sudo -u auto-download xvfb-run -a .venv/bin/python -m app.tools.printerval_session \
  --url 'https://printerval.com/.../folder-design?...&is_show_product_image=0' \
  --profile data/browser-profiles/printerval-main
```

URL 必须加引号，避免 `&` 被 shell 拆成后台任务。需要人工点击验证时，可临时接入服务器桌面/VNC 到同一个虚拟显示完成验证；本地调试也可以用 `PRINTERVAL_USE_XVFB=0 scripts/printerval_verify.sh '<url>'` 弹出真实浏览器窗口。验证通过后重试失败项即可。

## 5. systemd 服务

安装服务文件：

```bash
sudo cp /opt/auto_download/deploy/systemd/auto-download-xvfb.service /etc/systemd/system/auto-download.service
sudo systemctl daemon-reload
sudo systemctl enable auto-download
sudo systemctl start auto-download
```

如果确认不需要 Printerval 浏览器兜底，或只在已有桌面环境中运行，也可以改用
`deploy/systemd/auto-download.service`。Printerval 自动下载推荐使用上面的 Xvfb 版服务。

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

示例配置默认使用 `server_name dev.waysing.cn;`。如果部署到其他域名，把它改成真实域名；如果暂时没有域名，可以先填服务器公网 IP。

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/auto-download.conf /etc/nginx/sites-enabled/auto-download.conf
sudo nginx -t
sudo systemctl reload nginx
```

启用基础 HTTP 反代后，可以先验证：

```text
http://dev.waysing.cn
```

### 腾讯云/DNSPod 源站 HTTPS

当前 `waysing.cn` 由 DNSPod 管理。不要按 Cloudflare Flexible 配置；应在源站 Nginx 直接配置 HTTPS。

先在腾讯云控制台确认：

- DNSPod 里 `dev.waysing.cn` 的 A 记录指向当前服务器公网 IP。
- A 记录值不要填内网 IP、保留地址或带端口的值，例如不要填 `http://dev.waysing.cn:443`。
- 服务器安全组放行 TCP `80` 和 `443`。

申请并上传证书：

1. 在腾讯云 SSL 证书控制台为 `dev.waysing.cn` 申请免费证书。
2. 按 DNS 验证提示在 DNSPod 添加验证记录，等证书签发。
3. 下载 Nginx 格式证书，把证书和私钥上传到服务器：

```bash
sudo mkdir -p /etc/nginx/ssl
sudo cp dev.waysing.cn_bundle.crt /etc/nginx/ssl/dev.waysing.cn.crt
sudo cp dev.waysing.cn.key /etc/nginx/ssl/dev.waysing.cn.key
sudo chmod 600 /etc/nginx/ssl/dev.waysing.cn.key
```

Nginx 配置示例：

```nginx
server {
    listen 80;
    server_name dev.waysing.cn;

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name dev.waysing.cn;

    ssl_certificate /etc/nginx/ssl/dev.waysing.cn.crt;
    ssl_certificate_key /etc/nginx/ssl/dev.waysing.cn.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
```

更新 Nginx 后验证：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -i http://dev.waysing.cn/health
curl -i --max-time 15 https://dev.waysing.cn/health
```

配置源站 HTTPS 后，浏览器访问：

```text
https://dev.waysing.cn
```

期望结果：HTTP 会跳转到 HTTPS，HTTPS 返回 `{"status":"ok"}`。

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
