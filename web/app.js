/* Packaging Studio — interface */
(() => {
"use strict";

const $ = (id) => document.getElementById(id);

const api = async (path, opts = {}) => {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};

const state = {
  config: {}, agents: [], boxTypes: {}, models: [], scenes: [], pricing: {},
  projectId: null, project: null,
  pending: [], view: "flat", busy: false, three: null,
  surface: "studio", scene: "studio", editing: null,
};

/* small helpers ------------------------------------------------------ */

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function money(usd) {
  const n = Number(usd) || 0;
  if (n === 0) return "$0.00";
  if (n < 0.01) return "$" + n.toFixed(4);
  if (n < 1) return "$" + n.toFixed(3);
  return "$" + n.toFixed(2);
}

function tokens(n) {
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}

/* A deliberately small Markdown subset: paragraphs, lists, bold, italic,
   code, headings. Escaped first, so nothing the model writes becomes markup. */
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
}

function markdown(src) {
  const lines = escapeHtml(src).split("\n");
  let html = "", list = null, para = [];
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const closePara = () => { if (para.length) { html += `<p>${inlineMd(para.join("<br>"))}</p>`; para = []; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { closePara(); closeList(); continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      closePara();
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${inlineMd(m[1])}</li>`; continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      closePara();
      if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${inlineMd(m[1])}</li>`; continue;
    }
    if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
      closePara(); closeList(); html += `<h4>${inlineMd(m[1])}</h4>`; continue;
    }
    closeList(); para.push(line);
  }
  closePara(); closeList();
  return html;
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6500 : 3200);
}

function readFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(file);
  });
}

/* boot ---------------------------------------------------------------- */

async function boot() {
  // On the website, hosted.js first checks the access code and takes over
  // project storage. Locally there is nothing to wait for.
  await (window.PS_READY || null);
  try {
    const status = await api("/api/status");
    Object.assign(state, {
      config: status.config, agents: status.agents, boxTypes: status.box_types,
      models: status.models, scenes: status.scenes || [], pricing: status.pricing || {},
    });
    fillBoxTypes();
    fillModels();
    paintKeyState();
    paintScenes();
  } catch (err) {
    toast("Cannot reach the server: " + err.message, true);
    return;
  }
  await loadProjects();
  wire();
  if (!state.config.anthropic) {
    if (window.PS) toast("This site is not set up yet: its owner still has to add the Anthropic key.", true);
    else $("settings").showModal();
  }
}

/* Images and files: plain URLs locally, stored copies on the website. */
function setAssetUrl(id, apply) {
  const url = `/api/projects/${state.projectId}/assets/${id}`;
  if (!window.PS) { apply(url); return; }
  window.PS.assetUrl(state.projectId, id).then((u) => { if (u) apply(u); });
}

function paintKeyState() {
  const dot = $("keyDot"), label = $("keyLabel");
  if (state.config.anthropic) {
    dot.className = "dot ok";
    label.textContent = state.config.model_label || "Settings";
  } else {
    dot.className = "dot bad";
    label.textContent = "Add API key";
  }
  $("anthropicState").textContent = state.config.anthropic ? "saved" : "";
  $("geminiState").textContent = state.config.image_provider === "gemini" ? "saved" : "";
}

function fillBoxTypes() {
  const sel = $("boxType");
  sel.innerHTML = "";
  for (const [key, meta] of Object.entries(state.boxTypes)) {
    const opt = document.createElement("option");
    opt.value = key; opt.textContent = meta.name; opt.title = meta.use;
    sel.appendChild(opt);
  }
}

function fillModels() {
  const sel = $("modelSelect");
  sel.innerHTML = "";
  for (const m of state.models) {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = `${m.label} — ${m.note}`;
    sel.appendChild(opt);
  }
  sel.value = state.config.model;
  paintPrice();
  sel.addEventListener("change", paintPrice);
}

function priceFor(id) {
  const keys = Object.keys(state.pricing || {}).sort((a, b) => b.length - a.length);
  const key = keys.find((k) => id.startsWith(k));
  return key ? state.pricing[key] : null;
}

function paintPrice() {
  const p = priceFor($("modelSelect").value);
  $("priceNote").textContent = p
    ? `$${p.input.toFixed(2)} per million tokens in, $${p.output.toFixed(2)} out. A full design with a few rounds of edits usually costs cents to a couple of dollars.`
    : "";
}

/* projects ------------------------------------------------------------- */

async function loadProjects(selectId) {
  const { projects } = await api("/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = "";
  if (!projects.length) {
    const opt = document.createElement("option");
    opt.textContent = "No projects yet"; opt.value = "";
    sel.appendChild(opt);
    state.projectId = null; state.project = null;
    paintAll();
    return;
  }
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.box_type
      ? `${p.name} — ${state.boxTypes[p.box_type]?.short || p.box_type}` : p.name;
    sel.appendChild(opt);
  }
  const target = selectId || state.projectId || projects[0].id;
  sel.value = projects.some((p) => p.id === target) ? target : projects[0].id;
  await openProject(sel.value);
}

async function openProject(id) {
  if (!id) return;
  state.projectId = id;
  const { project } = await api(`/api/projects/${id}`);
  state.project = project;
  state.pending = [];
  closeEditor();
  paintAll();
}

async function newProject() {
  const name = prompt("Name for the new project", "Untitled project");
  if (name === null) return;
  const { project } = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name: name || "Untitled project", agent: "packaging_designer" }),
  });
  await loadProjects(project.id);
  $("input").focus();
}

/* painting -------------------------------------------------------------- */

function paintAll() {
  paintMessages();
  paintMeter();
  paintStructure();
  paintLayers();
  paintAssets();
  paintAttachments();
  paintGallery();
  refreshView();
}

function paintMeter() {
  const usage = state.project?.usage;
  const meter = $("meter");
  if (!usage || !usage.runs) { meter.hidden = true; return; }
  meter.hidden = false;
  $("meterCost").textContent = money(usage.cost_usd);
  $("meterLabel").textContent = `${usage.runs} request${usage.runs === 1 ? "" : "s"} · ` +
    `${tokens(usage.input_tokens)} in, ${tokens(usage.output_tokens)} out`;
}

