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
  config: {}, agents: [], boxTypes: {}, models: [],
  projectId: null, project: null,
  pending: [], view: "flat", busy: false, three: null,
};

/* money ------------------------------------------------------------- */

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

/* toast -------------------------------------------------------------- */

let toastTimer;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6500 : 3200);
}

/* boot --------------------------------------------------------------- */

async function boot() {
  try {
    const status = await api("/api/status");
    Object.assign(state, {
      config: status.config, agents: status.agents,
      boxTypes: status.box_types, models: status.models,
      pricing: status.pricing || {},
    });
    fillBoxTypes();
    fillModels();
    paintKeyState();
  } catch (err) {
    toast("Cannot reach the server: " + err.message, true);
    return;
  }
  await loadProjects();
  wire();
  if (!state.config.anthropic) $("settings").showModal();
}

function paintKeyState() {
  const dot = $("keyDot");
  const label = $("keyLabel");
  if (state.config.anthropic) {
    dot.className = "dot ok";
    label.textContent = state.config.model_label || "Settings";
  } else {
    dot.className = "dot bad";
    label.textContent = "Add API key";
  }
  const state_ = $("anthropicState");
  if (state_) state_.textContent = state.config.anthropic ? "saved" : "";
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

function paintPrice() {
  const id = $("modelSelect").value;
  const m = state.models.find((x) => x.id === id);
  const p = (state.pricing || {})[Object.keys(state.pricing || {})
    .find((k) => id.startsWith(k))] || null;
  $("priceNote").textContent = p
    ? `$${p.input.toFixed(2)} per million input tokens, $${p.output.toFixed(2)} per million output. A full design with a few rounds of edits usually lands between a few cents and a couple of dollars.`
    : (m ? m.note : "");
}

/* projects ----------------------------------------------------------- */

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
      ? `${p.name} — ${state.boxTypes[p.box_type]?.short || p.box_type}`
      : p.name;
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
  paintAll();
}

async function newProject() {
  const name = prompt("Name for the new project", "Untitled project");
  if (name === null) return;
  const { project } = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name: name || "Untitled project",
                           agent: "packaging_designer" }),
  });
  await loadProjects(project.id);
  $("input").focus();
}

/* painting ----------------------------------------------------------- */

function paintAll() {
  paintMessages();
  paintMeter();
  paintStructure();
  paintLayers();
  paintAssets();
  paintAttachments();
  refreshView();
}

function paintMeter() {
  const usage = state.project?.usage;
  const meter = $("meter");
  if (!usage || !usage.runs) { meter.hidden = true; return; }
  meter.hidden = false;
  $("meterCost").textContent = money(usage.cost_usd);
  $("meterLabel").textContent =
    `${usage.runs} request${usage.runs === 1 ? "" : "s"} · ` +
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
  el.textContent = text;
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
    $("structureNote").textContent = "Millimetres, inside dimensions.";
    return;
  }
  $("boxType").value = dl.box_type;
  $("dimW").value = Math.round(dl.dims.width);
  $("dimD").value = Math.round(dl.dims.depth);
  $("dimH").value = Math.round(dl.dims.height);
  $("structureNote").textContent =
    `Flat sheet ${Math.round(dl.sheet.width)} × ${Math.round(dl.sheet.height)} mm. ${dl.material_hint}`;
}

function paintLayers() {
  const list = $("layerList");
  const layers = state.project?.layers || [];
  $("layerCount").textContent = layers.length || "";
  list.innerHTML = "";
  if (!layers.length) {
    list.innerHTML = `<p class="note">Nothing drawn yet. Once the designer builds
      artwork, every element shows up here and can be hidden one at a time.</p>`;
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
    row.className = "layer" + (layer.visible === false ? " off" : "");

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
    type.textContent = layer.type;

    const eye = document.createElement("button");
    eye.className = "eye";
    eye.textContent = layer.visible === false ? "hidden" : "●";
    eye.title = layer.visible === false ? "Show" : "Hide";
    eye.onclick = () => toggleLayer(layer);

    row.append(swatch, name, type, eye);
    list.appendChild(row);
  }
}

