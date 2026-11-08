import "./style.css";
import { createTrendChart, displayTime, drawSparkline } from "./charts.js";

const byId = (id) => document.getElementById(id);
function text(id, value) {
  const element = byId(id);
  const label = String(value);
  if (element.textContent !== label) element.textContent = label;
}
const valueWithUnit = (value, unit = "", decimals = 1) =>
  Number.isFinite(value) ? `${value.toFixed(decimals)}${unit ? ` ${unit}` : ""}` : "—";
const trendChart = createTrendChart(byId("chart"));
const fishSelect = byId("fish-select");

let state;
let tank;
let tankRequested = false;
let online = false;
let pending = false;
let selectedView = "orbit";
let selectedRange = "1m";
let history;
let historyRequest = 0;
let loadingRange;
let retryDelay = 600;
let fishOptions = "";

const sensorCards = [
  ["temperature", "temperature_c", 1],
  ["transmittance", "transmittance_pct", 0],
];

function updateControls() {
  const controls = state?.controls;
  const simulation = state?.source === "simulation";
  const enabled = online && controls?.available && !pending;
  const feedButton = byId("feed");
  feedButton.disabled = !enabled || !controls.can_feed;
  const wait = controls?.last_feed_ms == null ? 0
    : Math.max(0, Math.ceil((controls.last_feed_ms + 30_000 - state.timestamp_ms) / 1000));
  text("feed", wait > 0 && online ? `餵食冷卻中 · ${wait} 秒` : "立即餵食");
  text("feed-mode", simulation ? "自動排程" : controls?.available ? "手動控制" : "未接入");
  if (online && controls?.next_feed_ms != null) {
    text("feed-timing", `下次餵食 ${displayTime(controls.next_feed_ms)}`);
  } else {
    text("feed-timing", controls?.available ? "設備可接受手動指令" : "等待設備控制器接入");
  }
  const lightLabel = controls?.light_on == null || !online
    ? "狀態未知"
    : `${controls.light_on ? "燈光開啟" : "燈光關閉"} · ${controls.light_mode === "auto" ? "自動" : "手動"}`;
  text("light-status", lightLabel);
  for (const button of document.querySelectorAll("[data-light]")) {
    button.disabled = !enabled || (button.dataset.light === "auto" && !simulation);
    button.setAttribute("aria-pressed", String(button.dataset.light === controls?.light_mode));
  }
  byId("demo").hidden = !simulation;
  for (const button of document.querySelectorAll("[data-demo]")) button.disabled = !online || pending;
  text("clarity-toggle", controls?.low_clarity ? "恢復透光" : "降低透光");
}

function renderFishSelection(fish, fresh) {
  const keys = JSON.stringify(fish.map((track) => track.id));
  if (keys !== fishOptions) {
    const previous = fishSelect.value;
    fishSelect.replaceChildren(new Option("全部魚群", ""), ...fish.map((track) => new Option(track.id, track.id)));
    fishSelect.value = fish.some((track) => track.id === previous) ? previous : "";
    fishOptions = keys;
  }
  fishSelect.disabled = !fish.length;
  const selected = fish.find((track) => track.id === fishSelect.value);
  tank?.selectFish(selected?.id ?? "");
  byId("fish-detail").hidden = !selected;
  if (!selected) return;

  const tracking = fresh && selected.status === "tracked";
  text("fish-detail-id", selected.id);
  text("fish-detail-status", !fresh ? "觀測已中斷" : tracking ? "追蹤中" : "追蹤遺失");
  const speed = tracking && selected.velocity ? Math.hypot(...selected.velocity) : null;
  text("fish-detail-speed", `游速 ${valueWithUnit(speed, "cm/s")}`);
  const pos = selected.position;
  const label = tracking ? "座標" : "最後位置";
  text("fish-detail-position", pos ? `${label} ${pos.x.toFixed(1)}, ${pos.y.toFixed(1)}, ${pos.z.toFixed(1)} cm` : `${label} —`);
}

