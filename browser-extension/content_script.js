const START_MESSAGE = "autoDownloadExtensionStart";
const STOP_MESSAGE = "autoDownloadExtensionStop";
const ACK_MESSAGE = "autoDownloadExtensionAck";

window.addEventListener("message", async (event) => {
  if (event.source !== window || !event.data) {
    return;
  }
  if (event.data.type !== START_MESSAGE && event.data.type !== STOP_MESSAGE) {
    return;
  }

  try {
    const response = event.data.type === START_MESSAGE
      ? await chrome.runtime.sendMessage({
        type: "configureAndStart",
        baseUrl: String(event.data.baseUrl || ""),
        batchId: String(event.data.batchId || "")
      })
      : await chrome.runtime.sendMessage({ type: "stop" });
    window.postMessage({
      type: ACK_MESSAGE,
      action: event.data.type === START_MESSAGE ? "start" : "stop",
      ok: true,
      response
    }, window.location.origin);
  } catch (error) {
    window.postMessage(
      {
        type: ACK_MESSAGE,
        action: event.data.type === START_MESSAGE ? "start" : "stop",
        ok: false,
        error: String(error && error.message ? error.message : error)
      },
      window.location.origin
    );
  }
});
