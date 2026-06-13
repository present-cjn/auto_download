# Browser Extension Internal Stable Testing

本文档记录当前“内部测试稳定版”的部署、插件安装、测试和排障流程。当前版本以浏览器插件下载为推荐测试链路，服务器 gdown 下载仅保留为管理员应急入口。

## 版本定位

- 适用范围：内部测试，面向少量操作员。
- Web 分支：`master`。
- 插件形态：Chrome unpacked extension，不是 CRX，也不是 Chrome Web Store 发布版。
- 固定插件 ID：`nodoinolmkijilpcgncdcglmplkleaie`。
- 默认 Web 地址：`https://dev.waysing.cn`。
- 当前不把浏览器下载文件回传服务器；文件保存到用户本机 Downloads。

当前 HTTPS 入口使用腾讯云/DNSPod + 源站 Nginx HTTPS。`dev.waysing.cn` 的 DNS A 记录需要指向服务器公网 IP，服务器安全组需要放行 TCP `80` 和 `443`。

## 当前内部稳定版记录

- 发布日期：2026-06-12。
- 默认入口：`https://dev.waysing.cn`。
- 服务器路径：`/opt/auto_download`。
- 发布分支：`master`。
- 合并策略：浏览器插件下载功能由 `feature/browser-extension-downloader` 合并到 `master`，后续物流订单接入从合并后的 `master` 新开 feature 分支。
- 主要内容：
  - 浏览器插件下载为推荐链路，服务器 gdown 下载仅保留为管理员应急入口。
  - 服务器备用下载入口已从普通操作员界面隐藏，仅管理员可展开应急使用。
  - 插件 manifest 已提交固定 public `key`，保持 unpacked/zip 安装时的扩展 ID 稳定。
  - 插件 manifest 已提交 Chrome Extension OAuth client ID，匹配固定扩展 ID。
  - 插件 popup 显示本轮已处理、成功、失败、当前 SKU 和上一次失败原因。
  - Chrome downloads 快速完成和长时间等待场景已加固，单文件下载等待最长 30 分钟。
  - 腾讯云/DNSPod + 源站 Nginx HTTPS 已作为默认部署方式。
- 已验证：
  - `http://dev.waysing.cn/health` 返回 `301` 并跳转到 HTTPS。
  - `https://dev.waysing.cn/health` 返回 `200 OK` 和 `{"status":"ok"}`。
  - 插件默认 Web 地址为 `https://dev.waysing.cn`。
- 已知限制：
  - 当前仍是 Chrome unpacked extension，不是 CRX 或 Chrome Web Store 发布版。
  - 固定扩展 ID 依赖 manifest 中的 public `key`；生成该 key 的 `.pem` 私钥必须由维护者离线保管，不能提交或分发。
  - 浏览器下载文件不回传服务器，正式输出以用户本机 Downloads 下的 SKU 目录为准。
  - 当前物流订单接入尚未开始，本阶段只收敛浏览器插件下载链路。

## 安全规则

- `browser-extension/manifest.json` 中的 public `key` 和 Chrome Extension OAuth client ID 可以提交，用于固定内部测试扩展 ID。
- 不提交生成固定扩展 ID 的 `.pem` 私钥；不要发给同事，也不要放进项目目录。
- 不提交 Google token、cookie、rclone config。
- 不提交客户 Excel、下载结果、`data/`、`orders/` 或 Downloads 里的图片。

## 服务器更新

在服务器执行：

```bash
cd /opt/auto_download
sudo -u auto-download git fetch origin
sudo -u auto-download git switch feature/browser-extension-downloader
sudo -u auto-download git pull
sudo -u auto-download /opt/auto_download/.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart auto-download
```

确认服务：

```bash
curl -i http://dev.waysing.cn/health
curl -i https://dev.waysing.cn/health
curl -i --max-time 15 "https://dev.waysing.cn/api/extension/batches/3/download-items?limit=1"
sudo journalctl -u auto-download -n 80 --no-pager
```

未登录访问插件 API 时，正常结果通常是 `303 See Other` 跳到登录页。如果是 `404`，说明服务端代码不是当前 `master` 或服务未重启。

## 本机插件更新

在本机项目目录执行：

```bash
cd /media/hzbz/dataset/project/auto_download
git fetch origin
git switch master
git pull
```

插件 manifest 已包含固定 public `key` 和 Chrome Extension OAuth client ID，不需要同事本机替换。

Chrome 中操作：

1. 打开 `chrome://extensions`。
2. 开启 Developer mode。
3. 如果插件已加载，确认 ID 为 `nodoinolmkijilpcgncdcglmplkleaie`，然后点击 Reload。
4. 如果插件目录变过，先 Remove，再 Load unpacked，选择 `browser-extension/`。
5. 刷新 Web 批次页。

## Google OAuth 配置

Drive 文件夹下载需要 Google Drive API 列出文件。Google Cloud 中创建 OAuth client：

- Application type: Chrome Extension
- Extension ID: `nodoinolmkijilpcgncdcglmplkleaie`
- Scope: `https://www.googleapis.com/auth/drive.readonly`

如果 OAuth consent screen 是 Testing 模式，把测试 Google 账号加入 Test users。只要 manifest 中的 public `key` 不变，unpacked/zip 安装后的扩展 ID 就会保持不变，OAuth client 不需要反复重建。

