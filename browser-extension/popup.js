const baseUrlInput = document.getElementById("baseUrl");
const batchIdInput = document.getElementById("batchId");
const stateEl = document.getElementById("state");
const doneEl = document.getElementById("done");
const failedEl = document.getElementById("failed");
const messageEl = document.getElementById("message");

async function loadState() {
  const state = await chrome.storage.local.get({
    baseUrl: "http://localhost:8000",
    batchId: "",
    running: false,
    done: 0,
    failed: 0,
    message: "",
    state: "未连接"
  });
  baseUrlInput.value = state.baseUrl;
  batchIdInput.value = state.batchId;
  stateEl.textContent = state.running ? "运行中" : state.state;
  doneEl.textContent = String(state.done || 0);
  failedEl.textContent = String(state.failed || 0);
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
