/* Packaging Studio — website mode.
 *
 * On the website the server keeps nothing. Every project lives in this
 * browser (IndexedDB); the server only computes: previews, dielines,
 * production files, photos, and the designer itself (core/hosted.py).
 *
 * This file sits between the interface (app.js) and the network. It answers
 * the interface's /api/projects/... requests from local storage, and sends
 * whatever needs computing to the server's /api/h/... endpoints with the
 * project attached. A designer run that wants to look at its work, or that
 * runs into the host's time limit, pauses; this file renders what was asked
 * for (through the interface's own renderer) and carries the run on with a
 * fresh request. The interface sees one unbroken conversation.
 *
 * When the page is served by `python3 server.py` none of this switches on.
 */
(() => {
"use strict";

const originalFetch = window.fetch.bind(window);
const KEY = { code: "ps.code", model: "ps.model", effort: "ps.effort", seeded: "ps.seeded" };
const local = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

let code = local.get(KEY.code) || "";
let status = null;

/* helpers ---------------------------------------------------------------- */

const respond = (data, code = 200) => new Response(JSON.stringify(data),
  { status: code, headers: { "content-type": "application/json" } });
const stamp = () => new Date().toISOString().slice(0, 19);
const uid = () => Math.random().toString(16).slice(2, 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const slug = (text) => (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
  .replace(/^-+|-+$/g, "").slice(0, 40) || "project";
const safeName = (s) => (s || "image").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 60) || "image";
const clone = (v) => (typeof structuredClone === "function" ? structuredClone(v) : JSON.parse(JSON.stringify(v)));

function failure(message, status) { return Object.assign(new Error(message), { status }); }

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

function base64ToBlob(b64, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime || "application/octet-stream" });
}

function dataUrlToBlob(url) {
  const m = /^data:([^;,]+)?(;base64)?,(.*)$/s.exec(url || "");
  if (!m) return null;
  if (m[2]) return base64ToBlob(m[3], m[1]);
  return new Blob([decodeURIComponent(m[3])], { type: m[1] || "text/plain" });
}

function textToBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function errorText(text, code) {
  try { return JSON.parse(text).error || `HTTP ${code}`; } catch { return (text || `HTTP ${code}`).slice(0, 300); }
}

/* Re-encode a picture so it travels light: big uploads and the photo
   references go to the server as JPEG (or PNG where transparency matters). */
async function reencode(blob, maxSide, keepAlpha) {
  const bitmap = await createImageBitmap(blob);
  const k = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * k));
  canvas.height = Math.max(1, Math.round(bitmap.height * k));
  const ctx = canvas.getContext("2d");
  const encode = (type, q) => new Promise((r) => canvas.toBlob(r, type, q));
  if (keepAlpha) {
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const png = await encode("image/png");
    if (png && png.size < 2_500_000) return png;
  }
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return (await encode("image/jpeg", 0.88)) || blob;
}

async function prepareUpload(blob) {
  const usable = ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(blob.type);
  if (usable && blob.size <= 1_200_000) return blob;
  try {
    return await reencode(blob, 1600, ["image/png", "image/webp", "image/gif"].includes(blob.type));
  } catch { return blob; }
}

async function lighterDataUrl(dataUrl, maxSide) {
  const blob = dataUrlToBlob(dataUrl);
  if (!blob) return dataUrl;
  try {
    const out = await reencode(blob, maxSide, false);
    return "data:image/jpeg;base64," + await blobToBase64(out);
  } catch { return dataUrl; }
}

/* storage: IndexedDB -------------------------------------------------------- */

let dbPromise = null;
function openDb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open("packaging-studio", 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("projects")) db.createObjectStore("projects", { keyPath: "id" });
        if (!db.objectStoreNames.contains("files")) db.createObjectStore("files");
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  return dbPromise;
}

const result = (req) => new Promise((resolve, reject) => {
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
});

async function idb(name, mode, work) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(name, mode);
    let value;
    Promise.resolve(work(tx.objectStore(name))).then((v) => { value = v; }, reject);
    tx.oncomplete = () => resolve(value);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("Storage was interrupted"));
  });
}

const fileKey = (pid, kind, name) => `${pid}/${kind}/${name}`;
const range = (prefix) => IDBKeyRange.bound(prefix, prefix + "￿");

