# rclone 本地配置说明

## 目标

先在本地验证 rclone 下载 Google Drive 的稳定性，不影响当前 Web 系统，也不使用服务器。rclone 配置文件、OAuth token、测试下载文件都只保留在本地，不能提交 Git。

## 安装

Linux 可以先检查是否已安装：

```bash
rclone version
```

如果未安装，按 rclone 官方安装方式安装。Ubuntu 常见方式：

```bash
sudo apt update
sudo apt install -y rclone
```

如果 apt 版本过旧，再使用 rclone 官方安装脚本或下载包。

## 配置 Google Drive Remote

执行：

```bash
rclone config
```

建议配置：

- remote 名称：`gdrive`
- storage 类型：`drive`
- scope：优先选择只读权限，例如 `drive.readonly`
- root folder：先留空，方便访问授权账号可见的 Drive 内容
- service account：本地 PoC 先不使用
- advanced config：先默认
- auto config：本地浏览器可用时选择 yes

如果只想测试某个 Google Drive 文件夹 ID，可以新建一个独立 remote，并在 advanced config 里设置 `root_folder_id` 为该文件夹 ID。rclone 官方文档说明 `root_folder_id` 可以让该 remote 以指定 Drive 文件夹作为起点。

建议后续正式测试时创建自己的 Google OAuth client ID / secret，不长期使用 rclone 默认 client。rclone 官方文档说明默认 client ID 被大量用户共享，建议自建 client ID 以降低共享配额影响。

配置完成后检查：

```bash
rclone listremotes
rclone lsd gdrive:
rclone ls gdrive: --max-depth 1
```

## 安全要求

- 不要把 rclone 配置文件提交 Git。
- 不要把客户文件、测试图片、下载结果提交 Git。
- 推荐把测试输出放在项目内 `rclone-test/` 或项目外 `/tmp/rclone-test/`。
- `.gitignore` 已忽略 `rclone-test/`、`rclone-results/`、`.rclone-test/` 和 `rclone.conf`。

## 常用排错

查看 remote 配置文件位置：

```bash
rclone config file
```

检查 remote 能否访问：

```bash
rclone about gdrive:
rclone lsd gdrive:
```

增加调试日志：

```bash
rclone ls gdrive: -vv --log-file ./rclone-results/debug.log
```

如果看到权限错误，先确认授权账号是否能在浏览器里无障碍访问对应文件夹或文件。

## 官方参考

- Google Drive backend: https://rclone.org/drive/
- Drive scopes、root folder ID、service account、copyid、limitations 和 client ID 均以官方文档为准。
