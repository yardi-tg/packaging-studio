#!/usr/bin/env python3
"""Packaging Studio - local server.

Run it with:   python3 server.py
Then open:     http://localhost:8777

Nothing is installed and nothing is uploaded. The only outbound traffic goes to
api.anthropic.com, using your own API key.
"""

from __future__ import annotations
import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "\n  This program needs Python 3.8 or newer.\n"
        "  Found Python %d.%d.\n\n"
        "  Open Terminal and run:\n\n"
        "      xcode-select --install\n\n"
        "  Then double-click start.command again.\n\n"
        % sys.version_info[:2]
    )
    raise SystemExit(1)

import base64
import json
import mimetypes
import queue
import re
import socket
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import artwork, config, dieline, fonts, imagegen, pdfwriter, registry, store, visual

MAX_BODY = 20 * 1024 * 1024
_RUNNING: set = set()
_RUN_LOCK = threading.Lock()
SETTING_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_EFFORT",
                "GEMINI_API_KEY", "OPENAI_API_KEY")


class Handler(BaseHTTPRequestHandler):
    server_version = "PackagingStudio"

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ------------------------------------------------------------

    def _send(self, code, body=b"", content_type="application/json",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False), "application/json")

    def _error(self, message, code=400):
        self._json({"error": message}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("Request too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        try:
            self._route_get()
        except Exception as exc:
            traceback.print_exc()
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as exc:
            traceback.print_exc()
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_DELETE(self):
        parts = [p for p in urllib.parse.urlparse(self.path)
                 .path.strip("/").split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "projects"]:
            return self._json({"ok": store.delete(parts[2])})
        self._error("Unknown path", 404)

    # -- GET ----------------------------------------------------------------

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in path.strip("/").split("/") if p]

        if path in ("/", "/index.html"):
            return self._serve_file("index.html")
        if parts and parts[0] == "static":
            return self._serve_file("/".join(parts[1:]))

        if path == "/api/status":
            return self._json({
                "config": config.status(),
                "agents": registry.catalog(),
                "box_types": dieline.BOX_TYPES,
                "models": config.MODEL_CHOICES,
                "pricing": config.MODEL_PRICING,
                "scenes": imagegen.scene_catalog(),
                "fonts": {k: {"name": v[0], "hint": v[2]}
                          for k, v in artwork.FONT_CATALOG.items()},
            })

        if path == "/api/projects":
            return self._json({"projects": store.list_projects()})

        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            project_id, rest = parts[2], parts[3:]
            project = store.load(project_id)
            if not project:
                return self._error("Project not found", 404)

            if not rest:
                return self._json({"project": _public(project)})

            if rest[0] == "assets" and len(rest) == 2:
                path_obj = store.asset_path(project_id, rest[1])
                if not path_obj:
                    return self._error("Image not found", 404)
                mime = mimetypes.guess_type(rest[1])[0] or "image/png"
                return self._send(200, path_obj.read_bytes(), mime)

            if rest[0] == "render":
                return self._render(project, query)
            if rest[0] == "textures":
                return self._textures(project)
            if rest[0] == "exports" and len(rest) == 1:
                return self._json({"exports": store.list_exports(project_id)})
            if rest[0] == "exports" and len(rest) == 2:
                path_obj = store.export_path(project_id, rest[1])
                if not path_obj:
                    return self._error("File not found", 404)
                mime = mimetypes.guess_type(rest[1])[0] or "application/octet-stream"
                return self._send(
                    200, path_obj.read_bytes(), mime,
                    {"Content-Disposition": f'attachment; filename="{rest[1]}"'},
                )

        self._error("Unknown path", 404)

    def _serve_file(self, rel: str):
        rel = re.sub(r"\.\.+", ".", rel).lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        if (not str(target).startswith(str(config.WEB_DIR.resolve()))
                or not target.exists()):
            return self._error("File not found", 404)
        mime = mimetypes.guess_type(target.name)[0] or "text/plain"
        charset = ("; charset=utf-8"
                   if mime.startswith("text") or "javascript" in mime else "")
        self._send(200, target.read_bytes(), mime + charset)

    def _render(self, project, query):
        dl = project.get("dieline")
        if not dl:
            return self._error("No structure yet", 409)
        spec = project.get("artwork") or {"panels": {}}
        mode = (query.get("mode") or ["flat"])[0]
        labels = (query.get("labels") or ["0"])[0] == "1"
        css = fonts.embed_css(spec)

        # The in-browser preview gets plain URLs: inlining every image as
        # base64 makes the document enormous and the browser crawl. Anything
        # that leaves the browser gets real data URIs so it stays standalone.
        standalone = mode in ("print", "panel")
        assets = store.asset_map(project, spec, as_data=standalone)

        if mode == "dieline":
            svg = dieline.to_svg(dl, show_labels=True, show_dimensions=True)
        elif mode == "panel":
            panel_id = (query.get("panel") or [""])[0]
            geo = next((p for p in dl["panels"] if p["id"] == panel_id), None)
            if not geo:
                return self._error("Unknown panel", 404)
            svg = artwork.render_panel_svg(spec, geo, assets,
                                           font_css=fonts.embed_css(spec, [geo["id"]]))
        elif mode == "print":
            svg = artwork.full_svg(spec, dl, assets, with_dieline=False, font_css=css)
        else:
            svg = artwork.full_svg(spec, dl, assets, with_dieline=True,
                                   labels=labels, font_css=css)
        self._send(200, svg, "image/svg+xml; charset=utf-8")

    def _textures(self, project):
        dl = project.get("dieline")
        if not dl:
            return self._error("No structure yet", 409)
        spec = project.get("artwork") or {"panels": {}}
        # Textures are loaded as <img>, which cannot resolve external
        # references, so these do need data URIs -- but only for the images
        # the design actually uses.
        assets = store.asset_map(project, spec, as_data=True)
        css = fonts.embed_css(spec)
        out = {}
        for geo in dl["panels"]:
            if not geo.get("printable", True):
                continue
            svg = artwork.render_panel_svg(spec, geo, assets, scale=10,
                                           font_css=fonts.embed_css(spec, [geo["id"]]))
            encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            w, h = artwork.design_size(geo)
            out[geo["id"]] = {
                "url": f"data:image/svg+xml;base64,{encoded}",
                "w": w, "h": h, "label": geo["label"],
            }
        self._json({"textures": out, "dims": dl["dims"], "box_type": dl["box_type"]})

    # -- POST ---------------------------------------------------------------

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.strip("/").split("/") if p]

        if parsed.path == "/api/settings":
            body = self._body()
            # An empty string is a deliberate "remove this key", so it is
            # passed through rather than filtered out.
            updates = {k: (body[k] or "").strip() for k in SETTING_KEYS if k in body}
            config.write_env(updates)
            return self._json({"ok": True, "config": config.status()})

        if parsed.path == "/api/settings/probe":
            # Runs on the user's machine, which is the only place with a direct
            # route to the image provider.
            try:
                result = imagegen.probe()
            except Exception as exc:
                result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
            result["config"] = config.status()
            return self._json(result)

        if parsed.path == "/api/projects":
            body = self._body()
            project = store.create(body.get("name") or "Untitled project",
                                   body.get("agent") or "packaging_designer")
            return self._json({"project": _public(project)})

        if len(parts) >= 4 and parts[:2] == ["api", "projects"]:
            project_id, action = parts[2], parts[3]
            project = store.load(project_id)
            if not project:
                return self._error("Project not found", 404)

            if action == "chat":
                return self._chat(project)
            if action == "assets":
                return self._upload(project)
            if action == "structure":
                return self._structure(project)
            if action == "layers":
                return self._layers(project)
            if action == "export":
                return self._export(project)
            if action == "photoshoot":
                return self._photoshoot(project)
            if action == "render_result":
                body = self._body()
                data = body.get("png") or ""
                if "," in data:
                    data = data.split(",", 1)[1]
                try:
                    png = base64.b64decode(data)
                except Exception:
                    png = b""
                ok = bool(png) and visual.deliver(body.get("request_id", ""), png,
                                                  body.get("media_type") or "image/png")
                return self._json({"ok": ok})
            if action == "clear":
                project["messages"] = []
                store.save(project)
                return self._json({"ok": True})
            if action == "rename":
                body = self._body()
                project["name"] = (body.get("name") or project["name"])[:80]
                store.save(project)
                return self._json({"ok": True})

        self._error("Unknown path", 404)

    def _upload(self, project):
        body = self._body()
        added = []
        for item in body.get("files") or []:
            data_url = item.get("data") or ""
            if "," in data_url:
                data_url = data_url.split(",", 1)[1]
            try:
                raw = base64.b64decode(data_url)
            except Exception:
                continue
            try:
                added.append(store.add_asset(project, item.get("name") or "image.png",
                                             raw, item.get("role") or "reference"))
            except ValueError as exc:
                return self._error(str(exc), 413)
        store.save(project)
        self._json({"ok": True, "assets": added})

    def _photoshoot(self, project):
        body = self._body()
        render = body.get("render") or ""
        if "," in render:
            render = render.split(",", 1)[1]
        try:
            png = base64.b64decode(render)
        except Exception:
            png = b""
        if len(png) < 1000:
            return self._error("No usable render was supplied. Open the 3D tab "
                               "first so there is something to photograph.", 400)
        front = body.get("front") or ""
        if "," in front:
            front = front.split(",", 1)[1]
        try:
            front_png = base64.b64decode(front) if front else None
        except Exception:
            front_png = None
        try:
            asset = imagegen.photoshoot(
                project, png,
                scene=body.get("scene") or "studio",
                notes=body.get("notes") or "",
                aspect=body.get("aspect") or "1:1",
                front_png=front_png if front_png and len(front_png) > 1000 else None,
            )
        except RuntimeError as exc:
            return self._error(str(exc), 502)
        store.save(project)
        self._json({"ok": True, "asset": asset,
                    "url": f"/api/projects/{project['id']}/assets/{asset['id']}"})

    def _structure(self, project):
        body = self._body()
        project["dieline"] = dieline.build(
            body.get("box_type", "ste"),
            body.get("width_mm", 50),
            body.get("depth_mm", 50),
            body.get("height_mm", 100),
            body.get("bleed_mm", 3),
        )
        if body.get("reset_artwork"):
            project["artwork"] = None
        store.save(project)
        self._json({"ok": True, "dieline": project["dieline"]})

    def _layers(self, project):
        body = self._body()
        spec = project.get("artwork")
        if not spec:
            return self._error("No artwork yet", 409)
        result = artwork.apply_patch(spec, body.get("patches") or [])
        project["artwork"] = result["spec"]
        store.save(project)
        self._json({"ok": True, "applied": result["applied"],
                    "layers": artwork.layer_index(project["artwork"])})

    def _export(self, project):
        dl = project.get("dieline")
        if not dl:
            return self._error("No structure yet", 409)
        spec = project.get("artwork") or {"panels": {}}
        assets = store.asset_map(project, spec, as_data=True)
        css = fonts.embed_css(spec)
        base = re.sub(r"[^A-Za-z0-9]+", "-",
                      (project.get("brief", {}).get("product_name")
                       or project.get("name", "package"))).strip("-").lower()[:40]
        dims = dl["dims"]
        suffix = f'{dims["width"]:.0f}x{dims["depth"]:.0f}x{dims["height"]:.0f}'

        store.write_export(project["id"], f"{base}-dieline-{suffix}.svg",
                           dieline.to_svg(dl))
        store.write_export(project["id"], f"{base}-dieline-{suffix}.pdf",
                           pdfwriter.dieline_pdf(dl))
        store.write_export(project["id"], f"{base}-artwork-{suffix}.svg",
                           artwork.full_svg(spec, dl, assets, with_dieline=False,
                                            font_css=css))
        store.write_export(project["id"], f"{base}-artwork-with-dieline-{suffix}.svg",
                           artwork.full_svg(spec, dl, assets, with_dieline=True,
                                            font_css=css))
        store.write_export(project["id"], f"{base}-spec-sheet.pdf",
                           pdfwriter.spec_sheet_pdf(project))
        for geo in dl["panels"]:
            if geo.get("printable", True):
                store.write_export(
                    project["id"], f"{base}-panel-{geo['id']}.svg",
                    artwork.render_panel_svg(spec, geo, assets,
                                             font_css=fonts.embed_css(spec, [geo["id"]])))
        store.write_export(project["id"], "READ-ME-FIRST.txt",
                           _export_readme(project, dl))
        store.save(project)
        self._json({"ok": True, "files": store.list_exports(project["id"])})

    # -- chat, streamed as server-sent events -------------------------------

    def _chat(self, project):
        # One conversation turn per project at a time. Two overlapping runs
        # would interleave their messages and corrupt the history.
        with _RUN_LOCK:
            if project["id"] in _RUNNING:
                return self._error("The designer is still working on this project. "
                                   "Wait for it to finish.", 409)
            _RUNNING.add(project["id"])
        try:
            return self._chat_inner(project)
        finally:
            with _RUN_LOCK:
                _RUNNING.discard(project["id"])

    def _chat_inner(self, project):
        body = self._body()
        text = (body.get("text") or "").strip()
        asset_ids = body.get("asset_ids") or []
        if not text and not asset_ids:
            return self._error("Empty message", 400)

        blocks = []
        for asset_id in asset_ids:
            asset = next((a for a in project.get("assets", [])
                          if a["id"] == asset_id), None)
            if not asset:
                continue
            block = store.asset_for_claude(project["id"], asset)
            if block:
                blocks.append(block)
                blocks.append({
                    "type": "text",
                    "text": f'[uploaded image "{asset["name"]}", referenced in '
                            f'artwork as src "asset:{asset["id"]}"]',
                })
        if text:
            blocks.append({"type": "text", "text": text})

        agent = registry.get(project.get("agent"))
        events: "queue.Queue" = queue.Queue()

        def work():
            try:
                agent.run(project, blocks, events.put)
            except Exception as exc:
                traceback.print_exc()
                events.put({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
                events.put({"type": "done", "text": ""})
            finally:
                events.put(None)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # The stream has no length, so it must end by closing the connection.
        # Announcing keep-alive here would leave the browser waiting forever.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        threading.Thread(target=work, daemon=True).start()

        while True:
            try:
                event = events.get(timeout=20)
            except queue.Empty:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                continue
            if event is None:
                break
            try:
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
        try:
            self.wfile.write(b'data: {"type":"end"}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _public(project: dict) -> dict:
    """The project without the raw API blocks the interface has no use for."""
    out = {k: v for k, v in project.items() if k != "messages"}
    out["messages"] = [
        {"role": m["role"], "text": m.get("display", ""),
         "tools": m.get("tools") or [], "cost": m.get("cost")}
        for m in project.get("messages", [])
        if not m.get("hidden") and m.get("display")
    ]
    if project.get("artwork"):
        out["layers"] = artwork.layer_index(project["artwork"])
    return out


def _export_readme(project, dl) -> str:
    dims = dl["dims"]
    fonts_used = (project.get("artwork") or {}).get("fonts") or {}
    names = [artwork.FONT_CATALOG.get(v, (v,))[0] for v in fonts_used.values()]
    usage = project.get("usage") or {}
    return f"""PACKAGING STUDIO - EXPORT
=========================

Project:    {project.get('name')}
Structure:  {dl['box_name']} ({dl['box_short']})
Inside:     W {dims['width']:.0f} x D {dims['depth']:.0f} x H {dims['height']:.0f} mm
Flat sheet: {dl['sheet']['width']:.0f} x {dl['sheet']['height']:.0f} mm
Material:   {dl['material_hint']}
API cost:   ${usage.get('cost_usd', 0):.4f} across {usage.get('runs', 0)} requests

FILES
-----
*-dieline-*.svg / .pdf
    The cutting contour on its own, at 1:1.
    Solid red  = cut line.
    Dashed blue = crease line (fold).
    This is what you send to the box maker.

*-artwork-*.svg
    The print image on the flat layout, without cut lines.
    Opens in Adobe Illustrator, Affinity Designer and Inkscape with every
    element still editable.

*-artwork-with-dieline-*.svg
    Both on top of each other. Good for checking, not for printing.

*-panel-*.svg
    Each face of the box on its own.

*-spec-sheet.pdf
    Technical spec sheet for the printer and the box maker.

CHECK BEFORE PRINTING
---------------------
1. Fonts: this file uses {', '.join(names) or 'system fonts'}.
   They are all free via Google Fonts and are embedded in the SVG. If your
   printer wants certainty, convert text to outlines in your design app
   (Type > Create Outlines) before handing the file over.
2. Colour: the file is RGB. Your printer will convert to CMYK, or you can.
   Expect slight shifts, especially in strong blues and oranges.
3. Bleed: {dl['bleed']:.0f} mm is allowed for. Background areas must run past
   the cut line.
4. Material thickness: on corrugated, thickness is added outside the inside
   dimension.
5. Have the mandatory information checked. For cosmetics in Ethiopia that
   means the EFDA rules. The copy here is a design proposal, not legal advice.

Made with Packaging Studio, running on your own machine.
"""


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def main():
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    upgraded = config.migrate()
    port = _free_port(config.get_int("PORT"))
    url = f"http://localhost:{port}"
    status = config.status()

    print("\n  PACKAGING STUDIO")
    print("  " + "-" * 46)
    print(f"  Running at:       {url}")
    print(f"  Model:            {status['model_label']}")
    print(f"  Anthropic key:    {'set' if status['anthropic'] else 'MISSING'}")
    print(f"  Image generation: {status['image_provider']}")
    print(f"  Projects in:      {config.PROJECTS_DIR}")
    if not status["anthropic"]:
        print("\n  No API key yet. Add one under Settings, top right.")
    if upgraded:
        print(f"\n  Model upgraded to {upgraded}.")
    print("\n  Press Ctrl+C to stop.\n")

    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        httpd.server_close()


if __name__ == "__main__":
    main()
