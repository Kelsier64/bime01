import { displayTime } from "./charts.js";
import { drawMarker } from "./track-overlay.js";

export function createCameraPreview(panel, liveImage, overlay, onLiveChange) {
  const capture = panel.querySelector("#camera-capture");
  const toggle = panel.querySelector("#camera-live");
  const status = panel.querySelector("#camera-status");
  const image = panel.querySelector("#camera-image");
  const fishCount = panel.querySelector("#camera-fish");
  const profile = panel.querySelector("#camera-profile");
  const context = overlay.getContext("2d");
  let enabled = false;
  let online = false;
  let capturing = false;
  let live = null;
  let nextTracking = null;
  let displayedTracking = null;

  function setStatus(message) {
    if (status.textContent !== message) status.textContent = message;
  }

  function updateButtons() {
    const active = Boolean(live);
    capture.disabled = !online || !enabled || capturing || active;
    toggle.disabled = !active && (!online || !enabled || capturing);
    fishCount.disabled = active;
    profile.disabled = active;
    toggle.setAttribute("aria-pressed", String(active));
    toggle.textContent = active ? "停止即時影像" : "啟動即時影像";
  }

  function showImage(element, blob) {
    const previous = element.getAttribute("src");
    if (previous) URL.revokeObjectURL(previous);
    if (blob) element.src = URL.createObjectURL(blob);
    else element.removeAttribute("src");
    element.hidden = !blob;
  }

  function stopLive(message) {
    const socket = live;
    if (!socket) return;
    live = null;
    socket.close();
    showImage(liveImage, null);
    overlay.hidden = true;
    nextTracking = null;
    displayedTracking = null;
    onLiveChange("idle");
    setStatus(message);
    updateButtons();
  }

  capture.addEventListener("click", async () => {
    capturing = true;
    updateButtons();
    setStatus("正在擷取相機影像…");
    try {
      const response = await fetch("/api/camera/snapshot", { method: "POST", signal: AbortSignal.timeout(12_000) });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail);
      }
      showImage(image, await response.blob());
      setStatus(`最近拍攝 ${displayTime(Date.now())}`);
    } catch (error) {
      setStatus(error.name === "Error" && error.message ? error.message : "擷取失敗，請再試一次");
    } finally {
      capturing = false;
      updateButtons();
    }
  });

  liveImage.addEventListener("load", () => {
    if (!live || liveImage.currentSrc !== liveImage.src || !context) return;
    const tracking = displayedTracking;
    if (!tracking || tracking.stage !== "tracking") {
      overlay.hidden = true;
      return;
    }
    if (overlay.width !== tracking.width) overlay.width = tracking.width;
    if (overlay.height !== tracking.height) overlay.height = tracking.height;
    context.clearRect(0, 0, overlay.width, overlay.height);
    for (const track of tracking.tracks) {
      const color = track.id % 2 ? "#a0ffba" : "#ffd669";
      context.strokeStyle = color;
      context.lineWidth = 2;
      context.beginPath();
      context.moveTo(...track.trail[0]);
      for (const point of track.trail.slice(1)) context.lineTo(...point);
      context.stroke();
      drawMarker(context, track.x_px, track.y_px, color, `ID ${track.id}`);
    }
    overlay.hidden = false;
  });

  toggle.addEventListener("click", () => {
    if (live) {
      stopLive("即時影像已停止");
      return;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const query = new URLSearchParams({ fish: fishCount.value, profile: profile.value });
    const socket = new WebSocket(`${protocol}//${location.host}/ws/camera/live?${query}`);
    socket.binaryType = "blob";
    live = socket;
    showImage(image, null);
    onLiveChange("starting");
    setStatus("正在連接 Zero 2 W 相機…");
    updateButtons();

    socket.addEventListener("message", ({ data }) => {
      if (live !== socket) return;
      if (typeof data === "string") {
        if (data.startsWith("{")) nextTracking = JSON.parse(data);
        else stopLive(data);
        return;
      }
      const firstFrame = liveImage.hidden;
      const trackingChanged = displayedTracking?.stage !== nextTracking?.stage
        || displayedTracking?.tracks.length !== nextTracking?.tracks.length;
      displayedTracking = nextTracking;
      nextTracking = null;
      showImage(liveImage, data.slice(0, data.size, "image/jpeg"));
      if (firstFrame || trackingChanged) onLiveChange("live");
      const stage = displayedTracking?.stage;
      setStatus(stage === "unavailable" ? "追蹤元件未載入"
        : stage === "learning" ? "建立追蹤背景中，請保持鏡頭固定"
        : stage === "invalid_frame" ? "等待有效相機畫格" : "Zero 2 W 即時影像");
    });
    socket.addEventListener("close", () => {
      if (live === socket) stopLive("即時影像已中斷，請重新啟動");
    });
  });

  panel.addEventListener("toggle", () => {
    if (!panel.open) stopLive("即時影像已停止");
  });
  window.addEventListener("pagehide", () => stopLive("即時影像已停止"));

  return {
    get tracking() {
      return displayedTracking;
    },
    update(isEnabled, isOnline) {
      if (enabled === isEnabled && online === isOnline) return;
      enabled = isEnabled;
      online = isOnline;
      panel.hidden = !enabled;
      if (!enabled) stopLive("相機預覽已停用");
      updateButtons();
    },
  };
}