function paintMessages() {
  const box = $("messages");
  box.innerHTML = "";
  const project = state.project;
  if (!project) {
    box.innerHTML = `<p class="opening">Create a project to get started.</p>`;
    return;
  }
  const agent = state.agents.find((a) => a.key === project.agent) || state.agents[0];
  $("agentTitle").textContent = agent ? agent.title : "Agent";

  if (!project.messages.length && agent) {
    const p = document.createElement("p");
    p.className = "opening";
    p.textContent = agent.greeting;
    box.appendChild(p);
    if (agent.starters?.length) {
      const wrap = document.createElement("div");
      wrap.className = "starters";
      for (const s of agent.starters) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "starter"; b.textContent = s + "…";
        b.onclick = () => {
          const input = $("input");
          input.value = s;
          input.focus();
          input.setSelectionRange(s.length, s.length);
          input.dispatchEvent(new Event("input"));
        };
        wrap.appendChild(b);
      }
      box.appendChild(wrap);
    }
  }
  for (const msg of project.messages) {
    box.appendChild(bubble(msg.role === "user" ? "user" : "agent", msg.text));
    if (msg.cost) box.appendChild(costLine(msg.cost));
  }
  box.scrollTop = box.scrollHeight;
}

function bubble(kind, text) {
  const el = document.createElement("div");
  el.className = "msg " + kind;
  if (kind === "agent") el.innerHTML = markdown(text);
  else el.textContent = text;
  return el;
}

function costLine(cost) {
  const el = document.createElement("div");
  el.className = "cost";
  const bits = [money(cost.cost_usd)];
  if (cost.steps) bits.push(`${cost.steps} step${cost.steps === 1 ? "" : "s"}`);
  bits.push(`${tokens(cost.input_tokens)} in, ${tokens(cost.output_tokens)} out`);
  if (cost.cached_tokens) bits.push(`${tokens(cost.cached_tokens)} cached`);
  el.textContent = bits.join("  ·  ");
  return el;
}

function paintStructure() {
  const dl = state.project?.dieline;
  if (!dl) {
    $("boxType").value = "ste";
    for (const id of ["dimW", "dimD", "dimH"]) $(id).value = "";
    $("dimW").placeholder = "50"; $("dimD").placeholder = "40"; $("dimH").placeholder = "120";
    $("structureNote").textContent = "Millimetres, inside dimensions. The designer fills these in, or set them yourself.";
    return;
  }
  $("boxType").value = dl.box_type;
  $("dimW").value = Math.round(dl.dims.width);
  $("dimD").value = Math.round(dl.dims.depth);
  $("dimH").value = Math.round(dl.dims.height);
  const blank = dl.blank || dl.sheet;
  $("structureNote").textContent =
    `Blank ${Math.round(blank.width)} × ${Math.round(blank.height)} mm. ${dl.material_hint}`;
}

const panelGeo = (id) => state.project?.dieline?.panels?.find((p) => p.id === id);
const panelLabel = (id) => panelGeo(id)?.label || id;

function paintLayers() {
  const list = $("layerList");
  const layers = state.project?.layers || [];
  $("layerCount").textContent = layers.length || "";
  list.innerHTML = "";
  if (!layers.length) {
    list.innerHTML = `<p class="note">Nothing drawn yet. Once the designer builds
      artwork, every element shows up here. Click one to edit it.</p>`;
    return;
  }
  let lastPanel = null;
  for (const layer of layers) {
    if (layer.panel !== lastPanel) {
      lastPanel = layer.panel;
      const head = document.createElement("div");
      head.className = "layer-head";
      head.textContent = panelLabel(layer.panel);
      list.appendChild(head);
    }
    const row = document.createElement("div");
    row.className = "layer" + (layer.visible === false ? " off" : "") +
      (state.editing === layer.id ? " selected" : "");
    row.dataset.id = layer.id;

    const swatch = document.createElement("span");
    const hex = layer.fill && /^#[0-9a-f]{3,8}$/i.test(layer.fill) ? layer.fill : null;
    swatch.className = "swatch" + (hex ? "" : " none");
    if (hex) swatch.style.background = hex;

    const name = document.createElement("span");
    name.className = "lname";
    name.textContent = layer.text || layer.name || layer.type;
    name.title = name.textContent;

    const type = document.createElement("span");
    type.className = "ltype";
    type.textContent = (layer.type || "").replace("_", " ");

    const eye = document.createElement("button");
    eye.className = "eye";
    eye.textContent = layer.visible === false ? "hidden" : "●";
    eye.title = layer.visible === false ? "Show" : "Hide";
    eye.onclick = (e) => { e.stopPropagation(); toggleLayer(layer); };

    row.append(swatch, name, type, eye);
    row.onclick = () => openEditor(layer, row);
    row.onmouseenter = () => highlight(layer.id, true);
    row.onmouseleave = () => highlight(layer.id, false);
    list.appendChild(row);
  }
}

/* Hovering a layer dims everything else in the flat view. */
function highlight(id, on) {
  const svg = $("view-flat").querySelector("svg");
  if (!svg) return;
  svg.classList.toggle("focusing", on);
  svg.querySelectorAll(`[data-layer="${CSS.escape(id)}"]`).forEach((g) => g.classList.toggle("lit", on));
}

async function patchLayers(patches) {
  const data = await api(`/api/projects/${state.projectId}/layers`, {
    method: "POST", body: JSON.stringify({ patches }),
  });
  state.project.layers = data.layers;
  paintLayers();
  refreshView();
  return data;
}

async function toggleLayer(layer) {
  try { await patchLayers([{ op: "update", id: layer.id, set: { visible: layer.visible === false } }]); }
  catch (err) { toast(err.message, true); }
}

/* layer editor ---------------------------------------------------------- */

function fullLayer(id) {
  for (const panel of Object.values(state.project?.artwork?.panels || {}))
    for (const l of panel.layers || []) if (l.id === id) return l;
  return null;
}

