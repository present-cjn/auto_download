const START_MESSAGE = "autoDownloadExtensionStart";
const STOP_MESSAGE = "autoDownloadExtensionStop";
const STATUS_MESSAGE = "autoDownloadExtensionStatus";
const ACK_MESSAGE = "autoDownloadExtensionAck";

window.addEventListener("message", async (event) => {
  if (event.source !== window || !event.data) {
    return;
  }
  if (event.data.type !== START_MESSAGE && event.data.type !== STOP_MESSAGE && event.data.type !== STATUS_MESSAGE) {
    return;
  }

  try {
    let response;
    if (event.data.type === START_MESSAGE) {
      response = await chrome.runtime.sendMessage({
        type: "configureAndStart",
        baseUrl: String(event.data.baseUrl || ""),
        batchId: String(event.data.batchId || "")
      });
    } else if (event.data.type === STATUS_MESSAGE) {
      response = await chrome.runtime.sendMessage({ type: "status" });
    } else {
      response = await chrome.runtime.sendMessage({ type: "stop" });
    }
    window.postMessage({
      type: ACK_MESSAGE,
      action: event.data.type === START_MESSAGE ? "start" : (event.data.type === STATUS_MESSAGE ? "status" : "stop"),
      ok: true,
      response
    }, window.location.origin);
  } catch (error) {
    window.postMessage(
      {
        type: ACK_MESSAGE,
        action: event.data.type === START_MESSAGE ? "start" : (event.data.type === STATUS_MESSAGE ? "status" : "stop"),
        ok: false,
        error: String(error && error.message ? error.message : error)
      },
      window.location.origin
    );
  }
});
