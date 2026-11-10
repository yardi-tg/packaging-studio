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
  config: {}, agents: [], boxTypes: {}, models: [], scenes: [],
  projectId: null, project: null,
  pending: [], view: "flat", busy: false, three: null,
  surface: "studio", scene: "studio",
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
      scenes: status.scenes || [], pricing: status.pricing || {},
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
  paintGallery();
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
    $("view-three").innerHTML = `<p class="note">The 3D library did not load.</p>`;
    return;
  }
  let data;
  try { data = await api(`/api/projects/${state.projectId}/textures`); }
  catch (err) { toast(err.message, true); return; }
  if (!state.three) { state.three = createThree($("three")); paintSurfaces(); }
  state.three.build(data);
}

/* Surfaces to stand the package on. Chosen by eye in the viewport, and the
   names line up with the photo scenes so one leads naturally into the other. */
const SURFACES = [
  { id: "studio", label: "Studio",  swatch: "#e9e6df" },
  { id: "marble", label: "Marble",  swatch: "#eceae5" },
  { id: "wood",   label: "Wood",    swatch: "#9a6c42" },
  { id: "linen",  label: "Linen",   swatch: "#d6cdbb" },
  { id: "shelf",  label: "Shelf",   swatch: "#c2a983" },
  { id: "dark",   label: "Dark",    swatch: "#23262d" },
];

function paintSurfaces() {
  const strip = $("sceneStrip");
  strip.innerHTML = "";
  for (const s of SURFACES) {
    const btn = document.createElement("button");
    btn.className = "surface" + (s.id === state.surface ? " on" : "");
    btn.innerHTML = `<i style="background:${s.swatch}"></i><span>${s.label}</span>`;
    btn.onclick = () => {
      state.surface = s.id;
      state.three?.setSurface(s.id);
      paintSurfaces();
    };
    strip.appendChild(btn);
  }
}

