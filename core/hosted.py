"""The website: the same studio, served from a stateless function.

On a host such as Vercel nothing survives between requests, and one request
may only run for a few minutes. So the work is split:

  the browser   keeps every project (IndexedDB), draws the previews the
                designer asks to see, and carries the conversation from one
                request to the next (web/hosted.js)
  this module   computes: SVG views, dielines, production files, photos, and
                the designer itself, for as long as one request allows

Each request carries the project; the answer carries back what changed. When
the designer wants to look at its work, or a request is running out of time,
the run pauses: the browser renders the panel (or just calls again) and the
next request picks the run up exactly where it stopped.

Access needs a numeric code (ACCESS_CODES, comma-separated). The API keys stay
in the server's environment and never reach the browser.

Everything here is plain functions over (method, path, headers, body), so the
same code runs under Vercel's Python runtime (api/index.py) and under
`python3 server.py --hosted` for trying it locally.
"""

from __future__ import annotations
import base64
import binascii
import hmac
import json
import queue
import threading
import time
import traceback
import urllib.parse
import zlib

from . import artwork, config, dieline, imagegen, outputs, registry, store

MAX_BODY = 40 * 1024 * 1024        # after decompression
EXAMPLE = config.ROOT / "projects" / "example-nigella-glow-serum" / "project.json"

# One request may run this long (Vercel's maxDuration is 300 s; keep a margin
# to send the last checkpoint). A new model call only starts in the first
# START_WINDOW seconds, so it has most of the request ahead of it.
REQUEST_SECONDS = 280.0
START_WINDOW = 100.0

# Wrong codes: after this many from one address in WINDOW seconds, stop
# listening to that address for a while. A four-digit code is short; this
# keeps guessing slow.
ATTEMPT_LIMIT = 12
ATTEMPT_WINDOW = 900
_ATTEMPTS: dict = {}
_ATTEMPT_LOCK = threading.Lock()


class Reply:
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8",
                 headers=None, stream=None):
        self.status = status
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.content_type = content_type
        self.headers = headers or {}
        self.stream = stream

    def header_list(self) -> list:
        out = [("Content-Type", self.content_type), ("Cache-Control", "no-store")]
        out.extend(self.headers.items())
        return out


class _BadRequest(Exception):
    pass


def _json(data, status=200) -> Reply:
    return Reply(status, json.dumps(data, ensure_ascii=False))


def _error(message, status=400) -> Reply:
    return _json({"error": message}, status)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def handle(method: str, path: str, query: str, headers: dict, body: bytes) -> Reply:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    store.use_memory({})
    try:
        route = _route_of(path, query)
        if route == "status":
            return _status(headers)
        if not route.startswith("h/"):
            return _error("Unknown path", 404)
        if _check_code(headers) != "ok":
            return _json({"error": "locked"}, 401)
        handler = _ROUTES.get(route[2:])
        if not handler:
            return _error("Unknown path", 404)
        return handler(_decode(headers, body))
    except _BadRequest as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        traceback.print_exc()
        return _error(f"{type(exc).__name__}: {exc}", 500)


def _route_of(path: str, query: str) -> str:
    path = urllib.parse.unquote(path or "/")
    # Behind a rewrite the function may see its own path; the original route
    # then travels in the query string.
    if path.rstrip("/") in ("/api", "/api/index", "/api/index.py"):
        route = (urllib.parse.parse_qs(query or "").get("route") or [""])[0]
        path = "/api/" + route.lstrip("/")
    if not path.startswith("/api/"):
        return ""
    return path[len("/api/"):].strip("/")


def _decode(headers: dict, body: bytes) -> dict:
    if headers.get("x-body-encoding") == "gzip":
        try:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = inflater.decompress(body, MAX_BODY + 1)
        except zlib.error as exc:
            raise _BadRequest("Could not unpack the request") from exc
        if len(body) > MAX_BODY or inflater.unconsumed_tail:
            raise _BadRequest("Request too large")
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BadRequest("The request was not valid JSON") from exc
    if not isinstance(data, dict):
        raise _BadRequest("Expected a JSON object")
    return data


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------