function panelLabel(id) {
  return state.project?.dieline?.panels?.find((p) => p.id === id)?.label || id;
}

async function toggleLayer(layer) {
  try {
    const data = await api(`/api/projects/${state.projectId}/layers`, {
      method: "POST",
      body: JSON.stringify({ patches: [{ op: "update", id: layer.id,
                                         set: { visible: layer.visible === false } }] }),
    });
    state.project.layers = data.layers;
    paintLayers();
    refreshView();
  } catch (err) { toast(err.message, true); }
}

function paintAssets() {
  const list = $("assetList");
  const assets = state.project?.assets || [];
  list.innerHTML = "";
  if (!assets.length) {
    list.innerHTML = `<p class="note">None yet. Drop one into the chat, or paste
      with ⌘V.</p>`;
    return;
  }
  for (const asset of assets) {
    const img = document.createElement("img");
    img.src = `/api/projects/${state.projectId}/assets/${asset.id}`;
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

/* views -------------------------------------------------------------- */

const hasStructure = () => Boolean(state.project?.dieline);

async function refreshView() {
  $("emptyState").hidden = hasStructure();
  if (!hasStructure()) return;
  if (state.view === "flat") await loadSvg("flat", "view-flat");
  else if (state.view === "dieline") await loadSvg("dieline", "view-dieline");
  else if (state.view === "panels") await loadFaces();
  else if (state.view === "three") await loadThree();
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
    target.innerHTML = `<p class="note">${err.message}</p>`;
  }
}

async function loadFaces() {
  const grid = $("faceGrid");
  const panels = (state.project?.dieline?.panels || [])
    .filter((p) => p.printable !== false);
  grid.innerHTML = "";
  for (const panel of panels) {
    const card = document.createElement("div");
    card.className = "face";
    const art = document.createElement("div");
    art.className = "face-art";
    art.innerHTML = `<p class="note">loading…</p>`;
    const cap = document.createElement("div");
    cap.className = "face-cap";
    cap.innerHTML = `<b>${panel.label}</b><span>${Math.round(panel.w)} × ${Math.round(panel.h)} mm</span>`;
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
      size.textContent = file.bytes > 1048576
        ? (file.bytes / 1048576).toFixed(1) + " MB"
        : Math.max(1, Math.round(file.bytes / 1024)) + " KB";
      const link = document.createElement("a");
      link.href = `/api/projects/${state.projectId}/exports/${encodeURIComponent(file.name)}`;
      link.textContent = "Download"; link.download = file.name;
      li.append(name, size, link);
      list.appendChild(li);
    }
  } catch (err) { toast(err.message, true); }
}

/* 3D ------------------------------------------------------------------ */

async function loadThree() {
  if (typeof THREE === "undefined") {
    $("view-three").innerHTML =
      `<p class="note">The 3D library did not load.</p>`;
    return;
  }
  let data;
  try { data = await api(`/api/projects/${state.projectId}/textures`); }
  catch (err) { toast(err.message, true); return; }
  if (!state.three) state.three = createThree($("three"));
  state.three.build(data);
}

