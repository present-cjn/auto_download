const SESSION_COOKIE = "app_session";
const DEFAULT_BASE_URL = "https://dev.waysing.cn";
const PROTOCOL_VERSION = 2;
const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const IMAGE_MIME_PREFIX = "image/";
const GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps.";
const GOOGLE_APPS_FOLDER_MIME = "application/vnd.google-apps.folder";
const DOWNLOAD_RETRY_DELAYS_MS = [3000, 8000, 15000];
const DOWNLOAD_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
const DOWNLOAD_POLL_INTERVAL_MS = 1000;
const DOWNLOAD_ITEM_HEARTBEAT_MS = 30 * 1000;
const DOWNLOAD_STALLED_TIMEOUT_MS = 5 * 60 * 1000;
const DOWNLOAD_PROGRESS_LOG_INTERVAL_MS = 15 * 1000;
const WEB_API_TIMEOUT_MS = 60 * 1000;
const DRIVE_API_TIMEOUT_MS = 120 * 1000;
const DRIVE_MEDIA_TIMEOUT_MS = 5 * 60 * 1000;
const EVENT_LOG_LIMIT = 200;
const RUNTIME_REQUEST_LOG_INTERVAL_MS = 15 * 1000;
const DOWNLOAD_PIPELINE_BLOB = "blob";
const DOWNLOAD_PIPELINE_HEADERS = "headers";
const RETRIABLE_DOWNLOAD_ERRORS = new Set([
  "NETWORK_FAILED",
  "NETWORK_TIMEOUT",
  "SERVER_FAILED",
  "SERVER_UNREACHABLE",
  "TIMEOUT",
  "extension_download_timeout",
  "extension_download_stalled",
  "extension_fetch_timeout"
]);
let activeDownloadResolvers = new Map();
let lastRuntimeRequestLogAt = new Map();
let pendingDownloadFilenames = new Map();
let pendingDownloadFilenameByUrl = new Map();
let workerRunning = false;
let stopRequested = false;
let stopInProgress = false;
let activeDownloadId = null;
let currentTask = null;
let driveResourceMetadataCache = new Map();

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

function taskEventContext(task = currentTask) {
  return {
    batchId: task?.batch_id || "",
    downloadItemId: task?.download_item_id || "",
    sku: task?.sku || "",
    sourceType: task?.source_type || ""
  };
}

async function recordEvent(event, { task = currentTask, message = "", detail = null } = {}) {
  const state = await chrome.storage.local.get({ eventLog: [] });
  const entry = {
    time: new Date().toISOString(),
    event,
    ...taskEventContext(task),
    message: String(message || "")
  };
  if (detail !== null && detail !== undefined) {
    entry.detail = detail;
  }
  const eventLog = Array.isArray(state.eventLog) ? state.eventLog : [];
  await chrome.storage.local.set({
    eventLog: [...eventLog, entry].slice(-EVENT_LOG_LIMIT)
  });
}

function elapsedMs(startedAt) {
  return Math.max(0, Date.now() - Number(startedAt || Date.now()));
}

function downloadDiagnosticDetail({
  file = {},
  filename = "",
  attempt = 0,
  maxAttempts = 0,
  pipeline = "",
  prepareStartedAt = 0,
  extra = {}
} = {}) {
  return {
    target_path: filename,
    attempt,
    maxAttempts,
    pipeline,
    prepareStartedAt: prepareStartedAt ? new Date(prepareStartedAt).toISOString() : "",
    elapsedMs: prepareStartedAt ? elapsedMs(prepareStartedAt) : 0,
    drive_file_id: file.id || "",
    drive_file_name: file.name || "",
    ...extra
  };
}

function rememberDownloadFilename(downloadId, filename) {
  if (!downloadId || !filename) {
    return;
  }
  pendingDownloadFilenames.set(downloadId, filename);
}

function rememberDownloadFilenameForUrl(url, filename) {
  if (!url || !filename) {
    return;
  }
  pendingDownloadFilenameByUrl.set(url, filename);
}

function forgetDownloadFilename(downloadId) {
  pendingDownloadFilenames.delete(downloadId);
}

function forgetDownloadFilenameForUrl(url) {
  pendingDownloadFilenameByUrl.delete(url);
}

function recordRuntimeRequest(event, detail = {}) {
  const now = Date.now();
  const shouldLimit = event === "runtime_status_request";
  const lastLoggedAt = Number(lastRuntimeRequestLogAt.get(event) || 0);
  if (shouldLimit && now - lastLoggedAt < RUNTIME_REQUEST_LOG_INTERVAL_MS) {
    return;
  }
  lastRuntimeRequestLogAt.set(event, now);
  recordEvent(event, {
    message: detail.message || "",
    detail: {
      ...detail,
      phase: workerRunning ? "running" : "idle",
      activeDownloadId,
      currentDownloadItemId: currentTask?.download_item_id || ""
    }
  }).catch(() => {});
}

async function setCurrentStage(stage, patch = {}, task = currentTask) {
  const now = new Date().toISOString();
  await setStatus({
    currentDownloadItemId: task?.download_item_id || "",
    currentSku: task?.sku || "",
    currentSourceType: task?.source_type || "",
    currentStage: stage,
    stageStartedAt: now,
    ...patch
  });
}

async function updateCurrentProgress(patch = {}) {
  await setStatus({
    ...patch,
    lastProgressAt: new Date().toISOString()
  });
}

