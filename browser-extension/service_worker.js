const SESSION_COOKIE = "app_session";
const DEFAULT_BASE_URL = "https://dev.waysing.cn";
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const IMAGE_MIME_PREFIX = "image/";
const DOWNLOAD_RETRY_DELAYS_MS = [3000, 8000, 15000];
const DOWNLOAD_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
const RETRIABLE_DOWNLOAD_ERRORS = new Set([
  "NETWORK_FAILED",
  "NETWORK_TIMEOUT",
  "SERVER_FAILED",
  "SERVER_UNREACHABLE",
  "TIMEOUT"
]);
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

function errorMessage(error) {
  return String(error && error.message ? error.message : error || "unknown error");
}

function isRetriableDownloadError(error) {
  const message = errorMessage(error);
  if (RETRIABLE_DOWNLOAD_ERRORS.has(message)) {
    return true;
  }
  return Array.from(RETRIABLE_DOWNLOAD_ERRORS).some((token) => message.includes(token));
}

function fileLabel(file) {
  return file.name || file.id || "drive-file";
}

function buildDownloadFailureDetail(task, file, filename, attempt, maxAttempts, error) {
  return [
    `sku=${task.sku}`,
    `source_type=${task.source_type}`,
    `download_item_id=${task.download_item_id}`,
    `drive_file_id=${file.id || ""}`,
    `drive_file_name=${file.name || ""}`,
    `target_path=${filename}`,
    `attempt=${attempt}/${maxAttempts}`,
    `chrome_error=${errorMessage(error)}`
  ].join(" ");
}

function enrichDownloadError(error, task, file, filename, attempt, maxAttempts) {
  const detail = buildDownloadFailureDetail(task, file, filename, attempt, maxAttempts, error);
  const enriched = new Error(detail);
  enriched.chromeError = errorMessage(error);
  enriched.driveFileId = file.id || "";
  enriched.driveFileName = file.name || "";
  enriched.targetPath = filename;
  enriched.attempt = attempt;
  enriched.maxAttempts = maxAttempts;
  return enriched;
}

async function setStatus(patch) {
  await chrome.storage.local.set(patch);
}

