"use strict";

const main = document.getElementById("main");
const bar = document.getElementById("bar");
const $ = (id) => document.getElementById(id);

const state = { deviceSort: { key: "name", dir: 1 }, deviceFilter: "", dash: null, portPick: {}, version: "", view: "dashboard", racks: [], unplaced: [], rackId: null, face: "front", selectedId: null, newFound: 0, live: {}, discovery: null, showIgnored: false, consoles: [], kinds: {}, kvms: [], kvmKinds: {}, openPort: null, screenPort: null, health: {}, monitoring: false, alertCount: 0, monitoringView: null, allSensors: false, graphPeriod: "day", readouts: {}, readout: "none", portSel: null, cables: [], serviceKinds: {}, serviceLevels: {}, cableSuggestions: 0, showCables: true, cablesView: null };
try { state.showCables = localStorage.getItem("rackmap-cables") !== "off"; } catch (err) { /* stays on */ }
try { state.readout = localStorage.getItem("rackmap-readout") || "none"; } catch (err) { /* private browsing: the choice just isn't remembered */ }

/* ---------- talking to the server ---------- */

async function api(path, body, method) {
  const options = { credentials: "same-origin" };
  if (method) options.method = method;
  if (body !== undefined) {
    options.method = method || "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("Can't reach rackmap. Check that it is running and try again.");
  }
  let data = {};
  try { data = await response.json(); } catch (err) { /* empty body */ }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.";
    const error = new Error(detail);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

/* ---------- small DOM helpers ---------- */

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(templateId) {
  main.replaceChildren($(templateId).content.cloneNode(true));
}

function message(text) {
  main.replaceChildren(h("p", { class: "loading", text }));
}

function setError(id, text) {
  const el = $(id);
  el.textContent = text || "";
  el.hidden = !text;
}

const uText = (u) => `U${Number(u).toLocaleString("en", { maximumFractionDigits: 1 })}`;

function span(device) {
  const top = device.position_u + device.u_height - 1;
  return top > device.position_u ? `${uText(device.position_u)}–${uText(top)}` : uText(device.position_u);
}

/* ---------- account screens ---------- */

function wireAccountForm(onSubmit) {
  const form = $("form");
  const button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("error", "");
    button.disabled = true;
    try {
      await onSubmit(Object.fromEntries(new FormData(form)));
    } catch (err) {
      setError("error", err.message);
    } finally {
      button.disabled = false;
    }
  });
}

function setupScreen() {
  bar.hidden = true;
  show("t-setup");
  wireAccountForm(async ({ name, password, confirm }) => {
    if (!name.trim()) throw new Error("Enter a name for the account.");
    if (password.length < 10) throw new Error("Use a password of at least 10 characters.");
    if (password !== confirm) throw new Error("The two passwords don't match.");
    await api("/api/setup", { name: name.trim(), password });
    await start();
  });
}

function loginScreen(sso) {
  bar.hidden = true;
  show("t-login");
  if (sso) {
    $("sso-button").textContent = sso;
    $("sso-box").hidden = false;
  }
  const problem = new URLSearchParams(location.search).get("sso_error");
  if (problem) {
    setError("error", problem);
    history.replaceState(null, "", "/");
  }
  wireAccountForm(async ({ name, password }) => {
    if (!name.trim() || !password) throw new Error("Enter your name and password.");
    const row = $("code-row");
    try {
      await api("/api/login", { name: name.trim(), password, code: row.hidden ? "" : $("form").elements.code.value.trim() });
    } catch (err) {
      if (err.data && err.data.needs_code && row.hidden) {
        row.hidden = false;
        $("form").elements.code.focus();
        return;
      }
      throw err;
    }
    await start();
  });
}

/* ---------- rack view ---------- */

const currentRack = () => state.racks.find((r) => r.id === state.rackId) || null;

function findDevice(id) {
  for (const rack of state.racks) {
    const hit = rack.devices.find((d) => d.id === id);
    if (hit) return hit;
  }
  return state.unplaced.find((d) => d.id === id) || null;
}

async function reload() {
  const overview = await api("/api/overview");
  state.racks = overview.racks;
  state.unplaced = overview.unplaced;
  if (!state.racks.some((r) => r.id === state.rackId)) state.rackId = state.racks.length ? state.racks[0].id : null;
  if (state.selectedId !== null && !findDevice(state.selectedId)) state.selectedId = null;
  $("who-name").textContent = overview.user;
  state.newFound = overview.new_found || 0;
  state.consoles = overview.console_servers || [];
  state.kinds = overview.console_kinds || {};
  state.kvms = overview.kvms || [];
  state.kvmKinds = overview.kvm_kinds || {};
  state.monitoring = Boolean(overview.monitoring);
  state.cables = overview.cables || [];
  state.serviceKinds = overview.service_kinds || {};
  state.wiring = Boolean(overview.wiring);
  state.serviceLevels = overview.service_levels || {};
  renderNav();
  if (DRAWER_VIEWS.includes(state.view)) render();
}

const VIEWS = ["dashboard", "devices", "racks", "map", "discovery", "consoles", "monitoring", "settings"];
const DRAWER_VIEWS = ["dashboard", "devices", "racks", "map"];  // pages where picking equipment slides its details in

function renderNav() {
  for (const view of VIEWS) {
    if (state.view === view) $(`nav-${view}`).setAttribute("aria-current", "page");
    else $(`nav-${view}`).removeAttribute("aria-current");
  }
  const alertBadge = $("nav-alerts");
  alertBadge.hidden = !state.alertCount;
  alertBadge.textContent = state.alertCount;
  alertBadge.setAttribute("aria-label", `${state.alertCount} needing attention`);
  const badge = $("nav-badge");
  badge.hidden = state.newFound === 0;
  badge.textContent = state.newFound;
  badge.setAttribute("aria-label", `${state.newFound} new`);
}

function render() {
  if (state.view === "devices") { if ($("device-rows")) renderDevices(); renderDetail(); paintLive(); return; }
  if (state.view === "dashboard") { if ($("dash")) renderDashboard(); renderDetail(); return; }
  if (state.view === "map") { if ($("map-box")) drawMap(); renderDetail(); return; }
  if (!$("rack-tabs")) { renderDetail(); return; }
  renderTabs();
  const rack = currentRack();
  $("no-racks").hidden = state.racks.length > 0;
  $("workspace").hidden = state.racks.length === 0 && state.unplaced.length === 0;
  document.querySelector(".rack-col").hidden = !rack;
  if (rack) renderRack(rack);
  renderUnplaced();
  renderDetail();
  paintLive();
}

function renderTabs() {
  $("rack-tabs").replaceChildren(...state.racks.map((rack) => h("button", {
    type: "button", role: "tab", class: "tab", "aria-selected": rack.id === state.rackId ? "true" : "false",
    onclick: () => { state.rackId = rack.id; state.selectedId = null; render(); },
  }, rack.name)));
}

/* What a device looks like from the side of the rack being viewed. */
function deviceBody(device, viewedFace) {
  const kind = device.kind;
  const sameSide = device.face === viewedFace;
  if (!sameSide && !device.full_depth) return null; // half-depth, mounted on the other side
  const showRear = !sameSide;
  const wanted = showRear ? "rear" : "front";
  const hasImage = kind && (showRear ? kind.has_rear_image : kind.has_front_image);
  const drawn = () => Faceplate.draw(device, kind, { rear: showRear });
  if (!hasImage) return drawn();
  const img = h("img", { src: `/api/catalog/${encodeURIComponent(kind.slug)}/image/${wanted}`, alt: "", loading: "lazy", draggable: "false" });
  img.addEventListener("error", () => img.replaceWith(drawn()), { once: true });
  return img;
}

function renderRack(rack) {
  $("rack-name").textContent = rack.name;
  const used = rack.devices.reduce((n, d) => n + d.u_height, 0);
  $("rack-sub").textContent = `${rack.height_u}U · ${rack.devices.length} ${rack.devices.length === 1 ? "item" : "items"} · ${Number(used.toFixed(1))}U used`;
  $("face-front").setAttribute("aria-pressed", String(state.face === "front"));
  $("face-rear").setAttribute("aria-pressed", String(state.face === "rear"));

  const rackEl = $("rack");
  rackEl.style.setProperty("--units", rack.height_u);

  const units = [];
  for (let u = rack.height_u; u >= 1; u -= 1) units.push(h("li", {}, String(u)));
  $("units").replaceChildren(...units);

  const bay = $("bay");
  const slots = [];
  for (let u = rack.height_u; u >= 1; u -= 1) {
    slots.push(h("button", {
      type: "button", class: "slot", "aria-label": `Add equipment at ${uText(u)}`, title: `Add equipment at ${uText(u)}`,
      onclick: () => openDeviceDialog(null, { rack_id: rack.id, position_u: u, face: state.face }),
    }));
  }
  const blocks = [];
  const labels = [];
  for (const device of rack.devices) {
    const body = deviceBody(device, state.face);
    const place = (el) => {
      el.style.setProperty("--pos", device.position_u);
      el.style.setProperty("--h", device.u_height);
      return el;
    };
    const select = () => { state.selectedId = device.id; render(); };
    const where = `${span(device)}${device.face === "rear" ? ", rear" : ""}`;
    if (body) {
      blocks.push(place(h("button", {
        type: "button", class: `device${device.id === state.selectedId ? " selected" : ""}`,
        "aria-label": `${device.name}, ${where}`, title: device.name, onclick: select, "data-device": String(device.id),
      }, body)));
    } else {
      blocks.push(place(h("button", {
        type: "button", class: `device other-side${device.id === state.selectedId ? " selected" : ""}`,
        "aria-label": `${device.name}, ${where}, mounted on the other side`, title: `${device.name} (other side)`, onclick: select,
      })));
    }
    labels.push(place(h("li", { class: device.id === state.selectedId ? "selected" : "" },
      h("button", { type: "button", class: "label", onclick: select },
        h("span", { class: "label-name" }, h("span", { class: "dot", "data-live": String(device.id), hidden: true }), device.name),
        h("span", { class: "label-meta" }, [where, device.ip].filter(Boolean).join(" · "), h("span", { class: "readout", "data-readout": String(device.id), hidden: true }))))));
  }
  bay.replaceChildren(...slots, ...blocks);
  $("labels").replaceChildren(...labels);
  drawCables(rack);
}

/* ---------- cables drawn beside the rack ---------- */

const CABLE_COLOURS = ["#2e9e7f", "#c77700", "#3b7ddd", "#b0489b", "#7a8a2e", "#d2553f"];
const cableText = (c) => `${c.a_name}${c.a_port ? ` ${c.a_port}` : ""} to ${c.b_name}${c.b_port ? ` ${c.b_port}` : ""}${c.note ? ` (${c.note})` : ""}`;

function drawCables(rack) {
  const layer = $("cable-layer");
  if (!layer) return;
  const rackEl = $("rack");
  const placed = new Map(rack.devices.map((d) => [d.id, d]));
  const cables = state.showCables ? state.cables.filter((c) => placed.has(c.a_device_id) && placed.has(c.b_device_id)) : [];
  $("cables-show").checked = state.showCables;
  $("cables-box").hidden = state.cables.length === 0;
  layer.replaceChildren();
  if (!cables.length) { rackEl.style.setProperty("--gutter", "0px"); return; }
  const unit = parseFloat(getComputedStyle(rackEl).getPropertyValue("--unit")) || 40;
  // where each end leaves its equipment: ends sharing a device are spread across its height
  const ends = new Map();
  for (const c of cables) for (const id of [c.a_device_id, c.b_device_id]) ends.set(id, (ends.get(id) || 0) + 1);
  const used = new Map();
  const yFor = (id) => {
    const d = placed.get(id);
    const n = ends.get(id);
    const i = used.get(id) || 0;
    used.set(id, i + 1);
    const top = (rack.height_u - (d.position_u - 1) - d.u_height) * unit;
    return top + ((i + 1) / (n + 1)) * d.u_height * unit;
  };
  const lines = cables.map((c) => ({ c, y1: yFor(c.a_device_id), y2: yFor(c.b_device_id) }))
    .map((l) => ({ ...l, top: Math.min(l.y1, l.y2), bottom: Math.max(l.y1, l.y2) }))
    .sort((p, q) => (p.bottom - p.top) - (q.bottom - q.top));
  // shortest cables hug the rack; a longer one moves out to the first lane it doesn't cross anything in
  const lanes = [];
  for (const l of lines) {
    let lane = 0;
    while ((lanes[lane] || []).some((o) => o.top <= l.bottom + 4 && l.top <= o.bottom + 4)) lane += 1;
    (lanes[lane] = lanes[lane] || []).push(l);
    l.lane = lane;
  }
  const step = unit < 30 ? 6 : 9;
  const width = 10 + lanes.length * step;
  rackEl.style.setProperty("--gutter", `${width}px`);
  layer.setAttribute("viewBox", `0 0 ${width} ${rack.height_u * unit}`);
  layer.setAttribute("width", width);
  layer.setAttribute("height", rack.height_u * unit);
  const NS = "http://www.w3.org/2000/svg";
  lines.forEach((l, i) => {
    const x = 6 + l.lane * step;
    const s = l.y2 > l.y1 ? 1 : -1;
    const r = Math.min(4, Math.abs(l.y2 - l.y1) / 2);
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", `M0 ${l.y1} H${x - r} Q${x} ${l.y1} ${x} ${l.y1 + s * r} V${l.y2 - s * r} Q${x} ${l.y2} ${x - r} ${l.y2} H0`);
    const mine = state.selectedId !== null && (l.c.a_device_id === state.selectedId || l.c.b_device_id === state.selectedId);
    path.setAttribute("class", `cable${mine ? " mine" : state.selectedId !== null ? " other" : ""}`);
    path.setAttribute("stroke", CABLE_COLOURS[i % CABLE_COLOURS.length]);
    const title = document.createElementNS(NS, "title");
    title.textContent = cableText(l.c);
    path.append(title);
    layer.append(path);
  });
}

function renderUnplaced() {
  $("unplaced-none").hidden = state.unplaced.length > 0;
  $("unplaced-list").replaceChildren(...state.unplaced.map((device) => h("li", {},
    h("button", { type: "button", class: `row-button${device.id === state.selectedId ? " selected" : ""}`, onclick: () => { state.selectedId = device.id; render(); } },
      h("span", { class: "label-name", text: device.name }),
      h("span", { class: "label-meta", text: [device.manufacturer, device.model].filter(Boolean).join(" ") || "No model recorded" })))));
}

/* ---------- live status ---------- */

let liveTimer = null;