/* --- procedural materials: no texture files to ship or download --- */

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
  wood: (ctx, w, h) => {
    ctx.fillStyle = "#8d6039"; ctx.fillRect(0, 0, w, h);
    for (let y = 0; y < h; y += 2 + Math.random() * 5) {
      ctx.strokeStyle = `rgba(${60 + Math.random() * 40},${35 + Math.random() * 25},18,${0.05 + Math.random() * 0.16})`;
      ctx.lineWidth = 0.6 + Math.random() * 2.4;
      ctx.beginPath();
      ctx.moveTo(0, y);
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
  marble: (ctx, w, h) => {
    ctx.fillStyle = "#e8e6e0"; ctx.fillRect(0, 0, w, h);
    const vein = (count, alpha, width) => {
      for (let i = 0; i < count; i++) {
        ctx.strokeStyle = `rgba(${86 + Math.random() * 46},${88 + Math.random() * 46},${96 + Math.random() * 46},${alpha * (0.4 + Math.random())})`;
        ctx.lineWidth = width * (0.4 + Math.random());
        ctx.beginPath();
        let x = Math.random() * w, y = -10;
        ctx.moveTo(x, y);
        while (y < h + 10) {
          x += (Math.random() - 0.45) * 64;
          y += 10 + Math.random() * 22;
          ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    };
    vein(9, 0.34, 3.2);    // the few bold ones
    vein(30, 0.14, 1.1);   // the hairline network
    grain(ctx, w, h, 5000, 0.03);
  },
  linen: (ctx, w, h) => {
    ctx.fillStyle = "#ded5c3"; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(120,108,88,.1)"; ctx.lineWidth = 1;
    for (let i = 0; i < w; i += 3) {
      ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(w, i); ctx.stroke();
    }
    grain(ctx, w, h, 14000, 0.07);
  },
  shelf: (ctx, w, h) => {
    SURFACE_ART.wood(ctx, w, h);
    ctx.fillStyle = "rgba(230,215,190,.26)"; ctx.fillRect(0, 0, w, h);
  },
  dark: (ctx, w, h) => {
    ctx.fillStyle = "#23262d"; ctx.fillRect(0, 0, w, h);
    grain(ctx, w, h, 18000, 0.22);
  },
  studio: (ctx, w, h) => { ctx.fillStyle = "#e4e1da"; ctx.fillRect(0, 0, w, h); },
};

const BACKDROP = {
  studio: ["#f4f2ee", "#dcd8d0"],
  marble: ["#f6f5f2", "#d9d6cf"],
  wood:   ["#efe3d2", "#c5a984"],
  linen:  ["#f1ebe0", "#d4c9b6"],
  shelf:  ["#efe6d8", "#c9b89c"],
  dark:   ["#1d2026", "#0e1014"],
};

function createThree(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true,
                                             preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  // Filmic tone mapping plus sRGB output is most of what separates a render
  // that looks like a render from one that looks like a photograph.
  if (THREE.ACESFilmicToneMapping) renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 200);
  const root = new THREE.Group();
  scene.add(root);

  const key = new THREE.DirectionalLight(0xfff6e8, 2.1);
  key.position.set(4.5, 7, 5.5);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 1; key.shadow.camera.far = 40;
  key.shadow.camera.left = -8; key.shadow.camera.right = 8;
  key.shadow.camera.top = 8; key.shadow.camera.bottom = -8;
  key.shadow.bias = -0.0008;
  key.shadow.radius = 3;

  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.7);
  rim.position.set(-6, 3.5, -4.5);
  const ambient = new THREE.AmbientLight(0xffffff, 0.22);
  scene.add(key, rim, ambient);

  /* A soft dark patch directly under the box. Shadow maps alone wash out on a
     bright surface, and without this the package looks like it is hovering. */
  const contactTexture = canvasTexture(256, 256, (ctx, w, h) => {
    const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
    g.addColorStop(0, "rgba(0,0,0,.55)");
    g.addColorStop(0.3, "rgba(0,0,0,.26)");
    g.addColorStop(0.62, "rgba(0,0,0,.07)");
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  });
  const contact = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.MeshBasicMaterial({ map: contactTexture, transparent: true,
                                  depthWrite: false })
  );
  contact.rotation.x = -Math.PI / 2;
  contact.position.y = 0.004;
  contact.renderOrder = 1;

  /* An environment map gives the carton something to reflect, which is what
     makes a varnished surface read as a surface instead of flat colour. */
  const pmrem = new THREE.PMREMGenerator(renderer);
  pmrem.compileEquirectangularShader?.();

  function buildEnvironment(top, bottom, warm) {
    const tex = canvasTexture(1024, 512, (ctx, w, h) => {
      const sky = ctx.createLinearGradient(0, 0, 0, h);
      sky.addColorStop(0, top);
      sky.addColorStop(0.52, top);
      sky.addColorStop(0.56, bottom);
      sky.addColorStop(1, bottom);
      ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);
      // key softbox
      const box = ctx.createRadialGradient(w * 0.3, h * 0.2, 10, w * 0.3, h * 0.2, w * 0.2);
      box.addColorStop(0, warm ? "#fffaf0" : "#ffffff");
      box.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = box; ctx.fillRect(0, 0, w, h * 0.6);
      // soft fill opposite
      const fill = ctx.createRadialGradient(w * 0.76, h * 0.3, 10, w * 0.76, h * 0.3, w * 0.17);
      fill.addColorStop(0, "rgba(255,255,255,.55)");
      fill.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = fill; ctx.fillRect(0, 0, w, h * 0.7);
    });
    tex.mapping = THREE.EquirectangularReflectionMapping;
    const target = pmrem.fromEquirectangular(tex);
    scene.environment = target.texture;
    tex.dispose();
  }

  let backdrop = null, ground = null;

  function buildStage(name) {
    const [top, bottom] = BACKDROP[name] || BACKDROP.studio;
    buildEnvironment(top, bottom, name === "wood" || name === "shelf");

    if (backdrop) { scene.remove(backdrop); backdrop.geometry.dispose(); backdrop.material.dispose(); }
    const sky = canvasTexture(16, 256, (ctx, w, h) => {
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, top); g.addColorStop(1, bottom);
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    });
    backdrop = new THREE.Mesh(
      new THREE.SphereGeometry(60, 32, 24),
      new THREE.MeshBasicMaterial({ map: sky, side: THREE.BackSide })
    );
    scene.add(backdrop);

    if (ground) { scene.remove(ground); ground.geometry.dispose(); ground.material.dispose(); }
    const surface = canvasTexture(512, 512, SURFACE_ART[name] || SURFACE_ART.studio);
    surface.wrapS = surface.wrapT = THREE.RepeatWrapping;
    surface.repeat.set(name === "studio" ? 1 : 3, name === "studio" ? 1 : 3);
    ground = new THREE.Mesh(
      new THREE.PlaneGeometry(80, 80),
      new THREE.MeshStandardMaterial({
        map: surface,
        roughness: name === "marble" ? 0.3 : name === "dark" ? 0.55 : 0.8,
        metalness: 0,
        envMapIntensity: name === "marble" ? 0.7 : 0.3,
      })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);
  }

  const ctl = { theta: 0.58, phi: 1.22, dist: 8.4, spin: false, surface: "studio" };
  let dragging = false, lastX = 0, lastY = 0;

  canvas.addEventListener("pointerdown", (e) => {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    ctl.theta -= (e.clientX - lastX) * 0.008;
    ctl.phi = Math.max(0.3, Math.min(1.52, ctl.phi - (e.clientY - lastY) * 0.008));
    lastX = e.clientX; lastY = e.clientY;
  });
  const stop = () => { dragging = false; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    ctl.dist = Math.max(3, Math.min(24, ctl.dist * (1 + Math.sign(e.deltaY) * 0.1)));
  }, { passive: false });

  const loader = new THREE.TextureLoader();
  const cache = new Map();
  const tex = (url) => {
    if (cache.has(url)) return cache.get(url);
    const t = loader.load(url);
    t.anisotropy = renderer.capabilities.getMaxAnisotropy?.() || 8;
    if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
    cache.set(url, t);
    return t;
  };

  /* Printed board: slightly rough, with a thin varnish on top. Clearcoat is
     what gives the edge highlight you see on a real laminated carton. */
  function face(entry, fallback) {
    const Material = THREE.MeshPhysicalMaterial || THREE.MeshStandardMaterial;
    const opts = {
      roughness: 0.52, metalness: 0.0, envMapIntensity: 0.75,
      clearcoat: 0.3, clearcoatRoughness: 0.42,
    };
    if (entry) opts.map = tex(entry.url); else opts.color = fallback;
    return new Material(opts);
  }

  function makeBox(textures, dims, scale) {
    const w = dims.width * scale, h = dims.height * scale, d = dims.depth * scale;
    const side = "#ded9d1";
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), [
      face(textures.right, side), face(textures.left, side),
      face(textures.top, side), face(textures.bottom, side),
      face(textures.front, side), face(textures.back, side),
    ]);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.position.y = h / 2;
    return mesh;
  }

  let current = null;
  function build(data) {
    while (root.children.length) root.remove(root.children[0]);
    const biggest = Math.max(data.dims.width, data.dims.height, data.dims.depth);
    const scale = 2.6 / biggest;
    current = data;
    root.add(makeBox(data.textures, data.dims, scale));

    const footprint = Math.max(data.dims.width, data.dims.depth) * scale;
    contact.scale.set(footprint * 2.1, footprint * 2.1, 1);
    root.add(contact);

    if (ctl.surface === "shelf") {
      const gap = data.dims.width * scale * 1.25;
      for (const offset of [-gap, gap]) {
        const clone = makeBox(data.textures, data.dims, scale);
        clone.position.x = offset;
        clone.rotation.y = offset < 0 ? 0.1 : -0.1;
        root.add(clone);
      }
    }
    buildStage(ctl.surface);
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

  buildStage("studio");

  (function loop() {
    requestAnimationFrame(loop);
    if (ctl.spin) ctl.theta += 0.0035;
    const r = ctl.dist;
    camera.position.set(
      r * Math.sin(ctl.phi) * Math.sin(ctl.theta),
      r * Math.cos(ctl.phi) + 1.2,
      r * Math.sin(ctl.phi) * Math.cos(ctl.theta)
    );
    camera.lookAt(0, 1.15, 0);
    renderer.render(scene, camera);
  })();

  return {
    build,
    setSurface: (name) => { ctl.surface = name; if (current) build(current); else buildStage(name); },
    toggleSpin: () => (ctl.spin = !ctl.spin),
    snapshot: () => renderer.domElement.toDataURL("image/png"),
    ready: () => Boolean(current),
  };
}