function openEditor(summary, anchor) {
  // Measure before repainting: paintLayers() replaces the row we were given.
  const r = anchor?.getBoundingClientRect?.() || { top: 120, left: window.innerWidth - 300 };
  const layer = fullLayer(summary.id) || summary;
  state.editing = layer.id;
  paintLayers();
  const fields = $("editorFields");
  fields.innerHTML = "";
  $("editorTitle").textContent = `${panelLabel(summary.panel)} · ${(layer.type || "").replace("_", " ")}`;

  const add = (html) => { fields.insertAdjacentHTML("beforeend", html); };
  const colour = typeof layer.fill === "string" ? layer.fill : (layer.color || "#111111");

  if (layer.type === "text" || layer.type === "circle_text") {
    add(`<label class="field"><span>Text</span><textarea data-key="text">${escapeHtml(layer.text || "")}</textarea></label>`);
    add(`<div class="pair">
      <label class="field"><span>Size, mm</span><input data-key="size" type="number" step="0.1" min="1.5" value="${layer.size ?? 3}"></label>
      <label class="field"><span>Colour</span><input data-key="fill" type="color" value="${toHex(colour)}"></label></div>`);
  } else if (layer.type === "barcode") {
    add(`<label class="field"><span>EAN-13 digits</span><input data-key="digits" type="text" value="${escapeHtml(layer.digits || "")}" placeholder="12 or 13 digits"></label>`);
    add(`<p class="note">Your GTIN comes from GS1. The check digit is worked out for you.</p>`);
  } else if (layer.type === "icon") {
    add(`<div class="pair">
      <label class="field"><span>Label</span><input data-key="label" type="text" value="${escapeHtml(layer.label || "")}" placeholder="e.g. 12M"></label>
      <label class="field"><span>Colour</span><input data-key="color" type="color" value="${toHex(layer.color || colour)}"></label></div>`);
  } else if (typeof layer.fill === "string" || ["rect", "ellipse", "arch", "star", "circle"].includes(layer.type)) {
    add(`<label class="field"><span>Colour</span><input data-key="fill" type="color" value="${toHex(colour)}"></label>`);
  } else if (layer.type === "pattern") {
    add(`<label class="field"><span>Colour</span><input data-key="color" type="color" value="${toHex(layer.color || "#000000")}"></label>`);
  }
  add(`<div class="pair">
    <label class="field"><span>x, mm</span><input data-key="x" type="number" step="0.5" value="${layer.x ?? 0}"></label>
    <label class="field"><span>y, mm</span><input data-key="y" type="number" step="0.5" value="${layer.y ?? 0}"></label></div>`);
  add(`<p class="note">Free and instant: this edits the file directly. Ask the designer in the chat for anything bigger.</p>`);

  const box = $("layerEditor");
  box.hidden = false;
  const top = Math.min(window.innerHeight - box.offsetHeight - 12, Math.max(12, r.top - 20));
  box.style.top = top + "px";
  box.style.left = Math.max(12, r.left - box.offsetWidth - 12) + "px";
  fields.querySelector("textarea, input")?.focus();
}

function toHex(value) {
  if (typeof value !== "string") return "#111111";
  let v = value.trim();
  if (/^#[0-9a-f]{3}$/i.test(v)) v = "#" + [...v.slice(1)].map((c) => c + c).join("");
  return /^#[0-9a-f]{6}$/i.test(v) ? v : "#111111";
}

function closeEditor() {
  $("layerEditor").hidden = true;
  if (state.editing) { state.editing = null; paintLayers(); }
}

async function saveEditor() {
  const id = state.editing;
  if (!id) return;
  const set = {};
  $("editorFields").querySelectorAll("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    set[key] = el.type === "number" ? Number(el.value) : el.value;
  });
  try {
    await patchLayers([{ op: "update", id, set }]);
    const { project } = await api(`/api/projects/${state.projectId}`);
    state.project = project;
    closeEditor();
    paintLayers();
    toast("Layer saved.");
  } catch (err) { toast(err.message, true); }
}

async function deleteFromEditor() {
  const id = state.editing;
  if (!id || !confirm("Delete this layer?")) return;
  try {
    await patchLayers([{ op: "delete", id }]);
    const { project } = await api(`/api/projects/${state.projectId}`);
    state.project = project;
    closeEditor();
    paintLayers();
  } catch (err) { toast(err.message, true); }
}

function paintAssets() {
  const list = $("assetList");
  const assets = (state.project?.assets || []).filter((a) => a.role !== "photo");
  list.innerHTML = "";
  if (!assets.length) {
    list.innerHTML = `<p class="note">None yet. Attach one in the chat, or paste with ⌘V.</p>`;
    return;
  }
  for (const asset of assets) {
    const img = document.createElement("img");
    setAssetUrl(asset.id, (url) => { img.src = url; });
    img.alt = asset.name; img.title = asset.name;
    list.appendChild(img);
  }
}

function paintAttachments() {
  const strip = $("attachStrip");
  strip.innerHTML = "";
  strip.hidden = state.pending.length === 0;
  state.pending.forEach((item, index) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    const img = document.createElement("img"); img.src = item.data;
    const label = document.createElement("span"); label.textContent = item.name.slice(0, 22);
    const close = document.createElement("button");
    close.textContent = "×"; close.title = "Remove";
    close.onclick = () => { state.pending.splice(index, 1); paintAttachments(); };
    chip.append(img, label, close);
    strip.appendChild(chip);
  });
}

/* views ------------------------------------------------------------------ */

const hasStructure = () => Boolean(state.project?.dieline);

async function refreshView() {
  $("emptyState").hidden = hasStructure();
  if (!hasStructure()) return;
  if (state.view === "flat") await loadSvg("flat", "view-flat");
  else if (state.view === "dieline") await loadSvg("dieline", "view-dieline");
  else if (state.view === "panels") await loadFaces();
  else if (state.view === "three") await loadThree();
  else if (state.view === "photo") { paintScenes(); paintGallery(); }
  else if (state.view === "export") await loadExports();
}

async function loadSvg(mode, targetId) {
  const target = $(targetId);
  try {
    const res = await fetch(`/api/projects/${state.projectId}/render` +
      `?mode=${mode}&labels=${mode === "flat" ? 0 : 1}&t=${Date.now()}`);
    if (!res.ok) throw new Error("Preview unavailable");
    target.innerHTML = await res.text();
  } catch (err) {
    target.innerHTML = `<p class="note">${escapeHtml(err.message)}</p>`;
  }
}

async function loadFaces() {
  const grid = $("faceGrid");
  const panels = (state.project?.dieline?.panels || []).filter((p) => p.printable !== false);
  grid.innerHTML = "";
  for (const panel of panels) {
    const card = document.createElement("div");
    card.className = "face";
    const art = document.createElement("div");
    art.className = "face-art";
    art.innerHTML = `<p class="note">loading…</p>`;
    const cap = document.createElement("div");
    cap.className = "face-cap";
    const w = panel.dw ?? panel.w, h = panel.dh ?? panel.h;
    cap.innerHTML = `<b>${escapeHtml(panel.label)}</b><span>${Math.round(w)} × ${Math.round(h)} mm</span>`;
    card.append(art, cap);
    grid.appendChild(card);
    fetch(`/api/projects/${state.projectId}/render?mode=panel&panel=${panel.id}&t=${Date.now()}`)
      .then((r) => r.text())
      .then((svg) => { art.innerHTML = svg; })
      .catch(() => { art.innerHTML = `<p class="note">failed</p>`; });
  }
}

async function loadExports() {
  try {
    const { exports } = await api(`/api/projects/${state.projectId}/exports`);
    const list = $("exportList");
    list.innerHTML = "";
    for (const file of exports) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "fname"; name.textContent = file.name;
      const size = document.createElement("span");
      size.className = "fsize";
      size.textContent = file.bytes > 1048576 ? (file.bytes / 1048576).toFixed(1) + " MB"
        : Math.max(1, Math.round(file.bytes / 1024)) + " KB";
      const link = document.createElement("a");
      link.href = file.url || `/api/projects/${state.projectId}/exports/${encodeURIComponent(file.name)}`;
      link.textContent = "Download"; link.download = file.name;
      li.append(name, size, link);
      list.appendChild(li);
    }
  } catch (err) { toast(err.message, true); }
}