function describeSince(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86400)} days`;
}

/* The reading shown beside each piece of equipment in the rack view, when one is switched on. */
function readoutText(entry) {
  if (!entry || state.readout === "none") return "";
  if (state.readout === "temp") return entry.temp === undefined ? "" : `${entry.temp} °C`;
  if (state.readout === "power") return entry.power === undefined ? "" : `${entry.power} W${entry.power_kind === "poe" ? " to powered devices" : ""}`;
  if (state.readout === "traffic") return entry.in_bps === undefined ? "" : `${rate(entry.in_bps)} in, ${rate(entry.out_bps)} out`;
  return "";
}

/* One line for the whole rack, from the equipment in it that the monitoring source reads. */
function paintTotals() {
  const line = $("rack-totals");
  const rack = currentRack();
  if (!line) return;
  const rows = rack ? rack.devices.map((d) => ({ d, r: (state.readouts || {})[String(d.id)] })).filter((x) => x.r) : [];
  const parts = [];
  const backups = rows.filter((x) => x.r.runtime !== undefined);
  const viaBackups = backups.some((x) => x.r.power_kind === "draw");   // what the backups feed is the rack's draw; adding servers' own figures would count it twice
  const drawing = (viaBackups ? backups : rows).filter((x) => x.r.power_kind === "draw");
  if (drawing.length) {
    const watts = Math.round(drawing.reduce((n, x) => n + x.r.power, 0));
    parts.push(viaBackups ? `${watts} W through the battery backup${drawing.length === 1 ? "" : "s"}` : `${watts} W drawn`);
  }
  const hot = rows.filter((x) => x.r.temp !== undefined).sort((p, q) => q.r.temp - p.r.temp)[0];
  if (hot) parts.push(`warmest: ${hot.d.name}, ${hot.r.temp} °C`);
  if (backups.length) {
    const least = backups.sort((p, q) => p.r.runtime - q.r.runtime)[0];
    parts.push(`battery: ${least.r.runtime} minutes${backups.length > 1 ? ` (${least.d.name}, the shortest)` : ""}`);
  }
  line.hidden = parts.length === 0;
  line.textContent = parts.join(" · ");
  const badge = $("cables-badge");
  if (badge) {
    badge.hidden = !state.cableSuggestions;
    badge.textContent = state.cableSuggestions;
    badge.setAttribute("aria-label", `${state.cableSuggestions} suggested`);
    $("cables-open").hidden = !state.cableSuggestions && !state.cables.length && state.racks.every((r) => r.devices.length < 2);
  }
}

function paintLive() {
  const live = state.live || {};
  const now = Date.now() / 1000;
  for (const dot of document.querySelectorAll("[data-live]")) {
    const entry = live[dot.dataset.live];
    const health = (state.health || {})[dot.dataset.live];
    dot.hidden = !entry && !health;
    if (health) {  // the monitoring source knows more than a ping does
      dot.className = `dot ${health.level === "ok" ? "up" : health.level === "warning" ? "warn" : "alert"}`;
      dot.title = health.level === "ok" ? "Healthy, according to the monitoring system" : health.why;
    } else if (entry) {
      dot.className = entry.up ? "dot up" : "dot down";
      dot.title = entry.up ? "Answering on the network" : "Not answering";
    }
  }
  for (const el of document.querySelectorAll("[data-readout]")) {
    const text = readoutText((state.readouts || {})[el.dataset.readout]);
    el.hidden = !text;
    el.textContent = text ? ` · ${text}` : "";
  }
  for (const block of document.querySelectorAll(".device[data-device]")) {
    const svg = block.querySelector("svg.faceplate");
    if (svg) Faceplate.light(svg, ((state.readouts || {})[block.dataset.device] || {}).lights);
  }
  paintTotals();
  for (const line of document.querySelectorAll("[data-live-text]")) {
    const entry = live[line.dataset.liveText];
    if (!entry) { line.textContent = "Not checked yet"; continue; }
    const since = describeSince(now - entry.since);
    line.textContent = entry.up ? (since === "just now" ? "Answering" : `Answering (for ${since})`) : (since === "just now" ? "Not answering" : `Not answering (for ${since})`);
    line.className = entry.up ? "" : "down-text";
  }
}

async function refreshLive() {
  clearTimeout(liveTimer);
  if (!DRAWER_VIEWS.includes(state.view)) return;
  try {
    const live = await api("/api/live");
    state.live = live.devices;
    state.health = live.health || {};
    state.readouts = live.readouts || {};
    state.cableSuggestions = live.cable_suggestions || 0;
    state.alertCount = live.alerts || 0;
    renderNav();
    paintLive();
    if (state.view === "dashboard") loadDashboard();
    if (state.view === "map") drawMap();
  } catch (err) { /* shown as "not checked"; the next round tries again */ }
  liveTimer = setTimeout(refreshLive, 30000);
}

/* ---------- management controller ---------- */

let controllerSeq = 0;
let editingControllerDevice = null;

const POWER_LABELS = {
  on: { label: "Power on", confirm: null },
  shutdown: { label: "Shut down", confirm: "The controller asks the operating system to shut down cleanly. Everything running on it stops." },
  restart: { label: "Restart", confirm: "The server is restarted. If the controller can't ask the operating system first, this is like pressing the reset button." },
  off: { label: "Force off", confirm: "Power is cut immediately, like holding the power button. Anything unsaved on the server is lost." },
};

function controllerRows(device, info, status, note, problem) {
  const rows = [];
  if (status) {
    const on = status.power_state === "On";
    rows.push(h("p", { class: "power-line" },
      h("span", { class: on ? "pill on" : "pill off", text: on ? "Powered on" : status.power_state === "Off" ? "Powered off" : status.power_state }),
      status.health ? h("span", { class: status.health === "OK" ? "muted" : "down-text", text: `Health: ${status.health}` }) : null));
    rows.push(h("dl", { class: "facts" },
      fact("Reports as", [status.manufacturer, status.model].filter(Boolean).join(" ")),
      fact("Service tag", status.serial && h("span", { class: "mono", text: status.serial })),
      fact("Hardware", [status.processors, status.memory_gib ? `${Math.round(status.memory_gib)} GB memory` : ""].filter(Boolean).join(", ")),
      fact("Power draw", status.watts !== null && status.watts !== undefined ? `${status.watts} W` : ""),
      fact("Temperatures", status.temperatures.length ? status.temperatures.map((t) => `${t.name.replace(/^System Board /, "")} ${t.celsius}°C`).join(", ") : ""),
      fact("Power supplies", status.power_supplies.length ? status.power_supplies.map((p) => `${p.name.replace(/ Status$/, "")} ${p.health || "?"}`).join(", ") : "")));
  }
  if (note) rows.push(h("p", { class: "hint", text: note }));
  if (problem) rows.push(h("p", { class: "error", text: problem }));
  if (status) {
    const wanted = status.power_state === "On" ? ["shutdown", "restart", "off"] : ["on"];
    const buttons = wanted.filter((a) => status.actions.includes(a)).map((action) =>
      h("button", { type: "button", class: action === "off" ? "danger" : "secondary", onclick: () => sendPower(device, action) }, POWER_LABELS[action].label));
    if (buttons.length) rows.push(h("div", { class: "buttons" }, buttons));
  }
  const links = [];
  if (info && info.has_console) links.push(h("button", { type: "button", class: "secondary", onclick: () => { state.view = "consoles"; state.openPort = `c${device.id}`; showView(); } }, "Open text console"));
  if (info && info.kvm_url) links.push(h("a", { class: "button-link", href: info.kvm_url, target: "_blank", rel: "noopener noreferrer" }, "Open remote screen"));
  links.push(h("button", { type: "button", class: "secondary", onclick: () => openControllerDialog(device, info) }, "Edit controller"));
  rows.push(h("div", { class: "buttons" }, links));
  return rows;
}

function ago(seconds) {
  if (seconds < 90) return "a moment ago";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  return `${Math.round(seconds / 3600)} hours ago`;
}

// The controller can take a long time to answer (half a minute on older ones), so the panel
// never waits for it: the buttons that don't need an answer appear at once, along with what the
// controller said last time, and the fresh answer replaces that when it arrives.
async function loadControllerPanel(device) {
  const seq = ++controllerSeq;
  const current = () => seq === controllerSeq && $("controller-panel") && state.selectedId === device.id;
  const show = (...args) => { if (current()) $("controller-panel").replaceChildren(h("h2", { text: "Management controller" }), ...controllerRows(device, ...args)); };
  const asking = "Asking the controller how the server is doing. Older controllers can take half a minute to answer.";
  let info = null;
  let status = null;
  try {
    info = (await api(`/api/devices/${device.id}/controller`)).controller;
  } catch (err) {
    return show(null, null, "", err.message);
  }
  show(info, null, asking, "");
  try {
    const kept = await api(`/api/devices/${device.id}/controller/status?last=true`);
    if (kept.known) status = kept;
  } catch { /* the fresh answer below reports any problem */ }
  if (status) show(info, status, `As of ${ago(status.age_seconds)}. Checking again…`, "");
  try {
    status = await api(`/api/devices/${device.id}/controller/status`);
    show(info, status, "", "");
  } catch (err) {
    show(info, status, status ? `As of ${ago(status.age_seconds)}.` : "", err.message || "The controller didn't answer.");
  }
}

async function sendPower(device, action) {
  const spec = POWER_LABELS[action];
  if (spec.confirm && !(await confirmDialog(`${spec.label}: “${device.name}”?`, spec.confirm, spec.label))) return;
  try {
    await api(`/api/devices/${device.id}/controller/power`, { action });
  } catch (err) {
    await failed(err);
    return;
  }
  const panel = $("controller-panel");
  if (panel) panel.replaceChildren(h("h2", { text: "Management controller" }), h("p", { class: "hint", text: `${spec.label} sent. Checking again in a few seconds…` }));
  setTimeout(() => { if (state.selectedId === device.id) loadControllerPanel(device); }, 6000);
}

function openControllerDialog(device, info) {
  editingControllerDevice = device;
  const form = $("controller-form");
  form.reset();
  $("controller-dialog-title").textContent = info ? `Controller for “${device.name}”` : `Connect the controller for “${device.name}”`;
  $("controller-save").textContent = info ? "Save" : "Connect";
  $("controller-delete").hidden = !info;
  $("controller-pw-note").hidden = !info;
  if (info) {
    for (const key of ["host", "username", "console_command", "kvm_url"]) form.elements[key].value = info[key];
  }
  form.dataset.editing = info ? "1" : "";
  setError("controller-error", "");
  $("controller-dialog").showModal();
  form.elements.host.focus();
}

function fact(label, value) {
  if (!value) return null;
  return [h("dt", { text: label }), h("dd", {}, value)];
}

function openConsoleTerminal(portId) {
  state.view = "consoles";
  state.openPort = portId;
  showView();
}

// What the details panel says about consoles. Three cases: this equipment is a console server
// (show what is plugged into it); a console server exists (offer its ports, right here); or
// there is none, and nothing is shown.
function consoleSection(device) {
  const own = state.consoles.find((server) => server.id === device.console_server_id);
  if (own) {
    const used = own.ports.filter((port) => port.device);
    return h("section", { class: "controller" },
      h("h2", { text: "Console server" }),
      h("dl", { class: "facts" },
        fact("Address", h("span", { class: "mono", text: own.host })),
        fact("Serial ports", `${own.port_count}, ${used.length} in use`)),
      used.length ? h("ul", { class: "port-links" }, used.map((port) => h("li", {},
        h("button", { type: "button", class: "link", onclick: () => openConsoleTerminal(port.id) }, `Port ${port.number}: ${port.device.name}`)))) : null,
      h("div", { class: "buttons" },
        h("button", { type: "button", class: "secondary", onclick: () => { state.view = "consoles"; showView(); } }, "All ports"),
        h("button", { type: "button", class: "secondary", onclick: () => openConsoleDialog(own) }, "Edit console server")));
  }
  if (!state.consoles.length) return "";
  const mine = device.console_ports || [];
  const choices = state.consoles.flatMap((server) => server.ports
    .filter((port) => !port.device || port.device.id === device.id)
    .map((port) => ({ id: port.id, text: `${server.name} · port ${port.number}${port.label && port.label !== device.name ? ` (${port.label})` : ""}` })));
  return h("section", { class: "controller" },
    h("h2", { text: "Console" }),
    portChoice("Console port", "device-console-port", choices, mine.length ? mine[0].id : null, `/api/devices/${device.id}/console-port`),
    mine.length ? h("div", { class: "buttons" }, mine.map((port) =>
      h("button", { type: "button", class: "primary", onclick: () => openConsoleTerminal(port.id) }, mine.length > 1 ? `Open console (port ${port.number})` : "Open console"))) : null);
}

// A dropdown of free ports that saves as soon as a choice is made.
function portChoice(label, id, choices, currentId, saveUrl) {
  const select = h("select", { id, "aria-label": label },
    h("option", { value: "" }, "Not connected"),
    ...choices.map((choice) => h("option", { value: String(choice.id) }, choice.text)));
  select.value = currentId === null ? "" : String(currentId);
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      await api(saveUrl, { port_id: select.value ? Number(select.value) : null }, "PUT");
      await reload();
    } catch (err) { await failed(err); await reload(); }
  });
  return h("label", { class: "inline-choice" }, label, select);
}

function kvmPortName(kvm, number) {
  return kvm.port_count > 1 ? `${kvm.name} · port ${number}` : kvm.name;
}

function screenLink(url, text) {
  return h("a", { class: "button-link", href: url, target: "_blank", rel: "noopener noreferrer" }, text);
}

// KVMs work like console servers: this equipment is a KVM (show what is plugged into it), or a
// KVM exists (offer its free ports here), or there is none and nothing is shown.
function kvmSection(device) {
  const own = state.kvms.find((kvm) => kvm.id === device.kvm_id);
  if (own) {
    const used = own.ports.filter((port) => port.device);
    return h("section", { class: "controller" },
      h("h2", { text: "KVM" }),
      h("dl", { class: "facts" },
        fact("Kind", own.kind_name),
        fact("Web address", own.url ? h("span", { class: "mono", text: own.url.replace(/^https?:\/\//, "") }) : "Not on the network"),
        fact("Ports", `${own.port_count}, ${used.length} in use`)),
      used.length ? h("ul", { class: "port-links" }, used.map((port) => h("li", {},
        h("button", { type: "button", class: "link", onclick: () => { state.selectedId = port.device.id; render(); } },
          own.port_count > 1 ? `Port ${port.number}: ${port.device.name}` : port.device.name)))) : null,
      h("div", { class: "buttons" },
        own.live && own.port_count === 1 ? h("button", { type: "button", class: "primary", onclick: () => openScreen(own.ports[0].id) }, "Open remote screen") : null,
        own.url ? screenLink(own.url, "Open KVM page") : null,
        h("button", { type: "button", class: "secondary", onclick: () => openKvmDialog(device, own) }, "Edit KVM")));
  }
  if (!state.kvms.length) return "";
  const mine = device.kvm_ports || [];
  const choices = state.kvms.flatMap((kvm) => kvm.ports
    .filter((port) => !port.device || port.device.id === device.id)
    .map((port) => ({ id: port.id, text: kvmPortName(kvm, port.number) })));
  const linked = mine.filter((port) => port.url);
  const live = linked.filter((port) => port.live);
  const outside = linked.filter((port) => !port.live);
  return h("section", { class: "controller" },
    h("h2", { text: "Remote screen" }),
    portChoice("KVM port", "device-kvm-port", choices, mine.length ? mine[0].id : null, `/api/devices/${device.id}/kvm-port`),
    linked.length ? h("div", { class: "buttons" },
      live.map((port) => h("button", { type: "button", class: "primary", onclick: () => openScreen(port.id) }, "Open remote screen")),
      outside.map((port) => screenLink(port.url, "Open remote screen"))) : null,
    mine.length && !linked.length ? h("p", { class: "hint", text: "This KVM has no web address, so there is nothing to open from here." }) : null,
    outside.length && outside[0].port_count > 1 ? h("p", { class: "hint", text: `Opens ${outside[0].kvm_name}; choose port ${outside[0].number} there.` }) : null);
}

let editingKvm = null;
let kvmDevice = null;

function openKvmDialog(device, kvm) {
  editingKvm = kvm || null;
  kvmDevice = device;
  const form = $("kvm-form");
  form.reset();
  $("kvm-kind").replaceChildren(...Object.entries(state.kvmKinds).map(([value, label]) => h("option", { value }, label)));
  $("kvm-dialog-title").textContent = kvm ? `KVM “${kvm.name}”` : `Set up “${device.name}” as a KVM`;
  $("kvm-delete").hidden = !kvm;
  $("kvm-pw-note").hidden = !(kvm && kvm.live);
  if (kvm) {
    for (const key of ["name", "kind", "url", "port_count", "username"]) form.elements[key].value = kvm[key];
  } else {
    form.elements.name.value = device.name.slice(0, 80);
    const guess = /pi-?kvm/i.test(device.name) ? "pikvm" : /dell/i.test(`${device.name} ${device.manufacturer}`) ? "dell" : "other";
    form.elements.kind.value = guess;
    form.elements.port_count.value = guess === "pikvm" ? 1 : 8;
    if (device.ip) form.elements.url.value = `https://${device.ip}`;
  }
  $("kvm-account").hidden = form.elements.kind.value !== "pikvm";
  setError("kvm-error", "");
  $("kvm-dialog").showModal();
  form.elements.name.focus();
}