const putFile = (key, blob, meta = {}) => idb("files", "readwrite", (s) => result(s.put({ blob, ...meta }, key)));
const getFile = (key) => idb("files", "readonly", (s) => result(s.get(key)));
const deleteFiles = (prefix) => idb("files", "readwrite", (s) => result(s.delete(range(prefix))));

async function listFiles(prefix) {
  const [keys, values] = await idb("files", "readonly", (s) =>
    Promise.all([result(s.getAllKeys(range(prefix))), result(s.getAll(range(prefix)))]));
  return keys.map((key, i) => ({ key, ...values[i] }));
}

const urls = new Map();
async function fileUrl(key) {
  if (urls.has(key)) return urls.get(key);
  const rec = await getFile(key);
  if (!rec || !rec.blob) return "";
  const url = URL.createObjectURL(rec.blob);
  urls.set(key, url);
  return url;
}
function forgetUrls(prefix) {
  for (const [key, url] of urls) if (key.startsWith(prefix)) { URL.revokeObjectURL(url); urls.delete(key); }
}

const cache = new Map();
let writes = Promise.resolve();
let warnedQuota = false;

async function getProject(id) {
  if (cache.has(id)) return cache.get(id);
  const p = await idb("projects", "readonly", (s) => result(s.get(id)));
  if (!p) return null;
  for (const m of p.messages || []) if (m.archived) delete m.api;
  cache.set(id, p);
  return p;
}

function persist(p) {
  p.updated = stamp();
  cache.set(p.id, p);
  writes = writes.then(() => idb("projects", "readwrite", (s) => result(s.put(p)))).catch((err) => {
    console.error("could not save project", err);
    if (!warnedQuota) {
      warnedQuota = true;
      alert("This browser refused to store more. Save your project with “Save file”, " +
            "then clear the chat history.");
    }
  });
  return writes;
}

async function allProjects() {
  const stored = await idb("projects", "readonly", (s) => result(s.getAll()));
  const byId = new Map(stored.map((p) => [p.id, p]));
  for (const [id, p] of cache) byId.set(id, p);
  return [...byId.values()];
}

async function listProjects() {
  return (await allProjects()).map((p) => ({
    id: p.id, name: p.name, agent: p.agent || "packaging_designer", updated: p.updated || "",
    has_artwork: Boolean(p.artwork), box_type: p.dieline?.box_type || null,
    product: p.brief?.product_name || null, cost_usd: p.usage?.cost_usd || 0,
  })).sort((a, b) => (b.updated > a.updated ? 1 : -1));
}

async function createProject({ name, agent }) {
  const p = {
    id: `${slug(name)}-${uid().slice(0, 6)}`, name: name || "Untitled project",
    agent: agent || "packaging_designer", created: stamp(), updated: stamp(),
    brief: {}, dieline: null, artwork: null, assets: [], messages: [], copy: {},
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_usd: 0, runs: 0 }, log: [],
  };
  await persist(p);
  return p;
}

async function deleteProject(id) {
  cache.delete(id);
  await writes;
  await idb("projects", "readwrite", (s) => result(s.delete(id)));
  await deleteFiles(`${id}/`);
  forgetUrls(`${id}/`);
}

/* views of a project ------------------------------------------------------- */

function layerIndex(spec) {
  const out = [];
  for (const [panel, data] of Object.entries(spec?.panels || {})) {
    for (const l of data.layers || []) {
      const fill = typeof l.fill === "string" ? l.fill : (typeof l.color === "string" ? l.color : null);
      out.push({ panel, id: l.id, name: l.name, type: l.type, text: l.text, size: l.size,
                 font: l.font, icon: l.icon, digits: l.digits, visible: l.visible ?? true, fill });
    }
  }
  return out;
}

function publicView(p) {
  const { messages, ...rest } = p;
  const out = clone(rest);
  out.messages = (messages || []).filter((m) => !m.hidden && m.display).map((m) => ({
    role: m.role, text: m.display || "", tools: m.tools || [], cost: m.cost || null,
  }));
  if (p.artwork) out.layers = layerIndex(p.artwork);
  return out;
}

function lean(p) {
  const { messages, log, ...rest } = p;
  return rest;
}