/* photos --------------------------------------------------------------- */

function paintScenes() {
  const wrap = $("sceneCards");
  wrap.innerHTML = "";
  for (const scene of state.scenes || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scene" + (scene.id === state.scene ? " on" : "");
    btn.innerHTML = `<b>${scene.label}</b><span>${scene.blurb}</span>`;
    btn.onclick = () => { state.scene = scene.id; paintScenes(); };
    wrap.appendChild(btn);
  }
  const intro = $("photoIntro");
  if (state.config.image_provider === "none") {
    intro.innerHTML = "This needs an image model, because Claude cannot make " +
      "photographs. Add a Google Gemini key under <b>Settings</b> and this tab " +
      "will put your package into real settings — held in a hand, on a marble " +
      "counter, on a shelf.";
  }
}

function paintGallery() {
  const gallery = $("gallery");
  const photos = (state.project?.assets || []).filter((a) => a.role === "photo");
  gallery.innerHTML = "";
  for (const photo of photos.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "shot-card";
    const url = `/api/projects/${state.projectId}/assets/${photo.id}`;
    card.innerHTML = `<img src="${url}" alt="${photo.scene || "photo"}" loading="lazy">`;
    const link = document.createElement("a");
    link.href = url; link.download = photo.name || "photo.png"; link.textContent = "Save";
    card.appendChild(link);
    gallery.appendChild(card);
  }
}

