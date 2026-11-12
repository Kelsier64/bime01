import "./style.css";
import { createTrendChart, drawHeatmap, formatDateTime, formatValue, METRICS } from "./charts.js";
import { createLiveView } from "./live.js";

const $ = (id) => document.getElementById(id);
const setText = (element, value) => {
  const text = String(value);
  if (element.textContent !== text) element.textContent = text;
};

const live = createLiveView({ frame: $("frame"), overlay: $("overlay"), message: $("stage-message"), stage: $("stage") });
const chart = createTrendChart($("chart"));
let state = null;
let online = false;
let range = "24h";
let history = null;
let feeding = null;
let calibrating = false;
let alertKey = "";
let retryDelay = 500;

const ALERT_NAMES = { camera_offline: "相機斷線", low_activity: "活動量偏低" };

// live connection --------------------------------------------------------------

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/live`);
  socket.binaryType = "blob";
  let watchdog;
  const arm = () => {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => socket.close(), 5000);
  };
  arm();
  socket.addEventListener("message", ({ data }) => {
    arm();
    if (typeof data === "string") {
      if (!online) setOnline(true);
      retryDelay = 500;
      acceptState(JSON.parse(data));
    } else if (state) {
      live.showFrame(data, state);
    }
  });
  socket.addEventListener("close", () => {
    clearTimeout(watchdog);
    setOnline(false);
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 8000);
  });
}

function setOnline(value) {
  online = value;
  const connection = $("connection");
  connection.classList.toggle("ok", value);
  setText(connection.lastElementChild, value ? "已連線" : "重新連線中");
  $("feed").disabled = !value;
}

function acceptState(next) {
  const previous = state;
  state = next;
  live.setState(next);
  renderLive();
  renderAlerts();
  const key = next.alerts.map((alert) => alert.kind + alert.since_ms).join();
  if (previous && (key !== alertKey || previous.feeder.last_feed_ms !== next.feeder.last_feed_ms)) {
    void loadEvents();
    void loadFeeding();
  }
  alertKey = key;
}

// rendering --------------------------------------------------------------------

function renderLive() {
  const { camera, light, tracking, calibration, source, demo } = state;
  setText($("source"), source === "fake" ? "假相機（模擬）" : "Pi Camera 3");
  const cameraChip = $("camera-chip");
  cameraChip.dataset.tone = camera.status === "live" ? "ok" : camera.status === "offline" ? "bad" : "";
  setText(cameraChip, camera.status === "live" ? `相機 ${camera.fps ?? "—"} fps` : camera.status === "offline" ? "相機離線" : "相機連線中");
  const lightChip = $("light-chip");
  lightChip.dataset.tone = light.lit === false ? "warn" : light.lit ? "ok" : "";
  setText(lightChip, light.lit === false ? "光線不足 · 暫停追蹤" : light.lit ? "光線充足" : "光線 —");
  const trackingChip = $("tracking-chip");
  const seen = tracking.fish.length;
  trackingChip.dataset.tone = tracking.stage !== "tracking" ? "" : seen >= tracking.expected ? "ok" : "warn";
  setText(trackingChip, tracking.stage === "tracking" ? `追蹤 ${seen} / ${tracking.expected} 尾`
    : tracking.stage === "learning" ? "建立背景中" : "追蹤 —");

  const list = $("fish-list");
  const items = tracking.fish.map((fish) => {
    const position = Number.isFinite(fish.x_cm) ? `x ${fish.x_cm.toFixed(1)} · 高 ${fish.y_cm.toFixed(1)} cm` : "未校正";
    const speed = Number.isFinite(fish.speed_cm_s) ? `${fish.speed_cm_s.toFixed(1)} cm/s` : "—";
    return `<li><strong>#${fish.id}</strong><span>${speed}</span><span>${position}</span></li>`;
  }).join("");
  const html = items || `<li class="muted">${tracking.stage === "dark" ? "關燈中，暫停追蹤" : "畫面中沒有偵測到魚"}</li>`;
  if (list.innerHTML !== html) list.innerHTML = html;

  $("calibration-notice").hidden = Boolean(calibration) || calibrating;
  $("demo").hidden = !demo;
  if (demo) {
    setText(document.querySelector('[data-demo="dark"]'), demo.dark ? "開燈" : "關燈");
    setText(document.querySelector('[data-demo="sluggish"]'), demo.sluggish ? "恢復活動" : "魚變遲緩");
    setText(document.querySelector('[data-demo="offline"]'), demo.offline_s ? `相機離線中 ${demo.offline_s} 秒` : "拔除相機 45 秒");
  }
  if (state.feeder.kind === "manual") setText($("feed"), "記錄餵食");
  else setText($("feed"), "立即餵食");
}