function referencedAssets(p) {
  const used = new Set();
  for (const panel of Object.values(p.artwork?.panels || {})) {
    for (const l of panel.layers || []) if (typeof l.src === "string" && l.src) used.add(l.src);
    const bg = panel.background;
    if (bg && typeof bg === "object" && typeof bg.src === "string") used.add(bg.src);
  }
  return (p.assets || []).filter((a, i) => used.has(`asset:${a.id}`) || used.has(`ref:${i}`) || used.has(a.id))
    .map((a) => a.id);
}

const encoded = new Map();
async function assetPayload(p, ids) {
  const out = [];
  for (const id of ids) {
    const key = fileKey(p.id, "asset", id);
    if (!encoded.has(key)) {
      const rec = await getFile(key);
      if (!rec || !rec.blob) continue;
      encoded.set(key, { id, mime: rec.blob.type || "image/png", data: await blobToBase64(rec.blob) });
    }
    out.push(encoded.get(key));
  }
  return out;
}

/* talking to the server ------------------------------------------------------ */

async function server(route, payload) {
  const text = JSON.stringify(payload);
  const headers = { "x-access-code": code };
  let body = text;
  if (text.length > 64_000 && typeof CompressionStream === "function") {
    body = await new Response(new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"))).blob();
    headers["x-body-encoding"] = "gzip";
    headers["content-type"] = "application/octet-stream";
  } else {
    headers["content-type"] = "application/json";
  }
  const res = await originalFetch("/api/h/" + route, { method: "POST", headers, body });
  if (res.status === 401) { lockAgain(); throw failure("Please enter the access code again.", 401); }
  if (res.status === 413) {
    throw failure("That was too much for one request. Clear the chat history (top of the chat) and try again.", 413);
  }
  return res;
}

async function serverJson(route, payload) {
  const res = await server(route, payload);
  const text = await res.text();
  if (!res.ok) throw failure(errorText(text, res.status), res.status);
  return text ? JSON.parse(text) : {};
}

/* Previews are asked for often and rarely change: keep the last few. */
const svgCache = new Map();
function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36) + ":" + text.length;
}

async function renderSvg(p, view) {
  const base = lean(p);
  const ids = referencedAssets(p);
  const key = hash(JSON.stringify([base.dieline, base.artwork, ids, view]));
  if (svgCache.has(key)) return svgCache.get(key);
  const res = await server("render", { project: base, assets: await assetPayload(p, ids), ...view });
  const text = await res.text();
  if (!res.ok) throw failure(errorText(text, res.status), res.status);
  svgCache.set(key, text);
  if (svgCache.size > 40) svgCache.delete(svgCache.keys().next().value);
  return text;
}

/* routes ----------------------------------------------------------------- */

function choice() {
  const s = status || {};
  const models = (s.models || []).map((m) => m.id);
  const efforts = s.efforts || ["high"];
  const model = models.includes(local.get(KEY.model)) ? local.get(KEY.model) : s.config?.model;
  const effort = efforts.includes(local.get(KEY.effort)) ? local.get(KEY.effort) : s.config?.effort;
  return { model, effort };
}

function statusView() {
  const s = clone(status);
  const { model, effort } = choice();
  s.config.model = model;
  s.config.effort = effort;
  s.config.model_label = (s.models || []).find((m) => m.id === model)?.label || s.config.model_label;
  return s;
}

