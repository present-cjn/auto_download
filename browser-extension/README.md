# Order Design Downloader Chrome Extension

This unpacked Chrome extension is a POC for local browser downloads.

## Load locally

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked and select this `browser-extension/` directory.
4. Log in to the Web tool in the same Chrome profile.
5. Open a batch detail page and copy the Web base URL and batch ID into the extension popup.

## Google Drive folders

Drive folder downloads require Google OAuth. Replace the placeholder `oauth2.client_id` in `manifest.json` with a Chrome Extension OAuth client ID that has Drive readonly scope enabled.

Single public Drive file links may work without OAuth. Private files and folders require the Chrome profile to authorize a Google account that can access the Drive resources.

## Output

Files are saved under the browser Downloads directory:

```text
auto-download/batch-<batch_id>/<sku>/<source_type>-<download_item_id>-<original_name>
```

Design and mockup images are intentionally saved into the same SKU folder.