/* letting the designer see its own work ------------------------------------ */

function svgToPng(svgText, longSide, type = "image/png", quality = 0.92) {
  // A data URL rather than a blob URL: Safari is stricter about blob SVGs on canvas.
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText);
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const w0 = img.naturalWidth || 800, h0 = img.naturalHeight || 800;
      const k = longSide / Math.max(w0, h0);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(w0 * k));
      canvas.height = Math.max(1, Math.round(h0 * k));
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      try { resolve(canvas.toDataURL(type, quality)); } catch (e) { reject(e); }
    };
    img.onerror = () => reject(new Error("render failed"));
    img.src = url;
  });
}

async function renderForAgent(requestId, panel, runEl) {
  try {
    const all = panel === "all";
    const query = all ? "mode=print" : `mode=panel&panel=${encodeURIComponent(panel)}`;
    const res = await fetch(`/api/projects/${state.projectId}/render?${query}&t=${Date.now()}`);
    const svg = await res.text();
    // give embedded webfonts a moment to decode before rasterising
    await new Promise((r) => setTimeout(r, 250));
    // On the website every picture travels with each later request, so it is
    // sent a little lighter.
    const png = await svgToPng(svg, all ? 1500 : 1100, "image/jpeg", window.PS ? 0.84 : 0.9);
    await api(`/api/projects/${state.projectId}/render_result`, {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, png, media_type: "image/jpeg" }),
    });
    if (runEl) {
      const look = document.createElement("div");
      look.className = "look";
      look.innerHTML = `<img alt="What the designer looked at" src="${png}"><span>Checked ${escapeHtml(all ? "the whole layout" : panelLabel(panel))}</span>`;
      runEl.after(look);
    }
    refreshView();
  } catch (err) {
    console.warn("render for agent failed", err);
    // Say so, so the designer does not wait for a picture that never comes.
    api(`/api/projects/${state.projectId}/render_result`, {
      method: "POST", body: JSON.stringify({ request_id: requestId, png: "", media_type: "image/jpeg" }),
    }).catch(() => {});
  }
}

/* 3D ---------------------------------------------------------------------- */

async function loadThree() {
  if (typeof THREE === "undefined") {
    $("view-three").innerHTML = `<p class="note">The 3D library did not load.</p>`;
    return;
  }
  let data;
  try { data = await api(`/api/projects/${state.projectId}/textures`); }
  catch (err) { toast(err.message, true); return; }
  if (!state.three) { state.three = createThree($("three")); paintSurfaces(); }
  state.three.build(data);
}

const SURFACES = [
  { id: "studio", label: "Studio", swatch: "#e9e6df" },
  { id: "marble", label: "Marble", swatch: "#eceae5" },
  { id: "wood", label: "Wood", swatch: "#9a6c42" },
  { id: "linen", label: "Linen", swatch: "#d6cdbb" },
  { id: "shelf", label: "Shelf", swatch: "#c2a983" },
  { id: "dark", label: "Dark", swatch: "#23262d" },
];

function paintSurfaces() {
  const strip = $("sceneStrip");
  strip.innerHTML = "";
  for (const s of SURFACES) {
    const btn = document.createElement("button");
    btn.className = "surface" + (s.id === state.surface ? " on" : "");
    btn.innerHTML = `<i style="background:${s.swatch}"></i><span>${s.label}</span>`;
    btn.onclick = () => { state.surface = s.id; state.three?.setSurface(s.id); paintSurfaces(); };
    strip.appendChild(btn);
  }
}

function canvasTexture(width, height, draw) {
  const c = document.createElement("canvas");
  c.width = width; c.height = height;
  draw(c.getContext("2d"), width, height);
  const t = new THREE.CanvasTexture(c);
  if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
  return t;
}

function grain(ctx, w, h, amount, alpha) {
  for (let i = 0; i < amount; i++) {
    ctx.fillStyle = `rgba(0,0,0,${Math.random() * alpha})`;
    ctx.fillRect(Math.random() * w, Math.random() * h, 1, 1);
  }
}

const SURFACE_ART = {
  wood(ctx, w, h) {
    ctx.fillStyle = "#8d6039"; ctx.fillRect(0, 0, w, h);
    for (let y = 0; y < h; y += 2 + Math.random() * 5) {
      ctx.strokeStyle = `rgba(${60 + Math.random() * 40},${35 + Math.random() * 25},18,${0.05 + Math.random() * 0.16})`;
      ctx.lineWidth = 0.6 + Math.random() * 2.4;
      ctx.beginPath(); ctx.moveTo(0, y);
      for (let x = 0; x <= w; x += 32) ctx.lineTo(x, y + Math.sin(x / 70) * 3.5 + (Math.random() - 0.5) * 2);
      ctx.stroke();
    }
    for (let i = 0; i < 5; i++) {
      const kx = Math.random() * w, ky = Math.random() * h;
      for (let r = 2; r < 18; r += 2.4) {
        ctx.strokeStyle = `rgba(70,42,20,${0.2 - r / 110})`;
        ctx.lineWidth = 1.1; ctx.beginPath();
        ctx.ellipse(kx, ky, r, r * 0.55, 0.5, 0, Math.PI * 2); ctx.stroke();
      }
    }
    grain(ctx, w, h, 9000, 0.05);
  },
  marble(ctx, w, h) {
    ctx.fillStyle = "#e8e6e0"; ctx.fillRect(0, 0, w, h);
    const vein = (count, alpha, width) => {
      for (let i = 0; i < count; i++) {
        ctx.strokeStyle = `rgba(${86 + Math.random() * 46},${88 + Math.random() * 46},${96 + Math.random() * 46},${alpha * (0.4 + Math.random())})`;
        ctx.lineWidth = width * (0.4 + Math.random());
        ctx.beginPath();
        let x = Math.random() * w, y = -10;
        ctx.moveTo(x, y);
        while (y < h + 10) { x += (Math.random() - 0.45) * 64; y += 10 + Math.random() * 22; ctx.lineTo(x, y); }
        ctx.stroke();
      }
    };
    vein(9, 0.34, 3.2);
    vein(30, 0.14, 1.1);
    grain(ctx, w, h, 5000, 0.03);
  },
  linen(ctx, w, h) {
    ctx.fillStyle = "#ded5c3"; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(120,108,88,.1)"; ctx.lineWidth = 1;
    for (let i = 0; i < w; i += 3) {
      ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(w, i); ctx.stroke();
    }
    grain(ctx, w, h, 14000, 0.07);
  },
  shelf(ctx, w, h) { SURFACE_ART.wood(ctx, w, h); ctx.fillStyle = "rgba(230,215,190,.26)"; ctx.fillRect(0, 0, w, h); },
  dark(ctx, w, h) { ctx.fillStyle = "#23262d"; ctx.fillRect(0, 0, w, h); grain(ctx, w, h, 18000, 0.22); },
  studio(ctx, w, h) { ctx.fillStyle = "#e4e1da"; ctx.fillRect(0, 0, w, h); },
};