async function currentDiagnosticDetail() {
  const state = await chrome.storage.local.get({
    currentStage: "",
    currentFileName: "",
    currentFileIndex: "",
    currentFileTotal: "",
    currentBytesReceived: "",
    currentFileSize: "",
    stageStartedAt: "",
    lastProgressAt: ""
  });
  return [
    `current_stage=${state.currentStage || ""}`,
    `current_file_name=${state.currentFileName || ""}`,
    `current_file_index=${state.currentFileIndex || ""}`,
    `current_file_total=${state.currentFileTotal || ""}`,
    `current_bytes_received=${state.currentBytesReceived || ""}`,
    `current_file_size=${state.currentFileSize || ""}`,
    `stage_started_at=${state.stageStartedAt || ""}`,
    `last_progress_at=${state.lastProgressAt || ""}`
  ].join(" ");
}

function clearCurrentDiagnosticPatch() {
  return {
    currentDownloadItemId: "",
    currentStage: "",
    currentFileName: "",
    currentFileIndex: "",
    currentFileTotal: "",
    currentBytesReceived: "",
    currentFileSize: "",
    stageStartedAt: "",
    lastProgressAt: ""
  };
}

function createRetriableError(message, errorCode, errorMessageText) {
  const error = new Error(message);
  error.errorCode = errorCode;
  error.errorMessage = errorMessageText;
  error.retriable = true;
  return error;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = WEB_API_TIMEOUT_MS, label = "fetch") {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw createRetriableError(
        `${label} timeout after ${Math.round(timeoutMs / 1000)} seconds`,
        "extension_fetch_timeout",
        `${label} 请求超时。`
      );
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

function withOperationTimeout(promise, timeoutMs, label) {
  let timeoutId = null;
  const timeoutPromise = new Promise((resolve, reject) => {
    timeoutId = setTimeout(() => {
      reject(createRetriableError(
        `${label} timeout after ${Math.round(timeoutMs / 1000)} seconds`,
        "extension_fetch_timeout",
        `${label} 请求超时。`
      ));
    }, timeoutMs);
  });
  return Promise.race([promise, timeoutPromise])
    .finally(() => clearTimeout(timeoutId));
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
  if (error?.retriable || RETRIABLE_DOWNLOAD_ERRORS.has(error?.errorCode)) {
    return true;
  }
  const message = errorMessage(error);
  if (RETRIABLE_DOWNLOAD_ERRORS.has(message)) {
    return true;
  }
  return Array.from(RETRIABLE_DOWNLOAD_ERRORS).some((token) => message.includes(token));
}

function fileLabel(file) {
  return file.name || file.id || "drive-file";
}

function directUrlFilename(url, fallback) {
  try {
    const parsed = new URL(String(url || ""));
    const pathPart = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).pop() || "");
    return sanitizePathPart(pathPart, fallback);
  } catch (error) {
    return sanitizePathPart(fallback, "downloaded-image");
  }
}

function driveResourceCacheKey(task) {
  if (!task.resource_kind || !task.resource_id || task.resource_kind === "url") {
    return "";
  }
  return `${task.resource_kind}:${task.resource_id}`;
}

function asFolderTask(task) {
  return {
    ...task,
    resource_kind: "folder"
  };
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
  await chrome.storage.local.set({
    ...patch,
    updatedAt: new Date().toISOString()
  });
}

function legacyStateToPhase(state) {
  const legacyState = String(state.state || "");
  if (state.stopping || stopInProgress) {
    return "stopping";
  }
  if (state.running || workerRunning) {
    return "running";
  }
  if (legacyState === "已停止") {
    return "stopped";
  }
  if (legacyState === "已完成") {
    return "completed";
  }
  if (legacyState === "错误") {
    return "failed";
  }
  if (!String(state.baseUrl || "").trim() || !String(state.batchId || "").trim()) {
    return "idle";
  }
  return "ready";
}

function batchRelation(runtimeBatchId, requestedBatchId) {
  const runtimeId = String(runtimeBatchId || "").trim();
  const requestedId = String(requestedBatchId || "").trim();
  if (!runtimeId) {
    return "none";
  }
  if (requestedId && runtimeId === requestedId) {
    return "same";
  }
  return requestedId ? "other" : "none";
}

function sourceTypeLabel(sourceType) {
  return sourceType === "mockup" ? "Mockup" : "Design";
}

function buildCurrentTask(state) {
  const task = currentTask || null;
  const sku = task?.sku || state.currentSku || "";
  const sourceType = task?.source_type || state.currentSourceType || "";
  if (!sku && !sourceType && !task?.download_item_id) {
    return null;
  }
  return {
    downloadItemId: task?.download_item_id || "",
    sku,
    sourceType,
    sourceTypeLabel: sourceType ? sourceTypeLabel(sourceType) : ""
  };
}

function buildLastError(state) {
  const message = state.lastFailureReason || "";
  if (!message) {
    return null;
  }
  return {
    code: state.lastFailureCode || "",
    sku: state.lastFailureSku || "",
    message
  };
}