async function route(url, method, init) {
  const path = url.pathname;
  let body = {};
  if (init && typeof init.body === "string" && init.body) {
    try { body = JSON.parse(init.body); } catch { body = {}; }
  }

  if (path === "/api/status") return respond(statusView());
  if (path === "/api/settings") {
    if (body.ANTHROPIC_MODEL) local.set(KEY.model, body.ANTHROPIC_MODEL);
    if (body.ANTHROPIC_EFFORT) local.set(KEY.effort, body.ANTHROPIC_EFFORT);
    return respond({ ok: true, config: statusView().config });
  }
  if (path === "/api/settings/probe") {
    const probe = await serverJson("probe", {});
    return respond({ ...probe, config: statusView().config });
  }
  if (path === "/api/projects") {
    if (method === "POST") return respond({ project: publicView(await createProject(body)) });
    return respond({ projects: await listProjects() });
  }

  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[1] !== "projects" || !parts[2]) return respond({ error: "Unknown path" }, 404);
  const [, , pid, action = "", extra] = parts;
  if (method === "DELETE" && !action) { await deleteProject(pid); return respond({ ok: true }); }
  const p = await getProject(pid);
  if (!p) return respond({ error: "Project not found" }, 404);

  switch (action) {
    case "":
      return respond({ project: publicView(p) });
    case "assets":
      if (extra) return fileResponse(fileKey(pid, "asset", extra));
      return respond(await addAssets(p, body.files || []));
    case "render": {
      const q = url.searchParams;
      if (!p.dieline) return respond({ error: "No structure yet" }, 409);
      const svg = await renderSvg(p, { mode: q.get("mode") || "flat", panel: q.get("panel"),
                                       labels: q.get("labels") === "1",
                                       scale: q.get("scale") ? Number(q.get("scale")) : null });
      return new Response(svg, { headers: { "content-type": "image/svg+xml; charset=utf-8" } });
    }
    case "textures":
      return respond(await textures(p));
    case "exports":
      if (extra) return fileResponse(fileKey(pid, "export", extra));
      return respond({ exports: await listExports(pid) });
    case "structure": {
      const { dieline } = await serverJson("structure", body);
      p.dieline = dieline;
      if (body.reset_artwork) p.artwork = null;
      await persist(p);
      return respond({ ok: true, dieline });
    }
    case "layers": {
      if (!p.artwork) return respond({ error: "No artwork yet" }, 409);
      const out = await serverJson("layers", { artwork: p.artwork, patches: body.patches || [] });
      p.artwork = out.spec;
      await persist(p);
      return respond({ ok: true, applied: out.applied, layers: out.layers });
    }
    case "export":
      return respond(await exportFiles(p));
    case "photoshoot":
      return respond(await photoshoot(p, body));
    case "render_result":
      deliverRender(body);
      return respond({ ok: true });
    case "clear":
      p.messages = [];
      delete p.run_state; delete p.pending_tools; delete p.carryover;
      await persist(p);
      return respond({ ok: true });
    case "rename":
      p.name = String(body.name || p.name).slice(0, 80);
      await persist(p);
      return respond({ ok: true });
    case "chat":
      return chatResponse(p, body);
    default:
      return respond({ error: "Unknown path" }, 404);
  }
}

async function fileResponse(key) {
  const rec = await getFile(key);
  if (!rec || !rec.blob) return respond({ error: "File not found" }, 404);
  return new Response(rec.blob, { headers: { "content-type": rec.blob.type || "application/octet-stream" } });
}

async function addAssets(p, files) {
  const added = [];
  for (const item of files) {
    const original = dataUrlToBlob(item.data || "");
    if (!original || !original.size) continue;
    const blob = await prepareUpload(original);
    const ext = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif" }[blob.type] || "";
    let base = safeName(item.name || "image");
    if (ext && !base.toLowerCase().endsWith(ext) && !(ext === ".jpg" && /\.jpe?g$/i.test(base))) {
      base = base.replace(/\.[A-Za-z0-9]+$/, "") + ext;
    }
    const id = `${uid()}_${base}`;
    await putFile(fileKey(p.id, "asset", id), blob);
    const asset = { id, name: item.name || base, role: item.role || "reference",
                    mime: blob.type || "image/png", bytes: blob.size, added: stamp() };
    (p.assets = p.assets || []).push(asset);
    added.push(asset);
  }
  await persist(p);
  return { ok: true, assets: added };
}

async function textures(p) {
  const dl = p.dieline;
  if (!dl) throw failure("No structure yet", 409);
  const faces = dl.panels.filter((g) => g.printable !== false);
  const entries = await Promise.all(faces.map(async (g) => {
    const svg = await renderSvg(p, { mode: "panel", panel: g.id, scale: 10 });
    const entry = { url: "data:image/svg+xml;base64," + textToBase64(svg),
                    w: g.dw ?? g.w, h: g.dh ?? g.h, label: g.label };
    if (hasFinish(p.artwork, g.id)) {
      const mask = await renderSvg(p, { mode: "finish", panel: g.id, scale: 10 });
      entry.finish = "data:image/svg+xml;base64," + textToBase64(mask);
    }
    return [g.id, entry];
  }));
  return { textures: Object.fromEntries(entries), dims: dl.dims, box_type: dl.box_type };
}

