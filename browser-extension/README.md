# Order Design Downloader Chrome Extension

This unpacked Chrome extension downloads Google Drive order images into local SKU folders and reports status back to the Web app.

## Load locally

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked and select this `browser-extension/` directory.
4. Log in to the Web tool in the same Chrome profile.
5. Open a batch detail page.
6. Click `用插件下载待处理项`.

The extension popup can still be used to view status, pause the current run, or manually enter a Web address and batch ID for debugging.

## Google Drive folders

Drive folder downloads require Google OAuth. Replace the placeholder `oauth2.client_id` in `manifest.json` with a Chrome Extension OAuth client ID that has Drive readonly scope enabled.

Single public Drive file links may work without OAuth. Private files and folders require the Chrome profile to authorize a Google account that can access the Drive resources.

For unpacked extensions, the Chrome extension ID shown in `chrome://extensions` must match the Chrome Extension OAuth client configured in Google Cloud.

## Output

Files are saved under the browser Downloads directory:

```text
auto-download/batch-<batch_id>/<sku>/<source_type>-<download_item_id>-<original_name>
```

Design and mockup images are intentionally saved into the same SKU folder.

## Web permissions

The first internal version uses broad `http://*/*` and `https://*/*` host permissions so the same unpacked extension can connect to the deployment domain and local test URLs. Narrow these permissions to the production domain before wider distribution.
