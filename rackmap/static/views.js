"use strict";

/* The pages that show everything at once: the dashboard, the table of all equipment, and the
   network map. They share app.js's state and helpers, and its slide-in details panel. */

/* ---------- dashboard ---------- */

function tile(label, value, note, tone, onclick) {
  return h(onclick ? "button" : "div", { class: `tile ${tone || ""}`, type: onclick ? "button" : null, onclick }, h("span", { class: "tile-label", text: label }), h("span", { class: "tile-value", text: value }), h("span", { class: "tile-note", text: note || "" }));
}

function pickDevice(id) {
  state.selectedId = id;
  render();
}

function renderDashboard() {
  const dash = state.dash;
  const devices = allDevices();
  const checked = devices.filter((d) => state.live[String(d.id)]);
  const down = checked.filter((d) => !state.live[String(d.id)].up);
  const tiles = [tile("Equipment", String(devices.length), checked.length ? `${checked.length - down.length} answering${down.length ? `, ${down.length} not` : ""}` : "not checked yet", down.length ? "bad" : "", () => { state.view = "devices"; showView(); })];
  const attention = [];
  for (const d of down) attention.push({ tone: "alert", text: `${d.name} is not answering`, id: d.id });
  if (dash) {
    for (const a of dash.alerts) attention.push({ tone: a.level === "warning" ? "warn" : "alert", text: `${a.source_name || "Monitoring"}: ${a.name}`, id: a.device_id });
    for (const sv of dash.services) for (const name of sv.unhealthy) attention.push({ tone: "warn", text: `${name} on ${sv.device_name} reports itself unhealthy`, id: sv.device_id });
    const known = dash.services.filter((sv) => sv.known);
    const things = known.reduce((n, sv) => n + sv.total, 0);
    const running = known.reduce((n, sv) => n + sv.running, 0);
    tiles.push(tile("Needs attention", String(attention.length), attention.length ? "listed below" : "all quiet", attention.length ? "bad" : "good"));
    tiles.push(tile("Connections", String(dash.services.length), known.length ? `${running} of ${things} things up` : "not asked yet", ""));
    const watts = Object.values(state.readouts || {}).filter((r) => r.runtime !== undefined && r.power_kind === "draw").reduce((n, r) => n + r.power, 0);
    if (watts) tiles.push(tile("Power", `${Math.round(watts)} W`, "through the battery backups", ""));
    if (dash.new_found) tiles.push(tile("Found on the network", String(dash.new_found), "not yet recorded", "", () => { state.view = "discovery"; showView(); }));
    $("dash-sub").textContent = `${dash.racks} rack${dash.racks === 1 ? "" : "s"} · ${dash.unplaced} piece${dash.unplaced === 1 ? "" : "s"} of equipment not in a rack`;
    $("dash-services-list").replaceChildren(dash.services.length ? h("ul", { class: "dash-list" }, dash.services.map((sv) => h("li", {},
      h("button", { type: "button", class: "row-button", onclick: () => pickDevice(sv.device_id) },
        h("span", { class: "label-name" }, sv.device_name, h("span", { class: "muted", text: ` · ${sv.kind_name}` })),
        h("span", { class: "label-meta", text: `${sv.known ? (sv.total ? `${sv.running} of ${sv.total} ${sv.word}` : "connected") : "not asked yet"} · ${sv.level_name}` })))))
      : h("p", { class: "hint", text: Object.keys(state.serviceKinds).length ? "No connections yet. Open a piece of equipment and choose “Connect a service”." : "Connections to the software and switches on your equipment come from add-ons, and none are installed here." }));
    $("dash-actions-list").replaceChildren(dash.actions.length ? h("ul", { class: "dash-list plain-text" }, dash.actions.map((e) => h("li", {},
      h("span", {}, h("strong", { text: `${ACTION_NAME[e.action] || e.action}: ` }), `${e.target} on ${e.device_name || "removed equipment"}`),
      h("span", { class: e.outcome === "failed" || e.outcome === "refused" ? "down-text" : "muted", text: `${OUTCOME[e.outcome] || e.outcome} · ${e.user} · ${new Date(`${e.at.replace(" ", "T")}Z`).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` }))))
      : h("p", { class: "hint", text: "Nothing has been done through rackmap yet." }));
  }
  $("dash-tiles").replaceChildren(...tiles);
  $("dash-attention-list").replaceChildren(attention.length ? h("ul", { class: "dash-list" }, attention.slice(0, 12).map((a) => h("li", {},
    h(a.id ? "button" : "div", { type: a.id ? "button" : null, class: "row-button", onclick: a.id ? () => pickDevice(a.id) : null }, h("span", { class: "label-name" }, h("span", { class: `dot ${a.tone}` }), a.text)))))
    : h("p", { class: "hint", text: checked.length || dash ? "Nothing needs attention." : "Checking…" }));
}

