const metrics = {
  speed_cm_s: { label: "平均游速", unit: "cm/s", padding: 2 },
  spacing_cm: { label: "魚群平均間距", unit: "cm", padding: 4 },
  temperature_c: { label: "水溫", unit: "°C", padding: 0.1 },
  transmittance_pct: { label: "透光度", unit: "%", padding: 2 },
};
const shortTime = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const exactTime = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const dateTime = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function displayTime(timestamp) {
  return exactTime.format(timestamp);
}

export function createTrendChart(container) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const empty = document.createElement("p");
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.setAttribute("role", "status");
  tooltip.hidden = true;
  container.append(svg, empty, tooltip);

  const left = 43;
  const right = 13;
  const top = 22;
  const bottom = 154;
  let points = [];
  let activeTime = null;
  let currentMetric;
  let currentStep;
  let chart;

  function nearestIndex(time) {
    return points.reduce((nearest, point, index) =>
      Math.abs(point.timestamp_ms - time) < Math.abs(points[nearest].timestamp_ms - time) ? index : nearest, 0);
  }

  function select(index) {
    const point = points[index];
    const cursor = svg.querySelector(".chart-cursor");
    if (!point || !cursor) return;
    activeTime = point.timestamp_ms;
    const x = chart.x(activeTime);
    cursor.removeAttribute("display");
    cursor.setAttribute("transform", `translate(${x} 0)`);
    cursor.lastElementChild.setAttribute("cy", chart.y(point[currentMetric]));
    const value = `${chart.clock(activeTime)}　${point[currentMetric].toFixed(2)} ${metrics[currentMetric].unit}`;
    if (tooltip.textContent !== value) tooltip.textContent = value;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(5, Math.min(x + 9, chart.width - tooltip.offsetWidth - 5))}px`;
  }

  function clearSelection() {
    activeTime = null;
    tooltip.hidden = true;
    svg.querySelector(".chart-cursor")?.setAttribute("display", "none");
  }

  function selectAtPointer(event) {
    if (!points.length || !chart) return;
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * chart.width;
    if (x < left || x > chart.width - right) {
      clearSelection();
      return;
    }
    const time = chart.start + (x - left) / (chart.width - left - right) * chart.span;
    select(nearestIndex(time));
  }

  container.addEventListener("pointerdown", selectAtPointer);
  container.addEventListener("pointermove", (event) => {
    if (event.pointerType === "mouse") selectAtPointer(event);
  });
  container.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") clearSelection();
  });
  container.addEventListener("blur", clearSelection);
  container.addEventListener("keydown", (event) => {
    if (!points.length) return;
    if (event.key === "Escape") {
      clearSelection();
      return;
    }
    const direction = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!direction) return;
    event.preventDefault();
    const index = activeTime === null ? points.length - 1 : nearestIndex(activeTime) + direction;
    select(Math.max(0, Math.min(points.length - 1, index)));
  });

  function render(history, metric, message = "正在讀取歷史資料…") {
    if (metric !== currentMetric || history?.step_ms !== currentStep) clearSelection();
    currentMetric = metric;
    currentStep = history?.step_ms;
    points = history?.series.filter((point) => Number.isFinite(point[metric])) ?? [];
    const { label, unit, padding } = metrics[metric];
    svg.toggleAttribute("hidden", !points.length);
    empty.hidden = Boolean(points.length);
    if (!points.length) {
      empty.textContent = history ? "這段時間尚無有效觀測" : message;
      container.removeAttribute("tabindex");
      container.removeAttribute("aria-label");
      clearSelection();
      return;
    }

    container.tabIndex = 0;
    container.setAttribute("role", "group");
    container.setAttribute("aria-label", `${label}歷史趨勢，使用左右方向鍵查看資料點`);
    const width = Math.max(220, container.clientWidth);
    const start = history.start_ms;
    const span = history.end_ms - start;
    const x = (time) => left + (time - start) / span * (width - left - right);
    const values = points.map((point) => point[metric]);
    const min = Math.max(0, Math.min(...values) - padding);
    const max = Math.max(...values) + padding;
    const y = (value) => top + (max - value) / (max - min) * (bottom - top);
    chart = { x, y, width, start, span, clock: currentStep === 900_000 ? dateTime.format : exactTime.format };

    const grid = Array.from({ length: 4 }, (_, index) => {
      const value = max - (max - min) * index / 3;
      const row = y(value);
      return `<line x1="${left}" x2="${width - right}" y1="${row}" y2="${row}" stroke="#dce9e0"/>
        <text x="${left - 7}" y="${row + 4}" text-anchor="end">${value.toFixed(1)}</text>`;
    }).join("");
    const clock = currentStep === 1000 ? exactTime : currentStep === 900_000 ? dateTime : shortTime;
    const labels = ["start", "middle", "end"].map((anchor, index) => {
      const time = start + span * index / 2;
      return `<text x="${x(time)}" y="173" text-anchor="${anchor}">${clock.format(time)}</text>`;
    }).join("");
    const segments = [];
    for (const point of points) {
      const last = segments.at(-1)?.at(-1);
      if (!last || point.timestamp_ms - last.timestamp_ms > currentStep * 1.65) segments.push([]);
      segments.at(-1).push(point);
    }
    const traces = segments.map((segment) => {
      const coordinates = segment.map((point) => `${x(point.timestamp_ms).toFixed(2)},${y(point[metric]).toFixed(2)}`);
      const first = x(segment[0].timestamp_ms);
      const last = x(segment.at(-1).timestamp_ms);
      return `<path d="M${first},${bottom} L${coordinates.join(" L")} L${last},${bottom} Z" fill="url(#trend-fill)"/>
        <polyline points="${coordinates.join(" ")}" fill="none" stroke="#21845c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="${last}" cy="${y(segment.at(-1)[metric])}" r="2.5" fill="#21845c"/>`;
    }).join("");
    const feeds = new Set(history.events.filter((event) => event.action === "feed").map((event) => event.timestamp_ms));
    if (history.feed) feeds.add(history.feed.timestamp_ms);
    const markers = [...feeds].filter((time) => time >= start && time <= history.end_ms).map((time) =>
      `<line x1="${x(time)}" x2="${x(time)}" y1="${top}" y2="${bottom}" stroke="#af792d" stroke-dasharray="4 3"><title>餵食 ${displayTime(time)}</title></line>`).join("");
    svg.setAttribute("viewBox", `0 0 ${width} 184`);
    svg.innerHTML = `<defs><linearGradient id="trend-fill" x2="0%" y2="100%"><stop stop-color="#a0d7b5" stop-opacity=".66"/><stop offset="1" stop-color="#e7f4e9" stop-opacity=".18"/></linearGradient></defs>
      <g fill="#526b5f" font-family="monospace" font-size="10">${grid}${labels}<text x="3" y="12">${unit}</text></g>
      ${traces}${markers}<g class="chart-cursor" display="none">
        <line y1="${top}" y2="${bottom}" stroke="#579176" stroke-dasharray="3 3"/>
        <circle r="4" fill="#196849" stroke="white" stroke-width="1.4"/>
      </g>`;
    if (activeTime !== null) select(nearestIndex(activeTime));
  }

  return { render };
}

export function drawSparkline(element, history, metric) {
  const values = (history?.series ?? []).map((point) => point[metric]).filter(Number.isFinite).slice(-30);
  if (values.length < 2) {
    element.replaceChildren();
    return;
  }
  const min = Math.min(...values);
  const span = Math.max(Math.max(...values) - min, metrics[metric].padding * 2);
  const coordinates = values.map((value, index) => `${index / (values.length - 1) * 91},${24 - (value - min) / span * 18}`);
  element.innerHTML = `<polygon points="0,31 ${coordinates.join(" ")} 91,31" fill="currentColor" opacity=".10"/>
    <polyline points="${coordinates.join(" ")}" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linejoin="round"/>`;
}