def _codes() -> list:
    return [c.strip() for c in (config.get("ACCESS_CODES") or "").split(",") if c.strip()]


def _client(headers: dict) -> str:
    raw = headers.get("x-forwarded-for") or headers.get("x-real-ip") or "local"
    return raw.split(",")[0].strip()


def _check_code(headers: dict) -> str:
    """ok, missing, wrong, blocked or unconfigured."""
    codes = _codes()
    if not codes:
        return "unconfigured"
    given = (headers.get("x-access-code") or "").strip()
    if not given:
        return "missing"
    who, now = _client(headers), time.time()
    with _ATTEMPT_LOCK:
        recent = [t for t in _ATTEMPTS.get(who, []) if now - t < ATTEMPT_WINDOW]
        _ATTEMPTS[who] = recent
        if len(recent) >= ATTEMPT_LIMIT:
            return "blocked"
    if any(hmac.compare_digest(given.encode(), code.encode()) for code in codes):
        return "ok"
    with _ATTEMPT_LOCK:
        _ATTEMPTS.setdefault(who, []).append(now)
    time.sleep(1.0)
    return "wrong"


def _status(headers: dict) -> Reply:
    verdict = _check_code(headers)
    if verdict != "ok":
        return _json({"hosted": True, "locked": True, "message": {
            "missing": "",
            "wrong": "That code is not right.",
            "blocked": "Too many wrong codes. Try again in a few minutes.",
            "unconfigured": "This site has no access code yet. Its owner sets "
                            "ACCESS_CODES in the hosting settings.",
        }[verdict]})
    model = config.hosted_default_model()
    return _json({
        "hosted": True, "locked": False,
        "config": {
            "anthropic": bool(config.get("ANTHROPIC_API_KEY")),
            "model": model,
            "model_label": config.pricing_for(model)["label"],
            "effort": config.hosted_default_effort(),
            "image_provider": config.image_provider(),
            "pricing": config.pricing_for(model),
        },
        "agents": registry.catalog(),
        "box_types": dieline.BOX_TYPES,
        "models": config.hosted_models(),
        "efforts": list(config.hosted_efforts()),
        "pricing": config.MODEL_PRICING,
        "scenes": imagegen.scene_catalog(),
        "fonts": {k: {"name": v[0], "hint": v[2]} for k, v in artwork.FONT_CATALOG.items()},
    })


# ---------------------------------------------------------------------------
# request contents
# ---------------------------------------------------------------------------

def _project(payload: dict) -> dict:
    project = payload.get("project")
    if not isinstance(project, dict):
        raise _BadRequest("No project in the request")
    project.setdefault("id", "project")
    project.setdefault("name", "Untitled project")
    for key in ("assets", "messages"):
        if not isinstance(project.get(key), list):
            project[key] = []
    return project


def _b64(value) -> bytes:
    if not isinstance(value, str) or not value:
        return b""
    if "," in value[:100]:
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value)
    except (binascii.Error, ValueError):
        return b""


def _files(payload: dict) -> dict:
    """The images sent along: asset id -> (bytes, mime)."""
    out = {}
    for item in payload.get("assets") or []:
        if isinstance(item, dict) and item.get("id"):
            data = _b64(item.get("data"))
            if data:
                out[str(item["id"])] = (data, item.get("mime") or "image/png")
    return out


def _asset_map(project: dict) -> dict:
    return store.asset_map(project, project.get("artwork") or {"panels": {}}, as_data=True)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def _r_render(payload: dict) -> Reply:
    project = _project(payload)
    store.use_memory(_files(payload))
    try:
        svg = outputs.render(project, payload.get("mode") or "flat", payload.get("panel"),
                             bool(payload.get("labels")), _asset_map(project),
                             payload.get("scale"))
    except ValueError as exc:
        return _error(str(exc), 409)
    except LookupError as exc:
        return _error(str(exc), 404)
    return Reply(200, svg, "image/svg+xml; charset=utf-8")