$("kvm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), kind: form.elements.kind.value, url: form.elements.url.value.trim(),
    port_count: Number(form.elements.port_count.value), device_id: kvmDevice.id,
    username: form.elements.kind.value === "pikvm" ? form.elements.username.value.trim() : "", password: form.elements.password.value };
  if (!body.name) return setError("kvm-error", "Give the KVM a name.");
  if (!(body.port_count >= 1 && body.port_count <= 64)) return setError("kvm-error", "A KVM has between 1 and 64 ports.");
  if (body.username && !body.url) return setError("kvm-error", "Enter the PiKVM's web address so rackmap can sign in to it.");
  if (body.username && !body.password && !(editingKvm && editingKvm.live)) return setError("kvm-error", "Enter the password for that account.");
  $("kvm-save").disabled = true;
  if (body.username) $("kvm-save").textContent = "Signing in to check…";
  try {
    if (editingKvm) await api(`/api/kvms/${editingKvm.id}`, body, "PUT"); else await api("/api/kvms", body);
    $("kvm-dialog").close("");
    await reload();
  } catch (err) {
    setError("kvm-error", err.message);
  } finally {
    $("kvm-save").disabled = false;
    $("kvm-save").textContent = "Save";
  }
});

$("kvm-kind").addEventListener("change", (event) => { $("kvm-account").hidden = event.target.value !== "pikvm"; });

$("kvm-delete").addEventListener("click", async () => {
  const kvm = editingKvm;
  $("kvm-dialog").close("");
  if (!(await confirmDialog(`Remove the KVM set-up for “${kvm.name}”?`, "The equipment stays in the rack; rackmap forgets that it is a KVM and what was plugged into its ports.", "Remove KVM set-up"))) return;
  try {
    await api(`/api/kvms/${kvm.id}`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

// Set-up choices that only apply to some equipment, kept out of the way as plain links.
function roleLinks(device) {
  const links = [];
  const plain = device.console_server_id === null && device.kvm_id === null && !device.has_controller;
  if (!device.has_controller && device.console_server_id === null && device.kvm_id === null) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openControllerDialog(device, null) }, "Set up a server controller (iDRAC, iLO…)"));
  }
  if (plain && !(device.console_ports || []).length) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openConsoleDialog(null, device) }, "This is a console server"));
  }
  if (plain && !(device.kvm_ports || []).length) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openKvmDialog(device, null) }, "This is a KVM"));
  }
  const free = Object.keys(state.serviceKinds).filter((kind) => !(device.services || []).some((s) => s.kind === kind));
  if (free.length) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openServiceDialog(device, null) },
      `Connect a service (${free.map((kind) => state.serviceKinds[kind].name).join(", ")})`));
  }
  return links.length ? h("p", { class: "role-links" }, links) : "";
}

/* ---------- services (Proxmox…) ---------- */

let serviceSeq = 0;
const openGroups = new Set();
let editingService = null; // { device, service }

function sizeText(bytes) {
  if (bytes === null || bytes === undefined) return "";
  const gb = bytes / 1024 ** 3;
  if (gb >= 1000) return `${(gb / 1024).toFixed(1)} TB`;
  if (gb >= 10) return `${Math.round(gb)} GB`;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 ** 2)} MB`;
}

function percent(part, whole) {
  return whole ? `${Math.round((part / whole) * 100)}%` : "";
}

async function sendServiceAction(device, service, status, guest, action) {
  const label = status.action_labels[action];
  const stopping = status.stopping.includes(action);
  let value = "";
  if ((status.action_asks || {})[action]) {
    const choices = (status.choices || {})[action];
    value = await askDialog(`${label}: “${guest.name}”`, status.action_asks[action], choices ? guest[action] || "" : guest.label || "", choices);
    if (value === null) return;
  }
  if (stopping && !(await confirmDialog(`${label}: “${guest.name}”?`, `${status.action_warnings[action] || "It stops running."} This is ${guest.label === guest.name ? service.kind_name : guest.label} on “${device.name}”.`, label))) return;
  const panel = $(`service-panel-${service.id}`);
  try {
    const sent = await api(`/api/services/${service.id}/action`, { target: guest.key, action, confirm: stopping ? guest.name : "", value });
    if (panel) panel.replaceChildren(h("h2", { text: service.kind_name }), h("p", { class: "hint", text: `${label} sent to “${guest.name}”${sent.detail ? ` (${sent.detail})` : ""}. Checking again in a few seconds…` }));
  } catch (err) {
    await failed(err);
  }
  setTimeout(() => { if (state.selectedId === device.id) loadServicePanel(device, service, ++serviceSeq); }, 5000);
}

async function setProtected(device, service, guest, on) {
  if (!on && !(await confirmDialog(`Remove protection from “${guest.name}”?`, "rackmap will then be able to stop and restart it when you ask.", "Remove protection"))) return;
  try {
    await api(`/api/services/${service.id}/protected`, { target: guest.key, protected: on }, "PUT");
  } catch (err) { await failed(err); }
  if (state.selectedId === device.id) loadServicePanel(device, service, ++serviceSeq);
}

function guestRow(device, service, status, g) {
  const use = typeof g.use === "string" ? g.use : g.running
    ? [g.cpus ? `${Math.round((g.cpu || 0) * 100)}% of ${g.cpus} CPU${g.cpus === 1 ? "" : "s"}` : "", g.memory_total ? `${sizeText(g.memory)} of ${sizeText(g.memory_total)}` : ""].filter(Boolean).join(", ")
    : [g.cpus ? `${g.cpus} CPU${g.cpus === 1 ? "" : "s"}` : "", g.memory_total ? sizeText(g.memory_total) : ""].filter(Boolean).join(", ");
  const buttons = (g.actions || []).map((action) => h("button", { type: "button", class: status.action_labels[action].startsWith("Force") ? "danger small" : "secondary small",
    onclick: () => sendServiceAction(device, service, status, g, action) }, status.action_labels[action]));
  const logs = [
    g.console ? h("button", { type: "button", class: "link", onclick: () => openServiceConsole(device, service, g.key, `${g.name} on ${device.name}`) }, "Console") : null,
    g.logs ? h("button", { type: "button", class: "link", onclick: () => openLogs(service, g) }, "Logs") : null];
  const canControl = service.level !== "view" && !g.plain;
  const shield = !canControl ? null : g.protected
    ? h("button", { type: "button", class: "link", onclick: () => setProtected(device, service, g, false) }, "Protected · remove")
    : h("button", { type: "button", class: "link", onclick: () => setProtected(device, service, g, true) }, "Protect");
  return h("li", { class: g.running ? "guest running" : "guest" },
    h("span", { class: g.running ? "pill on" : "pill off", text: g.state_text || (g.running ? "Running" : ["stopped", "exited"].includes(g.state) ? "Stopped" : g.state) }),
    h("span", { class: "guest-name" }, h("strong", { text: g.name }), h("span", { class: "muted", text: g.label && g.label !== g.name ? ` ${g.label}` : "" })),
    h("span", { class: g.unhealthy ? "down-text guest-use" : "muted guest-use", text: [g.detail, use + (g.running && g.uptime ? `, up ${upFor(g.uptime)}` : "")].filter(Boolean).join(" · ") }),
    buttons.length || shield || logs.some(Boolean) ? h("span", { class: "guest-actions" }, buttons, logs, shield) : null);
}

function serviceRows(device, service, status, note, problem) {
  const rows = [];
  if (status) {
    const many = status.nodes.length > 1;
    for (const node of status.nodes) {
      rows.push(h("p", { class: "power-line" },
        h("span", { class: node.online ? "pill on" : "pill alert", text: node.online ? "Online" : "Offline" }),
        h("span", { class: "muted", text: [many ? node.name : "", node.cpus ? `${Math.round((node.cpu || 0) * 100)}% of ${node.cpus} CPUs` : "",
          node.memory_total ? `${sizeText(node.memory)} of ${sizeText(node.memory_total)} memory` : "", node.uptime ? `up ${upFor(node.uptime)}` : ""].filter(Boolean).join(" · ") })));
    }
    if ((status.facts || []).length) rows.push(h("dl", { class: "facts" }, status.facts.flatMap(([label, value]) => fact(label, value))));
    const main = status.guests.filter((g) => !g.group);
    const counted = main.filter((g) => !g.uncounted);
    const running = counted.filter((g) => g.running).length;
    rows.push(h("h3", { class: "sub", text: main.length ? `${status.guests_title} (${running} of ${counted.length} ${status.count_word || "running"})` : `${status.guests_title}: none yet` }));
    if (main.length && status.count_word === "connected") {
      // a switch: its ports as a picture; pick one to see what can be done with it
      const picked = main.find((g) => g.key === state.portPick[service.id]) || null;
      const redraw = () => { const box = $(`service-panel-${service.id}`); if (box) box.replaceChildren(h("h2", { text: service.kind_name + (status.version ? ` ${status.version}` : "") }), ...serviceRows(device, service, status, note, problem)); };
      const short = (name) => name.replace(/^sfp-sfpplus/, "S").replace(/^(ether|Port |port)/, "").replace(/^.*[/:]/, "");
      rows.push(h("div", { class: "port-picture", role: "group", "aria-label": "Ports" }, main.map((g) => h("button", {
        type: "button", class: `port-cell ${g.state}${g.powering ? " powering" : ""}${picked && picked.key === g.key ? " picked" : ""}`,
        title: `${g.name}${g.label ? ` · ${g.label}` : ""} · ${g.state_text}${g.use ? ` · ${g.use}` : ""}${g.protected ? " · protected" : ""}`,
        "aria-pressed": picked && picked.key === g.key ? "true" : "false", "aria-label": `${g.name}, ${g.state_text}`,
        onclick: () => { state.portPick[service.id] = picked && picked.key === g.key ? null : g.key; redraw(); },
      }, short(g.name)))));
      rows.push(h("p", { class: "port-key" }, h("span", { class: "port-cell up key" }), "connected", h("span", { class: "port-cell down key" }), "no link", h("span", { class: "port-cell off key" }), "switched off", h("span", { class: "port-cell up powering key" }), "powering a device"));
      rows.push(picked ? h("ul", { class: "guests" }, guestRow(device, service, status, picked)) : h("p", { class: "hint", text: "Choose a port above to see it and what can be done with it." }));
      rows.push(h("details", { class: "guest-group", open: openGroups.has(`${service.id}:all`), ontoggle: (event) => { if (event.target.open) openGroups.add(`${service.id}:all`); else openGroups.delete(`${service.id}:all`); } },
        h("summary", { text: "All ports as a list" }), h("ul", { class: "guests" }, main.map((g) => guestRow(device, service, status, g)))));
    } else if (main.length) rows.push(h("ul", { class: "guests" }, main.map((g) => guestRow(device, service, status, g))));
    // further lists (devices on the network…) are folded away until asked for; which are open is remembered while the page is
    for (const group of [...new Set(status.guests.filter((g) => g.group).map((g) => g.group))]) {
      const items = status.guests.filter((g) => g.group === group);
      const id = `${service.id}:${group}`;
      rows.push(h("details", { class: "guest-group", open: openGroups.has(id), ontoggle: (event) => { if (event.target.open) openGroups.add(id); else openGroups.delete(id); } },
        h("summary", { text: `${group} (${items.filter((g) => g.running).length} of ${items.length} online)` }),
        h("ul", { class: "guests" }, items.map((g) => guestRow(device, service, status, g)))));
    }
    if (status.storage.length) {
      rows.push(h("h3", { class: "sub", text: "Storage" }));
      rows.push(h("dl", { class: "facts" }, status.storage.flatMap((s) => fact(s.name,
        s.active ? (s.total ? `${sizeText(s.used)} of ${sizeText(s.total)} used (${percent(s.used, s.total)})` : "Available") : h("span", { class: "down-text", text: "Not available" })))));
    }
  }
  for (const list of (status && status.lists) || []) {
    const id = `${service.id}:${list.title.split(" (")[0]}`;
    rows.push(h("details", { class: "guest-group", open: openGroups.has(id), ontoggle: (event) => { if (event.target.open) openGroups.add(id); else openGroups.delete(id); } },
      h("summary", { text: list.title }),
      h("ol", { class: "rule-list" }, list.lines.map((line) => h("li", { class: `rule ${line.tone || ""}`, text: line.text })))));
  }
  if (note) rows.push(h("p", { class: "hint", text: note }));
  if (problem) rows.push(h("p", { class: "error", text: problem }));
  rows.push(h("p", { class: "hint", text: `rackmap's level here: ${service.level_name.split(":")[0]}.${service.level === "view" ? " To get start, stop and restart buttons, raise it under Edit connection." : " Protect anything rackmap should never stop."}` }));
  rows.push(h("div", { class: "buttons" },
    status && status.host_shell ? h("button", { type: "button", class: "secondary", onclick: () => openServiceConsole(device, service, "host", `${device.name} · command line`) }, "Open its command line") : null,
    service.web_url ? h("a", { class: "button-link", href: service.web_url, target: "_blank", rel: "noopener noreferrer" }, `Open ${service.kind_name}`) : null,
    h("button", { type: "button", class: "secondary", onclick: () => openServiceDialog(device, service) }, "Edit connection")));
  return rows;
}

// Like the controller panel: what the service said last time shows at once, the fresh answer replaces it.
async function loadServicePanel(device, service, seq) {
  const id = `service-panel-${service.id}`;
  const current = () => seq === serviceSeq && $(id) && state.selectedId === device.id;
  const title = () => h("h2", { text: service.kind_name + (status && status.version ? ` ${status.version}` : "") });
  const show = (...args) => { if (current()) $(id).replaceChildren(title(), ...serviceRows(device, service, ...args)); };
  let status = null;
  try {
    const kept = await api(`/api/services/${service.id}/status?last=true`);
    if (kept.known) status = kept;
  } catch { /* the fresh answer below reports any problem */ }
  show(status, status ? `As of ${ago(status.age_seconds)}. Checking again…` : `Asking ${service.kind_name}…`, "");
  try {
    status = await api(`/api/services/${service.id}/status`);
    show(status, "", "");
  } catch (err) {
    show(status, status ? `As of ${ago(status.age_seconds)}.` : "", err.message || `${service.kind_name} didn't answer.`);
  }
}

function serviceSections(device) {
  return (device.services || []).map((service) => h("section", { class: "controller", id: `service-panel-${service.id}` },
    h("h2", { text: service.kind_name }), h("p", { class: "hint", text: `Asking ${service.kind_name}…` })));
}

function openServiceConsole(device, service, target, title) {
  state.view = "consoles";
  state.openPort = `s|${service.id}|${target}|${title.replaceAll("|", " ")}`;
  showView();
}

async function openLogs(service, guest) {
  $("log-title").textContent = `Logs: ${guest.name}`;
  $("log-text").textContent = "Fetching the last lines…";
  $("log-dialog").showModal();
  try {
    const got = await api(`/api/services/${service.id}/logs?target=${encodeURIComponent(guest.key)}`);
    $("log-text").textContent = got.text || "Nothing written yet.";
    $("log-text").scrollTop = $("log-text").scrollHeight;
  } catch (err) {
    $("log-text").textContent = err.message;
  }
}

// The commands of the chosen family, with any changes saved on this connection laid over them.
function fillFamily(overrides) {
  const kind = state.serviceKinds[$("service-form").elements.kind.value];
  if (!kind || !kind.profiles) return;
  const family = kind.profiles.families[$("service-family").value];
  $("service-family-note").textContent = family.tried ? "" : "These commands come from the maker's documentation and haven't been tried on real equipment by rackmap. Connect at View only first and check the port list looks right.";
  $("service-commands").replaceChildren(...Object.entries(kind.profiles.keys).map(([key, what]) => {
    const text = Object.prototype.hasOwnProperty.call(overrides, key) ? overrides[key] : family.commands[key];
    return h("label", {}, what, h("textarea", { name: `command-${key}`, rows: String(Math.max(1, text.split("\n").length)), spellcheck: "false", maxlength: "600" }, text));
  }));
}

function serviceOptions() {
  const form = $("service-form");
  const kind = state.serviceKinds[form.elements.kind.value];
  if (!kind || !kind.profiles) return {};
  const commands = {};
  for (const key of Object.keys(kind.profiles.keys)) commands[key] = form.elements[`command-${key}`].value;
  return { profile: $("service-family").value, commands };
}