/* Same test as artwork.layer_finishes on the server, loosely: any layer on
   this face that asks for foil, varnish or emboss. */
function hasFinish(spec, panelId) {
  const layers = spec?.panels?.[panelId]?.layers || [];
  return layers.some((l) => l.visible !== false && l.finish &&
    /foil|metallic|uv|gloss|varnish|emboss|deboss|raised/i.test([].concat(l.finish).join(" ")));
}

async function listExports(pid) {
  const files = await listFiles(`${pid}/export/`);
  const out = [];
  for (const f of files.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))) {
    out.push({ name: f.key.split("/").slice(2).join("/"), bytes: f.blob?.size || 0,
               modified: f.modified || "", url: await fileUrl(f.key) });
  }
  return out;
}

async function exportFiles(p) {
  if (!p.dieline) throw failure("No structure yet", 409);
  const base = lean(p);
  const assets = await assetPayload(p, referencedAssets(p));
  const { files } = await serverJson("export", { project: base, assets });
  const made = [];
  let next = 0;
  async function worker() {
    while (next < files.length) {
      const index = next++;
      const file = files[index];
      const res = await server("export", { project: base, assets, file: file.name });
      if (!res.ok) throw failure(errorText(await res.text(), res.status), res.status);
      const blob = new Blob([await res.arrayBuffer()], { type: file.mime });
      made.push({ file, blob, index });
    }
  }
  await Promise.all([worker(), worker(), worker(), worker()]);
  await deleteFiles(`${p.id}/export/`);
  forgetUrls(`${p.id}/export/`);
  for (const { file, blob, index } of made) {
    await putFile(fileKey(p.id, "export", file.name), blob, { order: index, modified: stamp() });
  }
  return { ok: true, files: await listExports(p.id) };
}

async function photoshoot(p, body) {
  // A PNG of the 3D view and of the front can each run to megabytes; as
  // JPEG they carry the same information at a fraction of the size.
  const render = await lighterDataUrl(body.render, 1600);
  const front = body.front ? await lighterDataUrl(body.front, 1600) : null;
  const res = await server("photoshoot", { project: lean(p), render, front, scene: body.scene,
                                           notes: body.notes, aspect: body.aspect });
  if (!res.ok) throw failure(errorText(await res.text(), res.status), res.status);
  const asset = JSON.parse(decodeURIComponent(res.headers.get("x-asset") || "%7B%7D"));
  const blob = await res.blob();
  await putFile(fileKey(p.id, "asset", asset.id), blob);
  (p.assets = p.assets || []).push(asset);
  await persist(p);
  return { ok: true, asset, url: await fileUrl(fileKey(p.id, "asset", asset.id)) };
}

/* the designer: one conversation across as many requests as it needs ------- */

const waiting = new Map();

function waitForRender(id, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { waiting.delete(id); resolve(null); }, ms);
    waiting.set(id, (picture) => { clearTimeout(timer); waiting.delete(id); resolve(picture); });
  });
}

function deliverRender(body) {
  const done = waiting.get(body.request_id);
  if (!done) return;
  const data = String(body.png || "");
  const comma = data.indexOf(",");
  const b64 = comma >= 0 ? data.slice(comma + 1) : data;
  done(b64.length > 1000 ? { media_type: body.media_type || "image/png", data: b64 } : null);
}

async function applyCheckpoint(p, cp) {
  const before = JSON.stringify([p.dieline, p.artwork, (p.assets || []).length]);
  const messages = p.messages || (p.messages = []);
  if (messages.length !== cp.from) {
    console.warn("conversation out of step", messages.length, cp.from);
    messages.length = Math.min(messages.length, cp.from);
  }
  for (const f of cp.flags || []) {
    const m = messages[f.i];
    if (!m) continue;
    if (f.archived) { m.archived = true; delete m.api; } else delete m.archived;
    if (f.cost) m.cost = f.cost; else delete m.cost;
  }
  for (const m of cp.append || []) {
    if (m.archived) delete m.api;
    messages.push(m);
  }
  for (const k of Object.keys(p)) if (k !== "messages" && !(k in cp.fields)) delete p[k];
  Object.assign(p, cp.fields);
  for (const f of cp.files || []) {
    await putFile(fileKey(p.id, "asset", f.id), base64ToBlob(f.data, f.mime));
  }
  persist(p);
  return JSON.stringify([p.dieline, p.artwork, (p.assets || []).length]) !== before;
}

