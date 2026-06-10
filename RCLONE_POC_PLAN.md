# rclone 本地 PoC 计划

## 测试目标

验证 rclone 是否能比 gdown 更稳定地下载 Google Drive 图片资源。当前阶段只做命令行 PoC，不改 Web 系统、不提交 token 或测试文件。

当前服务器已完成 `gdrive` remote 授权，服务器配置过程记录在 `RCLONE_SERVER_SETUP.md`。后续可以优先在服务器执行本 PoC，因为最终下载服务也会部署在服务器上。

## 测试范围

- 一个 gdown 曾经成功的 Design folder。
- 一个 gdown 曾经失败的 Design folder。
- 一个 gdown 曾经失败的 Mockup file。
- 10、50、100 个文件的连续下载测试。

测试时只记录脱敏结果，不记录客户真实文件名、客户链接或 token。

## 准备工作

确认 rclone 可用：

```bash
rclone version
rclone listremotes
rclone lsd gdrive:
rclone about gdrive:
```

创建测试目录：

```bash
mkdir -p rclone-test rclone-results
```

建议所有测试命令加保守参数：

```bash
--transfers 1 --checkers 1 --drive-pacer-min-sleep 500ms --drive-pacer-burst 5
```

## 单文件夹测试

优先用 rclone 能识别的 Drive 路径。如果只有 Google Drive 文件夹链接，先从链接里取 folder ID，再创建一个临时 remote，把 `root_folder_id` 设置为该 folder ID。

路径命令模板：

```bash
rclone copy "gdrive:<folder-path>" "./rclone-test/folder-case-1" \
  --transfers 1 --checkers 1 \
  --drive-pacer-min-sleep 500ms --drive-pacer-burst 5 \
  -P -vv --log-file "./rclone-results/folder-case-1.log"
```

检查下载结果：

```bash
find ./rclone-test/folder-case-1 -maxdepth 3 -type f -printf '%p | %s bytes\n'
file ./rclone-test/folder-case-1/* 2>/dev/null
```

folder ID 测试建议：

1. 复制一个新 remote，例如 `gdrive_case1`。
2. 设置该 remote 的 `root_folder_id=<folder-id>`。
3. 执行：

```bash
rclone copy "gdrive_case1:" "./rclone-test/folder-case-1" \
  --transfers 1 --checkers 1 \
  --drive-pacer-min-sleep 500ms --drive-pacer-burst 5 \
  -P -vv --log-file "./rclone-results/folder-case-1.log"
```

## 单文件测试

如果文件能用 rclone path 定位，使用 `copyto`：

```bash
rclone copyto "gdrive:<file-path>" "./rclone-test/mockup-case-1/mockup.png" \
  --transfers 1 --checkers 1 \
  --drive-pacer-min-sleep 500ms --drive-pacer-burst 5 \
  -P -vv --log-file "./rclone-results/mockup-case-1.log"
```

检查结果：

```bash
find ./rclone-test/mockup-case-1 -maxdepth 2 -type f -printf '%p | %s bytes\n'
file ./rclone-test/mockup-case-1/* 2>/dev/null
```

如果只有 Google Drive file ID，使用 rclone Drive backend 的 `copyid`：

```bash
rclone backend copyid "gdrive:" "<file-id>" "./rclone-test/mockup-case-1/mockup.png" \
  --drive-pacer-min-sleep 500ms --drive-pacer-burst 5 \
  -vv --log-file "./rclone-results/mockup-case-1.log"
```

如果下载结果是 HTML 文档，而不是图片，说明当前方式没有拿到真实文件内容。

## 连续下载测试

先准备一个本地文本文件，只放脱敏后的 rclone 路径或测试路径，不提交 Git：

```text
gdrive:<path-1>
gdrive:<path-2>
gdrive:<path-3>
```

手动分三轮测试：

- 10 个文件或文件夹。
- 50 个文件或文件夹。
- 100 个文件或文件夹。

每轮记录：

| 测试轮次 | 数量 | 成功数 | 失败数 | 总耗时 | 主要错误 | 结论 |
| --- | ---: | ---: | ---: | --- | --- | --- |
| 10 项 |  |  |  |  |  |  |
| 50 项 |  |  |  |  |  |  |
| 100 项 |  |  |  |  |  |  |

## 通过标准

- 能下载 gdown 失败过的 folder 或 file。
- 连续 50-100 个文件没有频繁 rate limit、permission、cannot download。
- 失败日志能明确区分权限、限流、路径不存在或不可下载。
- 下载结果是图片文件，不是 Google HTML 页面。

## 不通过标准

- rclone 本地也频繁出现 rate limit 或 permission。
- 只能浏览器手动打开，rclone 无法 API 下载。
- 需要大量人工干预才能下载。
- 下载结果不是图片。

## 结论模板

测试日期：

测试账号类型：个人 Google / Google Workspace / 共享盘

结论：

- 是否建议进入服务器 PoC：
- 是否建议替换 gdown：
- 主要风险：
- 下一步：

## 官方参考

- Google Drive backend: https://rclone.org/drive/
- 通用命令和 flags: https://rclone.org/docs/