function fillServiceKind() {
  const form = $("service-form");
  const kind = state.serviceKinds[form.elements.kind.value];
  if (!kind) return;
  $("service-account-label").textContent = kind.account_label;
  $("service-secret-label").textContent = kind.secret_label;
  form.elements.username.placeholder = kind.account_example;
  form.elements.port.placeholder = String(kind.port);
  $("service-help").textContent = kind.help;
  const editing = editingService && editingService.service;
  $("service-family-box").hidden = !kind.profiles;
  if (kind.profiles) {
    const families = kind.profiles.families;
    const saved = (editing && editing.options) || {};
    $("service-family").replaceChildren(...Object.entries(families).map(([slug, f]) => h("option", { value: slug, text: f.name })));
    if (saved.profile) $("service-family").value = saved.profile;
    fillFamily(saved.commands || {});
  }
  $("service-key-box").hidden = !kind.secret_optional;
  $("service-relearn-row").hidden = !(kind.secret_optional && editing);
  $("service-secret-note").hidden = !editing;
  $("service-secret-label").textContent = kind.secret_label + (kind.secret_optional ? " (optional)" : "");
  if (kind.secret_optional && !$("service-key").textContent) {
    api("/api/services/kinds").then((got) => { $("service-key").textContent = got.public_key; }).catch(() => {});
  }
}

function openServiceDialog(device, service) {
  editingService = { device, service };
  const form = $("service-form");
  form.reset();
  const taken = (device.services || []).map((s) => s.kind);
  const kinds = Object.entries(state.serviceKinds).filter(([slug]) => (service ? slug === service.kind : !taken.includes(slug)));
  $("service-kind").replaceChildren(...kinds.map(([slug, kind]) => h("option", { value: slug, text: kind.name })));
  $("service-kind-row").hidden = kinds.length < 2;
  if (!kinds.length) return;
  $("service-level").replaceChildren(...Object.entries(state.serviceLevels).map(([slug, name]) => h("option", { value: slug, text: name })));
  $("service-dialog-title").textContent = service ? `${service.kind_name} on “${device.name}”` : `Connect a service on “${device.name}”`;
  $("service-save").textContent = service ? "Save" : "Connect";
  $("service-delete").hidden = !service;
  $("service-secret-note").hidden = !service;
  if (service) {
    form.elements.host.value = service.host;
    form.elements.port.value = service.port;
    form.elements.username.value = service.username;
    form.elements.level.value = service.level;
  } else if (device.ip) {
    form.elements.host.value = device.ip;
  }
  fillServiceKind();
  setError("service-error", "");
  $("service-dialog").showModal();
  (form.elements.host.value ? form.elements.username : form.elements.host).focus();
}

$("service-kind").addEventListener("change", fillServiceKind);
$("service-family").addEventListener("change", () => fillFamily({}));

$("service-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const { device, service } = editingService;
  const kind = state.serviceKinds[form.elements.kind.value];
  const port = form.elements.port.value.trim();
  const body = { kind: form.elements.kind.value, host: form.elements.host.value.trim(), port: port ? Number(port) : null,
    username: form.elements.username.value.trim(), secret: form.elements.secret.value, level: form.elements.level.value, relearn: form.elements.relearn.checked, options: serviceOptions() };
  if (!body.host) return setError("service-error", "Enter the address the service is on.");
  if (port && !Number.isInteger(body.port)) return setError("service-error", "The port is a number.");
  if (!body.username) return setError("service-error", `Enter the ${kind.account_label.toLowerCase()}.`);
  if (!service && !body.secret && !kind.secret_optional) return setError("service-error", `Enter the ${kind.secret_label.toLowerCase()}.`);
  const button = $("service-save");
  button.disabled = true;
  button.textContent = "Signing in to check…";
  try {
    if (service) await api(`/api/services/${service.id}`, body, "PUT");
    else await api(`/api/devices/${device.id}/services`, body);
    $("service-dialog").close("");
    state.selectedId = device.id;
    await reload();
  } catch (err) {
    setError("service-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = service ? "Save" : "Connect";
  }
});

$("service-delete").addEventListener("click", async () => {
  const { device, service } = editingService;
  $("service-dialog").close("");
  if (!(await confirmDialog(`Disconnect ${service.kind_name} on “${device.name}”?`, `Its address and saved secret are deleted from rackmap. ${service.kind_name} itself isn't changed.`, "Disconnect"))) return;
  try {
    await api(`/api/services/${service.id}`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

/* ---------- cables ---------- */

function cableSection(device) {
  const mine = state.cables.filter((c) => c.a_device_id === device.id || c.b_device_id === device.id);
  if (!mine.length) return "";
  return h("section", { class: "controller" }, h("h2", { text: "Cables" }),
    h("ul", { class: "plain-list" }, mine.map((c) => {
      const near = c.a_device_id === device.id;
      const port = near ? c.a_port : c.b_port;
      const otherId = near ? c.b_device_id : c.a_device_id;
      const otherPort = near ? c.b_port : c.a_port;
      return h("li", {}, port ? h("span", { class: "mono", text: `${port} ` }) : null, "to ",
        h("button", { type: "button", class: "link", onclick: () => showInRack(otherId) }, near ? c.b_name : c.a_name),
        otherPort ? h("span", { class: "muted", text: `, its port ${otherPort}` }) : null,
        c.note ? h("span", { class: "muted", text: ` (${c.note})` }) : null);
    })),
    h("button", { type: "button", class: "link", onclick: () => openCablesDialog(device) }, "Cable list"));
}

function cableRow(c, actions) {
  return h("li", {}, h("span", {},
    h("strong", { text: c.a_name }), c.a_port ? h("span", { class: "mono", text: ` ${c.a_port}` }) : null, " to ",
    h("strong", { text: c.b_name }), c.b_port ? h("span", { class: "mono", text: ` ${c.b_port}` }) : null,
    c.note ? h("span", { class: "muted", text: ` (${c.note})` }) : null,
    c.why ? h("span", { class: "muted", text: `. ${c.why}.` }) : null), h("span", { class: "cable-actions" }, actions));
}

function renderCablesDialog() {
  const view = state.cablesView || { cables: [], suggestions: [] };
  const ends = (c) => ({ a_device_id: c.a_device_id, a_port: c.a_port, b_device_id: c.b_device_id, b_port: c.b_port });
  const act = async (work) => {
    setError("cable-error", "");
    try { await work(); await loadCables(); } catch (err) {
      if (err.status === 401) { $("cables-dialog").close(""); return start(); }
      setError("cable-error", err.message);
    }
  };
  $("cables-suggested-title").hidden = view.suggestions.length === 0;
  $("cables-suggested").replaceChildren(...view.suggestions.map((c) => cableRow(c, [
    h("button", { type: "button", class: "secondary small", onclick: () => act(() => api("/api/cables", ends(c))) }, "Add"),
    h("button", { type: "button", class: "link", onclick: () => act(() => api("/api/cables/dismiss", ends(c))) }, "Not a cable")])));
  $("cables-none").hidden = view.cables.length > 0;
  $("cables-recorded").replaceChildren(...view.cables.map((c) => cableRow(c, [
    h("button", { type: "button", class: "link", onclick: () => act(() => api(`/api/cables/${c.id}`, undefined, "DELETE")) }, "Remove")])));
}

async function loadCables() {
  state.cablesView = await api("/api/cables");
  state.cables = state.cablesView.cables;
  state.cableSuggestions = state.cablesView.suggestions.length;
  renderCablesDialog();
  if (DRAWER_VIEWS.includes(state.view)) render();
}

async function openCablesDialog(device) {
  const form = $("cable-form");
  form.reset();
  const everything = allDevices().slice().sort((p, q) => p.name.localeCompare(q.name));
  const options = () => everything.map((d) => h("option", { value: String(d.id) }, d.name));
  $("cable-a").replaceChildren(...options());
  $("cable-b").replaceChildren(h("option", { value: "" }, "Choose…"), ...options());
  if (device) $("cable-a").value = String(device.id);
  setError("cable-error", "");
  $("cables-dialog").showModal();
  try { await loadCables(); } catch (err) { setError("cable-error", err.message); }
}

$("cable-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.target.elements;
  if (!f.b_device_id.value) return setError("cable-error", "Choose the equipment at the other end.");
  setError("cable-error", "");
  $("cable-save").disabled = true;
  try {
    await api("/api/cables", { a_device_id: Number(f.a_device_id.value), a_port: f.a_port.value.trim(), b_device_id: Number(f.b_device_id.value), b_port: f.b_port.value.trim(), note: f.note.value.trim() });
    f.a_port.value = ""; f.b_port.value = ""; f.note.value = ""; f.b_device_id.value = "";
    await loadCables();
  } catch (err) {
    if (err.status === 401) { $("cables-dialog").close(""); return start(); }
    setError("cable-error", err.message);
  } finally {
    $("cable-save").disabled = false;
  }
});

function renderDetail() {
  const panel = $("detail");
  const device = state.selectedId === null ? null : findDevice(state.selectedId);
  const open = Boolean(device) && DRAWER_VIEWS.includes(state.view);
  panel.hidden = !open;
  $("drawer").hidden = !open;
  document.body.classList.toggle("drawer-open", open);
  if (!open) { panel.replaceChildren(); return; }
  const rack = state.racks.find((r) => r.id === device.rack_id);
  const placed = rack && device.position_u !== null;
  const model = [device.manufacturer, device.model].filter(Boolean).join(" ");
  const size = device.u_height > 0 ? `${Number(device.u_height)}U, ${device.full_depth ? "full depth" : "half depth"}` : "Not rack-mounted";
  let adminLink = null;
  if (device.admin_url) {
    adminLink = h("a", { href: device.admin_url, target: "_blank", rel: "noopener noreferrer" }, device.admin_url.replace(/^https?:\/\//, ""));
  }
  panel.replaceChildren(
    h("div", { class: "detail-head" },
      h("h2", { text: "Equipment" }),
      h("button", { type: "button", class: "link", "aria-label": "Close details", onclick: () => { state.selectedId = null; render(); } }, "Close")),
    h("h1", { text: device.name }),
    h("dl", { class: "facts" },
      fact("Model", model || "Not recorded"),
      fact("Size", size),
      fact("Location", placed ? `${rack.name}, ${span(device)}, ${device.face}` : "Not in a rack"),
      fact("IP address", device.ip && h("span", { class: "mono", text: device.ip })),
      device.ip ? [h("dt", { text: "On the network" }), h("dd", { id: "live-line", "data-live-text": String(device.id) }, "Not checked yet")] : null,
      fact("MAC address", device.mac && h("span", { class: "mono", text: device.mac })),
      fact("Serial number", device.serial && h("span", { class: "mono", text: device.serial })),
      fact("Admin page", adminLink),
      fact("Notes", device.notes && h("span", { class: "notes", text: device.notes }))),
    consoleSection(device),
    kvmSection(device),
    device.has_controller ? h("section", { class: "controller", id: "controller-panel" }, h("h2", { text: "Management controller" }), h("p", { class: "hint", text: "Asking the controller…" })) : "",
    ...serviceSections(device),
    cableSection(device),
    state.monitoring ? h("section", { class: "controller", id: "health-panel" }, h("h2", { text: "Health" }), h("p", { class: "hint", text: "Asking the monitoring system…" })) : "",
    h("div", { class: "buttons" },
      h("button", { type: "button", class: "secondary", onclick: () => openDeviceDialog(device) }, "Edit"),
      placed ? h("button", { type: "button", class: "secondary", onclick: () => takeOut(device) }, "Take out of rack") : null,
      placed && state.view !== "racks" ? h("button", { type: "button", class: "secondary", onclick: () => showInRack(device.id) }, "Show in rack") : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "danger", onclick: () => removeDevice(device) }, "Delete")),
    roleLinks(device));
  if (device.has_controller) loadControllerPanel(device);
  const seq = ++serviceSeq;
  for (const service of device.services || []) loadServicePanel(device, service, seq);
  if (state.monitoring) loadHealthPanel(device);
}

async function takeOut(device) {
  try {
    await api(`/api/devices/${device.id}`, { rack_id: null, position_u: null }, "PATCH");
    await reload();
  } catch (err) { await failed(err); }
}

async function removeDevice(device) {
  const ok = await confirmDialog(`Delete “${device.name}”?`, "Its record is removed from rackmap. This can't be undone.", "Delete equipment");
  if (!ok) return;
  try {
    await api(`/api/devices/${device.id}`, undefined, "DELETE");
    state.selectedId = null;
    await reload();
  } catch (err) { await failed(err); }
}

async function failed(err) {
  if (err.status === 401) { await start(); return; }
  await confirmDialog("That didn't work", err.message, null);
}

/* ---------- dialogs ---------- */

function askDialog(title, text, current, choices) {
  const dialog = $("ask-dialog");
  $("ask-title").textContent = title;
  $("ask-text").textContent = text;
  const field = choices ? $("ask-choice") : $("ask-input");
  $("ask-input").hidden = Boolean(choices);
  $("ask-choice").hidden = !choices;
  if (choices) $("ask-choice").replaceChildren(...choices.map((c) => h("option", { value: c.value, text: c.label })));
  field.value = current || (choices ? choices[0].value : "");
  dialog.returnValue = "";
  dialog.showModal();
  if (choices) field.focus(); else field.select();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok" ? field.value : null), { once: true });
  });
}

function confirmDialog(title, text, actionLabel) {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  const ok = $("confirm-ok");
  ok.hidden = !actionLabel;
  ok.textContent = actionLabel || "";
  dialog.querySelector("[data-close]").textContent = actionLabel ? "Cancel" : "Close";
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close(""));
}

/* rack dialog */
let editingRack = null;

function openRackDialog(rack) {
  editingRack = rack || null;
  const form = $("rack-form");
  form.reset();
  $("rack-dialog-title").textContent = rack ? "Edit rack" : "Add rack";
  $("rack-save").textContent = rack ? "Save rack" : "Add rack";
  $("rack-delete").hidden = !rack;
  if (rack) {
    form.elements.name.value = rack.name;
    form.elements.height_u.value = rack.height_u;
    form.elements.notes.value = rack.notes;
  }
  setError("rack-error", "");
  $("rack-dialog").showModal();
  form.elements.name.focus();
}

$("rack-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), height_u: Number(form.elements.height_u.value), notes: form.elements.notes.value };
  if (!body.name) return setError("rack-error", "Give the rack a name.");
  if (!Number.isInteger(body.height_u) || body.height_u < 1 || body.height_u > 60) return setError("rack-error", "A rack is between 1 and 60 units tall.");
  $("rack-save").disabled = true;
  try {
    const saved = editingRack ? await api(`/api/racks/${editingRack.id}`, body, "PUT") : await api("/api/racks", body);
    state.rackId = saved.id;
    $("rack-dialog").close("");
    await reload();
  } catch (err) {
    setError("rack-error", err.message);
  } finally {
    $("rack-save").disabled = false;
  }
});

