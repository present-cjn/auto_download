# rclone 服务器配置留存

## 目标

在无图形界面的 Ubuntu 服务器上配置 rclone Google Drive remote，用于验证 rclone 是否能替代 gdown 执行更稳定的 Drive 图片下载。

本流程只记录配置方法和验证命令，不记录 OAuth token、客户链接、客户文件名或下载结果。

## 已验证结论

本次服务器配置已验证通过：

```bash
rclone listremotes
rclone lsd gdrive:
rclone about gdrive:
```

成功标准：

- `rclone listremotes` 输出 `gdrive:`。
- `rclone lsd gdrive:` 能列出 Google Drive 根目录下的文件夹。
- `rclone about gdrive:` 能显示容量信息。

## 版本升级

Ubuntu apt 源里的 rclone 可能版本过旧。本次服务器曾出现旧版本：

```text
rclone v1.53.3-DEV
```

该版本可能导致 Google OAuth 报 `invalid_request`。建议使用 rclone 官方最新版。

手动升级步骤：

```bash
cd /tmp
curl -LO https://downloads.rclone.org/rclone-current-linux-amd64.zip
unzip rclone-current-linux-amd64.zip
cd rclone-*-linux-amd64
./rclone version
sudo cp rclone /usr/bin/rclone
sudo chmod 755 /usr/bin/rclone
hash -r
rclone version
```

验证目标：

```text
rclone v1.74.3
```

如果系统中同时存在 `/bin/rclone` 和 `/usr/bin/rclone`，确认二者指向的版本一致：

```bash
ls -l /bin/rclone /usr/bin/rclone
rclone version
```

## Google Drive Remote 配置

进入配置：

```bash
rclone config
```

如果没有 remote，选择 `n` 新建；如果已有 `gdrive`，选择 `e` 编辑。

推荐配置：

```text
name: gdrive
Storage: drive
client_id: 留空，PoC 阶段先使用 rclone 默认 client
client_secret: 留空
scope: drive.readonly
root_folder_id: 留空
service_account_file: 留空
Edit advanced config: n
Use web browser to automatically authenticate rclone with remote: y
Configure this as a Shared Drive (Team Drive): n
```

说明：

- `drive.readonly` 只允许读取元数据和下载文件，适合 PoC。
- PoC 阶段可先留空 `client_id` / `client_secret`；正式使用建议创建自己的 Google OAuth client，避免共享 rclone 默认 client 的配额和风险。
- 如果后续测试特定文件夹 ID，可以创建单独 remote 并设置 `root_folder_id`。

## 无图形服务器授权方式

服务器没有浏览器时，仍然可以选择：

```text
Use web browser to automatically authenticate rclone with remote: y
```

rclone 会启动本地回调服务并打印链接，例如：

```text
http://127.0.0.1:53682/auth?state=...
```

服务器会提示无法自动打开浏览器，这是正常的：

```text
Failed to open browser automatically (xdg-open not found)
```

在本地电脑新开终端，建立 SSH 端口转发。假设回调端口是 `53682`：

```bash
ssh -L 53682:127.0.0.1:53682 ubuntu@<server-ip>
```

如果 SSH 使用非默认端口：

```bash
ssh -p <ssh-port> -L 53682:127.0.0.1:53682 ubuntu@<server-ip>
```

保持 SSH 隧道窗口不要关闭，然后在本地浏览器打开服务器打印的链接：

```text
http://127.0.0.1:53682/auth?state=...
```

授权完成后，服务器上的 rclone 会收到 code，并继续写入 remote 配置。

## 配置保存和验证

rclone 显示配置摘要时，会包含 `token`。不要复制、提交或截图保存 token。

确认保存：

```text
Keep this "gdrive" remote? y
```

退出配置：

```text
q
```

验证：

```bash
rclone listremotes
rclone lsd gdrive:
rclone about gdrive:
```

## 安全要求

- `rclone.conf` 中包含 OAuth token，等同密码处理。
- 不要把 `rclone.conf` 提交 Git。
- 不要把 token 粘贴到文档、Issue、聊天记录或日志里。
- 如果 token 意外泄露，应到 Google 账号安全设置中撤销 rclone 授权，然后重新授权。
- 服务器上建议限制配置文件权限：

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

如果未来由 `auto-download` 服务用户运行 rclone，应把配置放到服务用户可读的位置，并限制权限：

```bash
sudo mkdir -p /home/auto-download/.config/rclone
sudo cp ~/.config/rclone/rclone.conf /home/auto-download/.config/rclone/rclone.conf
sudo chown -R auto-download:auto-download /home/auto-download/.config/rclone
sudo chmod 600 /home/auto-download/.config/rclone/rclone.conf
```

## 常见问题

### `invalid_request`

常见原因：rclone 版本过旧，或者无头授权方式使用不正确。

处理：

- 升级到 rclone 官方新版。
- 服务器无图形界面时，使用 SSH 隧道访问 `127.0.0.1:<callback-port>`。

### `xdg-open not found`

服务器没有桌面环境时正常。使用 SSH 隧道后，在本地浏览器打开 rclone 打印的链接。

### `gdrive:` 能列目录但无法下载客户文件

说明授权账号可访问自己的 Drive，但不一定有客户文件权限。需要确认：

- 客户文件是否共享给该 Google 账号。
- 链接是否来自共享盘或其他个人账号。
- 文件是否允许下载。

## 下一步 PoC

配置成功后，按照 `RCLONE_POC_PLAN.md` 测试：

- 一个 gdown 成功过的 folder。
- 一个 gdown 失败过的 folder。
- 一个 gdown 失败过的 mockup file。
- 10、50、100 个文件的连续下载稳定性。