async function relay(res, p, out) {
  const outcome = { suspend: null, done: false, checkpoints: 0, held: [] };
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === "checkpoint") {
          outcome.checkpoints++;
          // Let the preview catch up with what the designer just changed.
          if (await applyCheckpoint(p, ev)) out({ type: "artwork_update" });
          continue;
        }
        // Held back until the checkpoint that carries the latest artwork has
        // arrived, so the picture shows what the designer just did.
        if (ev.type === "render_request") { outcome.held.push(ev); continue; }
        if (ev.type === "suspend") { outcome.suspend = ev; continue; }
        if (ev.type === "end") continue;
        if (ev.type === "done") outcome.done = true;
        out(ev);
      }
    }
  } catch (err) {
    console.warn("stream interrupted", err);
  }
  return outcome;
}

async function runChat(p, body, out) {
  const { model, effort } = choice();
  const ids = body.asset_ids || [];
  let payload = { project: p, text: body.text || "", asset_ids: ids,
                  attachments: await assetPayload(p, ids), model, effort };
  let lost = 0;
  for (let round = 0; round < 80; round++) {
    let outcome = null;
    try {
      const res = await server("chat", payload);
      if (!res.ok || !res.body) throw failure(errorText(await res.text(), res.status), res.status);
      outcome = await relay(res, p, out);
    } catch (err) {
      if (err.status) throw err;   // the server answered: no point retrying
      outcome = { checkpoints: 0 };
    }
    if (outcome.done) return;
    if (outcome.suspend) {
      lost = 0;
      let pictures = {};
      if (outcome.suspend.reason === "render") {
        const asked = outcome.suspend.renders || [];
        const answers = asked.map((r) => waitForRender(r.request_id, 90_000).then((pic) => [r.request_id, pic]));
        for (const ev of outcome.held) out(ev);
        pictures = Object.fromEntries((await Promise.all(answers)).filter(([, pic]) => pic));
        out({ type: "status", text: "Looking" });
      } else {
        out({ type: "status", text: "Continuing" });
      }
      payload = { project: p, resume: pictures, model, effort };
      continue;
    }
    // The answer ended without a verdict: the connection dropped, or the host
    // stopped the request. Everything up to the last checkpoint is safe.
    if (++lost > 2) {
      throw new Error("The connection to the server was lost. Your design is saved up to " +
                      "the last finished step; send a message to carry on.");
    }
    out({ type: "status", text: "Reconnecting" });
    await sleep(1500 * lost);
    if (outcome.checkpoints > 0 || payload.resume) {
      if (!p.run_state) return;
      payload = { project: p, resume: payload.resume && !outcome.checkpoints ? payload.resume : {}, model, effort };
    } else {
      payload = { ...payload, project: p };
    }
  }
  throw new Error("The designer took too many rounds. Send a message to carry on.");
}

