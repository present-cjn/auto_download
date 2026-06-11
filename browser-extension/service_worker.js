const SESSION_COOKIE = "app_session";
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const IMAGE_MIME_PREFIX = "image/";
let activeDownloadResolvers = new Map();
let workerRunning = false;
let paused = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomDelayMs() {
  return (3 + Math.floor(Math.random() * 6)) * 1000;
}

function sanitizePathPart(value, fallback) {
  const cleaned = String(value || "")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .trim();
  return cleaned || fallback;
}

function joinDownloadPath(folder, filename) {
  return `${folder}/${sanitizePathPart(filename, "downloaded-image")}`;
}

async function setStatus(patch) {
  await chrome.storage.local.set(patch);
}

async function getConfig() {
  const config = await chrome.storage.local.get({
    baseUrl: "http://localhost:8000",
    batchId: "",
    done: 0,
    failed: 0
  });
  config.baseUrl = String(config.baseUrl || "").replace(/\/$/, "");
  config.batchId = String(config.batchId || "").trim();
  return config;
}

async function saveConfig(baseUrl, batchId) {
  const normalizedBaseUrl = String(baseUrl || "").replace(/\/$/, "");
  const normalizedBatchId = String(batchId || "").trim();
  if (!normalizedBaseUrl || !normalizedBatchId) {
    throw new Error("缺少 Web 地址或批次 ID。 ");
  }
  await chrome.storage.local.set({
    baseUrl: normalizedBaseUrl,
    batchId: normalizedBatchId
  });
  return { baseUrl: normalizedBaseUrl, batchId: normalizedBatchId };
}

async function getSessionToken(baseUrl) {
  const cookie = await chrome.cookies.get({ url: baseUrl, name: SESSION_COOKIE });
  if (!cookie || !cookie.value) {
    throw new Error("未找到 Web 登录会话，请先在浏览器中登录 Web。 ");
  }
  return cookie.value;
}

async function apiFetch(baseUrl, path, options = {}) {
  const token = await getSessionToken(baseUrl);
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-App-Session": token,
      ...(options.headers || {})
    }
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Web API ${response.status}: ${text.slice(0, 300)}`);
  }
  return response.json();
}

function hasConfiguredOAuthClient() {
  const clientId = chrome.runtime.getManifest().oauth2?.client_id || "";
  return clientId && !clientId.startsWith("REPLACE_WITH_");
}

async function getDriveToken(interactive = false) {
  if (!hasConfiguredOAuthClient()) {
    if (interactive) {
      throw new Error("Drive 文件夹需要先在 manifest.json 配置真实的 Chrome Extension OAuth client ID。 ");
    }
    return null;
  }
  try {
    const result = await chrome.identity.getAuthToken({ interactive, scopes: [DRIVE_SCOPE] });
    if (typeof result === "string") {
      return result;
    }
    if (result && typeof result.token === "string" && result.token) {
      return result.token;
    }
    throw new Error("Chrome 没有返回有效的 Drive OAuth token。 ");
  } catch (error) {
    if (interactive) {
      throw error;
    }
    return null;
  }
}

function googleDriveDownloadUrl(fileId) {
  return `https://drive.google.com/uc?export=download&id=${encodeURIComponent(fileId)}`;
}

function assertImageDownload(downloadItem, label) {
  const mime = String(downloadItem?.mime || "").toLowerCase();
  if (!mime || mime === "application/octet-stream") {
    return;
  }
  if (!mime.startsWith("image/")) {
    throw new Error(`${label} 下载结果不是图片，浏览器收到的类型是 ${mime}。这通常表示下载到了 Google Drive 预览页或权限提示页。`);
  }
}

async function driveFetchJson(url, token) {
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Drive API ${response.status}: ${text.slice(0, 300)}`);
  }
  return response.json();
}

async function listFolderImages(folderId, token) {
  const query = encodeURIComponent(`'${folderId}' in parents and trashed = false`);
  const fields = encodeURIComponent("nextPageToken,files(id,name,mimeType,size)");
  const files = [];
  let pageToken = "";
  do {
    const tokenParam = pageToken ? `&pageToken=${encodeURIComponent(pageToken)}` : "";
    const url = `https://www.googleapis.com/drive/v3/files?q=${query}&fields=${fields}&pageSize=1000&supportsAllDrives=true&includeItemsFromAllDrives=true${tokenParam}`;
    const data = await driveFetchJson(url, token);
    files.push(...(data.files || []));
    pageToken = data.nextPageToken || "";
  } while (pageToken);
  return files.filter((file) => String(file.mimeType || "").startsWith(IMAGE_MIME_PREFIX));
}

function waitForDownload(downloadId) {
  return new Promise((resolve, reject) => {
    activeDownloadResolvers.set(downloadId, { resolve, reject });
  });
}

chrome.downloads.onChanged.addListener((delta) => {
  if (!delta.state || !activeDownloadResolvers.has(delta.id)) {
    return;
  }
  const resolver = activeDownloadResolvers.get(delta.id);
  if (delta.state.current === "complete") {
    activeDownloadResolvers.delete(delta.id);
    resolver.resolve();
  } else if (delta.state.current === "interrupted") {
    activeDownloadResolvers.delete(delta.id);
    resolver.reject(new Error(delta.error?.current || "download interrupted"));
  }
});

