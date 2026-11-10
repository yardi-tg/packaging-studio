"""Letting the agent see its own work.

The server cannot rasterise SVG with the standard library alone, but the
browser showing the studio can. So when the agent asks to look at a panel, the
server sends the browser a render request over the live chat stream, the
browser draws the panel into a canvas and posts the picture back, and the
waiting tool call hands that picture to Claude.

If no browser answers -- the tab was closed, say -- the server tries the
machine's own renderer instead: QuickLook on macOS, rsvg-convert where it is
installed. Failing both, the agent carries on with the numeric layout check.
"""

from __future__ import annotations
import pathlib
import shutil
import subprocess
import tempfile
import threading
import uuid

_PENDING: dict = {}
_LOCK = threading.Lock()


def request() -> str:
    request_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _PENDING[request_id] = {"event": threading.Event(), "picture": None}
    return request_id


def deliver(request_id: str, data: bytes, media_type: str = "image/png") -> bool:
    with _LOCK:
        slot = _PENDING.get(request_id)
    if not slot or not data:
        return False
    if media_type not in ("image/png", "image/jpeg", "image/webp"):
        media_type = "image/png"
    slot["picture"] = (data, media_type)
    slot["event"].set()
    return True


def wait(request_id: str, timeout: float = 35.0):
    """Block until the browser answers. Returns (bytes, media_type) or None."""
    with _LOCK:
        slot = _PENDING.get(request_id)
    if not slot:
        return None
    slot["event"].wait(timeout)
    with _LOCK:
        _PENDING.pop(request_id, None)
    return slot["picture"]


def render_locally(project: dict, panel: str, width: int = 1400):
    """Rasterise without a browser, if this machine has something that can."""
    from . import artwork, fonts, store

    dl = project.get("dieline")
    spec = project.get("artwork")
    if not dl or not spec:
        return None
    assets = store.asset_map(project, spec, as_data=True)
    css = fonts.embed_css(spec)
    if panel == "all":
        svg = artwork.full_svg(spec, dl, assets, with_dieline=False, font_css=css)
    else:
        geo = next((p for p in dl["panels"] if p["id"] == panel), None)
        if not geo:
            return None
        svg = artwork.render_panel_svg(spec, geo, assets, scale=12,
                                       font_css=fonts.embed_css(spec, [panel]))

    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "design.svg"
        src.write_text(svg, encoding="utf-8")
        out = pathlib.Path(tmp) / "design.png"

        if shutil.which("rsvg-convert"):
            try:
                subprocess.run(["rsvg-convert", "-w", str(width), "-o", str(out), str(src)],
                               check=True, timeout=30, capture_output=True)
            except (subprocess.SubprocessError, OSError):
                pass

        if not out.exists() and shutil.which("qlmanage"):
            try:
                subprocess.run(["qlmanage", "-t", "-s", str(width), "-o", tmp, str(src)],
                               check=False, timeout=30, capture_output=True)
                made = pathlib.Path(tmp) / "design.svg.png"
                if made.exists():
                    made.rename(out)
            except (subprocess.SubprocessError, OSError):
                pass

        if out.exists() and out.stat().st_size > 1000:
            return out.read_bytes(), "image/png"
    return None