const BACKDROP = {
  studio: ["#f4f2ee", "#dcd8d0"], marble: ["#f6f5f2", "#d9d6cf"],
  wood: ["#efe3d2", "#c5a984"], linen: ["#f1ebe0", "#d4c9b6"],
  shelf: ["#efe6d8", "#c9b89c"], dark: ["#1d2026", "#0e1014"],
};

const CAMERAS = {
  front: { theta: 0.0, phi: 1.43, dist: 8.2 },
  three: { theta: 0.62, phi: 1.2, dist: 8.4 },
  side: { theta: Math.PI / 2, phi: 1.4, dist: 8.2 },
  top: { theta: 0.001, phi: 0.34, dist: 8.2 },
  back: { theta: Math.PI, phi: 1.35, dist: 8.2 },
};

function createThree(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  if (THREE.ACESFilmicToneMapping) renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.92;
  if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 200);
  const root = new THREE.Group();
  scene.add(root);

  const key = new THREE.DirectionalLight(0xfff6e8, 1.75);
  key.position.set(4.5, 7, 5.5);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, { near: 1, far: 40, left: -8, right: 8, top: 8, bottom: -8 });
  key.shadow.bias = -0.0008;
  key.shadow.radius = 3;
  // Low and to the side. Placed high behind the box, this light mirrored off
  // the lid straight into the camera and turned dark tops pale grey.
  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.6);
  rim.position.set(-7, 1.0, 1.5);
  const ambient = new THREE.AmbientLight(0xffffff, 0.22);
  scene.add(key, rim, ambient);

  // a soft dark patch under the box, so it sits on the surface instead of hovering
  const contact = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.MeshBasicMaterial({
      map: canvasTexture(256, 256, (ctx, w, h) => {
        const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
        g.addColorStop(0, "rgba(0,0,0,.55)"); g.addColorStop(0.3, "rgba(0,0,0,.26)");
        g.addColorStop(0.62, "rgba(0,0,0,.07)"); g.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
      }),
      transparent: true, depthWrite: false,
    })
  );
  contact.rotation.x = -Math.PI / 2;
  contact.position.y = 0.004;
  contact.renderOrder = 1;

  const pmrem = new THREE.PMREMGenerator(renderer);
  pmrem.compileEquirectangularShader?.();

  function buildEnvironment(top, bottom, warm) {
    // A studio, not a sky: the ceiling is dim and the light comes from two
    // softboxes. A bright overhead would mirror onto every lid and wash a dark
    // carton top out to grey.
    const tex = canvasTexture(1024, 512, (ctx, w, h) => {
      const sky = ctx.createLinearGradient(0, 0, 0, h);
      sky.addColorStop(0, "#3a3a3c");
      sky.addColorStop(0.32, "#77767a");
      sky.addColorStop(0.5, top);
      sky.addColorStop(0.56, bottom);
      sky.addColorStop(1, bottom);
      ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);
      const soft = (cx, cy, rx, ry, a) => {
        const g = ctx.createRadialGradient(cx, cy, 4, cx, cy, rx);
        g.addColorStop(0, `rgba(255,${warm ? 248 : 255},${warm ? 236 : 255},${a})`);
        g.addColorStop(0.55, `rgba(255,255,255,${a * 0.55})`);
        g.addColorStop(1, "rgba(255,255,255,0)");
        ctx.save(); ctx.translate(cx, cy); ctx.scale(1, ry / rx); ctx.translate(-cx, -cy);
        ctx.fillStyle = g; ctx.fillRect(cx - rx, cy - rx, rx * 2, rx * 2); ctx.restore();
      };
      soft(w * 0.3, h * 0.34, w * 0.11, h * 0.16, 1.0);   // key softbox, front left
      soft(w * 0.78, h * 0.38, w * 0.08, h * 0.12, 0.5);  // fill, back right
    });
    tex.mapping = THREE.EquirectangularReflectionMapping;
    scene.environment = pmrem.fromEquirectangular(tex).texture;
    tex.dispose();
  }

  let backdrop = null, ground = null;
  function buildStage(name) {
    const [top, bottom] = BACKDROP[name] || BACKDROP.studio;
    buildEnvironment(top, bottom, name === "wood" || name === "shelf");
    if (backdrop) { scene.remove(backdrop); backdrop.geometry.dispose(); backdrop.material.dispose(); }
    backdrop = new THREE.Mesh(
      new THREE.SphereGeometry(60, 32, 24),
      new THREE.MeshBasicMaterial({
        side: THREE.BackSide,
        map: canvasTexture(16, 256, (ctx, w, h) => {
          const g = ctx.createLinearGradient(0, 0, 0, h);
          g.addColorStop(0, top); g.addColorStop(1, bottom);
          ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
        }),
      })
    );
    scene.add(backdrop);
    if (ground) { scene.remove(ground); ground.geometry.dispose(); ground.material.dispose(); }
    const surface = canvasTexture(512, 512, SURFACE_ART[name] || SURFACE_ART.studio);
    surface.wrapS = surface.wrapT = THREE.RepeatWrapping;
    surface.repeat.set(name === "studio" ? 1 : 3, name === "studio" ? 1 : 3);
    ground = new THREE.Mesh(
      new THREE.PlaneGeometry(80, 80),
      new THREE.MeshStandardMaterial({
        map: surface, metalness: 0,
        roughness: name === "marble" ? 0.3 : name === "dark" ? 0.55 : 0.8,
        envMapIntensity: name === "marble" ? 0.7 : 0.3,
      })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);
  }

  const ctl = { ...CAMERAS.three, spin: false, surface: "studio", target: null };
  let dragging = false, lastX = 0, lastY = 0;
  // Draw only when something changed and the view can be seen: a scene this
  // rich costs real battery at 60 frames a second for nothing.
  let dirty = 3;
  const invalidate = (frames = 3) => { dirty = Math.max(dirty, frames); };
  canvas.addEventListener("pointerdown", (e) => {
    dragging = true; ctl.target = null; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    ctl.theta -= (e.clientX - lastX) * 0.008;
    ctl.phi = Math.max(0.3, Math.min(1.52, ctl.phi - (e.clientY - lastY) * 0.008));
    lastX = e.clientX; lastY = e.clientY;
    invalidate();
  });
  const stop = () => { dragging = false; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    ctl.dist = Math.max(3, Math.min(24, ctl.dist * (1 + Math.sign(e.deltaY) * 0.1)));
    invalidate();
  }, { passive: false });

  const loader = new THREE.TextureLoader();
  const cache = new Map();
  const tex = (url) => {
    if (cache.has(url)) return cache.get(url);
    const t = loader.load(url, () => invalidate());
    t.anisotropy = renderer.capabilities.getMaxAnisotropy?.() || 8;
    if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
    cache.set(url, t);
    return t;
  };

  // printed board: slightly rough, with a thin varnish that catches the light
  function face(entry, fallback) {
    const Material = THREE.MeshPhysicalMaterial || THREE.MeshStandardMaterial;
    const opts = { roughness: 0.6, metalness: 0, envMapIntensity: 0.45,
                   clearcoat: 0.16, clearcoatRoughness: 0.5 };
    if (entry) opts.map = tex(entry.url); else opts.color = fallback;
    return new Material(opts);
  }

  function box(w, h, d, faces) {
    const side = "#ded9d1";
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), [
      face(faces.right, side), face(faces.left, side), face(faces.top, side),
      face(faces.bottom, side), face(faces.front, side), face(faces.back, side),
    ]);
    mesh.castShadow = true; mesh.receiveShadow = true;
    return mesh;
  }

  function makePackage(t, dims, scale) {
    const w = dims.width * scale, h = dims.height * scale, d = dims.depth * scale;
    const group = new THREE.Group();
    if (t.lid_front && t.top) {
      // lid and base: the lid is a separate, slightly larger box over the top
      const base = box(w, h, d, { front: t.front, back: t.back, left: t.left, right: t.right, bottom: t.bottom });
      base.position.y = h / 2;
      const lidH = (t.lid_front.h || dims.height * 0.35) * scale;
      const lw = (t.top.w || dims.width + 3) * scale, ld = (t.top.h || dims.depth + 3) * scale;
      const lid = box(lw, lidH, ld, { top: t.top, front: t.lid_front, back: t.lid_back,
                                      left: t.lid_left, right: t.lid_right });
      lid.position.y = h - lidH / 2 + 0.6 * scale;
      group.add(base, lid);
    } else {
      const mesh = box(w, h, d, t);
      mesh.position.y = h / 2;
      group.add(mesh);
    }
    return group;
  }

  let current = null;
  function build(data) {
    while (root.children.length) root.remove(root.children[0]);
    const biggest = Math.max(data.dims.width, data.dims.height, data.dims.depth);
    const scale = 2.6 / biggest;
    current = data;
    root.add(makePackage(data.textures, data.dims, scale));
    const footprint = Math.max(data.dims.width, data.dims.depth) * scale;
    contact.scale.set(footprint * 2.1, footprint * 2.1, 1);
    root.add(contact);
    if (ctl.surface === "shelf") {
      const gap = data.dims.width * scale * 1.25;
      for (const offset of [-gap, gap]) {
        const clone = makePackage(data.textures, data.dims, scale);
        clone.position.x = offset;
        clone.rotation.y = offset < 0 ? 0.1 : -0.1;
        root.add(clone);
      }
    }
    buildStage(ctl.surface);
    resize();
    invalidate(30);
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    renderer.setSize(rect.width, rect.height, false);
    camera.aspect = rect.width / rect.height;
    camera.updateProjectionMatrix();
    invalidate();
  }
  new ResizeObserver(resize).observe(canvas.parentElement);
  buildStage("studio");

  function place() {
    const r = ctl.dist;
    camera.position.set(r * Math.sin(ctl.phi) * Math.sin(ctl.theta),
                        r * Math.cos(ctl.phi) + 1.2,
                        r * Math.sin(ctl.phi) * Math.cos(ctl.theta));
    camera.lookAt(0, 1.15, 0);
  }

  (function loop() {
    requestAnimationFrame(loop);
    if (document.hidden || !canvas.offsetParent) return;
    if (!ctl.target && !ctl.spin && dirty <= 0) return;
    if (dirty > 0) dirty--;
    if (ctl.target) {
      // ease towards a preset view
      let dt = ctl.target.theta - ctl.theta;
      dt = Math.atan2(Math.sin(dt), Math.cos(dt));
      ctl.theta += dt * 0.14;
      ctl.phi += (ctl.target.phi - ctl.phi) * 0.14;
      ctl.dist += (ctl.target.dist - ctl.dist) * 0.14;
      if (Math.abs(dt) < 0.002 && Math.abs(ctl.target.phi - ctl.phi) < 0.002) ctl.target = null;
    } else if (ctl.spin) {
      ctl.theta += 0.0035;
    }
    place();
    renderer.render(scene, camera);
  })();
  document.addEventListener("visibilitychange", () => invalidate());

  return {
    build,
    setSurface: (name) => { ctl.surface = name; if (current) build(current); else { buildStage(name); invalidate(); } },
    view: (name) => { ctl.spin = false; ctl.target = { ...CAMERAS[name] }; },
    toggleSpin: () => { invalidate(); return (ctl.spin = !ctl.spin); },
    redraw: () => invalidate(30),
    snapshot: () => { place(); renderer.render(scene, camera); return renderer.domElement.toDataURL("image/png"); },
    ready: () => Boolean(current),
  };
}