let dashWarming = false;
async function loadDashboard() {
  try { state.dash = await api("/api/dashboard"); } catch (err) { return; }
  if (state.view === "dashboard" && $("dash")) renderDashboard();
  // connections that haven't been asked since rackmap started are asked once, quietly, one at a time
  const cold = state.dash.services.filter((sv) => !sv.known);
  if (cold.length && !dashWarming) {
    dashWarming = true;
    for (const sv of cold) { try { await api(`/api/services/${sv.id}/status`); } catch (err) { /* shown on the equipment itself */ } }
    dashWarming = false;
    try { state.dash = await api("/api/dashboard"); } catch (err) { return; }
    if (state.view === "dashboard" && $("dash")) renderDashboard();
  }
}

function dashboardScreen() {
  show("t-dashboard");
  $("version").textContent = state.version;
  loadDashboard();
}

/* ---------- devices: every piece of equipment in one table ---------- */

const DEVICE_COLUMNS = [["name", "Name"], ["model", "Model"], ["ip", "Address"], ["where", "Where"], ["links", "Connections"], ["reading", "Readings"]];

function deviceRowData(d) {
  const rack = state.racks.find((r) => r.id === d.rack_id);
  const r = (state.readouts || {})[String(d.id)] || {};
  return { d, name: d.name, model: [d.manufacturer, d.model].filter(Boolean).join(" "), ip: d.ip || "",
    where: rack && d.position_u !== null ? `${rack.name}, ${span(d)}` : "Not in a rack",
    links: [d.has_controller ? "Controller" : "", ...(d.services || []).map((sv) => sv.kind_name.replace(/ \(.*\)$/, ""))].filter(Boolean).join(", "),
    reading: [r.temp !== undefined ? `${r.temp} °C` : "", r.power !== undefined ? `${Math.round(r.power)} W` : "", r.runtime !== undefined ? `${r.runtime} min battery` : ""].filter(Boolean).join(" · ") };
}

function ipKey(ip) {
  return /^\d+\.\d+\.\d+\.\d+$/.test(ip) ? ip.split(".").map((n) => n.padStart(3, "0")).join(".") : `~${ip}`;
}

function renderDevices() {
  const { key, dir } = state.deviceSort;
  $("device-head").replaceChildren(...DEVICE_COLUMNS.map(([k, label]) => h("th", { scope: "col", "aria-sort": k === key ? (dir === 1 ? "ascending" : "descending") : null },
    k === "reading" ? label : h("button", { type: "button", class: "sort", onclick: () => { state.deviceSort = { key: k, dir: k === key ? -dir : 1 }; renderDevices(); paintLive(); } }, label, k === key ? (dir === 1 ? " ▲" : " ▼") : ""))));
  const needle = state.deviceFilter.trim().toLowerCase();
  const rows = allDevices().map(deviceRowData).filter((x) => !needle || `${x.name} ${x.model} ${x.ip} ${x.where} ${x.links}`.toLowerCase().includes(needle));
  const value = (x) => (key === "ip" ? ipKey(x.ip) : String(x[key]).toLowerCase());
  rows.sort((a, b) => (value(a) < value(b) ? -dir : value(a) > value(b) ? dir : a.name.localeCompare(b.name)));
  $("device-none").hidden = allDevices().length > 0;
  $("device-rows").replaceChildren(...rows.map((x) => h("tr", { class: x.d.id === state.selectedId ? "selected" : "", tabindex: "0",
    onclick: () => pickDevice(x.d.id), onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pickDevice(x.d.id); } } },
    h("td", {}, h("span", { class: "dot", "data-live": String(x.d.id), hidden: true }), h("strong", { text: x.name })),
    h("td", { text: x.model || "—" }), h("td", { class: "mono", text: x.ip || "—" }), h("td", { text: x.where }), h("td", { text: x.links || "—" }), h("td", { text: x.reading || "—" }))));
}

function devicesScreen() {
  show("t-devices");
  $("version").textContent = state.version;
  $("add-device").addEventListener("click", () => openDeviceDialog(null));
  $("device-filter").value = state.deviceFilter;
  $("device-filter").addEventListener("input", (event) => { state.deviceFilter = event.target.value; renderDevices(); paintLive(); });
}

