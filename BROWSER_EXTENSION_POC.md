# Browser Extension Download

This feature moves Google Drive image downloads from the server to the user's Chrome browser while keeping the existing Web app as the order and batch control plane.

## Goal

The Web app keeps Excel upload, parsing, batch review, account permissions, and download status. A Chrome extension pulls pending or failed `download_items`, downloads images locally into SKU folders, and reports success or failure back to the Web app.

## How To Run

Start the Web app from this branch:

```bash
cd /opt/auto_download
sudo -u auto-download .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

If testing from a laptop through SSH tunneling:

```bash
ssh -i cjnmck_hk.pem -L 8001:127.0.0.1:8001 ubuntu@<server-ip>
```

Open the Web app locally:

```text
http://localhost:8001
```

Upload an Excel file as a new batch. The batch page shows a primary plugin download panel. The original server-side download buttons are still available as backup actions.

## Chrome Extension

Load the unpacked extension from:

```text
browser-extension/
```

In Chrome:

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select the `browser-extension/` directory.
5. Log in to the Web app in the same Chrome profile.
6. Open a batch page and click `用插件下载待处理项`.

The popup remains available for status checks, pause, and manual fallback if page-to-extension messaging needs to be tested.

Downloaded files are saved under the browser Downloads directory:

```text
auto-download/batch-<batch_id>/<sku>/<source_type>-<download_item_id>-<original_name>
```

Design and mockup images are intentionally saved into the same SKU folder.

## Google OAuth

Drive folder downloads require Google Drive API access because the extension must list the files inside a folder before downloading them.

`browser-extension/manifest.json` intentionally contains a placeholder OAuth client ID:

```text
REPLACE_WITH_CHROME_EXTENSION_OAUTH_CLIENT_ID.apps.googleusercontent.com
```

For real folder downloads, create a Google Cloud OAuth client:

- Application type: Chrome Extension
- Extension ID: the ID shown in `chrome://extensions` for the loaded extension
- Scope: `https://www.googleapis.com/auth/drive.readonly`

If the OAuth consent screen is in Testing mode, add the tester's Google account under Test users. Otherwise Google returns `access_denied`.

A different unpacked extension ID requires a different Chrome Extension OAuth client. For a stable project-owned client ID, package/key the extension consistently or publish through the Chrome extension workflow.

## Current Behavior

- The batch page can send the current Web base URL and batch ID to the extension.
- The extension reads the Web session cookie and sends it as `X-App-Session`.
- The Web app only returns batches the current user can access.
- The extension processes pending and failed download items, but a single run will not retry the same failed item repeatedly.
- Drive folder links are listed via Drive API and downloaded image by image.
- Drive file links are downloaded via Drive API when OAuth is available, with a fallback download URL for public files.
- Downloaded file names keep the source type and `download_items.id` prefix to avoid collisions and support debugging.

## Known Limits

- This is an internal unpacked-extension workflow, not a Chrome Web Store package.
- The Web app records browser-reported paths and counts; it does not verify files on the user's laptop.
- Existing server-side download buttons and `gdown` logic are still present as backup actions.
- There is no extension auto-update or managed OAuth configuration yet.
- Failed/partial local downloads should be validated during formal feature development.

## Validation

Current checks before committing this POC:

```text
node --check browser-extension/service_worker.js
node --check browser-extension/content_script.js
node --check browser-extension/popup.js
python -m pytest
```