/* photos ---------------------------------------------------------------- */

function paintScenes() {
  const wrap = $("sceneCards");
  wrap.innerHTML = "";
  for (const scene of state.scenes || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scene" + (scene.id === state.scene ? " on" : "");
    btn.innerHTML = `<b>${escapeHtml(scene.label)}</b><span>${escapeHtml(scene.blurb)}</span>`;
    btn.onclick = () => { state.scene = scene.id; paintScenes(); };
    wrap.appendChild(btn);
  }
  const intro = $("photoIntro");
  intro.innerHTML = state.config.image_provider === "none" && window.PS
    ? "Photos are not switched on for this site yet: its owner has to add a Google Gemini key."
    : state.config.image_provider === "none"
    ? "This needs an image model, because Claude cannot make photographs. Add a Google " +
      "Gemini key under <b>Settings</b> and this tab will put your package into real " +
      "settings — held in a hand, on a marble counter, on a shelf."
    : "Pick a setting. The 3D render goes to your image model, which builds a real scene " +
      "around it and keeps the package exactly as designed.";
}

function paintGallery() {
  const gallery = $("gallery");
  const photos = (state.project?.assets || []).filter((a) => a.role === "photo");
  gallery.innerHTML = "";
  for (const photo of photos.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "shot-card";
    const img = document.createElement("img");
    img.alt = photo.scene || "photo"; img.loading = "lazy";
    card.appendChild(img);
    const link = document.createElement("a");
    setAssetUrl(photo.id, (url) => { img.src = url; link.href = url; });
    link.download = photo.name || "photo.png"; link.textContent = "Save";
    card.appendChild(link);
    gallery.appendChild(card);
  }
}