def _r_structure(payload: dict) -> Reply:
    dl = dieline.build(payload.get("box_type", "ste"), payload.get("width_mm", 50),
                       payload.get("depth_mm", 50), payload.get("height_mm", 100),
                       payload.get("bleed_mm", 3))
    return _json({"ok": True, "dieline": dl})


def _r_layers(payload: dict) -> Reply:
    spec = payload.get("artwork")
    if not isinstance(spec, dict):
        return _error("No artwork yet", 409)
    result = artwork.apply_patch(spec, payload.get("patches") or [])
    return _json({"ok": True, "spec": result["spec"], "applied": result["applied"],
                  "not_found": result.get("not_found") or [],
                  "layers": artwork.layer_index(result["spec"])})


def _r_export(payload: dict) -> Reply:
    """Without "file": the list of production files. With it: that one file,
    as raw bytes. One file per request keeps every answer well under the
    host's size limit, however many fonts the design embeds."""
    project = _project(payload)
    store.use_memory(_files(payload))
    try:
        plan = outputs.export_plan(project)
    except ValueError as exc:
        return _error(str(exc), 409)
    wanted = payload.get("file")
    if not wanted:
        return _json({"ok": True, "files": [{"name": store.safe_name(name), "mime": mime}
                                            for name, mime, _ in plan]})
    for name, mime, make in plan:
        if store.safe_name(name) == wanted:
            return Reply(200, make(_asset_map(project)), mime)
    return _error("Unknown file", 404)


def _r_photoshoot(payload: dict) -> Reply:
    project = _project(payload)
    mem = store.use_memory({})
    render = _b64(payload.get("render"))
    if len(render) < 1000:
        return _error("No usable render was supplied. Open the 3D tab first so there "
                      "is something to photograph.", 400)
    front = _b64(payload.get("front"))
    options = dict(scene=payload.get("scene") or "studio", notes=payload.get("notes") or "",
                   aspect=payload.get("aspect") or "1:1",
                   front_png=front if len(front) > 1000 else None)
    try:
        asset = imagegen.photoshoot(project, render, **options)
    except RuntimeError as exc:
        # The configured image model may not exist on this key. Look for the
        # best one it can reach, remember it, and try once more.
        if not ("404" in str(exc) or "not found" in str(exc).lower()):
            return _error(str(exc), 502)
        probe = imagegen.probe()
        if not probe.get("ok"):
            return _error(f"{exc} ({probe.get('message', '')})", 502)
        try:
            asset = imagegen.photoshoot(project, render, **options)
        except RuntimeError as again:
            return _error(str(again), 502)
    data, mime = mem.files[asset["id"]]
    return Reply(200, data, mime,
                 {"X-Asset": urllib.parse.quote(json.dumps(asset, ensure_ascii=False))})


def _r_probe(payload: dict) -> Reply:
    try:
        result = imagegen.probe()
    except Exception as exc:
        result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    result["image_provider"] = config.image_provider()
    return _json(result)


def _r_example(payload: dict) -> Reply:
    try:
        return _json({"project": json.loads(EXAMPLE.read_text(encoding="utf-8"))})
    except (OSError, ValueError):
        return _error("No example bundled", 404)


# -- the designer -------------------------------------------------------------

class Clock:
    """How much of this request is left.

    A model call starts only while most of the request is still ahead. A call
    that would still overrun is stopped before the host cuts the request off,
    and the browser carries on with a fresh one."""

    def __init__(self, seconds: float = REQUEST_SECONDS, start_window: float = START_WINDOW):
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.start_window = start_window
        self.call_started = None

    def can_start(self) -> bool:
        now = time.monotonic()
        if now - self.started > self.start_window:
            return False
        self.call_started = now
        return True

    def had_full_time(self) -> bool:
        """Did the call that just ran out of time start with the whole request
        ahead of it? Then a retry would not help."""
        return self.call_started is not None and self.call_started - self.started < 20


