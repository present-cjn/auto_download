const SESSION_COOKIE = "app_session";
const DEFAULT_BASE_URL = "https://dev.waysing.cn";
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const IMAGE_MIME_PREFIX = "image/";
const DOWNLOAD_RETRY_DELAYS_MS = [3000, 8000, 15000];
const DOWNLOAD_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
const DOWNLOAD_POLL_INTERVAL_MS = 1000;
const RETRIABLE_DOWNLOAD_ERRORS = new Set([
  "NETWORK_FAILED",
  "NETWORK_TIMEOUT",
  "SERVER_FAILED",
  "SERVER_UNREACHABLE",
  "TIMEOUT"
]);
let activeDownloadResolvers = new Map();
let workerRunning = false;
let stopRequested = false;
let activeDownloadId = null;
let currentTask = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function interruptibleSleep(ms) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < ms) {
    if (stopRequested) {
      throw createStopError(currentTask);
    }
    await sleep(Math.min(250, ms - (Date.now() - startedAt)));
  }
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

function createStopError(task = currentTask, file = {}, partialFiles = []) {
  const parts = ["用户停止了插件下载。"];
  if (task) {
    parts.push(`sku=${task.sku}`);
    parts.push(`source_type=${task.source_type}`);
    parts.push(`download_item_id=${task.download_item_id}`);
  }
  if (file && (file.id || file.name)) {
    parts.push(`drive_file_id=${file.id || ""}`);
    parts.push(`drive_file_name=${file.name || ""}`);
  }
  parts.push(`partial_image_count=${partialFiles.length}`);
  const error = new Error(parts.join(" "));
  error.errorCode = "extension_stopped_by_user";
  error.errorMessage = "用户停止了插件下载。";
  error.userStopped = true;
  error.partialFiles = partialFiles;
  error.partialImageCount = partialFiles.length;
  return error;
}

function isStopError(error) {
  return Boolean(stopRequested || error?.userStopped || error?.errorCode === "extension_stopped_by_user");
}