async function shoot() {
  if (!state.projectId) return;
  if (!hasStructure()) { toast("Design something first.", true); return; }
  const status = $("photoStatus");
  if (!state.three || !state.three.ready()) {
    status.textContent = "Preparing the render…";
    await loadThree();
    await new Promise((r) => setTimeout(r, 1400));
  }
  const render = state.three?.snapshot();
  if (!render) { toast("Could not capture the render.", true); return; }
  const button = $("shoot");
  button.disabled = true; button.textContent = "Shooting";
  status.textContent = "Building the scene. This takes a few seconds.";
  // The flat front at high resolution, so the model has every letter sharp.
  let front = null;
  try {
    const res = await fetch(`/api/projects/${state.projectId}/render?mode=panel&panel=front&t=${Date.now()}`);
    if (res.ok) {
      const svg = await res.text();
      await new Promise((r) => setTimeout(r, 200));
      front = await svgToPng(svg, 1400);
    }
  } catch { front = null; }
  try {
    await api(`/api/projects/${state.projectId}/photoshoot`, {
      method: "POST",
      body: JSON.stringify({ render, front, scene: state.scene,
                             notes: $("photoNotes").value, aspect: $("photoAspect").value }),
    });
    await openProject(state.projectId);
    status.textContent = "Done. Shoot again for another take; every one comes out a little different.";
  } catch (err) {
    status.textContent = "";
    toast(err.message, true);
  } finally {
    button.disabled = false; button.textContent = "Shoot";
  }
}

/* sending ---------------------------------------------------------------- */

async function send(event) {
  event?.preventDefault();
  if (state.busy) return;
  if (!state.projectId) { toast("Create a project first.", true); return; }
  if (!state.config.anthropic) {
    if (window.PS) toast("This site is not set up yet: its owner still has to add the Anthropic key.", true);
    else $("settings").showModal();
    return;
  }

  const input = $("input");
  const text = input.value.trim();
  if (!text && !state.pending.length) return;

  let assetIds = [];
  if (state.pending.length) {
    try {
      const { assets } = await api(`/api/projects/${state.projectId}/assets`, {
        method: "POST", body: JSON.stringify({ files: state.pending }),
      });
      assetIds = assets.map((a) => a.id);
      state.project.assets = (state.project.assets || []).concat(assets);
      paintAssets();
    } catch (err) { toast("Upload failed: " + err.message, true); return; }
    state.pending = [];
    paintAttachments();
  }

  input.value = "";
  input.style.height = "auto";
  setBusy(true);

  const box = $("messages");
  box.querySelector(".starters")?.remove();
  box.appendChild(bubble("user", text || `[${assetIds.length} image${assetIds.length > 1 ? "s" : ""} sent]`));

  const pending = document.createElement("div");
  pending.className = "pending";
  pending.innerHTML = "<i></i><i></i><i></i>";
  box.appendChild(pending);
  box.scrollTop = box.scrollHeight;

  const runs = new Map();
  let liveCost = null, changed = false, live = null;

  try {
    const res = await fetch(`/api/projects/${state.projectId}/chat`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, asset_ids: assetIds }),
    });
    if (!res.ok || !res.body) throw new Error("The server refused the request.");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    let finished = false;
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        if (ev.type === "assistant_delta") {
          // the reply arriving as it is written
          if (!live) { live = bubble("agent", ""); box.appendChild(live); }
          live.textContent += ev.text;
          box.appendChild(pending);
        } else if (ev.type === "assistant_reset") {
          if (live) { live.remove(); live = null; }
        } else if (ev.type === "assistant") {
          if (live) { live.textContent = ev.text; live = null; }
          else box.appendChild(bubble("agent", ev.text));
          box.appendChild(pending);
        } else if (ev.type === "status") {
          pending.dataset.label = ev.text || "";
        } else if (ev.type === "tool_start") {
          live = null;
          const run = document.createElement("div");
          run.className = "run";
          run.innerHTML = `<span class="tick"></span><span>${escapeHtml(ev.label)}</span>`;
          runs.set(ev.id || ev.name, run);
          box.appendChild(run);
        } else if (ev.type === "tool_end") {
          const run = runs.get(ev.id || ev.name);
          if (run) run.className = "run " + (ev.ok ? "done" : "failed");
        } else if (ev.type === "render_request") {
          const run = [...runs.values()].pop();
          renderForAgent(ev.request_id, ev.panel, run);
        } else if (ev.type === "artwork_update" || ev.type === "project_update") {
          changed = true;
          refreshProjectQuietly();
        } else if (ev.type === "cost") {
          if (!liveCost) { liveCost = document.createElement("div"); liveCost.className = "cost"; }
          liveCost.textContent = `${money(ev.run.cost_usd)}  ·  running`;
          box.appendChild(liveCost);
          box.appendChild(pending);
        } else if (ev.type === "error") {
          pending.remove();
          box.appendChild(bubble("fail", ev.text));
        } else if (ev.type === "done") {
          if (liveCost && ev.run) liveCost.replaceWith(costLine(ev.run));
          if (ev.total) { state.project.usage = ev.total; paintMeter(); }
        } else if (ev.type === "end") {
          finished = true;
          reader.cancel().catch(() => {});
        }
        box.scrollTop = box.scrollHeight;
      }
    }
  } catch (err) {
    box.appendChild(bubble("fail", err.message));
  } finally {
    pending.remove();
    setBusy(false);
    // Keep the conversation as it played out (steps, previews) and only bring
    // everything else up to date.
    await syncProject();
    if (changed) toast("Design updated.");
    $("input").focus();
  }
}

async function syncProject() {
  try {
    const { project } = await api(`/api/projects/${state.projectId}`);
    state.project = project;
    paintMeter();
    paintStructure();
    paintLayers();
    paintAssets();
    paintGallery();
    refreshView();
    await refreshProjectList();
  } catch (err) { toast(err.message, true); }
}

async function refreshProjectList() {
  const { projects } = await api("/api/projects");
  const sel = $("projectSelect");
  const current = state.projectId;
  sel.innerHTML = "";
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.box_type ? `${p.name} — ${state.boxTypes[p.box_type]?.short || p.box_type}` : p.name;
    sel.appendChild(opt);
  }
  sel.value = current;
}

/* While the agent works, keep the preview in step without reloading the chat. */
let quietTimer = null;
function refreshProjectQuietly() {
  clearTimeout(quietTimer);
  quietTimer = setTimeout(async () => {
    try {
      const { project } = await api(`/api/projects/${state.projectId}`);
      const usage = state.project?.usage;
      state.project = { ...project, usage: usage || project.usage };
      paintStructure();
      paintLayers();
      refreshView();
    } catch { /* the final reload will catch up */ }
  }, 350);
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  $("send").textContent = busy ? "Working" : "Send";
}