function createThree(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true,
                                             preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 100);
  const root = new THREE.Group();
  scene.add(root);

  const key = new THREE.DirectionalLight(0xffffff, 0.95);
  key.position.set(4, 6, 5);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.near = 1; key.shadow.camera.far = 30;
  const fill = new THREE.DirectionalLight(0xffffff, 0.5);
  fill.position.set(-5, 2, -3);
  const ambient = new THREE.AmbientLight(0xffffff, 1.0);
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(60, 60),
    new THREE.ShadowMaterial({ opacity: 0.15 })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(key, fill, ambient, ground);

  const ctl = { theta: 0.62, phi: 1.17, dist: 7, spin: false, scene: "studio" };
  let dragging = false, lastX = 0, lastY = 0;

  canvas.addEventListener("pointerdown", (e) => {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    ctl.theta -= (e.clientX - lastX) * 0.008;
    ctl.phi = Math.max(0.18, Math.min(Math.PI - 0.18,
                                      ctl.phi - (e.clientY - lastY) * 0.008));
    lastX = e.clientX; lastY = e.clientY;
  });
  const stop = () => { dragging = false; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    ctl.dist = Math.max(2.2, Math.min(22, ctl.dist * (1 + Math.sign(e.deltaY) * 0.1)));
  }, { passive: false });

  const loader = new THREE.TextureLoader();
  const cache = new Map();
  const tex = (url) => {
    if (cache.has(url)) return cache.get(url);
    const t = loader.load(url);
    t.anisotropy = renderer.capabilities.getMaxAnisotropy?.() || 4;
    if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
    cache.set(url, t);
    return t;
  };

  const face = (entry, fallback) => entry
    ? new THREE.MeshStandardMaterial({ map: tex(entry.url), roughness: 0.72, metalness: 0.02 })
    : new THREE.MeshStandardMaterial({ color: fallback, roughness: 0.78 });

  function makeBox(textures, dims, scale) {
    const w = dims.width * scale, h = dims.height * scale, d = dims.depth * scale;
    const side = "#d8d3cb";
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), [
      face(textures.right, side), face(textures.left, side),
      face(textures.top, side), face(textures.bottom, side),
      face(textures.front, side), face(textures.back, side),
    ]);
    mesh.castShadow = true;
    mesh.position.y = h / 2;
    return mesh;
  }

  function applyScene(name) {
    ctl.scene = name;
    if (name === "dark") {
      scene.background = new THREE.Color(0x14171d);
      ambient.intensity = 0.3; key.intensity = 1.05; fill.intensity = 0.5;
      ground.material.opacity = 0.3;
    } else if (name === "shelf") {
      scene.background = new THREE.Color(0xe4dace);
      ambient.intensity = 0.95; key.intensity = 0.95; fill.intensity = 0.45;
      ground.material.opacity = 0.2;
    } else {
      scene.background = new THREE.Color(0xe9e6df);
      ambient.intensity = 1.0; key.intensity = 0.95; fill.intensity = 0.5;
      ground.material.opacity = 0.14;
    }
  }

  let current = null;
  function build(data) {
    while (root.children.length) root.remove(root.children[0]);
    const biggest = Math.max(data.dims.width, data.dims.height, data.dims.depth);
    const scale = 2.4 / biggest;
    current = data;
    root.add(makeBox(data.textures, data.dims, scale));

    if (ctl.scene === "shelf") {
      const gap = data.dims.width * scale * 1.22;
      for (const offset of [-gap, gap]) {
        const clone = makeBox(data.textures, data.dims, scale);
        clone.position.x = offset;
        clone.rotation.y = offset < 0 ? 0.09 : -0.09;
        root.add(clone);
      }
      const plank = new THREE.Mesh(
        new THREE.BoxGeometry(gap * 5, 0.09, data.dims.depth * scale * 3),
        new THREE.MeshStandardMaterial({ color: 0xc9b89d, roughness: 0.9 })
      );
      plank.position.y = -0.045;
      plank.receiveShadow = true;
      root.add(plank);
    }
    applyScene(ctl.scene);
    resize();
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    renderer.setSize(rect.width, rect.height, false);
    camera.aspect = rect.width / rect.height;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(canvas.parentElement);

  (function loop() {
    requestAnimationFrame(loop);
    if (ctl.spin) ctl.theta += 0.004;
    const r = ctl.dist;
    camera.position.set(
      r * Math.sin(ctl.phi) * Math.sin(ctl.theta),
      r * Math.cos(ctl.phi) + 1.1,
      r * Math.sin(ctl.phi) * Math.cos(ctl.theta)
    );
    camera.lookAt(0, 1.05, 0);
    renderer.render(scene, camera);
  })();

  return {
    build,
    setScene: (n) => { applyScene(n); if (current) build(current); },
    toggleSpin: () => (ctl.spin = !ctl.spin),
    snapshot: () => renderer.domElement.toDataURL("image/png"),
  };
}

/* sending ------------------------------------------------------------- */

