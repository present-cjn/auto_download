const baseUrlInput = document.getElementById("baseUrl");
const batchIdInput = document.getElementById("batchId");
const stateEl = document.getElementById("state");
const stateBadgeEl = document.getElementById("stateBadge");
const processedEl = document.getElementById("processed");
const doneEl = document.getElementById("done");
const failedEl = document.getElementById("failed");
const currentSkuEl = document.getElementById("currentSku");
const lastFailureEl = document.getElementById("lastFailure");
const messageEl = document.getElementById("message");
const startButton = document.getElementById("start");
const pauseButton = document.getElementById("pause");
const DEFAULT_BASE_URL = "https://dev.waysing.cn";

async function loadState() {
  const state = await chrome.storage.local.get({
    baseUrl: DEFAULT_BASE_URL,
    batchId: "",
    running: false,
    stopping: false,
    processed: 0,
    done: 0,
    failed: 0,
    currentSku: "",
    currentSourceType: "",
    lastFailureSku: "",
    lastFailureReason: "",
    message: "",
    state: "未连接"
  });
  baseUrlInput.value = state.baseUrl;
  batchIdInput.value = state.batchId;
  const visibleState = state.stopping ? "正在停止" : (state.running ? "运行中" : state.state);
  stateEl.textContent = visibleState;
  stateBadgeEl.textContent = visibleState;
  stateBadgeEl.dataset.state = visibleState;
  processedEl.textContent = String(state.processed || 0);
  doneEl.textContent = String(state.done || 0);
  failedEl.textContent = String(state.failed || 0);
  currentSkuEl.textContent = state.currentSku
    ? `${state.currentSku}${state.currentSourceType ? " · " + state.currentSourceType : ""}`
    : "当前没有正在下载的 SKU";
  lastFailureEl.textContent = state.lastFailureReason
    ? `${state.lastFailureSku ? state.lastFailureSku + ": " : ""}${state.lastFailureReason}`
    : "本轮暂无失败记录";
  messageEl.textContent = state.message || "";
  startButton.disabled = Boolean(state.running || state.stopping);
  pauseButton.disabled = Boolean(!state.running || state.stopping);
}

async function saveInputs() {
  await chrome.storage.local.set({
    baseUrl: baseUrlInput.value.replace(/\/$/, ""),
    batchId: batchIdInput.value.trim()
  });
}

startButton.addEventListener("click", async () => {
  startButton.disabled = true;
  messageEl.textContent = "正在启动插件下载...";
  try {
    await saveInputs();
    const response = await chrome.runtime.sendMessage({ type: "start" });
    if (!response?.ok) {
      messageEl.textContent = response?.message || response?.error || "插件启动失败。";
    }
  } catch (error) {
    messageEl.textContent = String(error && error.message ? error.message : error);
  }
  await loadState();
});

pauseButton.addEventListener("click", async () => {
  pauseButton.disabled = true;
  messageEl.textContent = "正在停止，当前下载会取消并回到 Web 重试...";
  try {
    const response = await chrome.runtime.sendMessage({ type: "stop" });
    if (!response?.ok) {
      messageEl.textContent = response?.message || response?.error || "插件停止失败。";
    }
  } catch (error) {
    messageEl.textContent = String(error && error.message ? error.message : error);
  }
  await loadState();
});

chrome.storage.onChanged.addListener(loadState);
loadState();
