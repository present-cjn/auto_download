const baseUrlInput = document.getElementById("baseUrl");
const batchIdInput = document.getElementById("batchId");
const stateEl = document.getElementById("state");
const processedEl = document.getElementById("processed");
const doneEl = document.getElementById("done");
const failedEl = document.getElementById("failed");
const currentSkuEl = document.getElementById("currentSku");
const lastFailureEl = document.getElementById("lastFailure");
const messageEl = document.getElementById("message");
const DEFAULT_BASE_URL = "https://dev.waysing.cn";

async function loadState() {
  const state = await chrome.storage.local.get({
    baseUrl: DEFAULT_BASE_URL,
    batchId: "",
    running: false,
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
  stateEl.textContent = state.running ? "运行中" : state.state;
  processedEl.textContent = String(state.processed || 0);
  doneEl.textContent = String(state.done || 0);
  failedEl.textContent = String(state.failed || 0);
  currentSkuEl.textContent = state.currentSku
    ? `${state.currentSku}${state.currentSourceType ? " · " + state.currentSourceType : ""}`
    : "-";
  lastFailureEl.textContent = state.lastFailureReason
    ? `${state.lastFailureSku ? state.lastFailureSku + ": " : ""}${state.lastFailureReason}`
    : "-";
  messageEl.textContent = state.message || "";
}

async function saveInputs() {
  await chrome.storage.local.set({
    baseUrl: baseUrlInput.value.replace(/\/$/, ""),
    batchId: batchIdInput.value.trim()
  });
}

document.getElementById("start").addEventListener("click", async () => {
  await saveInputs();
  await chrome.runtime.sendMessage({ type: "start" });
  await loadState();
});

document.getElementById("pause").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "pause" });
  await loadState();
});

chrome.storage.onChanged.addListener(loadState);
loadState();