## 标准测试流程

1. 用 Chrome 打开：

```text
https://dev.waysing.cn
```

2. 用同一个 Chrome Profile 登录 Web。
3. 上传一个小 Excel 批次，建议 10-20 个下载项。
4. 打开批次详情页。
5. 点击 `用插件下载待处理项`。
6. 首次下载 Drive folder 时完成 Google 授权。
7. 打开插件 popup 或 service worker console 观察状态；如需中断，优先点击 Web 批次页或 popup 的停止按钮。
8. 检查本机 Downloads：

```text
Downloads/auto-download/batch-<batch_id>/<sku>/
```

9. 刷新 Web 批次页，确认下载项状态、失败原因和图片数量。

## 验收标准

内部测试稳定版的最低验收：

- 连续 10-20 个下载项可以完成或给出可读失败原因。
- Drive folder 和 Drive file 都能下载。
- 失败项可以再次点击插件下载进行重试。
- `NETWORK_FAILED` 等临时下载错误会自动重试。
- Web 失败详情能显示具体 SKU、来源、Drive file id、目标路径和 Chrome 错误。
- 点击停止后，当前下载项会标记失败，并可从 Web 批次页重试。
- 如果 Chrome 下载到 HTML 或非图片文件，插件会删除错误文件并标记失败。
- 正式输出只看 SKU 目录，不依赖 Downloads 根目录的孤立文件。

## 常见问题

### 插件显示 `Failed to fetch`

先确认 base URL 和登录 URL 一致。当前默认使用 HTTPS：

```text
https://dev.waysing.cn
```

不要在插件里填 `http://dev.waysing.cn:443`。这是 HTTP 明文请求打到 HTTPS 端口，常见报错是 `The plain HTTP request was sent to HTTPS port`。

如果 `https://dev.waysing.cn` 超时，先排查源站 HTTPS：

- DNSPod 中 `dev.waysing.cn` 的 A 记录是否指向服务器公网 IP。
- 腾讯云服务器安全组是否放行 TCP `443`。
- Nginx 是否存在 `listen 443 ssl;` 配置。
- 证书和私钥路径是否正确，`sudo nginx -t` 是否通过。
- `curl -i --max-time 15 https://dev.waysing.cn/health` 是否返回 `{"status":"ok"}`。

在插件 service worker console 检查 cookie：

```js
await chrome.cookies.get({ url: "https://dev.waysing.cn", name: "app_session" })
```

返回 `null` 时，重新用同一个 Chrome Profile 登录 `https://dev.waysing.cn`。

### 插件 API 返回 `404`

服务器未运行当前 `master` 或未重启服务。检查：

```bash
cd /opt/auto_download
sudo -u auto-download git branch --show-current
sudo -u auto-download git log --oneline -3
sudo systemctl restart auto-download
```

### 插件 API 返回 `403`

当前 Web 用户无权访问这个批次。确认用上传该批次的账号登录，或使用管理员账号。

### OAuth 授权失败

检查：

- Google Cloud OAuth client 的 Extension ID 是否等于 `nodoinolmkijilpcgncdcglmplkleaie`。
- `chrome://extensions` 里的插件 ID 是否等于 `nodoinolmkijilpcgncdcglmplkleaie`。
- `browser-extension/manifest.json` 里的 OAuth client ID 是否仍是当前 Google Cloud 中配置的 Chrome Extension client。
- 测试 Google 账号是否加入 OAuth consent screen 的 Test users。
- 修改 manifest 后是否 Reload 插件。

### `NETWORK_FAILED`

这是 Chrome downloads API 报告的下载中断。当前插件会对单文件自动重试 4 次。若最终仍失败，Web 失败详情会记录失败文件、attempt 和 Chrome 错误。

如果 Chrome 在 Downloads 根目录留下类似 Drive file ID 命名的孤立文件，可以删除或忽略。正式输出只认：

```text
Downloads/auto-download/batch-<batch_id>/<sku>/
```

### 下载到 HTML 或 Chrome 下载栏显示继续

不要点击 Chrome 下载栏里的继续。插件会把 HTML、预览页、权限页或需要人工继续的下载标记为失败，并提示回到 Web 批次页重试。

当前支持的 Drive 文件夹链接包括：

```text
https://drive.google.com/drive/folders/<folder_id>
https://drive.google.com/drive/u/0/folders/<folder_id>
```

如果失败详情里 `drive_file_id=` 为空，并且文件名类似 `design-<id>-drive-file`，通常说明服务器没有把链接解析成 Drive folder/file。先确认服务器已更新到包含该链接解析修复的代码，再重试失败项。

如果需要中断当前批次，点击 Web 批次页的 `停止插件下载` 或 popup 的 `停止`。当前下载项会标记失败，之后从 Web 批次页重试。

### 页面按钮没有唤起插件

检查：

- 插件是否启用。
- 是否 Reload 了最新插件代码。
- Web 页面是否刷新过。
- 插件 manifest 的 `content_scripts` 是否仍允许当前域名。

也可以打开插件 popup，手动填写：

```text
https://dev.waysing.cn
```

再输入批次 ID 后点击开始。

## 后续生产化任务

- 收窄插件 `host_permissions` 到正式域名。
- 评估 100+ 下载项连续运行和停止后重试。
- 物流订单接入从合并后的 `master` 新开 feature 分支开发。