async function getConfig() {
  const config = await chrome.storage.local.get({
    baseUrl: DEFAULT_BASE_URL,
    batchId: "",
    processed: 0,
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
    const resolveDownload = () => {
      const resolver = activeDownloadResolvers.get(downloadId);
      if (!resolver) {
        return;
      }
      activeDownloadResolvers.delete(downloadId);
      resolver.resolve();
    };
    const rejectDownload = (error) => {
      const resolver = activeDownloadResolvers.get(downloadId);
      if (!resolver) {
        return;
      }
      activeDownloadResolvers.delete(downloadId);
      resolver.reject(error);
    };
    const timeoutId = setTimeout(() => {
      rejectDownload(new Error(`download timeout after ${Math.round(DOWNLOAD_WAIT_TIMEOUT_MS / 60000)} minutes`));
    }, DOWNLOAD_WAIT_TIMEOUT_MS);
    const settle = (callback) => (value) => {
      clearTimeout(timeoutId);
      callback(value);
    };
    activeDownloadResolvers.set(downloadId, {
      resolve: settle(resolve),
      reject: settle(reject)
    });
    chrome.downloads.search({ id: downloadId })
      .then((downloads) => {
        if (!activeDownloadResolvers.has(downloadId)) {
          return;
        }
        const current = downloads[0];
        if (current?.state === "complete") {
          resolveDownload();
        } else if (current?.state === "interrupted") {
          rejectDownload(new Error(current.error || "download interrupted"));
        }
      })
      .catch((error) => {
        rejectDownload(error);
      });
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

async function downloadWithRetry({ task, file, filename, downloadOptions }) {
  const maxAttempts = DOWNLOAD_RETRY_DELAYS_MS.length + 1;
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const label = fileLabel(file);
    await setStatus({
      state: "下载中",
      message: `${task.sku} · ${task.source_type} · ${label} (${attempt}/${maxAttempts})`
    });
    try {
      const downloadItem = await startBrowserDownload(downloadOptions);
      assertImageDownload(downloadItem, label);
      return downloadItem;
    } catch (error) {
      lastError = error;
      if (attempt < maxAttempts && isRetriableDownloadError(error)) {
        const waitMs = DOWNLOAD_RETRY_DELAYS_MS[attempt - 1];
        await setStatus({
          state: "重试中",
          message: `${task.sku} · ${label} 下载中断：${errorMessage(error)}，${Math.round(waitMs / 1000)} 秒后重试`
        });
        await sleep(waitMs);
        continue;
      }
      throw enrichDownloadError(error, task, file, filename, attempt, maxAttempts);
    }
  }
  throw enrichDownloadError(lastError, task, file, filename, maxAttempts, maxAttempts);
}

async function downloadDriveFileByApi(file, task, token) {
  const filename = joinDownloadPath(
    task.sku_folder,
    `${task.filename_prefix}${file.name || `${file.id}.jpg`}`
  );
  const downloadItem = await downloadWithRetry({
    task,
    file,
    filename,
    downloadOptions: {
      url: `https://www.googleapis.com/drive/v3/files/${file.id}?alt=media`,
      filename,
      headers: [{ name: "Authorization", value: `Bearer ${token}` }]
    }
  });
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
  const downloadItem = await downloadWithRetry({
    task,
    file: { id: task.resource_id || "", name: fallbackName },
    filename,
    downloadOptions: { url, filename }
  });
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
  for (const [index, file] of files.entries()) {
    if (paused) {
      throw new Error("用户暂停下载。 ");
    }
    try {
      downloaded.push(await downloadDriveFileByApi(file, task, token));
    } catch (error) {
      const detail = [
        errorMessage(error),
        `folder_file_index=${index + 1}/${files.length}`,
        `partial_image_count=${downloaded.length}`
      ].join(" ");
      const folderError = new Error(detail);
      folderError.partialFiles = downloaded;
      folderError.partialImageCount = downloaded.length;
      throw folderError;
    }
    await sleep(randomDelayMs());
  }
  return downloaded;
}

async function processTask(baseUrl, task) {
  await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/start`, {
    method: "POST",
    body: JSON.stringify({})
  });
  await setStatus({
    state: "下载中",
    message: `${task.sku} · ${task.source_type}`,
    currentSku: task.sku,
    currentSourceType: task.source_type
  });
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
    const failureReason = errorMessage(error);
    const partialFiles = Array.isArray(error.partialFiles) ? error.partialFiles : [];
    const partialImageCount = Number(error.partialImageCount || partialFiles.length || 0);
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/failure`, {
      method: "POST",
      body: JSON.stringify({
        error_code: "extension_download_failed",
        error_message: "浏览器插件下载失败。",
        error_detail: failureReason,
        files: partialFiles,
        partial_image_count: partialImageCount
      })
    });
    const state = await chrome.storage.local.get({ failed: 0 });
    await setStatus({
      failed: Number(state.failed || 0) + 1,
      message: `失败 ${task.sku}: ${failureReason.slice(0, 120)}`,
      lastFailureSku: task.sku,
      lastFailureReason: failureReason
    });
  } finally {
    await setStatus({ currentSku: "", currentSourceType: "" });
  }
}

async function incrementProcessedCount() {
  const state = await chrome.storage.local.get({ processed: 0 });
  await setStatus({ processed: Number(state.processed || 0) + 1 });
}

async function runQueue() {
  if (workerRunning) {
    return;
  }
  workerRunning = true;
  paused = false;
  const attemptedIds = new Set();
  await setStatus({
    running: true,
    state: "运行中",
    processed: 0,
    done: 0,
    failed: 0,
    currentSku: "",
    currentSourceType: "",
    lastFailureSku: "",
    lastFailureReason: "",
    message: "正在连接 Web..."
  });
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
        await incrementProcessedCount();
        await sleep(randomDelayMs());
      }
    }
  } catch (error) {
    await setStatus({
      state: "错误",
      currentSku: "",
      currentSourceType: "",
      lastFailureReason: errorMessage(error),
      message: String(error.message || error)
    });
  } finally {
    workerRunning = false;
    await setStatus({ running: false, currentSku: "", currentSourceType: "" });
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