function renderState() {
  const telemetry = online ? state?.telemetry : null;
  for (const [id, metric, decimals] of sensorCards) {
    text(id, valueWithUnit(telemetry?.[metric], "", decimals));
  }
  const clarityAlert = online && Boolean(state?.clarity_alert);
  byId("clarity-alert").hidden = !clarityAlert;
  byId("clarity-metric").dataset.alert = String(clarityAlert);
  const fresh = online && state?.tracking_fresh;
  const fish = state?.fish ?? [];
  const count = fish.filter((track) => track.status === "tracked").length;
  text("fish-count", fresh ? count : "—");
  text("fish-total", fish.length ? `/ ${fish.length} 尾` : "尾");
  const loss = fresh && count < fish.length;
  const meter = byId("track-meter");
  meter.dataset.loss = String(loss);
  meter.firstElementChild.style.width = fresh && fish.length ? `${count / fish.length * 100}%` : "0%";
  const status = byId("tank-status");
  status.dataset.state = fresh ? (loss ? "loss" : "fresh") : "stale";
  const statusLabel = fresh ? `${count} 尾追蹤中${loss ? ` · ${fish.length - count} 尾遺失` : ""}`
    : online ? "等待有效觀測" : state ? "即時連線中斷" : "等待魚群資料";
  if (status.lastElementChild.textContent !== statusLabel) status.lastElementChild.textContent = statusLabel;
  const fedAt = state?.controls.last_feed_ms;
  byId("feeding-label").hidden = !(online && state?.source === "simulation" && fedAt != null && state.timestamp_ms - fedAt < 14_000);
  byId("reset-view").disabled = !tank;
  if (state?.tank) text("tank-size", `${state.tank.width} × ${state.tank.height} × ${state.tank.depth} cm`);
  if (state) text("source", state.source === "simulation" ? "模擬模式" : "硬體模式");
  renderFishSelection(fish, fresh);
  updateControls();
}

async function loadTank() {
  tankRequested = true;
  try {
    const { createTank } = await import("./tank.js");
    tank = createTank(byId("tank"), state.tank);
    tank.setView(selectedView);
    tank.update(state);
    if (!online) tank.setOffline();
  } catch (error) {
    console.warn("Unable to initialize aquarium WebGL", error);
    byId("tank-fallback").hidden = false;
    byId("tank-guide").hidden = true;
    for (const button of document.querySelectorAll("[data-view]")) button.disabled = true;
  }
  renderState();
}

function acceptState(next) {
  if (state && next.timestamp_ms < state.timestamp_ms) return;
  state = next;
  if (!tankRequested) void loadTank();
  if (online) tank?.update(state);
  renderState();
}