async function getRuntimeState(compareBatchId = "") {
  const state = await chrome.storage.local.get({
    baseUrl: DEFAULT_BASE_URL,
    batchId: "",
    phase: "",
    running: false,
    stopping: false,
    processed: 0,
    done: 0,
    failed: 0,
    currentSku: "",
    currentSourceType: "",
    lastFailureSku: "",
    lastFailureCode: "",
    lastFailureReason: "",
    currentDownloadItemId: "",
    currentStage: "",
    currentFileName: "",
    currentFileIndex: "",
    currentFileTotal: "",
    currentBytesReceived: "",
    currentFileSize: "",
    stageStartedAt: "",
    lastProgressAt: "",
    message: "",
    state: "未连接",
    updatedAt: ""
  });
  const running = Boolean(workerRunning);
  const stopping = Boolean(stopInProgress);
  const normalizedState = {
    ...state,
    running,
    stopping,
    activeDownloadId
  };
  const storedPhase = String(state.phase || "");
  const canReuseStoredPhase = !running && !stopping && !["running", "stopping"].includes(storedPhase);
  const phase = canReuseStoredPhase && storedPhase
    ? storedPhase
    : legacyStateToPhase(normalizedState);
  normalizedState.phase = phase;
  normalizedState.batchRelation = batchRelation(state.batchId, compareBatchId);
  return {
    ...normalizedState,
    snapshot: {
      phase,
      baseUrl: state.baseUrl,
      batchId: String(state.batchId || ""),
      batchRelation: normalizedState.batchRelation,
      isRunning: running,
      isStopping: stopping,
      processed: Number(state.processed || 0),
      done: Number(state.done || 0),
      failed: Number(state.failed || 0),
      currentTask: buildCurrentTask(state),
      currentStage: state.currentStage || "",
      currentFileName: state.currentFileName || "",
      currentFileIndex: Number(state.currentFileIndex || 0),
      currentFileTotal: Number(state.currentFileTotal || 0),
      currentBytesReceived: Number(state.currentBytesReceived || 0),
      currentFileSize: Number(state.currentFileSize || 0),
      stageStartedAt: state.stageStartedAt || "",
      lastProgressAt: state.lastProgressAt || "",
      lastError: buildLastError(state),
      message: state.message || "",
      updatedAt: state.updatedAt || ""
    }
  };
}

function runtimeResponse(state, extra = {}) {
  return {
    ok: true,
    protocolVersion: PROTOCOL_VERSION,
    state,
    snapshot: state.snapshot,
    ...extra
  };
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
    batchId: normalizedBatchId,
    phase: "ready",
    updatedAt: new Date().toISOString()
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
  const response = await fetchWithTimeout(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-App-Session": token,
      ...(options.headers || {})
    }
  }, options.timeoutMs || WEB_API_TIMEOUT_MS, `Web API ${path}`);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Web API ${response.status}: ${text.slice(0, 300)}`);
  }
  return response.json();
}

function startDownloadItemHeartbeat(baseUrl, downloadItemId) {
  const heartbeat = async () => {
    try {
      const state = await chrome.storage.local.get({
        currentStage: "",
        currentFileIndex: "",
        currentFileTotal: "",
        currentFileName: "",
        lastProgressAt: ""
      });
      await apiFetch(baseUrl, `/api/extension/download-items/${downloadItemId}/heartbeat`, {
        method: "POST",
        body: JSON.stringify({
          currentStage: state.currentStage || "",
          currentFileIndex: state.currentFileIndex || "",
          currentFileTotal: state.currentFileTotal || "",
          currentFileName: state.currentFileName || "",
          lastProgressAt: state.lastProgressAt || ""
        })
      });
    } catch (error) {
      await setStatus({
        message: `当前项心跳更新失败：${errorMessage(error).slice(0, 120)}`
      });
    }
  };
  const timerId = setInterval(heartbeat, DOWNLOAD_ITEM_HEARTBEAT_MS);
  return () => clearInterval(timerId);
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

function googleDriveApiMediaUrl(fileId) {
  return `https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?alt=media&supportsAllDrives=true`;
}

async function getDownloadPipeline() {
  const state = await chrome.storage.local.get({ downloadPipeline: DOWNLOAD_PIPELINE_BLOB });
  return state.downloadPipeline === DOWNLOAD_PIPELINE_HEADERS
    ? DOWNLOAD_PIPELINE_HEADERS
    : DOWNLOAD_PIPELINE_BLOB;
}

function isImageMetadata(file) {
  return String(file?.mimeType || "").toLowerCase().startsWith(IMAGE_MIME_PREFIX);
}

function isDriveFolderMetadata(file) {
  return String(file?.mimeType || "").toLowerCase() === GOOGLE_APPS_FOLDER_MIME;
}

function isGoogleAppsMetadata(file) {
  return String(file?.mimeType || "").toLowerCase().startsWith(GOOGLE_APPS_MIME_PREFIX);
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
    const error = new Error(`${label} 下载结果不是图片，浏览器收到的类型是 ${mime || "unknown"}。这通常表示下载到了 Google Drive 预览页或权限提示页。失败项请在 Web 批次页重试。`);
    error.errorCode = "extension_non_image_download";
    error.errorMessage = "插件下载到了非图片文件。";
    throw error;
  }
  if (!metadataIsImage && downloadLooksHtml(downloadItem)) {
    await cleanupBadDownload(downloadItem);
    const error = new Error(`${label} 下载到了 HTML 文件。这通常表示 Google Drive 返回了预览页、权限页或确认页。失败项请在 Web 批次页重试。`);
    error.errorCode = "extension_non_image_download";
    error.errorMessage = "插件下载到了非图片文件。";
    throw error;
  }
}

async function driveFetchJson(url, token) {
  const response = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` }
  }, DRIVE_API_TIMEOUT_MS, "Drive API metadata/list");
  if (!response.ok) {
    const text = await response.text();
    throw new Error(formatDriveApiError(response.status, text));
  }
  return response.json();
}

function formatDriveApiError(status, text) {
  const detail = String(text || "").slice(0, 300);
  if (Number(status) === 404) {
    return [
      "Drive API 404: 找不到文件。",
      "常见原因：插件授权的 Google 账号没有权限、文件在“与我共享/共享云端硬盘”中但账号不匹配、文件已删除或链接失效。",
      detail
    ].join(" ");
  }
  return `Drive API ${status}: ${detail}`;
}

function nonImageDownloadError(label, mime) {
  const error = new Error(`${label} 下载结果不是图片，Drive API 返回的类型是 ${mime || "unknown"}。这通常表示权限页、预览页或非图片文件。失败项请回到 Web 批次页重试。`);
  error.errorCode = "extension_non_image_download";
  error.errorMessage = "插件下载到了非图片文件。";
  return error;
}