$("rack-delete").addEventListener("click", async () => {
  const rack = editingRack;
  $("rack-dialog").close("");
  const count = rack.devices.length;
  const text = count ? `Its ${count} ${count === 1 ? "item stays" : "items stay"} recorded and ${count === 1 ? "moves" : "move"} to “Not in a rack”.` : "The rack is empty.";
  if (!(await confirmDialog(`Delete “${rack.name}”?`, text, "Delete rack"))) return;
  try {
    await api(`/api/racks/${rack.id}`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

/* equipment dialog */
let editingDevice = null;
let chosenKind = null; // catalogue entry picked in the dialog
let modelMode = "search"; // search | chosen | custom
let searchTimer = null;
let searchSeq = 0;

function setModelMode(mode) {
  modelMode = mode;
  $("model-search").hidden = mode !== "search";
  $("model-chosen").hidden = mode !== "chosen";
  $("model-custom").hidden = mode !== "custom";
  if (mode === "chosen" && chosenKind) {
    $("chosen-name").textContent = `${chosenKind.manufacturer} ${chosenKind.model}`;
    $("chosen-meta").textContent = chosenKind.u_height > 0 ? `${Number(chosenKind.u_height)}U, ${chosenKind.full_depth ? "full depth" : "half depth"}` : "not rack-mounted";
  }
  refreshPositions();
}

function dialogSize() {
  const form = $("device-form");
  if (modelMode === "chosen" && chosenKind) return { u: chosenKind.u_height, full: chosenKind.full_depth };
  if (modelMode === "custom") return { u: Number(form.elements.u_height.value) || 0, full: form.elements.full_depth.value === "true" };
  if (editingDevice) return { u: editingDevice.u_height, full: editingDevice.full_depth };
  return { u: 1, full: true };
}

let positionSeq = 0;
async function refreshPositions(preferred) {
  const form = $("device-form");
  const rackId = form.elements.rack_id.value;
  const select = $("f-pos");
  const hint = $("f-pos-hint");
  const keep = preferred !== undefined ? String(preferred) : select.value;
  $("f-pos-label").hidden = !rackId;
  $("f-face").disabled = !rackId;
  hint.hidden = true;
  if (!rackId) { select.replaceChildren(); return; }
  const size = dialogSize();
  if (!(size.u > 0)) {
    select.replaceChildren();
    $("f-pos-label").hidden = true;
    hint.textContent = "This equipment has no rack height, so it can't be given a position. Choose “Not in a rack”, or set its height.";
    hint.hidden = false;
    return;
  }
  const seq = ++positionSeq;
  const query = new URLSearchParams({ u_height: size.u, face: form.elements.face.value, full_depth: size.full });
  if (editingDevice) query.set("ignore", editingDevice.id);
  let positions = [];
  try {
    positions = (await api(`/api/racks/${rackId}/free?${query}`)).positions;
  } catch (err) {
    if (seq === positionSeq) { hint.textContent = err.message; hint.hidden = false; }
    return;
  }
  if (seq !== positionSeq) return;
  select.replaceChildren(...positions.map((u) => {
    const top = u + size.u - 1;
    return h("option", { value: String(u) }, top > u ? `${uText(u)} (up to ${uText(top)})` : uText(u));
  }));
  if (!positions.length) {
    hint.textContent = "There's no free space this size on that side of the rack.";
    hint.hidden = false;
  } else if (positions.map(String).includes(keep)) {
    select.value = keep;
  }
}

function openDeviceDialog(device, preset) {
  editingDevice = device || null;
  chosenKind = device ? device.kind : null;
  const form = $("device-form");
  form.reset();
  $("device-dialog-title").textContent = device ? "Edit equipment" : "Add equipment";
  $("device-save").textContent = device ? "Save changes" : "Add equipment";
  $("model-q").value = "";
  $("model-results").replaceChildren();
  form.elements.name.placeholder = "What you call it";
  $("more").open = Boolean(device && (device.ip || device.mac || device.serial || device.admin_url || device.notes));
  $("f-rack").replaceChildren(
    h("option", { value: "" }, "Not in a rack"),
    ...state.racks.map((rack) => h("option", { value: String(rack.id) }, `${rack.name} (${rack.height_u}U)`)));
  const from = device || preset || {};
  form.elements.rack_id.value = from.rack_id ? String(from.rack_id) : (device ? "" : (state.rackId ? String(state.rackId) : ""));
  form.elements.face.value = from.face || state.face;
  if (device) {
    for (const key of ["name", "manufacturer", "model", "ip", "mac", "serial", "admin_url", "notes"]) form.elements[key].value = device[key];
    form.elements.u_height.value = device.u_height;
    form.elements.full_depth.value = String(device.full_depth);
  }
  if (!device && preset) {
    for (const key of ["name", "ip", "mac", "admin_url"]) if (preset[key]) form.elements[key].value = preset[key];
    if (preset.ip || preset.mac) $("more").open = true;
    if (preset.kind) chosenKind = preset.kind;
  }
  setError("device-error", "");
  modelMode = device ? (device.kind ? "chosen" : "custom") : (chosenKind ? "chosen" : "search");
  setModelMode(modelMode);
  if (!device && preset && preset.searchText && !chosenKind) {
    $("model-q").value = preset.searchText;
    $("model-q").dispatchEvent(new Event("input"));
  }
  refreshPositions(from.position_u !== null && from.position_u !== undefined ? from.position_u : undefined);
  $("device-dialog").showModal();
  (device ? form.elements.name : $("model-q")).focus();
}

function pickKind(kind) {
  chosenKind = kind;
  const form = $("device-form");
  if (!form.elements.name.value.trim()) form.elements.name.placeholder = `${kind.manufacturer} ${kind.model}`;
  setModelMode("chosen");
}

$("model-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const text = $("model-q").value.trim();
  const list = $("model-results");
  if (text.length < 2) { list.replaceChildren(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    let results;
    try {
      results = (await api(`/api/catalog?q=${encodeURIComponent(text)}`)).results;
    } catch (err) {
      if (seq === searchSeq) list.replaceChildren(h("li", { class: "hint", text: err.message }));
      return;
    }
    if (seq !== searchSeq) return;
    if (!results.length) {
      list.replaceChildren(h("li", { class: "hint", text: "Nothing matches. Try fewer words, or enter it by hand below." }));
      return;
    }
    list.replaceChildren(...results.map((kind) => h("li", {},
      h("button", { type: "button", class: "row-button", onclick: () => pickKind(kind) },
        h("span", { class: "label-name", text: `${kind.manufacturer} ${kind.model}` }),
        h("span", { class: "label-meta", text: [kind.u_height > 0 ? `${Number(kind.u_height)}U` : "not rack-mounted", kind.has_front_image ? "picture" : "drawn", kind.part_number].filter(Boolean).join(" · ") })))));
  }, 200);
});

$("model-manual").addEventListener("click", () => { chosenKind = null; setModelMode("custom"); $("device-form").elements.manufacturer.focus(); });
$("model-back").addEventListener("click", () => { setModelMode("search"); $("model-q").focus(); });
$("model-change").addEventListener("click", () => { chosenKind = null; setModelMode("search"); $("model-q").focus(); });
$("f-rack").addEventListener("change", () => refreshPositions());
$("f-face").addEventListener("change", () => refreshPositions());
$("device-form").elements.u_height.addEventListener("change", () => refreshPositions());
$("device-form").elements.full_depth.addEventListener("change", () => refreshPositions());

$("device-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const value = (key) => form.elements[key].value.trim();
  if (modelMode === "search") return setError("device-error", "Pick a model from the search results, or choose “enter it by hand”.");
  const body = { name: value("name"), ip: value("ip"), mac: value("mac"), serial: value("serial"), admin_url: value("admin_url"), notes: form.elements.notes.value };
  if (modelMode === "chosen") {
    body.type_slug = chosenKind.slug;
    body.u_height = chosenKind.u_height;
  } else {
    body.type_slug = null;
    body.manufacturer = value("manufacturer");
    body.model = value("model");
    body.u_height = Number(form.elements.u_height.value);
    body.full_depth = form.elements.full_depth.value === "true";
    if (!body.name && !body.manufacturer && !body.model) return setError("device-error", "Enter a name, or the maker and model.");
    if (!(body.u_height >= 0)) return setError("device-error", "Height must be a number of rack units (0 if it doesn't mount in a rack).");
  }
  const rackId = form.elements.rack_id.value;
  if (rackId) {
    if (!form.elements.position_u.value) return setError("device-error", "Choose a position in the rack, or set the rack to “Not in a rack”.");
    body.rack_id = Number(rackId);
    body.position_u = Number(form.elements.position_u.value);
    body.face = form.elements.face.value;
  } else {
    body.rack_id = null;
    body.position_u = null;
  }
  $("device-save").disabled = true;
  try {
    const saved = editingDevice ? await api(`/api/devices/${editingDevice.id}`, body, "PATCH") : await api("/api/devices", body);
    state.selectedId = saved.id;
    if (saved.rack_id) state.rackId = saved.rack_id;
    $("device-dialog").close("");
    if (state.view === "discovery") { await reload(); await loadDiscovery(); } else { await reload(); }
    setTimeout(refreshLive, 4000); // the server re-checks soon after equipment changes
  } catch (err) {
    setError("device-error", err.message);
  } finally {
    $("device-save").disabled = false;
  }
});

/* ---------- discovery ---------- */

let scanTimer = null;

function hostFacts(host) {
  const facts = [];
  if (host.vendor) facts.push(host.vendor);
  facts.push(host.looks_like);
  if (host.services.length) facts.push([...new Set(host.services)].join(", "));
  return facts.join(" · ");
}

function hostSays(host) {
  return [host.sys_descr, host.http_title && `Web page: “${host.http_title}”`].filter(Boolean).join(" · ");
}

async function addFromHost(host) {
  let kind = null;
  if (host.suggested_slug) {
    try { kind = await api(`/api/catalog/${encodeURIComponent(host.suggested_slug)}`); } catch (err) { kind = null; }
  }
  const ports = new Set(host.ports);
  const adminUrl = ports.has(443) ? `https://${host.ip}` : ports.has(80) ? `http://${host.ip}` : ports.has(8443) ? `https://${host.ip}:8443` : ports.has(8080) ? `http://${host.ip}:8080` : "";
  const realVendor = host.vendor && !host.vendor.startsWith("Private") ? host.vendor.split(" ")[0] : "";
  openDeviceDialog(null, { name: host.best_name, ip: host.ip, mac: host.mac, admin_url: adminUrl, kind, searchText: realVendor, rack_id: state.rackId, face: "front" });
}

function hostRow(host) {
  const title = host.best_name ? `${host.best_name}` : host.ip;
  const actions = [];
  if (host.device) {
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => { state.view = "racks"; state.selectedId = host.device.id; const d = findDevice(host.device.id); if (d && d.rack_id) state.rackId = d.rack_id; showView(); } }, "Show in rack view"));
  } else if (host.ignored) {
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => ignoreHost(host, false) }, "Stop ignoring"));
  } else {
    actions.push(h("button", { type: "button", class: "primary", onclick: () => addFromHost(host) }, "Add…"));
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => ignoreHost(host, true) }, "Ignore"));
  }
  return h("li", { class: "host card" },
    h("div", { class: "host-main" },
      h("div", { class: "host-title" },
        h("strong", { text: title }),
        h("span", { class: "mono", text: host.best_name ? host.ip : "" }),
        host.mac ? h("span", { class: "mono muted", text: host.mac }) : null),
      h("p", { class: "host-facts", text: hostFacts(host) }),
      hostSays(host) ? h("p", { class: "hint host-says", text: hostSays(host) }) : null,
      host.device ? h("p", { class: "hint", text: `Recorded as “${host.device.name}”.` }) : null,
      !host.device && host.suggested_slug ? h("p", { class: "hint", text: "rackmap recognised the model; Add… will fill it in." }) : null),
    h("div", { class: "host-actions" }, actions));
}

async function ignoreHost(host, ignored) {
  try {
    await api(`/api/discovery/hosts/${host.id}/ignore`, { ignored });
    await reload();
    await loadDiscovery();
  } catch (err) { await failed(err); }
}

function renderDiscovery() {
  const view = state.discovery;
  if (!view || state.view !== "discovery") return;
  const job = view.job;
  const running = job.state === "running";
  $("scan-go").disabled = running;
  $("scan-go").textContent = running ? "Scanning…" : "Scan now";
  const progress = $("scan-progress");
  progress.hidden = !running;
  if (running) {
    progress.max = job.total;
    progress.value = job.done;
    $("scan-status").textContent = `${job.done} of ${job.total} addresses checked, ${job.found} answering`;
  } else if (job.state === "finished") {
    const seconds = Math.max(1, Math.round(job.finished - job.started));
    $("scan-status").textContent = `Last scan: ${job.found} of ${job.total} addresses answered (${seconds}s). ${job.note || ""}`.trim();
  } else if (job.state === "failed") {
    $("scan-status").textContent = job.note;
  } else {
    $("scan-status").textContent = "";
  }

  const fresh = view.hosts.filter((x) => !x.ignored && !x.device);
  const known = view.hosts.filter((x) => x.device);
  const ignored = view.hosts.filter((x) => x.ignored && !x.device);
  $("found-title").textContent = view.hosts.length ? `${fresh.length} new, ${known.length} already recorded` : "Nothing found yet";
  $("found-empty").hidden = view.hosts.length > 0;
  $("h-new").hidden = fresh.length === 0;
  $("h-known").hidden = known.length === 0;
  $("h-ignored").hidden = !(state.showIgnored && ignored.length);
  $("show-ignored").parentElement.hidden = ignored.length === 0;
  $("show-ignored").parentElement.lastChild.textContent = ` Show ignored (${ignored.length})`;
  const scanned = new Set(view.hosts.map((x) => x.ip));
  const monitored = (view.monitored || []).filter((d) => !scanned.has(d.address));
  $("h-monitored").hidden = monitored.length === 0;
  $("monitored-hint").hidden = monitored.length === 0;
  $("hosts-monitored").replaceChildren(...monitored.map((d) =>
    h("li", { class: "host card" },
      h("div", { class: "host-main" },
        h("div", { class: "host-title" }, h("strong", { text: d.name }), d.address && d.address !== d.name ? h("span", { class: "mono", text: d.address }) : null),
        d.up ? null : h("p", { class: "down-text", text: "Not answering the monitoring system" })),
      h("div", { class: "host-actions" },
        h("button", { type: "button", class: "primary", onclick: () => openDeviceDialog(null, { name: d.name, ip: /^[0-9.]+$/.test(d.address) ? d.address : "", mac: "", admin_url: "", kind: null, searchText: "", rack_id: state.rackId, face: "front" }) }, "Add…")))));
  if (!view.hosts.length && monitored.length) { $("found-title").textContent = `${monitored.length} seen by the monitoring system`; $("found-empty").hidden = true; }
  $("hosts-new").replaceChildren(...fresh.map(hostRow));
  $("hosts-known").replaceChildren(...known.map(hostRow));
  $("hosts-ignored").replaceChildren(...(state.showIgnored ? ignored.map(hostRow) : []));
}

async function loadDiscovery(fillForm) {
  state.discovery = await api("/api/discovery");
  if (state.view !== "discovery") return;
  if (fillForm) {
    const form = $("scan-form");
    const settings = state.discovery.settings;
    form.elements.networks.value = settings.networks || state.discovery.suggested_networks.join("\n");
    form.elements.router.value = settings.router;
    $("community-note").hidden = !settings.community_set;
    $("router-box").open = Boolean(settings.router);
    const suggest = state.discovery.suggested_networks;
    $("net-suggest").hidden = suggest.length === 0;
    $("net-suggest").textContent = suggest.length ? `rackmap itself is on ${suggest.join(", ")}. One network per line; private ranges only.` : "";
  }
  renderDiscovery();
  clearTimeout(scanTimer);
  if (state.discovery.job.state === "running") {
    scanTimer = setTimeout(async () => {
      try { await loadDiscovery(); if (state.discovery.job.state !== "running") await reload(); } catch (err) { /* next click will report it */ }
    }, 1500);
  }
}

function discoveryScreen() {
  show("t-discovery");
  $("version").textContent = state.version;
  $("show-ignored").checked = state.showIgnored;
  $("show-ignored").addEventListener("change", (event) => { state.showIgnored = event.target.checked; renderDiscovery(); });
  $("scan-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    setError("scan-error", "");
    if (!form.elements.networks.value.trim()) return setError("scan-error", "Enter at least one network to scan, for example 192.168.1.0/24.");
    const body = { networks: form.elements.networks.value, router: form.elements.router.value.trim() };
    if (form.elements.community.value) body.community = form.elements.community.value;
    $("scan-go").disabled = true;
    try {
      await api("/api/discovery/settings", body, "PUT");
      await api("/api/discovery/scan", {});
      form.elements.community.value = "";
      await loadDiscovery(true);
    } catch (err) {
      $("scan-go").disabled = false;
      if (err.status === 401) return start();
      setError("scan-error", err.message);
    }
  });
}

/* ---------- consoles ---------- */

let terminal = null;
let fitAddon = null;
let socket = null;
let editingConsole = null;
let editingPort = null;

