"use strict";

/* Draws a generic faceplate for equipment that has no photograph in the database.
   Units are millimetres: a 19-inch panel is 482.6 wide and one rack unit is 44.45 tall. */

const Faceplate = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const PANEL_W = 482.6;
  const UNIT_H = 44.45;
  const EAR = 16;
  const SIZES = { rj45: [13, 10.5], sfp: [13.5, 8.5], qsfp: [18.5, 8.5], other: [9, 7] };
  const FILL = { rj45: "#11161a", sfp: "#3b4650", qsfp: "#4b5560", other: "#2a3238" };
  const MAX_PORTS = 96;

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }

  /* runs: [[class, count], ...] in the order the ports appear on the device */
  function layout(runs, height) {
    const total = runs.reduce((n, r) => n + r[1], 0);
    const rows = total > 10 && height >= UNIT_H ? 2 : 1;
    const gap = 1.6;
    const groupGap = 7;
    const ports = [];
    let x = 0;
    let budget = MAX_PORTS;
    for (const [kind, count] of runs) {
      const [w, h] = SIZES[kind] || SIZES.other;
      const n = Math.min(count, budget);
      budget -= n;
      const columns = Math.ceil(n / rows);
      for (let i = 0; i < n; i += 1) {
        const column = Math.floor(i / rows);
        const row = i % rows;
        ports.push({ kind, x: x + column * (w + gap), row, w, h });
      }
      x += columns * (w + gap) + groupGap;
      if (budget <= 0) break;
    }
    return { ports, width: Math.max(0, x - groupGap - gap), rows };
  }

  function draw(device, kind, options) {
    const units = Math.max(device.u_height || 1, 0.5);
    const height = units * UNIT_H;
    const rear = Boolean(options && options.rear);
    const svg = el("svg", { viewBox: `0 0 ${PANEL_W} ${height}`, preserveAspectRatio: "none", class: "faceplate", role: "img", "aria-hidden": "true" });
    svg.append(el("rect", { x: 0, y: 0, width: PANEL_W, height, fill: rear ? "#2b3136" : "#3a4147" }));
    svg.append(el("rect", { x: EAR, y: 1.2, width: PANEL_W - 2 * EAR, height: height - 2.4, rx: 1.5, fill: rear ? "#353c42" : "#4a5259" }));
    // mounting holes on the ears
    for (const cx of [EAR / 2, PANEL_W - EAR / 2]) {
      for (let u = 0; u < units; u += 1) {
        for (const dy of [6.35, 22.2, 38.1]) {
          const cy = u * UNIT_H + dy;
          if (cy < height - 2) svg.append(el("ellipse", { cx, cy, rx: 3.2, ry: 2.4, fill: "#14181b" }));
        }
      }
    }
    let runs = !rear && kind && kind.ports && Array.isArray(kind.ports.i) ? kind.ports.i.slice() : [];
    // a console server's serial ports are its front panel
    if (!rear && kind && kind.ports && kind.ports.cs > 0) runs = [["rj45", kind.ports.cs], ...runs];
    if (runs.length) {
      const { ports, width, rows } = layout(runs, height);
      const available = PANEL_W - 2 * EAR - 60;
      const scale = width > available ? available / width : 1;
      const rowH = 10.5;
      const blockH = rows * rowH + (rows - 1) * 2;
      const top = Math.max(3, (Math.min(height, UNIT_H * 2) - blockH) / 2);
      const group = el("g", { transform: `translate(${EAR + 46} ${top}) scale(${scale} 1)` });
      ports.forEach((p, i) => {
        group.append(el("rect", { x: p.x, y: p.row * (rowH + 2) + (rowH - p.h) / 2, width: p.w, height: p.h, rx: 0.8, fill: FILL[p.kind] || FILL.other, stroke: "#8a949c", "stroke-width": 0.5, "data-port": i, "data-fill": FILL[p.kind] || FILL.other }));
      });
      svg.append(group);
    } else if (!rear) {
      // nothing known about its ports: a plain panel with a vent pattern
      for (let x = EAR + 150; x < PANEL_W - EAR - 20; x += 9) {
        svg.append(el("rect", { x, y: height / 2 - Math.min(10, height / 3), width: 4, height: Math.min(20, height / 1.5), rx: 2, fill: "#2c3338" }));
      }
    }
    if (!rear) {
      svg.append(el("circle", { cx: EAR + 10, cy: Math.min(height / 2, UNIT_H / 2), r: 2.2, fill: "#4fc3a6" }));
    }
    return svg;
  }

  /* Lights the drawn ports from a monitoring source: one letter per port in the device's own order
     (u connected, e connected with errors, d nothing connected, o switched off). Only when the
     number of ports reported matches the number drawn; otherwise there is no telling which is which. */
  const LIGHT = { u: "#3ddc97", e: "#f0b429", o: "#1a1d20" };
  function light(svg, lights) {
    const rects = svg.querySelectorAll("rect[data-port]");
    const usable = typeof lights === "string" && lights.length === rects.length;
    rects.forEach((rect, i) => rect.setAttribute("fill", (usable && LIGHT[lights[i]]) || rect.dataset.fill));
    return usable;
  }

  return { draw, light, UNIT_RATIO: PANEL_W / UNIT_H };
})();