function googleAppsFileError(file) {
  const label = fileLabel(file);
  const mime = file?.mimeType || "unknown";
  const error = new Error(`${label} 不是可直接下载的图片文件。drive_file_id=${file?.id || ""} drive_file_name=${file?.name || ""} drive_file_mime_type=${mime}。这个链接指向 Google 文档/绘图/幻灯片/表格等在线文件，请改填原始图片文件链接，或包含图片文件的 Drive 文件夹链接。`);
  error.errorCode = "extension_google_apps_file";
  error.errorMessage = "链接指向 Google 在线文件，不是原始图片。";
  return error;
}

async function blobToDownloadUrl(blob) {
  if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
    const objectUrl = URL.createObjectURL(blob);
    return {
      url: objectUrl,
      cleanup: () => URL.revokeObjectURL(objectUrl)
    };
  }

  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunkSize = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  const mime = blob.type || "application/octet-stream";
  return {
    url: `data:${mime};base64,${btoa(binary)}`,
    cleanup: () => {}
  };
}

async function fetchDriveMediaAsDownloadUrl(file, token) {
  const label = fileLabel(file);
  const url = googleDriveApiMediaUrl(file.id);
  const response = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` }
  }, DRIVE_MEDIA_TIMEOUT_MS, "Drive API media");
  if (!response.ok) {
    const text = await response.text();
    throw new Error(formatDriveApiError(response.status, text));
  }

  const mime = String(response.headers.get("Content-Type") || "").split(";")[0].toLowerCase();
  const metadataIsImage = isImageMetadata(file);
  if (mime.includes("html") || (mime && mime !== "application/octet-stream" && !mime.startsWith("image/"))) {
    throw nonImageDownloadError(label, mime);
  }
  if (!metadataIsImage && !mime.startsWith("image/") && mime !== "application/octet-stream") {
    throw nonImageDownloadError(label, mime);
  }

  const blob = await withOperationTimeout(response.blob(), DRIVE_MEDIA_TIMEOUT_MS, "Drive API media blob");
  return blobToDownloadUrl(blob);
}

async function prepareDriveHeadersDownloadOptions(file, filename, token) {
  return {
    pipeline: DOWNLOAD_PIPELINE_HEADERS,
    downloadOptions: {
      url: googleDriveApiMediaUrl(file.id),
      filename,
      headers: [
        {
          name: "Authorization",
          value: `Bearer ${token}`
        }
      ]
    },
    cleanup: () => {}
  };
}

async function prepareDriveBlobDownloadOptions(file, filename, token) {
  const prepared = await fetchDriveMediaAsDownloadUrl(file, token);
  return {
    pipeline: DOWNLOAD_PIPELINE_BLOB,
    downloadOptions: { url: prepared.url, filename },
    cleanup: prepared.cleanup
  };
}

async function prepareDriveDownloadOptions(file, filename, token, diagnostic = {}) {
  const pipeline = await getDownloadPipeline();
  await recordEvent("download_pipeline_selected", {
    task: diagnostic.task || currentTask,
    message: pipeline,
    detail: downloadDiagnosticDetail({
      file,
      filename,
      attempt: diagnostic.attempt,
      maxAttempts: diagnostic.maxAttempts,
      pipeline,
      prepareStartedAt: diagnostic.prepareStartedAt
    })
  });
  if (pipeline === DOWNLOAD_PIPELINE_HEADERS) {
    return prepareDriveHeadersDownloadOptions(file, filename, token);
  }
  return prepareDriveBlobDownloadOptions(file, filename, token);
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

function waitForDownload(downloadId, downloadUrl = "") {
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
      forgetDownloadFilename(downloadId);
      if (resolver?.downloadUrl) {
        forgetDownloadFilenameForUrl(resolver.downloadUrl);
      }
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

    const cancelAndReject = async (error, event, detail = {}) => {
      try {
        await recordEvent(event, { message: errorMessage(error), detail });
      } catch (logError) {
        // Event logging should never hide the download failure.
      }
      try {
        await chrome.downloads.cancel(downloadId);
      } catch (cancelError) {
        // The download may already be complete, interrupted, or gone.
      }
      rejectDownload(error);
    };

    const inspectDownload = async (current) => {
      if (stopRequested) {
        rejectDownload(createStopError(currentTask));
        return;
      }
      if (!current) {
        rejectDownload(new Error("Chrome 下载记录不存在，下载可能已被浏览器或用户取消。"));
        return;
      }
      const resolver = activeDownloadResolvers.get(downloadId);
      const bytesReceived = Number(current.bytesReceived || 0);
      const fileSize = Number(current.fileSize || current.totalBytes || 0);
      if (resolver) {
        const now = Date.now();
        const progressChanged = bytesReceived !== resolver.lastBytesReceived || fileSize !== resolver.currentFileSize;
        if (progressChanged) {
          resolver.lastBytesReceived = bytesReceived;
          resolver.currentFileSize = fileSize;
          resolver.lastProgressAt = now;
          await updateCurrentProgress({
            currentBytesReceived: bytesReceived,
            currentFileSize: fileSize
          });
          if (!resolver.lastProgressLogAt || now - resolver.lastProgressLogAt >= DOWNLOAD_PROGRESS_LOG_INTERVAL_MS) {
            resolver.lastProgressLogAt = now;
            await recordEvent("download_progress", {
              detail: { downloadId, bytesReceived, fileSize, state: current.state || "" },
              message: fileSize ? `${bytesReceived}/${fileSize}` : String(bytesReceived)
            });
          }
        }
      }
      if (current.state === "complete") {
        await updateCurrentProgress({
          currentBytesReceived: bytesReceived,
          currentFileSize: fileSize
        });
        await recordEvent("download_complete", {
          detail: { downloadId, bytesReceived, fileSize, filename: current.filename || "" },
          message: current.filename || ""
        });
        resolveDownload();
        return;
      }
      if (current.state === "interrupted") {
        const error = new Error(current.error || "download interrupted");
        await recordEvent("download_interrupted", {
          detail: { downloadId, chromeError: current.error || "", bytesReceived, fileSize },
          message: errorMessage(error)
        });
        rejectDownload(error);
        return;
      }
      if (current.state === "in_progress" && resolver && Date.now() - resolver.lastProgressAt >= DOWNLOAD_STALLED_TIMEOUT_MS) {
        const stalledSeconds = Math.round((Date.now() - resolver.lastProgressAt) / 1000);
        const error = createRetriableError(
          `Chrome download stalled: bytesReceived unchanged for ${stalledSeconds} seconds`,
          "extension_download_stalled",
          "Chrome 下载长时间没有进展。"
        );
        await cancelAndReject(error, "stalled", {
          downloadId,
          bytesReceived,
          fileSize,
          stalledSeconds
        });
        return;
      }
      if (current.paused) {
        rejectDownload(new Error("浏览器下载已暂停或需要人工确认，本项已跳过。请在 Web 批次页重试。"));
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
          return inspectDownload(downloads[0]);
        })
        .catch((error) => {
          rejectDownload(error);
        });
    };
    const timeoutId = setTimeout(() => {
      const error = createRetriableError(
        `download timeout after ${Math.round(DOWNLOAD_WAIT_TIMEOUT_MS / 60000)} minutes`,
        "extension_download_timeout",
        "Chrome 下载超过 30 分钟。"
      );
      cancelAndReject(error, "timeout", { downloadId, timeoutMs: DOWNLOAD_WAIT_TIMEOUT_MS });
    }, DOWNLOAD_WAIT_TIMEOUT_MS);
    const pollId = setInterval(pollDownload, DOWNLOAD_POLL_INTERVAL_MS);
    activeDownloadId = downloadId;
    activeDownloadResolvers.set(downloadId, {
      resolve,
      reject,
      timeoutId,
      pollId,
      downloadUrl,
      lastBytesReceived: 0,
      currentFileSize: 0,
      lastProgressAt: Date.now(),
      lastProgressLogAt: 0
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
    forgetDownloadFilename(delta.id);
    if (activeDownloadId === delta.id) {
      activeDownloadId = null;
    }
    chrome.downloads.search({ id: delta.id })
      .then((downloads) => {
        const current = downloads[0] || {};
        forgetDownloadFilenameForUrl(current.url || "");
        forgetDownloadFilenameForUrl(current.finalUrl || "");
        return recordEvent("download_complete", {
          detail: {
            downloadId: delta.id,
            bytesReceived: Number(current.bytesReceived || 0),
            fileSize: Number(current.fileSize || current.totalBytes || 0),
            filename: current.filename || ""
          },
          message: current.filename || ""
        });
      })
      .catch(() => {})
      .finally(() => resolver.resolve());
  } else if (delta.state.current === "interrupted") {
    if (resolver?.timeoutId) {
      clearTimeout(resolver.timeoutId);
    }
    if (resolver?.pollId) {
      clearInterval(resolver.pollId);
    }
    activeDownloadResolvers.delete(delta.id);
    forgetDownloadFilename(delta.id);
    if (activeDownloadId === delta.id) {
      activeDownloadId = null;
    }
    const error = new Error(delta.error?.current || "download interrupted");
    chrome.downloads.search({ id: delta.id })
      .then((downloads) => {
        const current = downloads[0] || {};
        forgetDownloadFilenameForUrl(current.url || "");
        forgetDownloadFilenameForUrl(current.finalUrl || "");
      })
      .catch(() => {});
    recordEvent("download_interrupted", {
      detail: { downloadId: delta.id, chromeError: delta.error?.current || "" },
      message: errorMessage(error)
    })
      .catch(() => {})
      .finally(() => resolver.reject(error));
  }
});

chrome.downloads.onDeterminingFilename.addListener((downloadItem, suggest) => {
  const expectedFilename = pendingDownloadFilenames.get(downloadItem.id)
    || pendingDownloadFilenameByUrl.get(downloadItem.url || "")
    || pendingDownloadFilenameByUrl.get(downloadItem.finalUrl || "");
  if (!expectedFilename) {
    suggest();
    return;
  }
  rememberDownloadFilename(downloadItem.id, expectedFilename);
  forgetDownloadFilenameForUrl(downloadItem.url || "");
  forgetDownloadFilenameForUrl(downloadItem.finalUrl || "");
  recordEvent("download_filename_suggested", {
    detail: {
      downloadId: downloadItem.id,
      expectedFilename,
      chromeFilename: downloadItem.filename || "",
      url: downloadItem.url || "",
      finalUrl: downloadItem.finalUrl || ""
    },
    message: expectedFilename
  }).catch(() => {});
  suggest({
    filename: expectedFilename,
    conflictAction: "uniquify"
  });
});

async function startBrowserDownload(options, diagnostic = {}) {
  assertNotStopped();
  rememberDownloadFilenameForUrl(options.url || "", options.filename || diagnostic.filename || "");
  await recordEvent("download_call_start", {
    task: diagnostic.task || currentTask,
    message: options.filename || "",
    detail: downloadDiagnosticDetail({
      file: diagnostic.file,
      filename: options.filename || diagnostic.filename || "",
      attempt: diagnostic.attempt,
      maxAttempts: diagnostic.maxAttempts,
      pipeline: diagnostic.pipeline,
      prepareStartedAt: diagnostic.prepareStartedAt,
      extra: {
        url: options.url || "",
        hasHeaders: Array.isArray(options.headers) && options.headers.length > 0
      }
    })
  });
  const downloadId = await chrome.downloads.download({
    conflictAction: "uniquify",
    saveAs: false,
    ...options
  });
  rememberDownloadFilename(downloadId, options.filename || diagnostic.filename || "");
  await recordEvent("download_call_done", {
    task: diagnostic.task || currentTask,
    message: String(downloadId),
    detail: downloadDiagnosticDetail({
      file: diagnostic.file,
      filename: options.filename || diagnostic.filename || "",
      attempt: diagnostic.attempt,
      maxAttempts: diagnostic.maxAttempts,
      pipeline: diagnostic.pipeline,
      prepareStartedAt: diagnostic.prepareStartedAt,
      extra: { downloadId }
    })
  });
  await setCurrentStage("download_created", {
    currentBytesReceived: 0,
    currentFileSize: 0
  });
  await recordEvent("download_created", {
    task: diagnostic.task || currentTask,
    detail: downloadDiagnosticDetail({
      file: diagnostic.file,
      filename: options.filename || diagnostic.filename || "",
      attempt: diagnostic.attempt,
      maxAttempts: diagnostic.maxAttempts,
      pipeline: diagnostic.pipeline,
      prepareStartedAt: diagnostic.prepareStartedAt,
      extra: { downloadId, url: options.url || "" }
    }),
    message: options.filename || ""
  });
  await waitForDownload(downloadId, options.url || "");
  const downloads = await chrome.downloads.search({ id: downloadId });
  return downloads[0] || { id: downloadId };
}

async function downloadWithRetry({ task, file, filename, downloadOptions, prepareDownloadOptions }) {
  const maxAttempts = DOWNLOAD_RETRY_DELAYS_MS.length + 1;
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    assertNotStopped(task, file);
    const label = fileLabel(file);
    await setStatus({
      state: "下载中",
      message: `${task.sku} · ${task.source_type} · ${label} (${attempt}/${maxAttempts})`
    });
    let currentDownloadOptions = downloadOptions;
    let cleanupDownloadOptions = null;
    try {
      const prepareStartedAt = Date.now();
      await setCurrentStage("download_prepare_start", {
        currentFileName: label,
        currentBytesReceived: 0,
        currentFileSize: Number(file.size || 0)
      }, task);
      await recordEvent("download_prepare_start", {
        task,
        message: label,
        detail: downloadDiagnosticDetail({
          file,
          filename,
          attempt,
          maxAttempts,
          prepareStartedAt
        })
      });
      let pipeline = downloadOptions ? "direct" : "";
      if (prepareDownloadOptions) {
        const prepared = await prepareDownloadOptions({
          attempt,
          maxAttempts,
          prepareStartedAt
        });
        cleanupDownloadOptions = prepared.cleanup || null;
        currentDownloadOptions = prepared.downloadOptions;
        pipeline = prepared.pipeline || pipeline;
      }
      await recordEvent("download_options_ready", {
        task,
        message: label,
        detail: downloadDiagnosticDetail({
          file,
          filename,
          attempt,
          maxAttempts,
          pipeline,
          prepareStartedAt,
          extra: {
            url: currentDownloadOptions?.url || "",
            hasHeaders: Array.isArray(currentDownloadOptions?.headers) && currentDownloadOptions.headers.length > 0
          }
        })
      });
      await recordEvent("download_prepare_done", {
        task,
        message: label,
        detail: downloadDiagnosticDetail({
          file,
          filename,
          attempt,
          maxAttempts,
          pipeline,
          prepareStartedAt
        })
      });
      const downloadItem = await startBrowserDownload(currentDownloadOptions, {
        task,
        file,
        filename,
        attempt,
        maxAttempts,
        pipeline,
        prepareStartedAt
      });
      await assertImageDownload(downloadItem, file, label);
      return downloadItem;
    } catch (error) {
      if (isStopError(error)) {
        throw createStopError(task, file);
      }
      lastError = error;
      if (attempt < maxAttempts && isRetriableDownloadError(error)) {
        const waitMs = DOWNLOAD_RETRY_DELAYS_MS[attempt - 1];
        await recordEvent("retry_wait", {
          task,
          message: `${label}: ${errorMessage(error)}`,
          detail: { waitMs, attempt, maxAttempts, errorCode: error?.errorCode || "" }
        });
        await setCurrentStage("retry_wait", {
          currentFileName: label
        }, task);
        await setStatus({
          state: "重试中",
          message: `${task.sku} · ${label} 下载中断：${errorMessage(error)}，${Math.round(waitMs / 1000)} 秒后重试`
        });
        await interruptibleSleep(waitMs);
        continue;
      }
      throw enrichDownloadError(error, task, file, filename, attempt, maxAttempts);
    } finally {
      if (cleanupDownloadOptions) {
        cleanupDownloadOptions();
      }
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
    prepareDownloadOptions: async (diagnostic) => {
      return prepareDriveDownloadOptions(file, filename, token, {
        task,
        ...diagnostic
      });
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
    const cacheKey = driveResourceCacheKey(task);
    let metadata = cacheKey ? driveResourceMetadataCache.get(cacheKey) : null;
    if (!metadata) {
      await setCurrentStage("drive_metadata_start", {
        currentFileName: "",
        currentFileIndex: 1,
        currentFileTotal: 1,
        currentBytesReceived: 0,
        currentFileSize: 0
      }, task);
      metadata = await driveFetchJson(
        `https://www.googleapis.com/drive/v3/files/${task.resource_id}?fields=id,name,mimeType,size,webViewLink,exportLinks&supportsAllDrives=true`,
        token
      );
      if (cacheKey) {
        driveResourceMetadataCache.set(cacheKey, metadata);
      }
    }
    if (isDriveFolderMetadata(metadata)) {
      return downloadFolder(asFolderTask(task), token);
    }
    if (isGoogleAppsMetadata(metadata)) {
      throw googleAppsFileError(metadata);
    }
    await setStatus({
      currentFileName: metadata.name || metadata.id || "",
      currentFileIndex: 1,
      currentFileTotal: 1,
      currentFileSize: Number(metadata.size || 0)
    });
    return [await downloadDriveFileByApi(metadata, task, token)];
  }

  const fallbackName = task.resource_id
    ? `${task.filename_prefix}drive-file`
    : `${task.filename_prefix}${directUrlFilename(task.url, "image")}`;
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
  const cacheKey = driveResourceCacheKey(task);
  let files = cacheKey ? driveResourceMetadataCache.get(cacheKey) : null;
  if (!files) {
    await setCurrentStage("folder_list_start", {
      currentFileName: "",
      currentFileIndex: 0,
      currentFileTotal: 0,
      currentBytesReceived: 0,
      currentFileSize: 0
    }, task);
    await recordEvent("folder_list_start", {
      task,
      message: task.resource_id || ""
    });
    files = await listFolderImages(task.resource_id, token);
    if (cacheKey) {
      driveResourceMetadataCache.set(cacheKey, files);
    }
  }
  await setCurrentStage("folder_list_done", {
    currentFileName: "",
    currentFileIndex: 0,
    currentFileTotal: files.length,
    currentBytesReceived: 0,
    currentFileSize: 0
  }, task);
  await recordEvent("folder_list_done", {
    task,
    message: `${files.length} image(s)`,
    detail: { total: files.length }
  });
  if (!files.length) {
    throw new Error("Drive 文件夹中没有找到图片文件。 ");
  }
  const downloaded = [];
  for (const [index, file] of files.entries()) {
    assertNotStopped(task, file, downloaded);
    try {
      await setCurrentStage("folder_file_start", {
        currentFileName: file.name || file.id || "",
        currentFileIndex: index + 1,
        currentFileTotal: files.length,
        currentBytesReceived: 0,
        currentFileSize: Number(file.size || 0)
      }, task);
      await recordEvent("folder_file_start", {
        task,
        message: file.name || file.id || "",
        detail: {
          index: index + 1,
          total: files.length,
          drive_file_id: file.id || "",
          drive_file_name: file.name || ""
        }
      });
      downloaded.push(await downloadDriveFileByApi(file, task, token));
      await setStatus({
        currentFileIndex: index + 1,
        currentFileTotal: files.length,
        currentFileName: file.name || file.id || ""
      });
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
      folderError.errorCode = error?.errorCode || "extension_download_failed";
      folderError.errorMessage = error?.errorMessage || "浏览器插件下载失败。";
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
  let stopHeartbeat = null;
  try {
    await setCurrentStage("task_start", {
      currentFileName: "",
      currentFileIndex: 0,
      currentFileTotal: 0,
      currentBytesReceived: 0,
      currentFileSize: 0
    }, task);
    await recordEvent("task_start", {
      task,
      message: `${task.sku} · ${task.source_type}`
    });
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/start`, {
      method: "POST",
      body: JSON.stringify({})
    });
    stopHeartbeat = startDownloadItemHeartbeat(baseUrl, task.download_item_id);
    await setStatus({
      phase: "running",
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
    await setCurrentStage("success_post_start", {}, task);
    await recordEvent("success_post_start", {
      task,
      message: `${files.length} image(s)`,
      detail: { image_count: files.length }
    });
    await apiFetch(baseUrl, `/api/extension/download-items/${task.download_item_id}/success`, {
      method: "POST",
      body: JSON.stringify({ files, image_count: files.length })
    });
    await recordEvent("success_post_done", {
      task,
      message: `${files.length} image(s)`,
      detail: { image_count: files.length }
    });
    await setCurrentStage("task_success", {}, task);
    await recordEvent("task_success", {
      task,
      message: `${task.sku} · ${task.source_type}`
    });
    const state = await chrome.storage.local.get({ done: 0 });
    await setStatus({ done: Number(state.done || 0) + 1, message: `完成 ${task.sku}` });
  } catch (error) {
    const diagnosticDetail = await currentDiagnosticDetail();
    const failureReason = `${errorMessage(error)} ${diagnosticDetail}`.trim();
    const errorCode = error?.errorCode || "extension_download_failed";
    const errorSummary = error?.errorMessage || "浏览器插件下载失败。";
    const partialFiles = Array.isArray(error.partialFiles) ? error.partialFiles : [];
    const partialImageCount = Number(error.partialImageCount || partialFiles.length || 0);
    await setCurrentStage("failure_post_start", {}, task);
    await recordEvent("task_failure", {
      task,
      message: failureReason,
      detail: { errorCode, partialImageCount }
    });
    await recordEvent("failure_post_start", {
      task,
      message: failureReason,
      detail: { errorCode, partialImageCount }
    });
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
    await recordEvent("failure_post_done", {
      task,
      message: failureReason,
      detail: { errorCode, partialImageCount }
    });
    const state = await chrome.storage.local.get({ failed: 0 });
    await setStatus({
      failed: Number(state.failed || 0) + 1,
      message: `失败 ${task.sku}: ${failureReason.slice(0, 120)}`,
      lastFailureSku: task.sku,
      lastFailureCode: errorCode,
      lastFailureReason: failureReason
    });
  } finally {
    if (stopHeartbeat) {
      stopHeartbeat();
    }
    currentTask = null;
    await setStatus({ currentSku: "", currentSourceType: "", ...clearCurrentDiagnosticPatch() });
  }
}

async function incrementProcessedCount() {
  const state = await chrome.storage.local.get({ processed: 0 });
  await setStatus({ processed: Number(state.processed || 0) + 1 });
}

async function stopDownloads() {
  stopRequested = true;
  stopInProgress = Boolean(workerRunning);
  const downloadId = activeDownloadId;
  if (downloadId) {
    try {
      await chrome.downloads.cancel(downloadId);
    } catch (error) {
      // The download may have completed or disappeared between polling ticks.
    }
  }
  if (!workerRunning) {
    stopInProgress = false;
    await setStatus({
      phase: "stopped",
      running: false,
      stopping: false,
      state: "已停止",
      message: "当前没有正在运行的插件下载。"
    });
    const state = await getRuntimeState();
    return runtimeResponse(state, { message: "当前没有正在运行的插件下载。" });
  }
  await setStatus({
    phase: "stopping",
    running: true,
    stopping: true,
    state: "正在停止",
    message: "正在停止插件下载，当前下载会取消并回到 Web 重试。"
  });
  const state = await getRuntimeState();
  return runtimeResponse(state, { message: "正在停止插件下载。" });
}

async function startQueue(compareBatchId = "") {
  if (workerRunning || stopInProgress) {
    const state = await getRuntimeState(compareBatchId);
    return {
      ok: false,
      protocolVersion: PROTOCOL_VERSION,
      code: "already_running",
      message: state.stopping ? "插件正在停止，请等待停止完成后再重试。" : "插件正在下载中，请先停止或等待完成。",
      state,
      snapshot: state.snapshot
    };
  }
  runQueue();
  return runtimeResponse(await getRuntimeState(compareBatchId));
}

async function configureAndStartQueue(baseUrl, batchId) {
  if (workerRunning || stopInProgress) {
    return startQueue(batchId);
  }
  const config = await saveConfig(baseUrl, batchId);
  const response = await startQueue(batchId);
  return { ...response, config };
}

async function runQueue() {
  if (workerRunning) {
    return;
  }
  workerRunning = true;
  stopRequested = false;
  stopInProgress = false;
  driveResourceMetadataCache = new Map();
  const attemptedIds = new Set();
  await setStatus({
    phase: "running",
    running: true,
    stopping: false,
    state: "运行中",
    processed: 0,
    done: 0,
    failed: 0,
    currentSku: "",
    currentSourceType: "",
    ...clearCurrentDiagnosticPatch(),
    lastFailureSku: "",
    lastFailureCode: "",
    lastFailureReason: "",
    message: "正在连接 Web..."
  });
  let queueBatchId = "";
  try {
    const queueConfig = await getConfig();
    queueBatchId = queueConfig.batchId;
    try {
      await recordEvent("queue_start", {
        task: { batch_id: queueBatchId },
        message: "queue started"
      });
    } catch (error) {
      // Queue startup should not be blocked by diagnostic logging.
    }
    while (!stopRequested) {
      const config = await getConfig();
      queueBatchId = config.batchId;
      if (!config.baseUrl || !config.batchId) {
        throw new Error("请填写 Web 地址和批次 ID。 ");
      }
      const payload = await apiFetch(
        config.baseUrl,
        `/api/extension/batches/${encodeURIComponent(config.batchId)}/download-items?limit=20`
      );
      const items = (payload.items || []).filter((item) => !attemptedIds.has(item.download_item_id));
      if (!items.length) {
        await setStatus({ phase: "completed", state: "已完成", message: "没有待下载项。" });
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
      await setStatus({ phase: "stopped", state: "已停止", message: "已停止插件下载，失败项请回到 Web 批次页重试。" });
    }
  } catch (error) {
    await setStatus({
      phase: "failed",
      state: "错误",
      currentSku: "",
      currentSourceType: "",
      lastFailureCode: "extension_runtime_failed",
      lastFailureReason: errorMessage(error),
      message: String(error.message || error)
    });
  } finally {
    workerRunning = false;
    stopInProgress = false;
    try {
      await recordEvent("queue_stop", {
        task: { batch_id: queueBatchId },
        message: stopRequested ? "queue stopped" : "queue finished"
      });
    } catch (error) {
      // Queue cleanup should not be blocked by diagnostic logging.
    }
    await setStatus({ running: false, stopping: false, currentSku: "", currentSourceType: "", ...clearCurrentDiagnosticPatch() });
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "status") {
    recordRuntimeRequest("runtime_status_request", {
      message: String(message.batchId || ""),
      requestedBatchId: String(message.batchId || ""),
      senderUrl: sender?.url || sender?.tab?.url || ""
    });
    getRuntimeState(message.batchId)
      .then((state) => {
        sendResponse(runtimeResponse(state));
      })
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message ? error.message : error)
        });
      });
    return true;
  }
  if (message.type === "start") {
    recordRuntimeRequest("runtime_start_request", {
      senderUrl: sender?.url || sender?.tab?.url || ""
    });
    startQueue()
      .then((response) => {
        sendResponse(response);
      })
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message ? error.message : error)
        });
      });
    return true;
  }
  if (message.type === "configureAndStart") {
    recordRuntimeRequest("runtime_start_request", {
      message: String(message.batchId || ""),
      requestedBatchId: String(message.batchId || ""),
      baseUrl: String(message.baseUrl || ""),
      senderUrl: sender?.url || sender?.tab?.url || ""
    });
    configureAndStartQueue(message.baseUrl, message.batchId)
      .then((response) => {
        sendResponse(response);
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
    recordRuntimeRequest("runtime_stop_request", {
      senderUrl: sender?.url || sender?.tab?.url || ""
    });
    stopDownloads()
      .then((response) => {
        sendResponse(response);
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
    recordRuntimeRequest("runtime_stop_request", {
      senderUrl: sender?.url || sender?.tab?.url || ""
    });
    stopDownloads()
      .then((response) => {
        sendResponse(response);
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