/* ---------- network map: what is plugged into what ---------- */

let mapData = { cables: [], suggestions: [], wireless: [] };

async function loadMap(fresh) {
  try {
    const [cables, wiring] = await Promise.all([api("/api/cables"), fresh ? api("/api/wiring/refresh", {}) : api("/api/wiring")]);
    mapData = { ...cables, wireless: wiring.wireless, switches: wiring.switches, failed: wiring.failed || [] };
    if (fresh) mapData.suggestions = (await api("/api/cables")).suggestions;
  } catch (err) { if ($("map-box")) $("map-box").replaceChildren(h("p", { class: "error", text: err.message })); return; }
  drawMap();
}

function mapScreen() {
  show("t-map");
  $("version").textContent = state.version;
  $("map-refresh").hidden = !state.wiring;   // working out the wiring is an add-on
  $("map-refresh").addEventListener("click", async (event) => {
    event.target.disabled = true;
    event.target.textContent = "Asking the switches…";
    await loadMap(true);
    event.target.disabled = false;
    event.target.textContent = "Check the wiring again";
  });
}

function drawMap() {
  const box = $("map-box");
  if (!box) return;
  const devices = allDevices();
  // everything drawn is a node: recorded equipment ("d<id>") and things only seen on the network ("x<address>")
  const nodes = new Map(devices.map((d) => [`d${d.id}`, { id: `d${d.id}`, device: d, name: d.name, sub: d.ip || d.model || "" }]));
  const links = [];
  for (const c of mapData.cables) links.push({ a: `d${c.a_device_id}`, b: `d${c.b_device_id}`, kind: "sure", tip: `${c.a_name}${c.a_port ? ` ${c.a_port}` : ""} — ${c.b_name}${c.b_port ? ` ${c.b_port}` : ""}` });
  for (const c of mapData.suggestions) links.push({ a: `d${c.a_device_id}`, b: `d${c.b_device_id}`, kind: "guess", tip: `${c.a_name}${c.a_port ? ` ${c.a_port}` : ""} — ${c.b_name}${c.b_port ? ` ${c.b_port}` : ""} (seen, not confirmed)` });
  for (const w of (mapData.wireless || []).slice(0, 80)) {
    const id = w.device_id ? `d${w.device_id}` : `x${w.mac}`;
    if (!w.device_id) nodes.set(id, { id, device: null, name: w.name, sub: w.ip && w.ip !== w.name ? w.ip : w.mac, small: true });
    links.push({ a: `d${w.via_device_id}`, b: id, kind: w.wifi ? "air" : "beyond", tip: `${w.name} ${w.wifi ? "over Wi-Fi through" : "reached through"} ${(nodes.get(`d${w.via_device_id}`) || {}).name || ""}${w.port ? ` ${w.port}` : ""}` });
  }
  const real = links.filter((l) => nodes.has(l.a) && nodes.has(l.b));
  const near = new Map([...nodes.keys()].map((id) => [id, new Set()]));
  for (const l of real) { near.get(l.a).add(l.b); near.get(l.b).add(l.a); }
  const joined = [...nodes.keys()].filter((id) => near.get(id).size);
  const air = real.filter((l) => l.kind === "air").length;
  $("map-sub").textContent = joined.length
    ? [`${mapData.cables.length} recorded cable${mapData.cables.length === 1 ? "" : "s"}`, mapData.suggestions.length ? `${mapData.suggestions.length} more seen but not confirmed (dashed)` : "", air ? `${air} over Wi-Fi (dotted)` : ""].filter(Boolean).join(" · ")
    : "";
  if (!joined.length) {
    box.replaceChildren(h("p", { class: "hint", text: state.wiring ? "Nothing to draw yet. Connect your switches (and your firewall, so rackmap can tell which address is which device) under each one's “Connect a service”, then choose “Check the wiring again”. Cables can also be recorded by hand from the rack view." : "Nothing to draw yet. Record cables from the rack view (or accept the ones your monitoring system has seen) and they are drawn here." }));
    return;
  }
  // rows: start from the best-connected thing, then everything one hop away, and so on
  const level = new Map();
  const left = new Set(joined);
  while (left.size) {
    // the firewall goes at the top when there is one; otherwise whatever has the most cables
    const gate = (id) => (nodes.get(id).device && (nodes.get(id).device.services || []).some((sv) => sv.kind === "pfsense") ? 1 : 0);
    const cabled = (id) => real.filter((l) => l.kind !== "air" && l.kind !== "beyond" && (l.a === id || l.b === id)).length;
    const root = [...left].sort((a, b) => gate(b) - gate(a) || cabled(b) - cabled(a) || near.get(b).size - near.get(a).size)[0];
    const base = level.size ? Math.max(...level.values()) + 1 : 0;
    const queue = [root];
    level.set(root, base);
    left.delete(root);
    while (queue.length) {
      const at = queue.shift();
      for (const next of near.get(at)) if (left.has(next)) { left.delete(next); level.set(next, level.get(at) + 1); queue.push(next); }
    }
  }
  const rows = [];
  for (const [id, lv] of level) (rows[lv] = rows[lv] || []).push(id);
  // keep each row in the order of what it hangs from, so lines cross less
  const parentOf = (id) => [...near.get(id)].find((n) => level.get(n) === level.get(id) - 1);
  rows.forEach((row, y) => { if (y) row.sort((a, b) => rows[y - 1].indexOf(parentOf(a)) - rows[y - 1].indexOf(parentOf(b)) || nodes.get(a).name.localeCompare(nodes.get(b).name)); });
  const W = 168, H = 46, GX = 22, GY = 78;
  const widest = Math.max(...rows.filter(Boolean).map((r) => r.length));
  const width = Math.max(widest * (W + GX) + GX, 600);
  const at = new Map();
  rows.forEach((row, y) => { if (!row) return; const start = (width - (row.length * (W + GX) - GX)) / 2; row.forEach((id, x) => at.set(id, { x: start + x * (W + GX), y: 20 + y * (H + GY) })); });
  const NS = "http://www.w3.org/2000/svg";
  const el = (name, attrs, text) => { const n = document.createElementNS(NS, name); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text !== undefined) n.textContent = text; return n; };
  const svg = el("svg", { viewBox: `0 0 ${width} ${40 + rows.length * (H + GY) - GY}`, class: "netmap", role: "img", "aria-label": "Network map" });
  svg.style.minWidth = `${Math.round(width * 0.62)}px`;
  for (const l of real) {
    const a = at.get(l.a), b = at.get(l.b);
    const mid = a.y === b.y ? a.y - 26 : (a.y + b.y + H) / 2;
    const line = el("path", { d: `M${a.x + W / 2} ${a.y + H / 2} C${a.x + W / 2} ${mid}, ${b.x + W / 2} ${mid}, ${b.x + W / 2} ${b.y + H / 2}`, class: `map-link ${l.kind}` });
    line.append(el("title", {}, l.tip));
    svg.append(line);
  }
  for (const [id, pos] of at) {
    const node = nodes.get(id);
    const live = node.device ? state.live[String(node.device.id)] : null;
    const picked = node.device && node.device.id === state.selectedId;
    const g = el("g", { class: `map-node${picked ? " selected" : ""}${node.small ? " small" : ""}`, transform: `translate(${pos.x} ${pos.y})`, ...(node.device ? { tabindex: "0", role: "button" } : {}) });
    g.append(el("rect", { width: W, height: H, rx: 8 }), el("circle", { cx: 14, cy: H / 2, r: 4.5, class: node.device ? (live ? (live.up ? "up" : "down") : "unknown") : "seen" }),
      el("text", { x: 26, y: 20 }, node.name.length > 20 ? `${node.name.slice(0, 19)}…` : node.name), el("text", { x: 26, y: 35, class: "sub" }, node.sub.length > 22 ? `${node.sub.slice(0, 21)}…` : node.sub));
    if (node.device) {
      g.addEventListener("click", () => pickDevice(node.device.id));
      g.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pickDevice(node.device.id); } });
    } else {
      g.append(el("title", {}, "Seen on the network, not recorded as equipment"));
    }
    svg.append(g);
  }
  const loose = devices.filter((d) => !near.get(`d${d.id}`).size);
  box.replaceChildren(svg,
    h("p", { class: "port-key" }, h("span", { class: "map-key sure" }), "recorded cable", h("span", { class: "map-key guess" }), "seen, not confirmed", h("span", { class: "map-key air" }), "Wi-Fi", h("span", { class: "map-key beyond" }), "reached through it"),
    loose.length ? h("p", { class: "hint", text: `Not drawn, because nothing shows how they are connected: ${loose.map((d) => d.name).join(", ")}.` }) : "",
    (mapData.failed || []).length ? h("p", { class: "error", text: `Couldn't ask: ${mapData.failed.join("; ")}` }) : "");
}
