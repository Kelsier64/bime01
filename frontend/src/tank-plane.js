import { drawMarker } from "./track-overlay.js";

export function createTank(canvas) {
  const context = canvas.getContext("2d");
  const trails = new Map();
  let state;
  let fresh;
  let selected;
  let lastObservation;

  function render() {
    if (!context || !state) return;
    const { width, height, feed_point, feed_radius } = state.tank;
    const viewportWidth = canvas.clientWidth;
    const viewportHeight = canvas.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(viewportWidth * dpr);
    const pixelHeight = Math.round(viewportHeight * dpr);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, viewportWidth, viewportHeight);

    const scale = Math.min((viewportWidth - 28) / width, (viewportHeight - 62) / height);
    if (scale <= 0) return;
    const left = (viewportWidth - width * scale) / 2;
    const top = (viewportHeight - height * scale) / 2;
    const point = ({ x, y }) => [left + (x + width / 2) * scale, top + (height - y) * scale];
    context.fillStyle = "#1b4447";
    context.fillRect(left, top, width * scale, height * scale);
    context.lineWidth = 1;
    context.strokeStyle = "#477172";
    context.beginPath();
    for (let column = 1; column < 8; column++) {
      const x = left + column * width * scale / 8;
      context.moveTo(x, top);
      context.lineTo(x, top + height * scale);
    }
    for (let row = 1; row < 4; row++) {
      const y = top + row * height * scale / 4;
      context.moveTo(left, y);
      context.lineTo(left + width * scale, y);
    }
    context.stroke();
    context.strokeRect(left, top, width * scale, height * scale);
    context.save();
    context.beginPath();
    context.rect(left, top, width * scale, height * scale);
    context.clip();
    context.beginPath();
    context.arc(...point({ x: feed_point[0], y: feed_point[1] }), feed_radius * scale, 0, 2 * Math.PI);
    context.fillStyle = "#d0a44a27";
    context.fill();
    context.restore();

    for (const fish of state.fish) {
      if (!fish.position) continue;
      const tracked = fresh && fish.status === "tracked";
      const active = !selected || selected === fish.id;
      context.globalAlpha = active ? 1 : 0.3;
      const trail = trails.get(fish.id) ?? [];
      if (tracked && trail.length > 1) {
        context.strokeStyle = "#8bdbb291";
        context.lineWidth = 2;
        context.beginPath();
        context.moveTo(...point(trail[0]));
        for (const position of trail.slice(1)) context.lineTo(...point(position));
        context.stroke();
      }
      drawMarker(context, ...point(fish.position), tracked ? "#a2f2c7" : "#a1aaa9",
        fish.id === selected ? fish.id : "");
    }
    context.globalAlpha = 1;
  }

  new ResizeObserver(render).observe(canvas);
  return {
    update(next, isFresh, selectedId) {
      if (!next) return;
      if (!isFresh || state?.run_id !== next.run_id) {
        trails.clear();
        lastObservation = undefined;
      }
      if (isFresh && next.observation_timestamp_ms !== lastObservation) {
        const timestamp = next.observation_timestamp_ms;
        const tracked = new Set();
        for (const fish of next.fish) {
          if (fish.status !== "tracked") continue;
          tracked.add(fish.id);
          const trail = (trails.get(fish.id) ?? []).filter((position) => position.timestamp >= timestamp - 2000);
          trail.push({ ...fish.position, timestamp });
          trails.set(fish.id, trail);
        }
        for (const id of trails.keys()) if (!tracked.has(id)) trails.delete(id);
        lastObservation = timestamp;
      }
      state = next;
      fresh = isFresh;
      selected = selectedId;
      render();
    },
  };
}