function allDevices() {
  return [...state.racks.flatMap((r) => r.devices), ...state.unplaced].sort((a, b) => a.name.localeCompare(b.name));
}

function findPort(portId) {
  for (const server of state.consoles) {
    const port = server.ports.find((p) => p.id === portId);
    if (port) return { server, port };
  }
  return null;
}

function portName(port) {
  return port.label || (port.device ? port.device.name : "");
}

function closeTerminal() {
  if (socket) { socket.onclose = null; socket.onmessage = null; try { socket.close(); } catch (err) { /* already closed */ } socket = null; }
  if (terminal) { terminal.dispose(); terminal = null; fitAddon = null; }
  state.openPort = null;
  const card = $("term-card");
  if (card) card.hidden = true;
}

function setTermStatus(text, canReconnect) {
  if (!$("term-status")) return;
  $("term-status").textContent = text;
  $("term-reconnect").hidden = !canReconnect;
}

function openTerminal(portId) {
  const isController = typeof portId === "string";
  let title;
  let path;
  if (isController && portId.startsWith("s|")) {
    // a console reached through a service: "s|<service id>|<target>|<title>"
    const [, serviceId, target, ...rest] = portId.split("|");
    title = rest.join("|");
    path = `/ws/service/${Number(serviceId)}/console?target=${encodeURIComponent(target)}`;
  } else if (isController) {
    const device = findDevice(Number(portId.slice(1)));
    if (!device) return;
    title = `${device.name} · text console`;
    path = `/ws/controller/${device.id}`;
  } else {
    const found = findPort(portId);
    if (!found) return;
    const name = portName(found.port);
    title = `${found.server.name} · port ${found.port.number}${name ? ` · ${name}` : ""}`;
    path = `/ws/console/${portId}`;
  }
  closeTerminal();
  state.openPort = portId;
  $("term-card").hidden = false;
  $("term-title").textContent = title;
  setTermStatus("Connecting…", false);
  terminal = new Terminal({ cursorBlink: true, convertEol: false, scrollback: 5000, fontSize: 14,
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', theme: { background: "#0e1110", foreground: "#e8ece9", cursor: "#4fc3a6" } });
  fitAddon = new FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open($("term"));
  fitAddon.fit();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}${path}`);
  ws.binaryType = "arraybuffer";
  socket = ws;
  const send = (message) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)); };
  terminal.onData((data) => send({ t: "in", d: data }));
  terminal.onResize(({ cols, rows }) => send({ t: "size", cols, rows }));
  let ended = false;
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") { terminal.write(new Uint8Array(event.data)); return; }
    const note = JSON.parse(event.data);
    if (note.state === "open") {
      setTermStatus("Connected. Press Enter if the screen is blank.", false);
      send({ t: "size", cols: terminal.cols, rows: terminal.rows });
      terminal.focus();
    } else if (note.state === "failed" || note.state === "closed") {
      ended = true;
      setTermStatus(note.message || "Disconnected.", true);
    }
  };
  ws.onclose = (event) => {
    if (ended) return;
    setTermStatus(event.code === 4401 ? "Your sign-in has expired. Sign in again to use the console." : "The connection to rackmap was lost.", true);
  };
  $("term-card").scrollIntoView({ block: "start", behavior: "smooth" });
}

window.addEventListener("resize", () => { if (fitAddon && state.view === "consoles") { try { fitAddon.fit(); } catch (err) { /* not laid out yet */ } } });

function renderConsoles() {
  if (state.view !== "consoles" || !$("console-servers")) return;
  $("no-consoles").hidden = state.consoles.length > 0 || typeof state.openPort === "string";
  $("console-actions").hidden = state.consoles.length === 0;
  $("console-servers").replaceChildren(...state.consoles.map((server) => h("section", { class: "card console-server" },
    h("div", { class: "rack-head" },
      h("div", {},
        h("h1", { text: server.name }),
        h("p", { class: "hint", text: `${server.kind_name} · ${server.host}${server.port === 22 ? "" : `:${server.port}`} · ${server.port_count} ports` })),
      h("button", { type: "button", class: "link", onclick: () => openConsoleDialog(server) }, "Edit")),
    h("ul", { class: "ports" }, server.ports.map((port) => h("li", { class: port.id === state.openPort ? "port open" : "port" },
      h("button", { type: "button", class: "port-open", onclick: () => openTerminal(port.id), "aria-label": `Open port ${port.number}${portName(port) ? `, ${portName(port)}` : ""}` },
        h("span", { class: "port-number", text: String(port.number) }),
        h("span", { class: "port-label", text: portName(port) || "Not labelled" })),
      h("button", { type: "button", class: "link port-edit", onclick: () => openPortDialog(server, port), "aria-label": `Label port ${port.number}` }, "Label")))))));
}

async function loadConsoles() {
  const view = await api("/api/consoles");
  state.consoles = view.servers;
  state.kinds = view.kinds;
  renderConsoles();
}

function openConsoleDialog(server, forDevice) {
  // Started from a piece of equipment: if rackmap already knows one console server that isn't
  // tied to equipment yet, this is most likely it, so offer that one instead of a second copy.
  const loose = state.consoles.filter((s) => s.device_id === null);
  const note = $("console-existing-note");
  note.hidden = true;
  if (!server && forDevice && loose.length === 1) {
    server = loose[0];
    note.textContent = `rackmap already has the console server “${server.name}”, not yet tied to equipment. Saving ties it to “${forDevice.name}”.`;
    note.hidden = false;
  }
  editingConsole = server || null;
  const form = $("console-form");
  form.reset();
  $("console-device").replaceChildren(h("option", { value: "" }, "Not in the rack yet"),
    ...allDevices().map((device) => h("option", { value: String(device.id) }, device.name)));
  $("console-kind").replaceChildren(...Object.entries(state.kinds).map(([value, label]) => h("option", { value }, label)));
  $("console-dialog-title").textContent = server ? "Edit console server" : "Connect a console server";
  $("console-save").textContent = server ? "Save" : "Connect";
  $("console-delete").hidden = !server;
  $("console-pw-note").hidden = !server;
  if (server) {
    for (const key of ["name", "kind", "host", "port", "username", "port_count"]) form.elements[key].value = server[key];
    form.elements.device_id.value = server.device_id === null ? "" : String(server.device_id);
  }
  if (forDevice) {
    form.elements.device_id.value = String(forDevice.id);
    if (!server) {
      form.elements.name.value = forDevice.name.slice(0, 80);
      form.elements.host.value = forDevice.ip || "";
    }
  }
  setError("console-error", "");
  $("console-dialog").showModal();
  form.elements.name.focus();
}

$("console-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), kind: form.elements.kind.value, host: form.elements.host.value.trim(),
    port: Number(form.elements.port.value) || 22, username: form.elements.username.value.trim(), password: form.elements.password.value,
    port_count: Number(form.elements.port_count.value), device_id: form.elements.device_id.value ? Number(form.elements.device_id.value) : null };
  if (!body.name) return setError("console-error", "Give the console server a name.");
  if (!body.host) return setError("console-error", "Enter the console server's address.");
  if (!body.username) return setError("console-error", "Enter the account rackmap signs in with.");
  if (!editingConsole && !body.password) return setError("console-error", "Enter the password for that account.");
  if (!(body.port_count >= 1 && body.port_count <= 96)) return setError("console-error", "A console server has between 1 and 96 serial ports.");
  const button = $("console-save");
  button.disabled = true;
  button.textContent = "Signing in to check…";
  try {
    if (editingConsole) await api(`/api/consoles/${editingConsole.id}`, body, "PUT"); else await api("/api/consoles", body);
    $("console-dialog").close("");
    await reload();
    if (state.view === "consoles") await loadConsoles();
  } catch (err) {
    setError("console-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = editingConsole ? "Save" : "Connect";
  }
});

$("console-delete").addEventListener("click", async () => {
  const server = editingConsole;
  $("console-dialog").close("");
  if (!(await confirmDialog(`Remove “${server.name}”?`, "Its port labels and saved password are deleted from rackmap. The console server itself isn't changed.", "Remove console server"))) return;
  try {
    if (state.openPort !== null && server.ports.some((p) => p.id === state.openPort)) closeTerminal();
    await api(`/api/consoles/${server.id}`, undefined, "DELETE");
    await loadConsoles();
    await reload();
  } catch (err) { await failed(err); }
});

function openPortDialog(server, port) {
  editingPort = port;
  const form = $("port-form");
  form.reset();
  $("port-dialog-title").textContent = `${server.name} · port ${port.number}`;
  $("port-device").replaceChildren(h("option", { value: "" }, "Not tied to equipment"),
    ...allDevices().map((device) => h("option", { value: String(device.id) }, device.name)));
  form.elements.label.value = port.label;
  form.elements.device_id.value = port.device ? String(port.device.id) : "";
  setError("port-error", "");
  $("port-dialog").showModal();
  form.elements.label.focus();
}

$("port-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  $("port-save").disabled = true;
  try {
    await api(`/api/consoles/ports/${editingPort.id}`, { label: form.elements.label.value.trim(), device_id: form.elements.device_id.value ? Number(form.elements.device_id.value) : null }, "PUT");
    $("port-dialog").close("");
    await loadConsoles();
    await reload();
  } catch (err) {
    setError("port-error", err.message);
  } finally {
    $("port-save").disabled = false;
  }
});

function consolesScreen() {
  show("t-consoles");
  $("version").textContent = state.version;
  $("add-first-console").addEventListener("click", () => openConsoleDialog(null));
  $("add-console").addEventListener("click", () => openConsoleDialog(null));
  $("term-close").addEventListener("click", () => { closeTerminal(); renderConsoles(); });
  $("term-reconnect").addEventListener("click", () => { const id = state.openPort; if (id !== null) { openTerminal(id); renderConsoles(); } });
}

$("controller-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const device = editingControllerDevice;
  const body = { host: form.elements.host.value.trim(), username: form.elements.username.value.trim(), password: form.elements.password.value,
    console_command: form.elements.console_command.value.trim(), kvm_url: form.elements.kvm_url.value.trim() };
  if (!body.host) return setError("controller-error", "Enter the controller's address.");
  if (!body.username) return setError("controller-error", "Enter the account rackmap signs in with.");
  if (!form.dataset.editing && !body.password) return setError("controller-error", "Enter the password for that account.");
  const button = $("controller-save");
  button.disabled = true;
  button.textContent = "Signing in to check…";
  try {
    await api(`/api/devices/${device.id}/controller`, body, "PUT");
    $("controller-dialog").close("");
    state.selectedId = device.id;
    await reload();
  } catch (err) {
    setError("controller-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = form.dataset.editing ? "Save" : "Connect";
  }
});

$("controller-delete").addEventListener("click", async () => {
  const device = editingControllerDevice;
  $("controller-dialog").close("");
  if (!(await confirmDialog(`Disconnect the controller for “${device.name}”?`, "Its address and saved password are deleted from rackmap. The controller itself isn't changed.", "Disconnect controller"))) return;
  try {
    await api(`/api/devices/${device.id}/controller`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

/* ---------- remote screen (PiKVM) ---------- */

let screenSocket = null;
let screenTimer = null;
let screenMoveTimer = null;
let screenPending = null;
const screenKeys = new Set();

function openScreen(portId) {
  state.view = "screen";
  state.screenPort = portId;
  showView();
}

function screenSend(message) {
  if (screenSocket && screenSocket.readyState === WebSocket.OPEN) screenSocket.send(JSON.stringify(message));
}

function releaseScreenKeys() {
  for (const code of screenKeys) screenSend({ t: "key", code, down: false });
  screenKeys.clear();
}

function closeScreen() {
  clearTimeout(screenTimer);
  clearTimeout(screenMoveTimer);
  screenPending = null;
  if (screenSocket) {
    releaseScreenKeys();
    const socket = screenSocket;
    screenSocket = null;
    socket.onclose = null;
    socket.close();
  }
  const img = $("screen-img");
  if (img) { img.onerror = null; img.onload = null; img.removeAttribute("src"); }  // ends the live stream
}

function setScreenStatus(text, canReconnect) {
  if (!$("screen-status")) return;
  $("screen-status").textContent = text;
  $("screen-reconnect").hidden = !canReconnect;
}

// The picture: the KVM's live stream, or (if the browser can't show that) a fresh still every moment.
function startPicture(portId) {
  const img = $("screen-img");
  const note = $("screen-note");
  const stills = () => {
    if (state.view !== "screen" || state.screenPort !== portId || !screenSocket) return;
    img.onload = () => { note.hidden = true; screenTimer = setTimeout(stills, 400); };
    img.onerror = () => { note.hidden = false; note.textContent = "No picture from the KVM right now. Trying again…"; screenTimer = setTimeout(stills, 3000); };
    img.src = `/api/kvm-ports/${portId}/snapshot?t=${Date.now()}`;
  };
  img.onload = () => { note.hidden = true; };
  img.onerror = stills;
  img.src = `/api/kvm-ports/${portId}/stream?t=${Date.now()}`;
}

function connectScreen() {
  const portId = state.screenPort;
  closeScreen();
  setScreenStatus("Connecting…", false);
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/kvm/${portId}`);
  screenSocket = socket;
  let power = "";
  let signal = "";
  const describe = () => setScreenStatus(["Connected.", power, signal].filter(Boolean).join(" "), false);
  socket.onmessage = (event) => {
    let note;
    try { note = JSON.parse(event.data); } catch { return; }
    if (note.state === "open") {
      $("screen-title").textContent = note.device ? `${note.device} · screen` : `${note.kvm} · screen`;
      if (note.url) $("screen-page").href = note.url;
      describe();
      startPicture(portId);
    } else if (note.state === "power") {
      power = note.on ? "The machine's power light is on." : "";  // a dark light may only mean the KVM's power cable isn't fitted
      describe();
    } else if (note.state === "signal") {
      signal = note.online ? "" : "No video signal: the machine may be off or asleep, or its video cable isn't connected.";
      describe();
    } else if (note.state === "failed" || note.state === "closed") {
      setScreenStatus(note.message || "The connection closed.", true);
    }
  };
  socket.onclose = () => {
    if (screenSocket !== socket) return;
    screenSocket = null;
    clearTimeout(screenTimer);
    if ($("screen-reconnect") && $("screen-reconnect").hidden) setScreenStatus("Disconnected.", true);
  };
}

function screenPoint(event) {
  const rect = $("screen-img").getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return { x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)) };
}

const SCREEN_BUTTONS = ["left", "middle", "right"];

function screenScreen() {
  show("t-screen");
  $("version").textContent = state.version;
  const area = $("screen");
  const img = $("screen-img");
  $("screen-close").addEventListener("click", () => { closeScreen(); state.view = "racks"; showView(); });
  $("screen-reconnect").addEventListener("click", connectScreen);
  $("screen-cad").addEventListener("click", () => { screenSend({ t: "shortcut", keys: ["ControlLeft", "AltLeft", "Delete"] }); area.focus(); });
  $("screen-type").addEventListener("click", () => { $("type-form").reset(); setError("type-error", ""); $("type-dialog").showModal(); });
  $("screen-power").addEventListener("click", openPowerDialog);

  area.addEventListener("keydown", (event) => {
    event.preventDefault();
    if (event.repeat || !event.code) return;  // the machine repeats a held key by itself
    screenKeys.add(event.code);
    screenSend({ t: "key", code: event.code, down: true });
  });
  area.addEventListener("keyup", (event) => {
    event.preventDefault();
    if (!event.code) return;
    screenKeys.delete(event.code);
    screenSend({ t: "key", code: event.code, down: false });
  });
  area.addEventListener("blur", releaseScreenKeys);

  img.addEventListener("mousemove", (event) => {
    if (document.activeElement !== area) return;
    screenPending = screenPoint(event);
    if (screenMoveTimer) return;  // at most one position every 30 ms, always the latest
    screenMoveTimer = setTimeout(() => {
      screenMoveTimer = null;
      if (screenPending) screenSend({ t: "move", ...screenPending });
      screenPending = null;
    }, 30);
  });
  let taking = false;
  const button = (down) => (event) => {
    event.preventDefault();
    if (down && document.activeElement !== area) {  // the first click only takes control; it isn't passed on
      area.focus();
      taking = true;
      return;
    }
    if (taking) { if (!down) taking = false; return; }
    const point = screenPoint(event);
    const name = SCREEN_BUTTONS[event.button];
    if (!point || !name) return;
    screenSend({ t: "move", ...point });
    screenSend({ t: "button", b: name, down });
  };
  img.addEventListener("mousedown", button(true));
  img.addEventListener("mouseup", button(false));
  img.addEventListener("contextmenu", (event) => event.preventDefault());
  img.addEventListener("wheel", (event) => {
    if (document.activeElement !== area) return;
    event.preventDefault();
    screenSend({ t: "wheel", dy: event.deltaY > 0 ? -5 : 5 });
  }, { passive: false });
}