async function startBrowserDownload(options) {
  const downloadId = await chrome.downloads.download({
    conflictAction: "uniquify",
    saveAs: false,
    ...options
  });
  await waitForDownload(downloadId);
  const downloads = await chrome.downloads.search({ id: downloadId });
  return downloads[0] || { id: downloadId };
}

async function downloadDriveFileByApi(file, task, token) {
  const filename = joinDownloadPath(
    task.sku_folder,
    `${task.filename_prefix}${file.name || `${file.id}.jpg`}`
  );
  const downloadItem = await startBrowserDownload({
    url: `https://www.googleapis.com/drive/v3/files/${file.id}?alt=media`,
    filename,
    headers: [{ name: "Authorization", value: `Bearer ${token}` }]
  });
  assertImageDownload(downloadItem, file.name || file.id);
  return {
    file_name: filename.split("/").pop(),
    local_path: filename,
    file_size: Number(file.size || downloadItem.fileSize || 0)
  };
}

async function downloadSingleFile(task, token) {
  if (token && task.resource_id) {
    const metadata = await driveFetchJson(
      `https://www.googleapis.com/drive/v3/files/${task.resource_id}?fields=id,name,mimeType,size`,
      token
    );
    return [await downloadDriveFileByApi(metadata, task, token)];
  }

  const fallbackName = `${task.filename_prefix}drive-file`;
  const filename = joinDownloadPath(task.sku_folder, fallbackName);
  const url = task.resource_id ? googleDriveDownloadUrl(task.resource_id) : task.url;
  const downloadItem = await startBrowserDownload({ url, filename });
  assertImageDownload(downloadItem, fallbackName);
  return [{
    file_name: fallbackName,
    local_path: filename,
    file_size: Number(downloadItem.fileSize || 0)
  }];
}

async function downloadFolder(task, token) {
  if (!token) {
    throw new Error("Drive 文件夹下载需要 Google OAuth 授权。请在插件授权 Google Drive 读取权限。");
  }
  const files = await listFolderImages(task.resource_id, token);
  if (!files.length) {
    throw new Error("Drive 文件夹中没有找到图片文件。 ");
  }
  const downloaded = [];
  for (const file of files) {
    if (paused) {
      throw new Error("用户暂停下载。 ");
    }
    downloaded.push(await downloadDriveFileByApi(file, task, token));
    await sleep(randomDelayMs());
  }
  return downloaded;
}

async function processTask(baseUrl, task) {
  await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/start`, {
    method: "POST",
    body: JSON.stringify({})
  });
  await setStatus({ state: "下载中", message: `${task.sku} · ${task.source_type}` });
  try {
    let token = await getDriveToken(false);
    if ((task.resource_kind === "folder" || task.resource_kind === "file") && !token && hasConfiguredOAuthClient()) {
      token = await getDriveToken(true);
    }
    let files;
    if (task.resource_kind === "folder") {
      files = await downloadFolder(task, token);
    } else if (task.resource_kind === "file") {
      files = await downloadSingleFile(task, token);
    } else {
      files = await downloadSingleFile(task, null);
    }
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/success`, {
      method: "POST",
      body: JSON.stringify({ files, image_count: files.length })
    });
    const state = await chrome.storage.local.get({ done: 0 });
    await setStatus({ done: Number(state.done || 0) + 1, message: `完成 ${task.sku}` });
  } catch (error) {
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/failure`, {
      method: "POST",
      body: JSON.stringify({
        error_code: "extension_download_failed",
        error_message: "浏览器插件下载失败。",
        error_detail: String(error && error.message ? error.message : error)
      })
    });
    const state = await chrome.storage.local.get({ failed: 0 });
    await setStatus({ failed: Number(state.failed || 0) + 1, message: `失败 ${task.sku}: ${String(error.message || error).slice(0, 120)}` });
  }
}

async function runQueue() {
  if (workerRunning) {
    return;
  }
  workerRunning = true;
  paused = false;
  const attemptedIds = new Set();
  await setStatus({ running: true, state: "运行中", done: 0, failed: 0, message: "正在连接 Web..." });
  try {
    while (!paused) {
      const config = await getConfig();
      if (!config.baseUrl || !config.batchId) {
        throw new Error("请填写 Web 地址和批次 ID。 ");
      }
      const payload = await apiFetch(
        config.baseUrl,
        `/api/extension/batches/${encodeURIComponent(config.batchId)}/download-items?limit=20`
      );
      const items = (payload.items || []).filter((item) => !attemptedIds.has(item.download_item_id));
      if (!items.length) {
        await setStatus({ state: "已完成", message: "没有待下载项。" });
        break;
      }
      for (const task of items) {
        if (paused) {
          break;
        }
        attemptedIds.add(task.download_item_id);
        await processTask(config.baseUrl, task);
        await sleep(randomDelayMs());
      }
    }
  } catch (error) {
    await setStatus({ state: "错误", message: String(error.message || error) });
  } finally {
    workerRunning = false;
    await setStatus({ running: false });
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "start") {
    runQueue();
    sendResponse({ ok: true });
    return true;
  }
  if (message.type === "configureAndStart") {
    saveConfig(message.baseUrl, message.batchId)
      .then((config) => {
        runQueue();
        sendResponse({ ok: true, config });
      })
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message ? error.message : error)
        });
      });
    return true;
  }
  if (message.type === "pause") {
    paused = true;
    setStatus({ running: false, state: "已暂停", message: "已暂停，当前下载完成后停止。" });
    sendResponse({ ok: true });
    return true;
  }
  return false;
});