function assertNotStopped(task = currentTask, file = {}, partialFiles = []) {
  if (stopRequested) {
    throw createStopError(task, file, partialFiles);
  }
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
  enriched.errorCode = error?.errorCode || "extension_download_failed";
  enriched.errorMessage = error?.errorMessage || "浏览器插件下载失败。";
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

function isImageMetadata(file) {
  return String(file?.mimeType || "").toLowerCase().startsWith(IMAGE_MIME_PREFIX);
}

function downloadLooksHtml(downloadItem) {
  const mime = String(downloadItem?.mime || "").toLowerCase();
  const filename = String(downloadItem?.filename || "").toLowerCase();
  return mime.includes("html") || filename.endsWith(".html") || filename.endsWith(".htm");
}

async function cleanupBadDownload(downloadItem) {
  if (!downloadItem?.id) {
    return;
  }
  try {
    await chrome.downloads.removeFile(downloadItem.id);
  } catch (error) {
    // The file may already be gone or Chrome may not expose a local path yet.
  }
  try {
    await chrome.downloads.erase({ id: downloadItem.id });
  } catch (error) {
    // Download history cleanup is best-effort only.
  }
}

async function assertImageDownload(downloadItem, file, label) {
  const mime = String(downloadItem?.mime || "").toLowerCase();
  const metadataIsImage = isImageMetadata(file);
  if ((!mime || mime === "application/octet-stream") && !downloadLooksHtml(downloadItem)) {
    return;
  }
  if (!mime.startsWith("image/")) {
    await cleanupBadDownload(downloadItem);
    const error = new Error(`${label} 下载结果不是图片，浏览器收到的类型是 ${mime || "unknown"}。这通常表示下载到了 Google Drive 预览页或权限提示页。失败项请回到 Web 批次页重试，不要点 Chrome 下载栏继续。`);
    error.errorCode = "extension_non_image_download";
    error.errorMessage = "插件下载到了非图片文件。";
    throw error;
  }
  if (!metadataIsImage && downloadLooksHtml(downloadItem)) {
    await cleanupBadDownload(downloadItem);
    const error = new Error(`${label} 下载到了 HTML 文件。这通常表示 Google Drive 返回了预览页、权限页或确认页。失败项请回到 Web 批次页重试，不要点 Chrome 下载栏继续。`);
    error.errorCode = "extension_non_image_download";
    error.errorMessage = "插件下载到了非图片文件。";
    throw error;
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
    const cleanup = () => {
      const resolver = activeDownloadResolvers.get(downloadId);
      if (resolver?.timeoutId) {
        clearTimeout(resolver.timeoutId);
      }
      if (resolver?.pollId) {
        clearInterval(resolver.pollId);
      }
      activeDownloadResolvers.delete(downloadId);
      if (activeDownloadId === downloadId) {
        activeDownloadId = null;
      }
    };
    const resolveDownload = () => {
      const resolver = activeDownloadResolvers.get(downloadId);
      if (!resolver) {
        return;
      }
      cleanup();
      resolver.resolve();
    };
    const rejectDownload = (error) => {
      const resolver = activeDownloadResolvers.get(downloadId);
      if (!resolver) {
        return;
      }
      cleanup();
      resolver.reject(error);
    };

    const inspectDownload = (current) => {
      if (stopRequested) {
        rejectDownload(createStopError(currentTask));
        return;
      }
      if (!current) {
        rejectDownload(new Error("Chrome 下载记录不存在，下载可能已被浏览器或用户取消。"));
        return;
      }
      if (current.state === "complete") {
        resolveDownload();
        return;
      }
      if (current.state === "interrupted") {
        rejectDownload(new Error(current.error || "download interrupted"));
        return;
      }
      if (current.paused) {
        rejectDownload(new Error("Chrome 下载已暂停或需要在下载栏继续。请不要点 Chrome 下载栏继续，请回到 Web 重试。"));
        return;
      }
      if (current.danger && current.danger !== "safe" && current.danger !== "accepted") {
        rejectDownload(new Error(`Chrome 阻止或标记了下载：${current.danger}。请回到 Web 重试。`));
        return;
      }
      if (current.exists === false) {
        rejectDownload(new Error("Chrome 下载文件不存在，可能已被删除或拦截。"));
      }
    };
    const pollDownload = () => {
      chrome.downloads.search({ id: downloadId })
        .then((downloads) => {
          if (!activeDownloadResolvers.has(downloadId)) {
            return;
          }
          inspectDownload(downloads[0]);
        })
        .catch((error) => {
          rejectDownload(error);
        });
    };
    const timeoutId = setTimeout(() => {
      rejectDownload(new Error(`download timeout after ${Math.round(DOWNLOAD_WAIT_TIMEOUT_MS / 60000)} minutes`));
    }, DOWNLOAD_WAIT_TIMEOUT_MS);
    const pollId = setInterval(pollDownload, DOWNLOAD_POLL_INTERVAL_MS);
    activeDownloadId = downloadId;
    activeDownloadResolvers.set(downloadId, {
      resolve,
      reject,
      timeoutId,
      pollId
    });
    pollDownload();
  });
}

chrome.downloads.onChanged.addListener((delta) => {
  if (!delta.state || !activeDownloadResolvers.has(delta.id)) {
    return;
  }
  const resolver = activeDownloadResolvers.get(delta.id);
  if (delta.state.current === "complete") {
    if (resolver?.timeoutId) {
      clearTimeout(resolver.timeoutId);
    }
    if (resolver?.pollId) {
      clearInterval(resolver.pollId);
    }
    activeDownloadResolvers.delete(delta.id);
    if (activeDownloadId === delta.id) {
      activeDownloadId = null;
    }
    resolver.resolve();
  } else if (delta.state.current === "interrupted") {
    if (resolver?.timeoutId) {
      clearTimeout(resolver.timeoutId);
    }
    if (resolver?.pollId) {
      clearInterval(resolver.pollId);
    }
    activeDownloadResolvers.delete(delta.id);
    if (activeDownloadId === delta.id) {
      activeDownloadId = null;
    }
    resolver.reject(new Error(delta.error?.current || "download interrupted"));
  }
});