$("type-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const text = form.elements.text.value + (form.elements.enter.checked ? "\n" : "");
  if (!text) return setError("type-error", "Enter the text to type.");
  $("type-send").disabled = true;
  try {
    await api(`/api/kvm-ports/${state.screenPort}/text`, { text });
    $("type-dialog").close("");
    form.reset();
    if ($("screen")) $("screen").focus();
  } catch (err) {
    setError("type-error", err.message);
  } finally {
    $("type-send").disabled = false;
  }
});

async function openPowerDialog() {
  setError("power-error", "");
  $("power-now").textContent = "";
  $("power-dialog").showModal();
  try {
    const now = await api(`/api/kvm-ports/${state.screenPort}/power`);
    $("power-now").textContent = !now.wired ? "The KVM reports that its power cable isn't in use."
      : now.on === null ? "" : now.on ? "The machine's power light is on." : "The KVM sees no power light: the machine is off, or the KVM's power cable isn't fitted to it.";
  } catch (err) { setError("power-error", err.message); }
}

for (const button of document.querySelectorAll("#power-dialog [data-power]")) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    setError("power-error", "");
    try {
      await api(`/api/kvm-ports/${state.screenPort}/power`, { action: button.dataset.power });
      $("power-dialog").close("");
    } catch (err) {
      setError("power-error", err.message);
    } finally {
      button.disabled = false;
    }
  });
}

/* ---------- start-up ---------- */

/* ---------- monitoring source ---------- */

let healthSeq = 0;
let monitoringTimer = null;
const LEVEL_TEXT = { ok: "Healthy", warning: "Warning", alert: "Alert" };
const levelClass = (level) => (level === "ok" ? "on" : level === "warning" ? "warn" : "alert");

function rate(bps) {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)} Gb/s`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`;
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kb/s`;
  return `${bps} b/s`;
}

function upFor(seconds) {
  if (!seconds) return "";
  const days = Math.floor(seconds / 86400);
  if (days >= 2) return `${days} days`;
  const hours = Math.floor(seconds / 3600);
  if (hours >= 2) return `${hours} hours`;
  return `${Math.max(1, Math.round(seconds / 60))} minutes`;
}

function sensorValue(sensor) {
  const n = Number(sensor.value);
  const shown = Number.isInteger(n) ? String(n) : n.toFixed(1);
  return sensor.unit ? `${shown} ${sensor.unit}` : shown;
}

function linkChoice(device, data) {
  const select = h("select", { id: "health-link", "aria-label": "Monitored as" },
    h("option", { value: "auto" }, "Match by address or name"),
    h("option", { value: "none" }, "Not monitored"),
    ...data.choices.map((c) => h("option", { value: `k:${c.key}` }, c.address && c.address !== c.name ? `${c.name} (${c.address})` : c.name)));
  select.value = data.link === null || data.link === undefined ? "auto" : data.link === "" ? "none" : `k:${data.link}`;
  select.addEventListener("change", async () => {
    select.disabled = true;
    const key = select.value === "auto" ? null : select.value === "none" ? "" : select.value.slice(2);
    try { await api(`/api/devices/${device.id}/monitoring-link`, { key }, "PUT"); } catch (err) { await failed(err); }
    refreshLive();
    loadHealthPanel(device);
  });
  return h("label", { class: "inline-choice" }, "Monitored as", select);
}

function healthRows(device, data, portList) {
  const rows = [];
  const m = data.matched;
  if (data.error) rows.push(h("p", { class: "error", text: data.error }));
  if (!m) {
    rows.push(h("p", { class: "hint", text: data.link === "" ? "Monitoring is switched off for this equipment." : "The monitoring system has no device with this address or name. Give this equipment its IP address, or pick the device here." }));
    rows.push(linkChoice(device, data));
    return rows;
  }
  rows.push(h("p", { class: "power-line" },
    h("span", { class: `pill ${levelClass(m.level)}`, text: LEVEL_TEXT[m.level] }),
    m.level === "ok" ? null : h("span", { class: m.level === "alert" ? "down-text" : "warn-text", text: m.why })));
  const ports = data.ports;
  rows.push(h("dl", { class: "facts" },
    fact("Reports as", [m.hardware, m.version].filter(Boolean).join(", ")),
    fact("Running for", upFor(m.uptime)),
    fact("Ports", ports && ports.total ? `${ports.up} of ${ports.total} connected` : ""),
    fact("Traffic now", ports && (ports.in_bps || ports.out_bps) ? `${rate(ports.in_bps)} in, ${rate(ports.out_bps)} out` : ""),
    fact("Port errors", ports && ports.errors ? h("span", { class: "warn-text", text: `${ports.errors} in the last few minutes` }) : "")));
  rows.push(...portSection(device, portList || []));
  if (data.alerts && data.alerts.length) {
    rows.push(h("ul", { class: "plain-list" }, data.alerts.map((a) =>
      h("li", { class: a.level === "alert" ? "down-text" : "warn-text" }, a.name, a.since ? h("span", { class: "muted", text: ` (since ${a.since})` }) : null))));
  }
  if (data.sensors && data.sensors.length) {
    const shown = data.sensors.slice(0, state.allSensors ? 60 : 8);
    rows.push(h("dl", { class: "facts sensors" }, ...shown.map((s) =>
      [h("dt", { text: s.label }), h("dd", { class: s.level === "alert" ? "down-text" : s.level === "warning" ? "warn-text" : "" }, sensorValue(s))])));
    if (data.sensors.length > 8) {
      rows.push(h("button", { type: "button", class: "link", onclick: () => { state.allSensors = !state.allSensors; loadHealthPanel(device); } },
        state.allSensors ? "Show fewer readings" : `Show all ${data.sensors.length} readings`));
    }
  }
  if (data.graphs && data.graphs.length) {
    const period = state.graphPeriod;
    rows.push(h("div", { class: "buttons" }, ["day", "week"].map((p) =>
      h("button", { type: "button", class: "link", "aria-pressed": p === period ? "true" : "false", onclick: () => { state.graphPeriod = p; loadHealthPanel(device); } }, p === "day" ? "Last day" : "Last week"))));
    rows.push(h("div", { class: "graphs" }, data.graphs.map((g) =>
      h("figure", {}, h("figcaption", { text: g.title }),
        h("a", { href: `/api/devices/${device.id}/health/graph?name=${encodeURIComponent(g.name)}&period=${period}&full=true`, target: "_blank", rel: "noopener", title: "Open the full graph" },
          h("img", { loading: "lazy", alt: `${g.title}, last ${period}`, src: `/api/devices/${device.id}/health/graph?name=${encodeURIComponent(g.name)}&period=${period}` }))))));
  }
  if (data.events && data.events.length) {
    rows.push(h("details", {}, h("summary", { text: "Recent log lines" }),
      h("ul", { class: "plain-list log" }, data.events.map((e) => h("li", {}, h("span", { class: "mono muted", text: e.time }), " ", e.text)))));
  }
  rows.push(linkChoice(device, data));
  return rows;
}

function speedText(bps) {
  if (!bps) return "";
  if (bps >= 1e9) return `${Number((bps / 1e9).toFixed(1))} Gb/s`;
  return `${Math.round(bps / 1e6)} Mb/s`;
}

function portSummary(p) {
  const bits = [p.label ? `${p.name} (${p.label})` : p.name];
  bits.push(p.state === "off" ? "switched off" : p.state === "down" ? "nothing connected" : `connected${p.speed ? ` at ${speedText(p.speed)}` : ""}`);
  if (p.errors) bits.push(`${p.errors} errors`);
  if (p.poe) bits.push(`powering a device (${p.poe} W)`);
  const names = p.plugged.map((x) => x.name);
  if (names.length) bits.push(`to ${names.join(", ")}`);
  return bits.join(", ");
}

function portDetail(p) {
  const state_ = p.state === "off" ? "Switched off" : p.state === "down" ? "Nothing connected" : `Connected${p.speed ? ` at ${speedText(p.speed)}` : ""}`;
  let plugged = null;
  if (p.plugged.length) {
    plugged = h("ul", { class: "plain-list" }, p.plugged.map((x) => h("li", {},
      x.device_id ? h("button", { type: "button", class: "link", onclick: () => showInRack(x.device_id) }, x.name) : h("strong", { text: x.name }),
      x.port ? h("span", { class: "muted", text: `, its port ${x.port}` }) : null,
      x.address && x.address !== x.name ? h("span", { class: "mono muted", text: ` ${x.address}` }) : null,
      x.note ? h("span", { class: "muted", text: ` (${x.note})` }) : null)));
  } else if (p.behind) {
    plugged = `${p.behind} devices are reached through this port, so it leads to another switch or an access point.`;
  }
  return h("div", { class: "port-detail", id: "port-detail" },
    h("h3", {}, p.name, p.label ? h("span", { class: "muted", text: ` ${p.label}` }) : null),
    h("dl", { class: "facts" },
      fact("State", state_),
      fact("Traffic now", p.state === "up" && (p.in_bps || p.out_bps) ? `${rate(p.in_bps)} in, ${rate(p.out_bps)} out` : ""),
      fact("Errors", p.errors ? h("span", { class: "warn-text", text: `${p.errors} in the last few minutes` }) : ""),
      fact("Power sent", p.poe ? `${p.poe} W down the cable (PoE)` : ""),
      fact("Plugged in", plugged)));
}

/* A small drawing of the device's sockets, each one lit by its state; choosing one shows what is known about it. */
function portSection(device, ports) {
  if (!ports.length) return [];
  const selected = ports.find((p) => p.id === state.portSel) || null;
  const rows = ports.length > 10 ? 2 : 1;
  const grid = h("div", { class: "ports", style: `--rows:${rows};--cols:${Math.ceil(ports.length / rows)}`, role: "group", "aria-label": "Ports" }, ports.map((p) => {
    const look = p.state === "off" ? "off" : p.state === "down" ? "down" : p.errors ? "err" : "up";
    return h("button", {
      type: "button", class: `port ${look}${p.poe ? " poe" : ""}`, title: portSummary(p), "aria-label": portSummary(p), "aria-pressed": selected === p ? "true" : "false",
      onclick: () => { state.portSel = selected === p ? null : p.id; loadHealthPanel(device); },
    });
  }));
  const up = ports.filter((p) => p.state === "up").length;
  return [
    h("h3", { class: "sub", text: "Ports" }),
    grid,
    selected ? portDetail(selected) : h("p", { class: "hint", text: `${up} of ${ports.length} connected. Green is connected, amber has errors, hollow has nothing connected, grey is switched off, and a dot marks a port powering a device. Choose a port for what is plugged into it.` }),
  ];
}

async function loadHealthPanel(device) {
  const seq = ++healthSeq;
  const current = () => seq === healthSeq && $("health-panel") && state.selectedId === device.id;
  let data;
  let ports = { ports: [] };
  try {
    [data, ports] = await Promise.all([api(`/api/devices/${device.id}/health`), api(`/api/devices/${device.id}/ports`).catch(() => ({ ports: [] }))]);
  } catch (err) {
    if (current()) $("health-panel").replaceChildren(h("h2", { text: "Health" }), h("p", { class: "error", text: err.message }));
    return;
  }
  if (current()) $("health-panel").replaceChildren(h("h2", { text: "Health" }), ...healthRows(device, data, ports.ports || []));
}

function showInRack(deviceId) {
  state.view = "racks";
  state.selectedId = deviceId;
  const device = findDevice(deviceId);
  if (device && device.rack_id) state.rackId = device.rack_id;
  showView();
}

function renderMonitoring() {
  const view = state.monitoringView;
  if (!view || !$("source-state")) return;
  const source = view.source;
  const box = $("source-state");
  if (!source) {
    box.replaceChildren(h("p", { class: "hint", text: "Not connected yet." }));
  } else {
    const read = view.state.fetched ? ago(Date.now() / 1000 - view.state.fetched) : "not yet";
    box.replaceChildren(
      h("dl", { class: "facts" },
        fact("System", source.says || view.kinds[source.kind]),
        fact("Address", h("span", { class: "mono", text: source.url })),
        fact("Devices", `${view.state.devices}, ${view.state.matched} matched to equipment here`),
        fact("Last read", read),
        fact("Certificate", source.pinned ? "Remembered when it was connected (it can't be verified the usual way)" : "")),
      view.state.error ? h("p", { class: "error", text: view.state.error }) : "");
  }
  $("source-edit").textContent = source ? "Edit" : "Connect a monitoring system";
  $("source-edit").className = source ? "secondary" : "primary";
  const alerts = view.alerts;
  $("alerts-title").textContent = !source ? "Alerts" : alerts.length ? `${alerts.length} ${alerts.length === 1 ? "thing needs" : "things need"} attention` : "Nothing is wrong right now";
  $("alerts-empty").textContent = !source ? "Alerts from the monitoring system appear here once one is connected." : alerts.length ? "" : "No open alerts, and every monitored device is answering.";
  $("alerts-empty").hidden = Boolean(source && alerts.length);
  $("alerts-list").replaceChildren(...alerts.map((a) => {
    const device = a.device_id ? findDevice(a.device_id) : null;
    return h("li", { class: "host card" },
      h("div", { class: "host-main" },
        h("div", { class: "host-title" },
          h("span", { class: `pill ${levelClass(a.level)}`, text: LEVEL_TEXT[a.level] }),
          h("strong", { text: device ? device.name : a.source_name || "Unknown device" })),
        h("p", { class: "host-facts", text: a.name }),
        a.since ? h("p", { class: "hint", text: `Since ${a.since}` }) : null,
        device ? null : h("p", { class: "hint", text: "Not recorded in rackmap." })),
      h("div", { class: "host-actions" }, device ? h("button", { type: "button", class: "secondary", onclick: () => showInRack(device.id) }, "Show in rack view") : null));
  }));
  const unmatched = view.unmatched;
  $("h-unmatched").hidden = unmatched.length === 0;
  $("unmatched-hint").hidden = unmatched.length === 0;
  $("unmatched-list").replaceChildren(...unmatched.map((d) =>
    h("li", { class: "host card" },
      h("div", { class: "host-main" },
        h("div", { class: "host-title" }, h("strong", { text: d.name }), d.address && d.address !== d.name ? h("span", { class: "mono", text: d.address }) : null),
        d.up ? null : h("p", { class: "down-text", text: "Not answering the monitoring system" })),
      h("div", { class: "host-actions" },
        h("button", { type: "button", class: "primary", onclick: () => openDeviceDialog(null, { name: d.name, ip: /^[0-9.]+$/.test(d.address) ? d.address : "", mac: "", admin_url: "", kind: null, searchText: "", rack_id: state.rackId, face: "front" }) }, "Add…")))));
}

async function loadMonitoring() {
  clearTimeout(monitoringTimer);
  state.monitoringView = await api("/api/monitoring");
  state.monitoring = Boolean(state.monitoringView.source);
  state.alertCount = state.monitoringView.alerts.length;
  renderNav();
  if (state.view !== "monitoring") return;
  renderMonitoring();
  monitoringTimer = setTimeout(() => { loadMonitoring().catch(() => {}); }, 60000);
}

function openMonitoringDialog() {
  const view = state.monitoringView || { kinds: { librenms: "LibreNMS" }, source: null };
  const form = $("monitoring-form");
  form.reset();
  $("monitoring-kind").replaceChildren(...Object.entries(view.kinds).map(([value, text]) => h("option", { value }, text)));
  if (view.source) {
    form.elements.kind.value = view.source.kind;
    form.elements.url.value = view.source.url;
  }
  $("monitoring-dialog-title").textContent = view.source ? "Monitoring system" : "Connect a monitoring system";
  $("monitoring-save").textContent = view.source ? "Save" : "Connect";
  $("monitoring-delete").hidden = !view.source;
  $("monitoring-token-note").hidden = !view.source;
  setError("monitoring-error", "");
  $("monitoring-dialog").showModal();
  form.elements.url.focus();
}

$("monitoring-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { kind: form.elements.kind.value, url: form.elements.url.value.trim(), token: form.elements.token.value.trim() };
  if (!body.url) return setError("monitoring-error", "Enter the address you open the monitoring system at.");
  setError("monitoring-error", "");
  $("monitoring-save").disabled = true;
  try {
    await api("/api/monitoring", body, "PUT");
    $("monitoring-dialog").close("");
    await reload();
    await loadMonitoring();
  } catch (err) {
    if (err.status === 401) { $("monitoring-dialog").close(""); return start(); }
    setError("monitoring-error", err.message);
  } finally {
    $("monitoring-save").disabled = false;
  }
});

$("monitoring-delete").addEventListener("click", async () => {
  $("monitoring-dialog").close("");
  if (!(await confirmDialog("Disconnect the monitoring system?", "rackmap forgets its address and token and stops showing health and alerts. Nothing changes in the monitoring system itself.", "Disconnect"))) return;
  try {
    await api("/api/monitoring", undefined, "DELETE");
    state.health = {};
    await reload();
    await loadMonitoring();
  } catch (err) { await failed(err); }
});

function monitoringScreen() {
  show("t-monitoring");
  $("version").textContent = state.version;
  $("source-edit").addEventListener("click", openMonitoringDialog);
}

/* ---------- settings: remote access over Tailscale ---------- */

function commandLine(text) {
  return h("p", {}, h("code", { class: "command", text }));
}

function renderRemote(view) {
  const box = $("remote-state");
  const buttons = $("remote-buttons");
  if (!box) return;
  const again = h("button", { type: "button", class: "secondary", onclick: () => loadRemote() }, "Check again");
  const act = (path, label, kind) => h("button", { type: "button", class: kind, onclick: async (event) => {
    event.target.disabled = true;
    setError("remote-error", "");
    try { renderRemote(await api(path, {})); } catch (err) {
      if (err.status === 401) return start();
      setError("remote-error", err.message);
      event.target.disabled = false;
    }
  } }, label);
  if (view.state === "missing") {
    box.replaceChildren(
      h("p", { text: "Tailscale isn't installed on the machine rackmap runs on (or rackmap runs in a container that can't see it)." }),
      h("p", { class: "hint" }, "To set it up: install Tailscale on that machine from ", h("a", { href: "https://tailscale.com/download", target: "_blank", rel: "noopener noreferrer" }, "tailscale.com/download"), ", sign it in to your tailnet, then let rackmap's account manage it:"),
      commandLine("sudo tailscale up"), commandLine(`sudo tailscale set --operator=${view.account}`),
      h("p", { class: "hint", text: "With Docker, run those on the host and publish rackmap's port to it; the README has the details." }));
    buttons.replaceChildren(again);
    return;
  }
  if (view.state === "stopped" || view.state === "needs-login") {
    box.replaceChildren(
      h("p", { text: view.state === "stopped" ? "Tailscale is installed on this machine but isn't running." : "Tailscale is installed on this machine but isn't signed in to a tailnet." }),
      view.error ? h("p", { class: "hint", text: view.error }) : null,
      h("p", { class: "hint", text: "On the machine, sign it in:" }), commandLine("sudo tailscale up"));
    buttons.replaceChildren(again);
    return;
  }
  if (view.state === "no-permission") {
    box.replaceChildren(
      h("p", { text: "Tailscale is running, but rackmap's account isn't allowed to manage it." }),
      h("p", { class: "hint", text: "Run this once on the machine:" }), commandLine(`sudo tailscale set --operator=${view.account}`));
    buttons.replaceChildren(again);
    return;
  }
  const direct = view.direct.map((url) => h("li", {}, h("a", { href: url, class: "mono", text: url })));
  box.replaceChildren(
    h("p", { class: "power-line" },
      h("span", { class: `pill ${view.serving ? "on" : "off"}`, text: view.serving ? "On" : "Off" }),
      view.serving ? h("a", { href: view.url, class: "mono", text: view.url }) : h("span", { class: "hint", text: "rackmap is not published to your tailnet." })),
    h("dl", { class: "facts" },
      fact("This machine", view.name ? h("span", { class: "mono", text: view.name }) : ""),
      fact("Tailnet", view.tailnet),
      fact("Tailscale address", view.addresses.length ? h("span", { class: "mono", text: view.addresses.join(", ") }) : "")),
    view.serving
      ? h("p", { class: "hint", text: "Open that address on any device signed in to your tailnet. You still sign in to rackmap as usual. Browsers accept its certificate without a warning." })
      : h("p", { class: "hint", text: view.certificates
        ? "Turning it on publishes rackmap at this machine's Tailscale name, with a certificate browsers accept. Only devices in your tailnet can reach it."
        : "Turning it on needs HTTPS certificates switched on for your tailnet: in the Tailscale admin console, open DNS and enable HTTPS Certificates. Tailscale says so if they are still off." }),
    direct.length ? h("details", {}, h("summary", { text: "Other ways in from your tailnet" }),
      h("p", { class: "hint", text: "These reach rackmap directly on its own port. They work whether or not the switch above is on, but the browser warns about rackmap's home-made certificate the first time." }),
      h("ul", { class: "plain-list" }, direct)) : null);
  buttons.replaceChildren(view.serving ? act("/api/remote/disable", "Turn off", "secondary") : act("/api/remote/enable", "Turn on remote access", "primary"), again);
}

async function loadRemote() {
  setError("remote-error", "");
  try { renderRemote(await api("/api/remote")); } catch (err) {
    if (err.status === 401) return start();
    setError("remote-error", err.message);
  }
}

const ACTION_NAME = { start: "Start", shutdown: "Shut down", reboot: "Restart", stop: "Force stop", snapshot: "Snapshot", protect: "Protect", unprotect: "Remove protection" };
const OUTCOME = { sent: "Sent", done: "Done", refused: "Refused by rackmap", failed: "Failed" };

async function loadActions() {
  let list;
  try {
    list = (await api("/api/actions")).actions;
  } catch (err) {
    if ($("actions-list")) $("actions-list").replaceChildren(h("p", { class: "error", text: err.message }));
    return;
  }
  if (!$("actions-list")) return;
  if (!list.length) return $("actions-list").replaceChildren(h("p", { class: "hint", text: "Nothing yet." }));
  $("actions-list").replaceChildren(h("ul", { class: "action-log" }, list.map((e) => h("li", {},
    h("span", { class: "muted mono", text: new Date(e.at.replace(" ", "T") + "Z").toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) }),
    h("span", {}, h("strong", { text: `${ACTION_NAME[e.action] || e.action}: ` }), `${e.target} · ${e.service_name} on ${e.device_name || "removed equipment"}`),
    h("span", { class: e.outcome === "failed" || e.outcome === "refused" ? "down-text" : "muted", text: `${OUTCOME[e.outcome] || e.outcome}${e.detail ? `: ${e.detail}` : ""} · by ${e.user}` })))));
}

function fillSso(view) {
  const form = $("sso-form");
  if (!form) return;
  const saved = view.sso;
  $("sso-return").textContent = view.return_address;
  form.elements.issuer.value = saved ? saved.issuer : "";
  form.elements.client_id.value = saved ? saved.client_id : "";
  form.elements.allowed.value = saved ? saved.allowed.join(", ") : "";
  form.elements.label.value = saved ? saved.label : "";
  form.elements.secret.value = "";
  $("sso-secret-note").hidden = !saved;
  $("sso-remove").hidden = !saved;
  $("sso-save").textContent = saved ? "Save and check" : "Turn on";
}

async function loadSso() {
  try { fillSso(await api("/api/sso")); } catch (err) { setError("sso-error", err.message); }
  const form = $("sso-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("sso-error", "");
    const body = Object.fromEntries(["issuer", "client_id", "secret", "allowed", "label"].map((k) => [k, k === "secret" ? form.elements[k].value : form.elements[k].value.trim()]));
    const button = $("sso-save");
    button.disabled = true;
    try {
      fillSso(await api("/api/sso", body, "PUT"));
      button.textContent = "Saved";
    } catch (err) {
      setError("sso-error", err.message);
    } finally {
      button.disabled = false;
    }
  });
  $("sso-remove").addEventListener("click", async () => {
    if (!(await confirmDialog("Turn off single sign-on?", "People will sign in with rackmap's own names and passwords only. Accounts made through the provider stay, but can't be entered until it is turned on again.", "Turn off"))) return;
    try {
      await api("/api/sso", undefined, "DELETE");
      fillSso(await api("/api/sso"));
    } catch (err) { await failed(err); }
  });
}

function renderAccount(info, fresh) {
  const box = $("account-state");
  if (!box) return;
  const rows = [];
  if (!info.has_password) {
    rows.push(h("p", { text: `You are signed in as “${info.name}” through your sign-in provider. That account has no rackmap password, so its password and second step are the provider's.` }));
    box.replaceChildren(...rows);
    $("password-form").hidden = true;
    return;
  }
  $("password-form").hidden = false;
  if (fresh) {
    rows.push(h("p", { class: "pill on", text: "The second step is on" }),
      h("p", { text: "Keep these recovery codes somewhere safe, such as your password manager. Each one gets you in once if you lose the phone. They are not shown again." }),
      h("pre", { class: "key-line", text: fresh.join("\n") }));
  } else if (info.second_step) {
    rows.push(h("p", {}, h("span", { class: "pill on", text: "Second step: on" }), ` Signing in as “${info.name}” with the password also asks for a code from your authenticator app. ${info.recovery_left} recovery code${info.recovery_left === 1 ? "" : "s"} left.`));
    rows.push(h("div", { class: "buttons" }, h("button", { type: "button", class: "danger", onclick: async () => {
      const password = $("password-form").elements.current.value;
      if (!password) return setError("account-error", "Type your current password in the box below first, then press this again.");
      try { renderAccount(await api("/api/account/second-step/remove", { password })); setError("account-error", ""); } catch (err) { setError("account-error", err.message); }
    } }, "Turn the second step off")));
  } else {
    rows.push(h("p", {}, h("span", { class: "pill off", text: "Second step: off" }), ` Anyone who learns the password for “${info.name}” can sign in. Add a code from an authenticator app (the same kind of app you use for other sites) to stop that.`));
    rows.push(h("div", { class: "buttons" }, h("button", { type: "button", class: "primary", onclick: async () => {
      let made;
      try { made = await api("/api/account/second-step/start", {}); } catch (err) { return setError("account-error", err.message); }
      setError("account-error", "");
      const code = h("input", { inputmode: "numeric", maxlength: "7", autocomplete: "one-time-code", placeholder: "123456" });
      const confirm = async () => {
        try {
          const done = await api("/api/account/second-step/confirm", { code: code.value });
          renderAccount(done, done.recovery_codes);
          setError("account-error", "");
        } catch (err) { setError("account-error", err.message); }
      };
      code.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); confirm(); } });
      box.replaceChildren(
        h("p", { text: "1. Scan this with your authenticator app, or type the key in by hand." }),
        h("img", { class: "qr", src: made.qr, alt: "QR code for the authenticator app", width: "200", height: "200" }),
        h("pre", { class: "key-line", text: made.secret }),
        h("p", { text: "2. Enter the six-digit code the app now shows for rackmap." }),
        h("div", { class: "buttons" }, code, h("button", { type: "button", class: "primary", onclick: confirm }, "Turn on"),
          h("button", { type: "button", class: "secondary", onclick: () => loadAccount() }, "Cancel")));
      code.focus();
    } }, "Set up the second step")));
  }
  box.replaceChildren(...rows);
}