async function shoot() {
  if (!state.projectId) return;
  if (!hasStructure()) { toast("Design something first.", true); return; }
  const status = $("photoStatus");

  // The render is the input, so the 3D view has to have run at least once.
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
  try {
    await api(`/api/projects/${state.projectId}/photoshoot`, {
      method: "POST",
      body: JSON.stringify({
        render, scene: state.scene,
        notes: $("photoNotes").value, aspect: $("photoAspect").value,
      }),
    });
    await openProject(state.projectId);
    status.textContent = "Done. Shoot again for another take — results vary.";
  } catch (err) {
    status.textContent = "";
    toast(err.message, true);
  } finally {
    button.disabled = false; button.textContent = "Shoot";
  }
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

  $("shoot").addEventListener("click", shoot);
  $("photoNotes").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); shoot(); }
  });
  $("toPhoto").addEventListener("click", () => {
    document.querySelector('.seg[data-view="photo"]').click();
    if ((state.scenes || []).some((s) => s.id === state.surface)) {
      state.scene = state.surface;
      paintScenes();
    }
    shoot();
  });

  $("probeBtn").addEventListener("click", async (e) => {
    const out = $("probeResult");
    e.target.disabled = true;
    out.textContent = "Checking…";
    try {
      const data = await api("/api/settings/probe", { method: "POST" });
      state.config = data.config || state.config;
      out.textContent = data.message || (data.ok ? "Connected." : "Not working.");
      out.style.color = data.ok ? "var(--good)" : "var(--bad)";
      paintScenes();
    } catch (err) {
      out.textContent = err.message;
      out.style.color = "var(--bad)";
    } finally { e.target.disabled = false; }
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
