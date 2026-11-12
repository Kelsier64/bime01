export function drawMarker(context, x, y, color, label) {
  context.fillStyle = color;
  context.strokeStyle = "#102d2b";
  context.lineWidth = 2;
  context.beginPath();
  context.arc(x, y, 5, 0, 2 * Math.PI);
  context.fill();
  context.stroke();
  if (label) {
    context.font = "bold 12px system-ui, sans-serif";
    context.lineWidth = 3;
    context.strokeText(label, x + 8, y - 7);
    context.fillText(label, x + 8, y - 7);
  }
}