function renderAlerts() {
  const container = $("alerts");
  container.hidden = !state.alerts.length;
  const html = state.alerts.map((alert) => `<div class="alert">
      <strong>${ALERT_NAMES[alert.kind] ?? alert.kind}</strong>
      <span>${escapeHtml(alert.message)}</span>
      <time>自 ${formatDateTime(alert.since_ms)}</time>
      ${alert.snapshot ? `<a href="/api/snapshots/${alert.snapshot}" target="_blank" rel="noopener">快照</a>` : ""}
    </div>`).join("");
  if (container.innerHTML !== html) container.innerHTML = html;
}

function renderHistory() {
  const metric = $("metric").value;
  chart.render(history, metric);
  drawHeatmap($("heatmap"), history?.heat);
  const summary = history?.summary;
  if (history) {
    const hours = (summary.lit_s / 3600).toFixed(1);
    setText($("history-subtitle"), `有光觀測 ${hours} 小時 · ${METRICS[metric].label}平均 ${formatValue(summary[metric], metric)}`);
  }
}

function renderFeeding() {
  const container = $("feeding");
  if (!feeding) {
    container.innerHTML = '<p class="muted">還沒有餵食紀錄。餵食後按下按鈕，系統會比較前後的游速與游動高度。</p>';
    return;
  }
  const rows = [
    ["平均游速", "speed"],
    ["平均高度", "height"],
    ["活動比例", "moving"],
  ];
  const progress = feeding.collecting
    ? `<progress max="${feeding.window_min * 60}" value="${feeding.elapsed_s}"></progress><p class="muted">正在收集餵食後 ${feeding.window_min} 分鐘資料</p>`
    : "";
  container.innerHTML = `<p>${formatDateTime(feeding.ts_ms)} 餵食 · 前後各 ${feeding.window_min} 分鐘
      ${feeding.snapshot ? ` · <a href="/api/snapshots/${feeding.snapshot}" target="_blank" rel="noopener">快照</a>` : ""}</p>
    ${progress}
    <table><thead><tr><th></th><th>餵食前</th><th>餵食後</th></tr></thead><tbody>
      ${rows.map(([label, key]) => `<tr><th scope="row">${label}</th>
        <td>${formatValue(feeding.before[key], key)}</td><td>${formatValue(feeding.after[key], key)}</td></tr>`).join("")}
      <tr><th scope="row">有光觀測</th><td>${feeding.before.lit_s} 秒</td><td>${feeding.after.lit_s} 秒</td></tr>
    </tbody></table>`;
}

const EVENT_LABELS = {
  feed: (event) => event.detail.origin === "manual" ? "手動餵食" : "餵食器餵食",
  alert: (event) => `警示：${ALERT_NAMES[event.detail.alert] ?? event.detail.alert}`,
  alert_cleared: (event) => `解除：${ALERT_NAMES[event.detail.alert] ?? event.detail.alert}（${Math.round(event.detail.duration_s / 60)} 分鐘）`,
  calibration: (event) => `重新校正（${event.detail.tank_width_cm ?? "—"} × ${event.detail.tank_height_cm ?? "—"} cm）`,
};

function renderEvents(events) {
  const list = $("events");
  if (!events.length) {
    list.innerHTML = '<li class="muted">尚無事件</li>';
    return;
  }
  list.innerHTML = events.map((event) => {
    const label = EVENT_LABELS[event.kind]?.(event) ?? event.kind;
    const message = event.kind === "alert" ? `<span class="muted">${escapeHtml(event.detail.message ?? "")}</span>` : "";
    const snapshot = event.snapshot ? `<a href="/api/snapshots/${event.snapshot}" target="_blank" rel="noopener">快照</a>` : "";
    return `<li data-kind="${event.kind}"><time>${formatDateTime(event.ts_ms)}</time><strong>${label}</strong>${message}${snapshot}</li>`;
  }).join("");
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

// data loading -----------------------------------------------------------------

async function getJson(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : "指令失敗";
    throw new Error(detail);
  }
  return body;
}

let historyRequest = 0;
async function loadHistory() {
  const request = ++historyRequest;
  try {
    const result = await getJson(`/api/history?range=${range}`);
    if (request !== historyRequest) return;
    history = result;
    renderHistory();
  } catch {
    if (request === historyRequest && !history) chart.render(null, $("metric").value, "暫時無法讀取歷史資料");
  }
}

async function loadDay() {
  try {
    const { summary } = await getJson("/api/history?range=24h");
    setText($("day-speed"), formatValue(summary.speed, "speed"));
    setText($("day-moving"), formatValue(summary.moving, "moving"));
    setText($("day-lit"), `${(summary.lit_s / 3600).toFixed(1)} 小時`);
  } catch { /* keep previous values */ }
}

let feedingTimer;
async function loadFeeding() {
  clearTimeout(feedingTimer);
  try {
    feeding = await getJson("/api/feeding");
    renderFeeding();
  } catch { /* retry below */ }
  feedingTimer = setTimeout(loadFeeding, feeding?.collecting ? 3000 : 60_000);
}

async function loadEvents() {
  try {
    renderEvents(await getJson("/api/events?limit=30"));
  } catch { /* retry on next poll */ }
}

