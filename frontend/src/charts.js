// History trend chart (SVG) and position heatmap (canvas).

export const METRICS = {
  speed: { label: "平均游速", unit: "cm/s", scale: 1, digits: 1 },
  moving: { label: "活動比例", unit: "%", scale: 100, digits: 0 },
  height: { label: "平均高度", unit: "cm", scale: 1, digits: 1 },
  fish: { label: "可見魚數", unit: "尾", scale: 1, digits: 1 },
  lit_ratio: { label: "光照比例", unit: "%", scale: 100, digits: 0 },
};

const clock = new Intl.DateTimeFormat("zh-TW", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayClock = new Intl.DateTimeFormat("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const day = new Intl.DateTimeFormat("zh-TW", { month: "numeric", day: "numeric" });

export const formatDateTime = (ms) => dayClock.format(ms);

export function formatValue(value, metric) {
  const { scale, digits, unit } = METRICS[metric];
  return Number.isFinite(value) ? `${(value * scale).toFixed(digits)} ${unit}` : "—";
}

export function createTrendChart(container) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const empty = document.createElement("p");
  empty.className = "chart-empty";
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  container.append(svg, empty, tooltip);
  const left = 44;
  const right = 12;
  const top = 16;
  const bottom = 170;
  let view = null;

  container.addEventListener("pointermove", (event) => {
    if (!view?.points.length) return;
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * view.width;
    const time = view.start + (x - left) / (view.width - left - right) * view.span;
    const point = view.points.reduce((best, item) =>
      Math.abs(item.ts_ms - time) < Math.abs(best.ts_ms - time) ? item : best);
    tooltip.textContent = `${dayClock.format(point.ts_ms)}　${formatValue(point[view.metric], view.metric)}`;
    tooltip.hidden = false;
    const px = view.x(point.ts_ms) / view.width * rect.width;
    tooltip.style.left = `${Math.max(4, Math.min(px + 8, rect.width - tooltip.offsetWidth - 4))}px`;
    const cursor = svg.querySelector(".cursor");
    cursor.setAttribute("transform", `translate(${view.x(point.ts_ms)} 0)`);
    cursor.removeAttribute("display");
    cursor.querySelector("circle").setAttribute("cy", view.y(point[view.metric] * METRICS[view.metric].scale));
  });
  container.addEventListener("pointerleave", () => {
    tooltip.hidden = true;
    svg.querySelector(".cursor")?.setAttribute("display", "none");
  });

  function render(history, metric, message = "正在讀取歷史資料…") {
    const { scale, unit } = METRICS[metric];
    const points = (history?.series ?? []).filter((point) => Number.isFinite(point[metric]));
    svg.toggleAttribute("hidden", !points.length);
    empty.hidden = Boolean(points.length);
    tooltip.hidden = true;
    if (!points.length) {
      empty.textContent = history ? "這段時間沒有有效觀測" : message;
      view = null;
      return;
    }
    const width = Math.max(260, container.clientWidth);
    const start = history.start_ms;
    const span = history.end_ms - start;
    const step = history.step_ms;
    const x = (time) => left + (time - start) / span * (width - left - right);
    const values = points.map((point) => point[metric] * scale);
    const max = Math.max(...values, metric === "fish" ? 1 : 0) * 1.1 || 1;
    const min = 0;
    const y = (value) => top + (max - value) / (max - min) * (bottom - top);
    view = { points, metric, x, y, width, start, span };

    const grid = [0, 1, 2, 3].map((index) => {
      const value = max - (max - min) * index / 3;
      return `<line x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}" class="grid"/>
        <text x="${left - 6}" y="${y(value) + 4}" text-anchor="end">${value.toFixed(value < 10 ? 1 : 0)}</text>`;
    }).join("");
    const format = span <= 90_000_000 ? clock : day;
    const labels = [0, 0.5, 1].map((fraction, index) => {
      const time = start + span * fraction;
      return `<text x="${x(time)}" y="${bottom + 18}" text-anchor="${["start", "middle", "end"][index]}">${format.format(time)}</text>`;
    }).join("");
    const segments = [];
    for (const point of points) {
      const last = segments.at(-1)?.at(-1);
      if (!last || point.ts_ms - last.ts_ms > step * 1.5) segments.push([]);
      segments.at(-1).push(point);
    }
    const traces = segments.map((segment) => {
      const coords = segment.map((point) => `${x(point.ts_ms + step / 2).toFixed(1)},${y(point[metric] * scale).toFixed(1)}`);
      if (coords.length === 1) {
        const [cx, cy] = coords[0].split(",");
        return `<circle cx="${cx}" cy="${cy}" r="2.5" class="trace-dot"/>`;
      }
      const first = x(segment[0].ts_ms + step / 2);
      const lastX = x(segment.at(-1).ts_ms + step / 2);
      return `<path d="M${first},${bottom} L${coords.join(" L")} L${lastX},${bottom} Z" class="area"/>
        <polyline points="${coords.join(" ")}" class="trace"/>`;
    }).join("");
    const feeds = (history.events ?? []).filter((event) => event.kind === "feed")
      .map((event) => `<line x1="${x(event.ts_ms)}" x2="${x(event.ts_ms)}" y1="${top}" y2="${bottom}" class="feed-marker"><title>餵食 ${dayClock.format(event.ts_ms)}</title></line>`)
      .join("");
    const alerts = (history.events ?? []).filter((event) => event.kind === "alert")
      .map((event) => `<line x1="${x(event.ts_ms)}" x2="${x(event.ts_ms)}" y1="${top}" y2="${bottom}" class="alert-marker"><title>${event.detail.message ?? "警示"}</title></line>`)
      .join("");
    svg.setAttribute("viewBox", `0 0 ${width} ${bottom + 26}`);
    svg.innerHTML = `<g class="axis">${grid}${labels}<text x="2" y="11">${unit}</text></g>${traces}${feeds}${alerts}
      <g class="cursor" display="none"><line y1="${top}" y2="${bottom}"/><circle r="4"/></g>`;
  }

  return { render };
}

export function drawHeatmap(canvas, heat) {
  const context = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#eef4f0";
  context.fillRect(0, 0, width, height);
  if (!heat) return;
  const { columns, rows, counts } = heat;
  const max = Math.max(...counts);
  const cellWidth = width / columns;
  const cellHeight = height / rows;
  counts.forEach((count, index) => {
    if (!count) return;
    const level = count / max;
    context.fillStyle = `rgba(214, 112, 26, ${0.08 + level * 0.85})`;
    context.fillRect((index % columns) * cellWidth + 1, Math.floor(index / columns) * cellHeight + 1, cellWidth - 2, cellHeight - 2);
  });
  if (!max) {
    context.fillStyle = "#6b8077";
    context.font = "13px system-ui, sans-serif";
    context.textAlign = "center";
    context.fillText("尚無位置資料", width / 2, height / 2);
  }
}