function chatResponse(p, body) {
  const encoder = new TextEncoder();
  let controller;
  const stream = new ReadableStream({ start(c) { controller = c; } });
  const out = (ev) => {
    try { controller.enqueue(encoder.encode("data: " + JSON.stringify(ev) + "\n\n")); } catch { /* closed */ }
  };
  runChat(p, body, out)
    .catch((err) => {
      out({ type: "error", text: err.message || String(err) });
      out({ type: "done", text: "" });
    })
    .finally(() => {
      out({ type: "end" });
      try { controller.close(); } catch { /* already closed */ }
    });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

/* project files: the browser is the only copy, so make backups easy -------- */

async function saveProjectFile(pid) {
  const p = await getProject(pid);
  if (!p) return;
  const files = [];
  for (const a of p.assets || []) {
    const rec = await getFile(fileKey(pid, "asset", a.id));
    if (rec && rec.blob) files.push({ id: a.id, mime: rec.blob.type, data: await blobToBase64(rec.blob) });
  }
  const blob = new Blob([JSON.stringify({ format: "packaging-studio-project", version: 1, project: p, files })],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${slug(p.name)}.packaging.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

async function openProjectFile(file) {
  let data;
  try { data = JSON.parse(await file.text()); } catch { data = null; }
  if (!data || data.format !== "packaging-studio-project" || !data.project || !data.project.id) {
    alert("That is not a Packaging Studio project file.");
    return;
  }
  const p = data.project;
  if (await getProject(p.id)) {
    p.id = `${slug(p.name)}-${uid().slice(0, 6)}`;
    p.name = `${p.name} (copy)`;
  }
  for (const f of data.files || []) await putFile(fileKey(p.id, "asset", f.id), base64ToBlob(f.data, f.mime));
  await persist(p);
  location.reload();
}

/* the code gate ------------------------------------------------------------ */

async function fetchStatus(withCode) {
  const res = await originalFetch("/api/status", { headers: { "x-access-code": withCode || "" } });
  return res.json();
}

function gate(message) {
  const el = document.getElementById("gate");
  const input = document.getElementById("gateCode");
  const note = document.getElementById("gateError");
  el.hidden = false;
  document.body.classList.add("locked");
  note.textContent = message || "";
  setTimeout(() => input.focus(), 50);
  return new Promise((resolve) => {
    let busy = false;
    const attempt = async () => {
      const value = input.value.replace(/\D/g, "");
      if (busy || value.length < 4) return;
      busy = true;
      note.textContent = "";
      el.classList.add("checking");
      try {
        const s = await fetchStatus(value);
        if (s && !s.locked) {
          code = value;
          local.set(KEY.code, value);
          el.hidden = true;
          document.body.classList.remove("locked");
          resolve(s);
          return;
        }
        note.textContent = (s && s.message) || "That code is not right.";
        el.classList.remove("shake"); void el.offsetWidth; el.classList.add("shake");
        input.value = "";
      } catch {
        note.textContent = "Cannot reach the site. Check your connection.";
      } finally {
        busy = false;
        el.classList.remove("checking");
        input.focus();
      }
    };
    input.addEventListener("input", () => {
      input.value = input.value.replace(/\D/g, "").slice(0, 4);
      if (input.value.length === 4) attempt();
    });
    document.getElementById("gateForm").addEventListener("submit", (e) => { e.preventDefault(); attempt(); });
  });
}

let relocking = false;
function lockAgain() {
  if (relocking) return;
  relocking = true;
  code = "";
  local.set(KEY.code, null);
  gate("Please enter the access code again.").then(() => location.reload());
}

/* start -------------------------------------------------------------------- */

async function seedExample() {
  if (local.get(KEY.seeded)) return;
  try {
    if ((await listProjects()).length) { local.set(KEY.seeded, "1"); return; }
    const { project } = await serverJson("example", {});
    if (project) await persist(project);
    local.set(KEY.seeded, "1");
  } catch { /* no example; start empty */ }
}

function adaptInterface() {
  const efforts = status.efforts || [];
  document.querySelectorAll("#effortSelect option").forEach((opt) => {
    if (!efforts.includes(opt.value)) opt.remove();
  });
  const current = () => document.getElementById("projectSelect")?.value;
  document.getElementById("saveFile")?.addEventListener("click", () => current() && saveProjectFile(current()));
  const picker = document.getElementById("openFileInput");
  document.getElementById("openFile")?.addEventListener("click", () => picker?.click());
  picker?.addEventListener("change", (e) => { const f = e.target.files[0]; if (f) openProjectFile(f); });
}

function install() {
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) {
      return originalFetch(input, init);
    }
    try {
      return await route(url, ((init && init.method) || "GET").toUpperCase(), init);
    } catch (err) {
      return respond({ error: err.message || String(err) }, err.status || 500);
    }
  };
  window.PS = {
    hosted: true,
    assetUrl: (pid, id) => fileUrl(fileKey(pid, "asset", id)),
  };
}

window.PS_READY = (async () => {
  let s;
  try { s = await fetchStatus(code); } catch { return; }   // app.js reports the failure
  if (!s || !s.hosted) return;                            // the local server
  document.body.classList.add("hosted");
  if (s.locked) s = await gate(code ? s.message : "");
  status = s;
  install();
  adaptInterface();
  try { navigator.storage && navigator.storage.persist && navigator.storage.persist(); } catch { /* optional */ }
  await seedExample();
})();
})();