async function send(event) {
  event?.preventDefault();
  if (state.busy) return;
  if (!state.projectId) { toast("Create a project first.", true); return; }
  if (!state.config.anthropic) { $("settings").showModal(); return; }

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
  box.appendChild(bubble("user",
    text || `[${assetIds.length} image${assetIds.length > 1 ? "s" : ""} sent]`));

  const pending = document.createElement("div");
  pending.className = "pending";
  pending.innerHTML = "<i></i><i></i><i></i>";
  box.appendChild(pending);
  box.scrollTop = box.scrollHeight;

  const runs = new Map();
  let liveCost = null;

  try {
    const res = await fetch(`/api/projects/${state.projectId}/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, asset_ids: assetIds }),
    });
    if (!res.ok || !res.body) throw new Error("The server refused the request.");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let event;
        try { event = JSON.parse(line.slice(6)); } catch { continue; }

        if (event.type === "assistant") {
          pending.remove();
          box.appendChild(bubble("agent", event.text));
        } else if (event.type === "tool_start") {
          const run = document.createElement("div");
          run.className = "run";
          run.innerHTML = `<span class="tick"></span><span>${event.label}</span>`;
          runs.set(event.id || event.name, run);
          box.appendChild(run);
        } else if (event.type === "tool_end") {
          const run = runs.get(event.id || event.name);
          if (run) run.className = "run " + (event.ok ? "done" : "failed");
        } else if (event.type === "cost") {
          if (!liveCost) {
            liveCost = document.createElement("div");
            liveCost.className = "cost";
            box.appendChild(liveCost);
          }
          liveCost.textContent =
            `${money(event.run.cost_usd)}  ·  running`;
          box.appendChild(pending);
        } else if (event.type === "error") {
          pending.remove();
          box.appendChild(bubble("fail", event.text));
        } else if (event.type === "done") {
          if (liveCost && event.run) liveCost.replaceWith(costLine(event.run));
          if (event.total) {
            state.project.usage = event.total;
            paintMeter();
          }
        }
        box.scrollTop = box.scrollHeight;
      }
    }
  } catch (err) {
    box.appendChild(bubble("fail", err.message));
  } finally {
    pending.remove();
    setBusy(false);
    await openProject(state.projectId);
    $("input").focus();
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  $("send").textContent = busy ? "Working" : "Send";
}

/* wiring -------------------------------------------------------------- */

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
    e.target.disabled = true;
    e.target.textContent = "Generating";
    try {
      await api(`/api/projects/${state.projectId}/export`, { method: "POST" });
      await loadExports();
      toast("Files written to the project folder.");
    } catch (err) { toast(err.message, true); }
    finally { e.target.disabled = false; e.target.textContent = "Generate files"; }
  });

  $("sceneSelect").addEventListener("change", (e) => state.three?.setScene(e.target.value));
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
    $("anthropicKey").value = "";
    $("geminiKey").value = "";
    $("openaiKey").value = "";
    $("modelSelect").value = state.config.model;
    paintPrice();
    $("settings").showModal();
  });

  document.querySelectorAll(".clear").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const data = await api("/api/settings", {
          method: "POST", body: JSON.stringify({ [btn.dataset.clear]: "" }),
        });
        state.config = data.config;
        toast("Key removed.");
      } catch (err) { toast(err.message, true); }
    });
  });

  $("saveSettings").addEventListener("click", async (e) => {
    e.preventDefault();
    const payload = { ANTHROPIC_MODEL: $("modelSelect").value };
    for (const [key, el] of [["ANTHROPIC_API_KEY", "anthropicKey"],
                             ["GEMINI_API_KEY", "geminiKey"],
                             ["OPENAI_API_KEY", "openaiKey"]]) {
      const value = $(el).value.trim();
      if (value) payload[key] = value;
    }
    try {
      const data = await api("/api/settings", { method: "POST",
                                                body: JSON.stringify(payload) });
      state.config = data.config;
      paintKeyState();
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
      if (file) state.pending.push({ name: file.name || "pasted.png",
                                     data: await readFile(file) });
    }
    paintAttachments();
  });
}

function readFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(file);
  });
}

boot();
})();