// actions ----------------------------------------------------------------------

$("feed").addEventListener("click", async () => {
  const feedback = $("feed-feedback");
  $("feed").disabled = true;
  try {
    feeding = await getJson("/api/feed", { method: "POST" });
    renderFeeding();
    feedback.dataset.error = "false";
    feedback.textContent = "已記錄餵食，正在比較前後行為。";
    void loadEvents();
    void loadFeeding();
  } catch (error) {
    feedback.dataset.error = "true";
    feedback.textContent = error.message;
  } finally {
    $("feed").disabled = !online;
  }
});

for (const button of document.querySelectorAll("[data-demo]")) {
  button.addEventListener("click", async () => {
    try {
      acceptState(await getJson("/api/demo", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: button.dataset.demo }),
      }));
    } catch (error) {
      $("feed-feedback").textContent = error.message;
    }
  });
}

for (const button of document.querySelectorAll("[data-range]")) {
  button.addEventListener("click", () => {
    range = button.dataset.range;
    for (const item of document.querySelectorAll("[data-range]")) item.setAttribute("aria-pressed", String(item === button));
    history = null;
    renderHistory();
    void loadHistory();
  });
}
$("metric").addEventListener("change", renderHistory);
new ResizeObserver(() => renderHistory()).observe($("chart"));

// calibration ------------------------------------------------------------------

let calibrationPoints = [];
function updateCalibrationBar(points) {
  calibrationPoints = points;
  const hint = points.length < 4
    ? `在畫面上點選水體的「${live.nextCornerName(points.length)}」角（${points.length} / 4）`
    : "四角已選好，確認魚缸內寬與水深後儲存。";
  setText($("calibrate-hint"), hint);
  $("calibrate-save").disabled = points.length < 4;
  $("calibrate-undo").disabled = !points.length;
}

$("calibrate").addEventListener("click", () => {
  if (!state?.camera.width) {
    $("feed-feedback").textContent = "需要相機畫面才能校正";
    return;
  }
  calibrating = true;
  $("calibrate-bar").hidden = false;
  $("calibrate").hidden = true;
  $("tank-width").value = state.calibration?.tank_width_cm ?? "";
  $("tank-height").value = state.calibration?.tank_height_cm ?? "";
  live.startCalibration(updateCalibrationBar);
  renderLive();
});

function endCalibration() {
  calibrating = false;
  $("calibrate-bar").hidden = true;
  $("calibrate").hidden = false;
  live.stopCalibration();
  if (state) renderLive();
}

$("calibrate-undo").addEventListener("click", () => live.undoPoint());
$("calibrate-cancel").addEventListener("click", endCalibration);
$("calibrate-save").addEventListener("click", async () => {
  const width = Number($("tank-width").value);
  const height = Number($("tank-height").value);
  if (!(width > 0 && height > 0)) {
    setText($("calibrate-hint"), "請輸入魚缸內寬與水深（cm）");
    return;
  }
  try {
    const settings = await getJson("/api/settings");
    settings.calibration = {
      corners: calibrationPoints, frame_width: state.camera.width, frame_height: state.camera.height,
      tank_width_cm: width, tank_height_cm: height,
    };
    await putSettings(settings);
    endCalibration();
    void loadEvents();
  } catch (error) {
    setText($("calibrate-hint"), error.message === "指令失敗"
      ? "四角需依序為左上、右上、右下、左下，並圍成一個四邊形" : error.message);
  }
});

async function putSettings(settings) {
  return getJson("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings),
  });
}

// settings dialog --------------------------------------------------------------

const dialog = $("settings");
const form = $("settings-form");
let loadedSettings = null;

$("open-settings").addEventListener("click", async () => {
  try {
    loadedSettings = await getJson("/api/settings");
  } catch (error) {
    $("feed-feedback").textContent = error.message;
    return;
  }
  for (const [name, value] of Object.entries(loadedSettings)) {
    const input = form.elements.namedItem(name);
    if (input) input.value = name === "low_activity_ratio" ? Math.round(value * 100) : value;
  }
  const level = state?.light.brightness;
  setText($("brightness-now"), Number.isFinite(level) ? `目前畫面亮度 ${level}` : "");
  setText($("settings-feedback"), "");
  dialog.showModal();
});
$("settings-cancel").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const settings = { ...loadedSettings };
  for (const [name, value] of data.entries()) {
    settings[name] = name === "detection" ? value : name === "low_activity_ratio" ? Number(value) / 100 : Number(value);
  }
  try {
    await putSettings(settings);
    dialog.close();
  } catch (error) {
    setText($("settings-feedback"), error.message === "指令失敗" ? "有欄位超出允許範圍" : error.message);
  }
});

// start ------------------------------------------------------------------------

renderHistory();
renderFeeding();
connect();
void loadHistory();
void loadDay();
void loadFeeding();
void loadEvents();
setInterval(() => {
  void loadHistory();
  void loadDay();
}, 60_000);
setInterval(loadEvents, 30_000);