async function startBrowserDownload(options) {
  assertNotStopped();
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
    assertNotStopped(task, file);
    const label = fileLabel(file);
    await setStatus({
      state: "下载中",
      message: `${task.sku} · ${task.source_type} · ${label} (${attempt}/${maxAttempts})`
    });
    try {
      const downloadItem = await startBrowserDownload(downloadOptions);
      await assertImageDownload(downloadItem, file, label);
      return downloadItem;
    } catch (error) {
      if (isStopError(error)) {
        throw createStopError(task, file);
      }
      lastError = error;
      if (attempt < maxAttempts && isRetriableDownloadError(error)) {
        const waitMs = DOWNLOAD_RETRY_DELAYS_MS[attempt - 1];
        await setStatus({
          state: "重试中",
          message: `${task.sku} · ${label} 下载中断：${errorMessage(error)}，${Math.round(waitMs / 1000)} 秒后重试`
        });
        await interruptibleSleep(waitMs);
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
    assertNotStopped(task, file, downloaded);
    try {
      downloaded.push(await downloadDriveFileByApi(file, task, token));
    } catch (error) {
      if (isStopError(error)) {
        throw createStopError(task, file, downloaded);
      }
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
    try {
      await interruptibleSleep(randomDelayMs());
    } catch (error) {
      if (isStopError(error)) {
        throw createStopError(task, file, downloaded);
      }
      throw error;
    }
  }
  return downloaded;
}

async function processTask(baseUrl, task) {
  currentTask = task;
  try {
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
    const errorCode = error?.errorCode || "extension_download_failed";
    const errorSummary = error?.errorMessage || "浏览器插件下载失败。";
    const partialFiles = Array.isArray(error.partialFiles) ? error.partialFiles : [];
    const partialImageCount = Number(error.partialImageCount || partialFiles.length || 0);
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/failure`, {
      method: "POST",
      body: JSON.stringify({
        error_code: errorCode,
        error_message: errorSummary,
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
    currentTask = null;
    await setStatus({ currentSku: "", currentSourceType: "" });
  }
}

async function incrementProcessedCount() {
  const state = await chrome.storage.local.get({ processed: 0 });
  await setStatus({ processed: Number(state.processed || 0) + 1 });
}

async function stopDownloads() {
  stopRequested = true;
  const downloadId = activeDownloadId;
  if (downloadId) {
    try {
      await chrome.downloads.cancel(downloadId);
    } catch (error) {
      // The download may have completed or disappeared between polling ticks.
    }
  }
  if (!workerRunning) {
    await setStatus({
      running: false,
      state: "已停止",
      message: "当前没有正在运行的插件下载。"
    });
    return;
  }
  await setStatus({
    running: true,
    state: "正在停止",
    message: "正在停止插件下载，当前下载会取消并回到 Web 重试。"
  });
}

async function runQueue() {
  if (workerRunning) {
    return;
  }
  workerRunning = true;
  stopRequested = false;
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
    while (!stopRequested) {
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
        if (stopRequested) {
          break;
        }
        attemptedIds.add(task.download_item_id);
        await processTask(config.baseUrl, task);
        await incrementProcessedCount();
        if (stopRequested) {
          break;
        }
        try {
          await interruptibleSleep(randomDelayMs());
        } catch (error) {
          if (isStopError(error)) {
            break;
          }
          throw error;
        }
      }
    }
    if (stopRequested) {
      await setStatus({ state: "已停止", message: "已停止插件下载，失败项请回到 Web 批次页重试。" });
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
    stopDownloads()
      .then(() => {
        sendResponse({ ok: true });
      })
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message ? error.message : error)
        });
      });
    return true;
  }
  if (message.type === "stop") {
    stopDownloads()
      .then(() => {
        sendResponse({ ok: true });
      })
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message ? error.message : error)
        });
      });
    return true;
  }
  return false;
});
