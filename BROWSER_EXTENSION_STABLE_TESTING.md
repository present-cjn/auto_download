# Browser Extension Internal Stable Testing

本文档记录当前“内部测试稳定版”的部署、插件安装、测试和排障流程。当前版本以浏览器插件下载为推荐测试链路，服务器 gdown 下载保留为备用。

## 版本定位

- 适用范围：内部测试，面向少量操作员。
- Web 分支：`feature/browser-extension-downloader`。
- 插件形态：Chrome unpacked extension，不是 CRX，也不是 Chrome Web Store 发布版。
- 默认 Web 地址：`http://dev.waysing.cn`。
- 当前不把浏览器下载文件回传服务器；文件保存到用户本机 Downloads。

当前 HTTPS 不是默认测试入口。`https://dev.waysing.cn` 的 443 连接曾超时，后续需要单独修 Cloudflare、证书、Nginx 或安全组。

## 安全规则

- 不提交真实 OAuth client ID。
- 不提交 Google token、cookie、rclone config。
- 不提交客户 Excel、下载结果、`data/`、`orders/` 或 Downloads 里的图片。
- `browser-extension/manifest.json` 在 Git 中必须保持占位符：

```text
REPLACE_WITH_CHROME_EXTENSION_OAUTH_CLIENT_ID.apps.googleusercontent.com
```

本机测试时可以临时替换为真实 Chrome Extension OAuth client ID，但提交前必须排除该文件或恢复占位符。

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
curl -i --max-time 15 "http://dev.waysing.cn/api/extension/batches/3/download-items?limit=1"
sudo journalctl -u auto-download -n 80 --no-pager
```

未登录访问插件 API 时，正常结果通常是 `303 See Other` 跳到登录页。如果是 `404`，说明服务端代码不是当前 feature 分支或服务未重启。

## 本机插件更新

在本机项目目录执行：

```bash
cd /media/hzbz/dataset/project/auto_download
git fetch origin
git switch feature/browser-extension-downloader
git pull
```

编辑本机插件 manifest：

```text
browser-extension/manifest.json
```

把占位符替换为 Google Cloud 中创建的 Chrome Extension OAuth client ID。这个改动只保留在本机，不提交。

Chrome 中操作：

1. 打开 `chrome://extensions`。
2. 开启 Developer mode。
3. 如果插件已加载，点击 Reload。
4. 如果插件目录变过，先 Remove，再 Load unpacked，选择 `browser-extension/`。
5. 刷新 Web 批次页。

## Google OAuth 配置

Drive 文件夹下载需要 Google Drive API 列出文件。Google Cloud 中创建 OAuth client：

- Application type: Chrome Extension
- Extension ID: `chrome://extensions` 里当前 unpacked 插件的 ID
- Scope: `https://www.googleapis.com/auth/drive.readonly`

如果 OAuth consent screen 是 Testing 模式，把测试 Google 账号加入 Test users。扩展 ID 变化后，需要创建或更新对应的 OAuth client。

## 标准测试流程

1. 用 Chrome 打开：

```text
http://dev.waysing.cn
```

2. 用同一个 Chrome Profile 登录 Web。
3. 上传一个小 Excel 批次，建议 10-20 个下载项。
4. 打开批次详情页。
5. 点击 `用插件下载待处理项`。
6. 首次下载 Drive folder 时完成 Google 授权。
7. 打开插件 popup 或 service worker console 观察状态。
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
- 正式输出只看 SKU 目录，不依赖 Downloads 根目录的孤立文件。

## 常见问题

### 插件显示 `Failed to fetch`

先确认 base URL 和登录 URL 一致。如果用 HTTP 测试：

```text
http://dev.waysing.cn
```

不要在插件里填 `https://dev.waysing.cn`。当前 HTTPS 443 未作为默认测试入口。

在插件 service worker console 检查 cookie：

```js
await chrome.cookies.get({ url: "http://dev.waysing.cn", name: "app_session" })
```

返回 `null` 时，重新用同一个 Chrome Profile 登录 `http://dev.waysing.cn`。

### 插件 API 返回 `404`

服务器未运行当前 feature 分支或未重启服务。检查：

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

- Google Cloud OAuth client 的 Extension ID 是否等于 `chrome://extensions` 里的插件 ID。
- `browser-extension/manifest.json` 是否替换成本机真实 client ID。
- 测试 Google 账号是否加入 OAuth consent screen 的 Test users。
- 修改 manifest 后是否 Reload 插件。

### `NETWORK_FAILED`

这是 Chrome downloads API 报告的下载中断。当前插件会对单文件自动重试 4 次。若最终仍失败，Web 失败详情会记录失败文件、attempt 和 Chrome 错误。

如果 Chrome 在 Downloads 根目录留下类似 Drive file ID 命名的孤立文件，可以删除或忽略。正式输出只认：

```text
Downloads/auto-download/batch-<batch_id>/<sku>/
```

### 页面按钮没有唤起插件

检查：

- 插件是否启用。
- 是否 Reload 了最新插件代码。
- Web 页面是否刷新过。
- 插件 manifest 的 `content_scripts` 是否仍允许当前域名。

也可以打开插件 popup，手动填写：

```text
http://dev.waysing.cn
```

再输入批次 ID 后点击开始。

## 后续生产化任务

- 修复 HTTPS/443，切换默认测试入口到 `https://dev.waysing.cn`。
- 固定插件 ID 或打包 CRX，减少 OAuth client 反复绑定。
- 收窄插件 `host_permissions` 到正式域名。
- 增加更清晰的 Web 插件状态面板。
- 评估 100+ 下载项连续运行和暂停恢复。