/* wiring ----------------------------------------------------------------- */

function wire() {
  $("composer").addEventListener("submit", send);
  const input = $("input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 190) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); }
  });

  $("projectSelect").addEventListener("change", (e) => openProject(e.target.value));
  $("newProject").addEventListener("click", newProject);
  $("deleteProject").addEventListener("click", async () => {
    if (!state.projectId) return;
    if (!confirm(`Delete "${state.project?.name}" and all its files? This cannot be undone.`)) return;
    try {
      await api(`/api/projects/${state.projectId}`, { method: "DELETE" });
      state.projectId = null;
      await loadProjects();
      toast("Project deleted.");
    } catch (err) { toast(err.message, true); }
  });

  $("fileInput").addEventListener("change", async (e) => {
    for (const file of e.target.files) {
      if (file.size > 12 * 1024 * 1024) { toast(`${file.name} is over 12 MB.`, true); continue; }
      state.pending.push({ name: file.name, data: await readFile(file) });
    }
    e.target.value = "";
    paintAttachments();
  });

  document.querySelectorAll(".seg").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".seg").forEach((t) => t.classList.remove("on"));
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("on"));
      tab.classList.add("on");
      state.view = tab.dataset.view;
      $("view-" + state.view).classList.add("on");
      refreshView();
    });
  });

  $("refresh").addEventListener("click", () => openProject(state.projectId));

  $("applyStructure").addEventListener("click", async () => {
    if (!state.projectId) return;
    try {
      await api(`/api/projects/${state.projectId}/structure`, {
        method: "POST",
        body: JSON.stringify({
          box_type: $("boxType").value,
          width_mm: Number($("dimW").value) || 50,
          depth_mm: Number($("dimD").value) || 40,
          height_mm: Number($("dimH").value) || 100,
        }),
      });
      await openProject(state.projectId);
      toast("Structure updated. The artwork stayed put, so check the margins.");
    } catch (err) { toast(err.message, true); }
  });

  $("doExport").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Generating";
    try {
      await api(`/api/projects/${state.projectId}/export`, { method: "POST" });
      await loadExports();
      toast(window.PS ? "Files are ready. Download what you need below." : "Files written to the project folder.");
    } catch (err) { toast(err.message, true); }
    finally { e.target.disabled = false; e.target.textContent = "Generate files"; }
  });

  $("camBar").addEventListener("click", (e) => {
    const name = e.target.closest("button")?.dataset.cam;
    if (name) { state.three?.view(name); $("spin").textContent = "Spin"; }
  });
  $("shoot").addEventListener("click", shoot);
  $("photoNotes").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); shoot(); }
  });
  $("toPhoto").addEventListener("click", () => {
    document.querySelector('.seg[data-view="photo"]').click();
    if ((state.scenes || []).some((s) => s.id === state.surface)) { state.scene = state.surface; paintScenes(); }
    shoot();
  });
  $("spin").addEventListener("click", (e) => {
    e.target.textContent = state.three?.toggleSpin() ? "Stop" : "Spin";
  });
  $("shot").addEventListener("click", () => {
    if (!state.three) return;
    const a = document.createElement("a");
    a.href = state.three.snapshot();
    a.download = (state.project?.name || "mockup").replace(/\W+/g, "-").toLowerCase() + ".png";
    a.click();
  });

  $("editorClose").addEventListener("click", closeEditor);
  $("editorSave").addEventListener("click", saveEditor);
  $("editorDelete").addEventListener("click", deleteFromEditor);
  $("layerEditor").addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeEditor();
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEditor();
  });

  $("clearChat").addEventListener("click", async () => {
    if (!state.projectId) return;
    if (!confirm("Clear the chat history? The design and files stay.")) return;
    try {
      await api(`/api/projects/${state.projectId}/clear`, { method: "POST" });
      await openProject(state.projectId);
      toast("History cleared. The designer still sees the brief, structure and artwork.");
    } catch (err) { toast(err.message, true); }
  });

  $("openSettings").addEventListener("click", () => {
    $("anthropicKey").value = ""; $("geminiKey").value = ""; $("openaiKey").value = "";
    $("modelSelect").value = state.config.model;
    $("effortSelect").value = state.config.effort || "high";
    $("probeResult").textContent = "";
    paintPrice();
    paintKeyState();
    $("settings").showModal();
  });

  document.querySelectorAll(".clear").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const data = await api("/api/settings", { method: "POST", body: JSON.stringify({ [btn.dataset.clear]: "" }) });
        state.config = data.config;
        paintKeyState();
        paintScenes();
        toast("Key removed.");
      } catch (err) { toast(err.message, true); }
    });
  });

  $("probeBtn").addEventListener("click", async (e) => {
    const out = $("probeResult");
    const pendingKey = $("geminiKey").value.trim();
    e.target.disabled = true;
    out.style.color = "";
    out.textContent = "Checking…";
    try {
      if (pendingKey) {
        const saved = await api("/api/settings", { method: "POST", body: JSON.stringify({ GEMINI_API_KEY: pendingKey }) });
        state.config = saved.config;
        $("geminiKey").value = "";
      }
      const data = await api("/api/settings/probe", { method: "POST" });
      state.config = data.config || state.config;
      out.textContent = data.message || (data.ok ? "Connected." : "Not working.");
      out.style.color = data.ok ? "var(--good)" : "var(--bad)";
      paintKeyState();
      paintScenes();
    } catch (err) {
      out.textContent = err.message;
      out.style.color = "var(--bad)";
    } finally { e.target.disabled = false; }
  });

  $("saveSettings").addEventListener("click", async (e) => {
    e.preventDefault();
    const payload = { ANTHROPIC_MODEL: $("modelSelect").value,
                      ANTHROPIC_EFFORT: $("effortSelect").value };
    for (const [key, el] of [["ANTHROPIC_API_KEY", "anthropicKey"], ["GEMINI_API_KEY", "geminiKey"],
                             ["OPENAI_API_KEY", "openaiKey"]]) {
      const value = $(el).value.trim();
      if (value) payload[key] = value;
    }
    try {
      const data = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      state.config = data.config;
      paintKeyState();
      paintScenes();
      $("settings").close();
      toast("Settings saved.");
      if (!state.project) await loadProjects();
    } catch (err) { toast(err.message, true); }
  });

  window.addEventListener("paste", async (e) => {
    const items = [...(e.clipboardData?.items || [])].filter((i) => i.type.startsWith("image/"));
    if (!items.length) return;
    for (const item of items) {
      const file = item.getAsFile();
      if (file) state.pending.push({ name: file.name || "pasted.png", data: await readFile(file) });
    }
    paintAttachments();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("layerEditor").hidden) closeEditor();
  });
}

boot();
})();
