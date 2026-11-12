import "./style.css";
import { drawMarker } from "./track-overlay.js";

const video = document.getElementById("replay-video");
const canvas = document.getElementById("replay-overlay");
const status = document.getElementById("replay-status");
const context = canvas.getContext("2d");
const colors = ["#58f5bd", "#ffcb64", "#70d4ff", "#ff98cf"];
const frames = [];
let failed = false;
let lastFrame;
let frameRequest;

function fail(message) {
  failed = true;
  status.textContent = message;
  clearOverlay();
}

function clearOverlay() {
  context.save();
  context.resetTransform();
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.restore();
}

function render(time) {
  if (failed || !frames.length || !video.videoWidth || video.seeking) return;
  const latest = frames.findLastIndex((frame) => frame.time <= time + 0.001);
  const index = latest >= 0 && time - frames[latest].time <= 0.18 ? latest : -1;
  if (index === lastFrame) return;
  lastFrame = index;
  clearOverlay();
  const current = frames[index];
  const tracks = current?.tracks ?? [];
  const tracked = tracks.filter((track) => track.point);
  const lost = tracks.length - tracked.length;
  const label = tracked.length || lost
    ? `${tracked.length} 尾追蹤中${lost ? ` · ${lost} 尾遺失` : ""}`
    : "這一格沒有有效位置";
  if (status.textContent !== label) status.textContent = label;

  for (const { id, point } of tracked) {
    const color = colors[(id - 1) % colors.length];
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.beginPath();
    context.moveTo(...point);
    for (let previous = index - 1; previous >= 0 && current.time - frames[previous].time <= 1; previous--) {
      const earlier = frames[previous].tracks.find((track) => track.id === id)?.point;
      if (!earlier || frames[previous + 1].time - frames[previous].time > 0.18) break;
      context.lineTo(...earlier);
    }
    context.stroke();
    drawMarker(context, ...point, color, `ID ${id}`);
  }
}

function resize() {
  const rect = video.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  if (!video.videoWidth || !rect.width || !rect.height) return;
  const scale = Math.min(canvas.width / video.videoWidth, canvas.height / video.videoHeight);
  context.setTransform(scale, 0, 0, scale, (canvas.width - video.videoWidth * scale) / 2,
    (canvas.height - video.videoHeight * scale) / 2);
  lastFrame = undefined;
  render(video.currentTime);
}

function scheduleFrame() {
  if (!video.requestVideoFrameCallback || frameRequest !== undefined) return;
  frameRequest = video.requestVideoFrameCallback((_, { mediaTime }) => {
    frameRequest = undefined;
    render(mediaTime);
    if (!video.paused) scheduleFrame();
  });
}

new ResizeObserver(resize).observe(video);
video.addEventListener("loadeddata", resize);
video.addEventListener("play", scheduleFrame);
video.addEventListener("seeking", () => {
  lastFrame = undefined;
  clearOverlay();
});
video.addEventListener("seeked", () => render(video.currentTime));
video.addEventListener("timeupdate", () => {
  if (!video.requestVideoFrameCallback) render(video.currentTime);
});
video.addEventListener("error", () => fail("無法載入魚缸正面影片。"));

try {
  const response = await fetch("/api/replay/tracks");
  if (!response.ok) throw new Error("尚未準備測試影片與追蹤資料。");
  const [header, ...rows] = (await response.text()).trim().split(/\r?\n/);
  if (header !== "frame,time_s,id,status,x_px,y_px") throw new Error("追蹤 CSV 格式不符。");
  for (const row of rows) {
    const [, seconds, trackId, tracking, x, y] = row.split(",");
    const time = Number(seconds);
    const id = Number(trackId);
    const point = tracking === "tracked" && x && y ? [Number(x), Number(y)] : null;
    if (!Number.isFinite(time) || time < 0 || !Number.isInteger(id) || id < 1 ||
        (tracking === "tracked" ? !point?.every(Number.isFinite) : tracking !== "lost")) {
      throw new Error("追蹤 CSV 包含無效座標。");
    }
    if (frames.at(-1)?.time > time) throw new Error("追蹤 CSV 的時間順序有誤。");
    if (frames.at(-1)?.time !== time) frames.push({ time, tracks: [] });
    frames.at(-1).tracks.push({ id, point });
  }
  if (!frames.length) throw new Error("追蹤 CSV 沒有資料。");
  video.src = "/api/replay/video";
  document.getElementById("replay-download").hidden = false;
} catch (error) {
  fail(error.message);
}
