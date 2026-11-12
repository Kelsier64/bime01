// Live camera frame with tracking overlay and the four-corner calibration tool.

const CORNER_NAMES = ["左上", "右上", "右下", "左下"];
const COLORS = ["#ffd166", "#7ee2b8", "#8ecbff", "#f7a1c4"];

export function createLiveView({ frame, overlay, message, stage }) {
  const context = overlay.getContext("2d");
  let objectUrl = null;
  let frameState = null;
  let latestState = null;
  let calibrating = null;
  let onCalibrationChange = () => {};

  function draw() {
    const state = latestState?.camera.status === "live" ? frameState ?? latestState : latestState;
    if (!state) return;
    const width = state?.camera.width;
    const height = state?.camera.height;
    if (!width || !height) return;
    if (overlay.width !== width || overlay.height !== height) {
      overlay.width = width;
      overlay.height = height;
      stage.style.aspectRatio = `${width} / ${height}`;
    }
    context.clearRect(0, 0, width, height);

    const corners = calibrating?.points ?? scaledCorners(state.calibration, width, height);
    if (corners?.length) {
      context.save();
      context.strokeStyle = calibrating ? "#ffd166" : "#ffffffaa";
      context.lineWidth = 2;
      context.setLineDash(calibrating ? [] : [6, 5]);
      context.beginPath();
      corners.forEach(([x, y], index) => (index ? context.lineTo(x, y) : context.moveTo(x, y)));
      if (corners.length === 4) context.closePath();
      context.stroke();
      if (calibrating) {
        context.font = "bold 13px system-ui, sans-serif";
        corners.forEach(([x, y], index) => {
          context.fillStyle = "#ffd166";
          context.beginPath();
          context.arc(x, y, 5, 0, 2 * Math.PI);
          context.fill();
          const right = x > width - 60;
          const below = y < 24;
          label(CORNER_NAMES[index], right ? x - 38 : x + 8, below ? y + 20 : y - 8, "#ffd166");
        });
      }
      context.restore();
    }

    if (calibrating || !state.camera || state.camera.status !== "live") return;
    for (const fish of state.tracking.fish) {
      const color = COLORS[(fish.id - 1) % COLORS.length];
      if (fish.trail.length > 1) {
        context.strokeStyle = color;
        context.globalAlpha = 0.7;
        context.lineWidth = 2;
        context.beginPath();
        fish.trail.forEach(([x, y], index) => (index ? context.lineTo(x, y) : context.moveTo(x, y)));
        context.stroke();
        context.globalAlpha = 1;
      }
      context.fillStyle = color;
      context.strokeStyle = "#102d2b";
      context.lineWidth = 2;
      context.beginPath();
      context.arc(fish.x_px, fish.y_px, 6, 0, 2 * Math.PI);
      context.fill();
      context.stroke();
      const speed = Number.isFinite(fish.speed_cm_s) ? ` · ${fish.speed_cm_s.toFixed(1)} cm/s` : "";
      label(`#${fish.id}${speed}`, fish.x_px + 10, fish.y_px - 10, color);
    }
  }

  function label(text, x, y, color) {
    context.font = "bold 13px system-ui, sans-serif";
    context.lineWidth = 4;
    context.strokeStyle = "#102d2b";
    context.strokeText(text, x, y);
    context.fillStyle = color;
    context.fillText(text, x, y);
  }

  frame.addEventListener("load", () => {
    frame.hidden = false;
    draw();
  });

  overlay.addEventListener("click", (event) => {
    if (!calibrating || calibrating.points.length >= 4) return;
    const rect = overlay.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * overlay.width;
    const y = (event.clientY - rect.top) / rect.height * overlay.height;
    calibrating.points.push([Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
    onCalibrationChange(calibrating.points);
    draw();
  });

  return {
    showFrame(blob, state) {
      frameState = state;
      const previous = objectUrl;
      objectUrl = URL.createObjectURL(blob);
      frame.src = objectUrl;
      if (previous) URL.revokeObjectURL(previous);
    },
    setState(state) {
      latestState = state;
      const status = state.camera.status;
      const text = status === "live" ? ""
        : status === "connecting" ? "正在連接相機…"
        : `相機離線${state.camera.error ? `：${state.camera.error}` : ""}`;
      message.textContent = text;
      message.hidden = !text;
      stage.dataset.status = status;
      if (status !== "live") draw();
    },
    startCalibration(onChange) {
      calibrating = { points: [] };
      onCalibrationChange = onChange;
      stage.dataset.calibrating = "true";
      onChange(calibrating.points);
      draw();
    },
    undoPoint() {
      calibrating?.points.pop();
      onCalibrationChange(calibrating?.points ?? []);
      draw();
    },
    stopCalibration() {
      calibrating = null;
      delete stage.dataset.calibrating;
      draw();
    },
    nextCornerName(count) {
      return CORNER_NAMES[count];
    },
  };
}

function scaledCorners(calibration, width, height) {
  if (!calibration) return null;
  const sx = width / calibration.frame_width;
  const sy = height / calibration.frame_height;
  return calibration.corners.map(([x, y]) => [x * sx, y * sy]);
}