async function loadAccount() {
  try { renderAccount(await api("/api/account")); } catch (err) { setError("account-error", err.message); }
}

function wirePasswordForm() {
  const form = $("password-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const { current, fresh, again } = form.elements;
    if (fresh.value !== again.value) return setError("account-error", "The two new passwords aren't the same.");
    try {
      await api("/api/account/password", { current: current.value, new: fresh.value });
      form.reset();
      setError("account-error", "");
      $("password-done").hidden = false;
    } catch (err) {
      $("password-done").hidden = true;
      setError("account-error", err.message);
    }
  });
}

function settingsScreen() {
  show("t-settings");
  $("version").textContent = state.version;
  wirePasswordForm();
  loadAccount();
  loadSso();
  loadActions();
}

function racksScreen() {
  show("t-home");
  $("version").textContent = state.version;
  $("add-rack").addEventListener("click", () => openRackDialog(null));
  $("add-first-rack").addEventListener("click", () => openRackDialog(null));
  $("add-device").addEventListener("click", () => openDeviceDialog(null));
  $("edit-rack").addEventListener("click", () => openRackDialog(currentRack()));
  $("face-front").addEventListener("click", () => { state.face = "front"; render(); });
  $("face-rear").addEventListener("click", () => { state.face = "rear"; render(); });
  $("cables-show").addEventListener("change", () => {
    state.showCables = $("cables-show").checked;
    try { localStorage.setItem("rackmap-cables", state.showCables ? "on" : "off"); } catch (err) { /* not remembered */ }
    render();
  });
  $("cables-open").addEventListener("click", () => openCablesDialog(null));
  const pick = $("readout-pick");
  $("readout-box").hidden = !state.monitoring;
  pick.value = state.readout;
  pick.addEventListener("change", () => {
    state.readout = pick.value;
    try { localStorage.setItem("rackmap-readout", state.readout); } catch (err) { /* not remembered */ }
    paintLive();
  });
}

async function showView() {
  bar.hidden = false;
  clearTimeout(scanTimer);
  clearTimeout(liveTimer);
  clearTimeout(monitoringTimer);
  if (state.view !== "consoles" || !$("term-card")) { const keep = state.view === "consoles" ? state.openPort : null; closeTerminal(); state.openPort = keep; }
  if (state.view !== "screen") closeScreen();
  if (!DRAWER_VIEWS.includes(state.view)) renderDetail();
  renderNav();
  try {
    if (state.view === "screen") {
      screenScreen();
      connectScreen();
    } else if (state.view === "discovery") {
      discoveryScreen();
      await loadDiscovery(true);
    } else if (state.view === "consoles") {
      const wanted = state.openPort;
      consolesScreen();
      await loadConsoles();
      if (wanted !== null) { openTerminal(wanted); renderConsoles(); }
    } else if (state.view === "monitoring") {
      monitoringScreen();
      await loadMonitoring();
    } else if (state.view === "settings") {
      settingsScreen();
      await loadRemote();
    } else if (state.view === "dashboard") {
      dashboardScreen();
      render();
      refreshLive();
    } else if (state.view === "devices") {
      devicesScreen();
      render();
      refreshLive();
    } else if (state.view === "map") {
      mapScreen();
      await loadMap();
      refreshLive();
    } else {
      racksScreen();
      render();
      refreshLive();
    }
  } catch (err) {
    if (err.status === 401) return start();
    message(err.message);
  }
}

for (const view of VIEWS) {
  $(`nav-${view}`).addEventListener("click", () => { if (view === "consoles" && state.view === "consoles") return; state.view = view; showView(); });
}

async function start() {
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    message(err.message);
    return;
  }
  state.version = status.version;
  if (status.needs_setup) return setupScreen();
  if (!status.user) return loginScreen(status.sso);
  if (location.search) history.replaceState(null, "", "/");
  try {
    await reload();
  } catch (err) {
    if (err.status === 401) return loginScreen(status.sso);
    message(err.message);
    return;
  }
  await showView();
}

$("sign-out").addEventListener("click", async () => {
  closeTerminal();
  closeScreen();
  try { await api("/api/logout", {}); } catch (err) { /* signed out either way */ }
  await start();
});

start();
