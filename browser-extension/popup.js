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
const PHASE_LABELS = {
  idle: "空闲",
  ready: "就绪",
  running: "运行中",
  stopping: "正在停止",
  stopped: "已停止",
  completed: "已完成",
  failed: "错误",
  disconnected: "未连接"
};
const STARTABLE_PHASES = new Set(["ready", "stopped", "completed", "failed"]);
let currentPhase = "idle";

function phaseFromStoredState(state) {
  if (state.stopping) {
    return "stopping";
  }
  if (state.running) {
    return "running";
  }
  if (state.phase) {
    return state.phase;
  }
  if (!state.baseUrl || !state.batchId) {
    return "idle";
  }
  return "ready";
}

function snapshotFromStoredState(state) {
  const phase = phaseFromStoredState(state);
  return {
    phase,
    baseUrl: state.baseUrl,
    batchId: state.batchId,
    processed: Number(state.processed || 0),
    done: Number(state.done || 0),
    failed: Number(state.failed || 0),
    currentTask: state.currentSku
      ? {
          sku: state.currentSku,
          sourceType: state.currentSourceType || "",
          sourceTypeLabel: state.currentSourceType === "mockup" ? "Mockup" : "Design"
        }
      : null,
    lastError: state.lastFailureReason
      ? {
          sku: state.lastFailureSku || "",
          message: state.lastFailureReason
        }
      : null,
    message: state.message || ""
  };
}

function renderSnapshot(snapshot) {
  const phase = snapshot.phase || "idle";
  currentPhase = phase;
  const visibleState = PHASE_LABELS[phase] || phase;
  baseUrlInput.value = snapshot.baseUrl || DEFAULT_BASE_URL;
  batchIdInput.value = snapshot.batchId || "";
  stateEl.textContent = visibleState;
  stateBadgeEl.textContent = visibleState;
  stateBadgeEl.dataset.state = phase;
  processedEl.textContent = String(snapshot.processed || 0);
  doneEl.textContent = String(snapshot.done || 0);
  failedEl.textContent = String(snapshot.failed || 0);
  currentSkuEl.textContent = snapshot.currentTask
    ? `${snapshot.currentTask.sku || "-"}${snapshot.currentTask.sourceTypeLabel ? " · " + snapshot.currentTask.sourceTypeLabel : ""}`
    : "当前没有正在下载的 SKU";
  lastFailureEl.textContent = snapshot.lastError?.message
    ? `${snapshot.lastError.sku ? snapshot.lastError.sku + ": " : ""}${snapshot.lastError.message}`
    : "本轮暂无失败记录";
  messageEl.textContent = snapshot.message || "";
  startButton.disabled = !canStartFromInputs(phase);
  pauseButton.disabled = phase !== "running";
}

function canStartFromInputs(phase = currentPhase) {
  if (phase === "running" || phase === "stopping") {
    return false;
  }
  if (STARTABLE_PHASES.has(phase)) {
    return true;
  }
  return Boolean(baseUrlInput.value.trim() && batchIdInput.value.trim());
}

function updateInputDrivenButtons() {
  startButton.disabled = !canStartFromInputs();
}

async function loadState() {
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
    message: "",
    state: "未连接"
  });
  let snapshot = snapshotFromStoredState(state);
  try {
    const response = await chrome.runtime.sendMessage({ type: "status" });
    if (response?.snapshot) {
      snapshot = response.snapshot;
    }
  } catch (error) {
    snapshot = {
      ...snapshot,
      phase: "disconnected",
      message: String(error && error.message ? error.message : error)
    };
  }
  renderSnapshot(snapshot);
}

async function saveInputs() {
  await chrome.storage.local.set({
    baseUrl: baseUrlInput.value.replace(/\/$/, ""),
    batchId: batchIdInput.value.trim(),
    phase: "ready",
    updatedAt: new Date().toISOString()
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
baseUrlInput.addEventListener("input", updateInputDrivenButtons);
batchIdInput.addEventListener("input", updateInputDrivenButtons);
loadState();