function setConnection(connected, label) {
  online = connected;
  const indicator = byId("connection");
  indicator.classList.toggle("online", connected);
  indicator.lastElementChild.textContent = label;
  if (!connected) tank?.setOffline();
  renderState();
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/live`);
  let timeout;
  function startTimeout() {
    clearTimeout(timeout);
    timeout = setTimeout(() => socket.close(), 5000);
  }
  startTimeout();
  socket.addEventListener("message", (event) => {
    startTimeout();
    if (!online) setConnection(true, "即時連線");
    retryDelay = 600;
    acceptState(JSON.parse(event.data));
  });
  socket.addEventListener("close", () => {
    clearTimeout(timeout);
    setConnection(false, "重新連線中");
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 6000);
  });
}

function renderHistory() {
  if (!history) return;
  trendChart.render(history, byId("metric").value);
  for (const [id, metric] of sensorCards) drawSparkline(byId(`spark-${id}`), history, metric);
  const { summary, feed, events } = history;
  text("avg-speed", valueWithUnit(summary.speed_cm_s, "cm/s"));
  text("low-speed", valueWithUnit(summary.low_speed_pct, "%", 0));
  const zones = [
    ["左側", summary.left_count ?? 0],
    ["中間", summary.middle_count ?? 0],
    ["右側", summary.right_count ?? 0],
  ];
  const largest = Math.max(...zones.map(([, count]) => count));
  text("favorite-zone", largest ? zones.filter(([, count]) => count === largest).map(([zone]) => zone).join("、") : "—");

  if (feed) {
    const rows = [
      ["平均游速", "speed_cm_s", "cm/s"],
      ["魚群平均間距", "spacing_cm", "cm"],
      ["投餌區平均魚數", "near_feed", "尾"],
    ];
    const time = displayTime(feed.timestamp_ms);
    const elapsed = Math.max(0, Math.min(15, (history.end_ms - feed.timestamp_ms) / 1000));
    const nearFeedDelta = Number.isFinite(feed.before.near_feed) && Number.isFinite(feed.after.near_feed)
      ? feed.after.near_feed - feed.before.near_feed : null;
    let comparison = "";
    if (feed.collecting) {
      comparison = `<progress class="feed-progress" max="15" value="${elapsed}" aria-label="餵食後觀測進度"></progress>`;
    } else if (nearFeedDelta !== null) {
      const sign = nearFeedDelta > 0 ? "+" : "";
      comparison = `<div class="feed-summary">投餌區平均魚數變化 <strong>${sign}${nearFeedDelta.toFixed(1)} 尾</strong></div>`;
    }
    byId("response").innerHTML = `<p>${time} · ${feed.collecting ? "正在收集餵食後資料" : "最近一次餵食"}</p>${comparison}
      <table><thead><tr><th scope="col">指標</th><th scope="col">餵食前</th><th scope="col">餵食後</th></tr></thead>
      <tbody>${rows.map(([label, key, unit]) => `<tr><th scope="row">${label}</th>
        <td>${valueWithUnit(feed.before[key], unit)}</td><td>${valueWithUnit(feed.after[key], unit)}</td></tr>`).join("")}</tbody></table>
      <p>有效觀測樣本　餵前 ${feed.before.samples} · 餵後 ${feed.after.samples}</p>`;
  } else {
    byId("response").innerHTML = '<div class="empty"><strong>等待首次餵食</strong><p>啟動餵食後，這裡會顯示魚群的活動與聚集變化。</p></div>';
  }

  const list = byId("events");
  list.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("li");
    empty.textContent = "這段時間尚無設備事件";
    list.append(empty);
  }
  for (const event of events) {
    const item = document.createElement("li");
    item.dataset.action = event.action;
    const eventLabel = document.createElement("span");
    const action = event.action === "feed" ? "餵食" : event.enabled ? "燈光開啟" : "燈光關閉";
    eventLabel.textContent = `${action} · ${event.origin === "auto" ? "自動" : "手動"}`;
    const time = document.createElement("time");
    time.dateTime = new Date(event.timestamp_ms).toISOString();
    time.textContent = displayTime(event.timestamp_ms);
    item.append(eventLabel, time);
    list.append(item);
  }
}

async function loadHistory() {
  if (loadingRange === selectedRange) return;
  loadingRange = selectedRange;
  const requestId = ++historyRequest;
  try {
    const response = await fetch(`/api/history?range=${selectedRange}`, { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error("History request failed");
    const result = await response.json();
    if (requestId !== historyRequest) return;
    history = result;
    renderHistory();
  } catch {
    if (requestId === historyRequest && !history) trendChart.render(null, byId("metric").value, "暫時無法讀取歷史資料");
  } finally {
    if (requestId === historyRequest) loadingRange = undefined;
  }
}

async function command(path, method, body, success) {
  if (pending) return;
  pending = true;
  updateControls();
  const feedback = byId("feedback");
  feedback.dataset.error = "false";
  feedback.textContent = "正在送出指令…";
  try {
    const response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "指令執行失敗");
    acceptState(result);
    feedback.textContent = success;
    void loadHistory();
  } catch (error) {
    feedback.dataset.error = "true";
    feedback.textContent = error instanceof TypeError ? "連線失敗，請確認即時連線" : error.message;
  } finally {
    pending = false;
    updateControls();
  }
}

for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => {
    selectedView = button.dataset.view;
    for (const item of document.querySelectorAll("[data-view]")) {
      item.setAttribute("aria-pressed", String(item === button));
    }
    tank?.setView(selectedView);
  });
}
byId("reset-view").addEventListener("click", () => tank?.setView(selectedView));
fishSelect.addEventListener("change", renderState);
for (const button of document.querySelectorAll("[data-range]")) {
  button.addEventListener("click", () => {
    selectedRange = button.dataset.range;
    for (const item of document.querySelectorAll("[data-range]")) {
      item.setAttribute("aria-pressed", String(item === button));
    }
    history = undefined;
    trendChart.render(history, byId("metric").value);
    void loadHistory();
  });
}
byId("feed").addEventListener("click", () => command("/api/control/feed", "POST", undefined, "餵食已執行，正在記錄反應。"));
for (const button of document.querySelectorAll("[data-light]")) {
  button.addEventListener("click", () => command("/api/control/light", "PUT", { mode: button.dataset.light }, "照明設定已更新。"));
}
const demoMessages = { reset: "已重跑模擬，歷史資料已保留。", clarity: "透光度情境已切換。", lost: "正在模擬兩尾魚追蹤遺失。" };
for (const button of document.querySelectorAll("[data-demo]")) {
  const action = button.dataset.demo;
  button.addEventListener("click", () => command("/api/demo", "POST", { action }, demoMessages[action]));
}
byId("metric").addEventListener("change", () => trendChart.render(history, byId("metric").value));
new ResizeObserver(() => {
  if (history) trendChart.render(history, byId("metric").value);
}).observe(byId("chart"));

const navigation = [...document.querySelectorAll(".rail nav a")];
function updateNavigation() {
  const active = navigation.find((link) => link.hash === location.hash) ?? navigation[0];
  for (const link of navigation) {
    if (link === active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}
window.addEventListener("hashchange", updateNavigation);
updateNavigation();
trendChart.render(history, byId("metric").value);
connect();
await loadHistory();
setInterval(loadHistory, 2000);
