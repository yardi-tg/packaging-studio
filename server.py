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
import os
import queue
import re
import socket
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if "--hosted" in sys.argv:
    # Try the website build locally: projects in the browser, keys from the
    # environment or .env, and an access code (ACCESS_CODES, default 6767 here).
    os.environ["PACKAGING_HOSTED"] = "1"
    os.environ.setdefault("ACCESS_CODES", "6767")

from core import artwork, config, dieline, fonts, imagegen, outputs, registry, store, visual

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

    def _hosted_call(self) -> bool:
        """Website mode: the API is core/hosted.py, as on Vercel."""
        if not config.HOSTED:
            return False
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return False
        from core import hosted
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        reply = hosted.handle(self.command, parsed.path, parsed.query, headers, body)
        self.send_response(reply.status)
        for key, value in reply.header_list():
            self.send_header(key, value)
        if reply.stream is None:
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            try:
                self.wfile.write(reply.body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return True
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in reply.stream:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def do_GET(self):
        try:
            if self._hosted_call():
                return
            self._route_get()
        except Exception as exc:
            traceback.print_exc()
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        try:
            if self._hosted_call():
                return
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
        mode = (query.get("mode") or ["flat"])[0]
        spec = project.get("artwork") or {"panels": {}}
        # The in-browser preview gets plain URLs: inlining every image as
        # base64 makes the document enormous and the browser crawl. Anything
        # that leaves the browser gets real data URIs so it stays standalone.
        standalone = mode in ("print", "panel")
        assets = store.asset_map(project, spec, as_data=standalone)
        try:
            svg = outputs.render(project, mode, (query.get("panel") or [""])[0],
                                 (query.get("labels") or ["0"])[0] == "1", assets)
        except ValueError as exc:
            return self._error(str(exc), 409)
        except LookupError as exc:
            return self._error(str(exc), 404)
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
        out = {}
        for face in outputs.texture_panels(project):
            svg = outputs.render(project, "panel", face["id"], assets=assets, scale=10)
            encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            out[face["id"]] = {"url": f"data:image/svg+xml;base64,{encoded}",
                               "w": face["w"], "h": face["h"], "label": face["label"]}
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
        try:
            plan = outputs.export_plan(project)
        except ValueError as exc:
            return self._error(str(exc), 409)
        spec = project.get("artwork") or {"panels": {}}
        assets = store.asset_map(project, spec, as_data=True)
        for name, _mime, make in plan:
            store.write_export(project["id"], name, make(assets))
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

        blocks = store.message_blocks(project, text, asset_ids)

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
            if event.get("type") in ("checkpoint", "suspend"):
                continue    # website bookkeeping; the project is on disk here
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


_public = outputs.public


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