class _Tracker:
    """Works out what changed since the browser's copy, so each checkpoint
    carries only the new messages and not the whole history back."""

    def __init__(self, project: dict):
        messages = project.get("messages") or []
        self.sent = len(messages)
        self.flags = [self._flags(m) for m in messages]

    @staticmethod
    def _flags(message: dict):
        return (bool(message.get("archived")), message.get("cost"))

    def checkpoint(self, project: dict) -> dict:
        messages = project.get("messages") or []
        changed = []
        for i in range(min(self.sent, len(messages))):
            now = self._flags(messages[i])
            if now != self.flags[i]:
                changed.append({"i": i, "archived": now[0], "cost": now[1]})
                self.flags[i] = now
        start, append = self.sent, messages[self.sent:]
        self.flags.extend(self._flags(m) for m in append)
        self.sent = len(messages)
        return {"type": "checkpoint", "from": start, "append": append, "flags": changed,
                "fields": {k: v for k, v in project.items() if k != "messages"},
                "files": store.take_new_files()}


def _model_choice(payload: dict) -> tuple:
    models = [m["id"] for m in config.hosted_models()]
    model = payload.get("model") if payload.get("model") in models else config.hosted_default_model()
    effort = payload.get("effort")
    if effort not in config.hosted_efforts():
        effort = config.hosted_default_effort()
    return model, effort


def _r_chat(payload: dict) -> Reply:
    project = _project(payload)
    if not config.get("ANTHROPIC_API_KEY"):
        return _error("This site has no Anthropic API key yet. Its owner adds "
                      "ANTHROPIC_API_KEY in the hosting settings.", 503)
    model, effort = _model_choice(payload)
    files = {}
    for item in payload.get("attachments") or []:
        if isinstance(item, dict) and item.get("id"):
            data = _b64(item.get("data"))
            if data:
                files[str(item["id"])] = (data, item.get("mime") or "image/png")
    resume = payload.get("resume")
    if resume is not None and not isinstance(resume, dict):
        resume = {}
    text = (payload.get("text") or "").strip()
    asset_ids = [a for a in payload.get("asset_ids") or [] if isinstance(a, str)]
    if resume is None and not text and not asset_ids:
        return _error("Empty message", 400)
    agent = registry.get(project.get("agent"))

    def work(emit_raw):
        store.use_memory(files)
        tracker = _Tracker(project)

        def emit(event):
            if event.get("type") == "checkpoint":
                event = tracker.checkpoint(project)
            emit_raw(event)

        blocks = store.message_blocks(project, text, asset_ids) if resume is None else []
        agent.run(project, blocks, emit, model=model, effort=effort,
                  resume=resume, clock=Clock())

    return Reply(200, content_type="text/event-stream; charset=utf-8",
                 headers={"X-Accel-Buffering": "no"}, stream=_event_stream(work))


def _event_stream(work):
    """Server-sent events from a worker thread, with keep-alives so proxies
    never take a quiet stretch for a dead connection."""
    events: "queue.Queue" = queue.Queue()

    def run():
        try:
            work(events.put)
        except Exception as exc:
            traceback.print_exc()
            # No checkpoint here: the browser keeps its last consistent copy.
            events.put({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            events.put({"type": "done", "text": ""})
        finally:
            events.put(None)

    threading.Thread(target=run, daemon=True).start()
    yield b": open\n\n"
    while True:
        try:
            event = events.get(timeout=15)
        except queue.Empty:
            yield b": keepalive\n\n"
            continue
        if event is None:
            break
        yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
    yield b'data: {"type":"end"}\n\n'


_ROUTES = {
    "render": _r_render,
    "structure": _r_structure,
    "layers": _r_layers,
    "export": _r_export,
    "photoshoot": _r_photoshoot,
    "probe": _r_probe,
    "example": _r_example,
    "chat": _r_chat,
}
