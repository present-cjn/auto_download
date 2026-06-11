const START_MESSAGE = "autoDownloadExtensionStart";
const ACK_MESSAGE = "autoDownloadExtensionAck";

window.addEventListener("message", async (event) => {
  if (event.source !== window || !event.data || event.data.type !== START_MESSAGE) {
    return;
  }

  try {
    const response = await chrome.runtime.sendMessage({
      type: "configureAndStart",
      baseUrl: String(event.data.baseUrl || ""),
      batchId: String(event.data.batchId || "")
    });
    window.postMessage({ type: ACK_MESSAGE, ok: true, response }, window.location.origin);
  } catch (error) {
    window.postMessage(
      {
        type: ACK_MESSAGE,
        ok: false,
        error: String(error && error.message ? error.message : error)
      },
      window.location.origin
    );
  }
});
