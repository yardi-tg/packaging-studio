"""Project storage. Everything lives as plain files on your machine.

projects/<id>/project.json   brief, dieline, artwork spec, chat history, usage
projects/<id>/assets/        uploaded reference images, logos, generated art
projects/<id>/exports/       generated SVG, PDF and PNG files
"""

from __future__ import annotations
import base64
import datetime
import json
import mimetypes
import re
import shutil
import uuid

from . import config

MAX_ASSET_BYTES = 12 * 1024 * 1024


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40] or "project"


def _dir(project_id: str):
    return config.PROJECTS_DIR / project_id


def list_projects() -> list:
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in config.PROJECTS_DIR.iterdir():
        meta = path / "project.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": data.get("id", path.name),
            "name": data.get("name", path.name),
            "agent": data.get("agent", "packaging_designer"),
            "updated": data.get("updated", ""),
            "has_artwork": bool(data.get("artwork")),
            "box_type": (data.get("dieline") or {}).get("box_type"),
            "product": (data.get("brief") or {}).get("product_name"),
            "cost_usd": (data.get("usage") or {}).get("cost_usd", 0),
        })
    out.sort(key=lambda p: p["updated"], reverse=True)
    return out


def create(name: str, agent: str = "packaging_designer") -> dict:
    project_id = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
    path = _dir(project_id)
    (path / "assets").mkdir(parents=True, exist_ok=True)
    (path / "exports").mkdir(parents=True, exist_ok=True)
    project = {
        "id": project_id,
        "name": name or "Untitled project",
        "agent": agent,
        "created": _now(),
        "updated": _now(),
        "brief": {},
        "dieline": None,
        "artwork": None,
        "assets": [],
        "messages": [],
        "copy": {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                  "cost_usd": 0.0, "runs": 0},
        "log": [],
    }
    save(project)
    return project


def load(project_id: str):
    meta = _dir(project_id) / "project.json"
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save(project: dict) -> dict:
    project["updated"] = _now()
    path = _dir(project["id"])
    path.mkdir(parents=True, exist_ok=True)
    (path / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return project


def delete(project_id: str) -> bool:
    path = _dir(project_id)
    if path.exists():
        shutil.rmtree(path)
        return True
    return False


def add_asset(project: dict, filename: str, data: bytes,
              role: str = "reference") -> dict:
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError("File too large. 12 MB maximum per image.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "image")[:60] or "image"
    asset_id = f"{uuid.uuid4().hex[:8]}_{safe}"
    path = _dir(project["id"]) / "assets" / asset_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    asset = {
        "id": asset_id,
        "name": filename,
        "role": role,
        "mime": mimetypes.guess_type(safe)[0] or "image/png",
        "bytes": len(data),
        "added": _now(),
    }
    project.setdefault("assets", []).append(asset)
    return asset


def asset_path(project_id: str, asset_id: str):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", asset_id or "")
    path = _dir(project_id) / "assets" / safe
    return path if path.exists() else None


def asset_data_url(project_id: str, asset_id: str, mime: str = "image/png") -> str:
    path = asset_path(project_id, asset_id)
    if not path:
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _referenced_sources(spec: dict) -> set:
    """Which asset references does this artwork actually use?

    Inlining every uploaded image as base64 makes the preview enormous and the
    browser crawl. So we only ever embed what the design really points at.
    """
    used = set()
    for panel in ((spec or {}).get("panels") or {}).values():
        for layer in panel.get("layers") or []:
            src = layer.get("src")
            if isinstance(src, str) and src:
                used.add(src)
        bg = panel.get("background")
        if isinstance(bg, dict) and isinstance(bg.get("src"), str):
            used.add(bg["src"])
    return used


def asset_map(project: dict, spec: dict = None, as_data: bool = True) -> dict:
    """Map 'asset:<id>' and 'ref:<n>' onto something an <image> can load.

    as_data=True  -> base64 data URLs. Needed when the SVG is loaded as an
                     image (3D textures) or handed to someone else (exports),
                     because an external reference would not resolve there.
    as_data=False -> plain URLs back into this server. Much lighter, and what
                     the in-browser preview should use.
    """
    wanted = _referenced_sources(spec) if spec is not None else None
    out = {}
    for index, asset in enumerate(project.get("assets", [])):
        keys = [f"asset:{asset['id']}", f"ref:{index}", asset["id"]]
        if wanted is not None and not any(k in wanted for k in keys):
            continue
        if as_data:
            value = asset_data_url(project["id"], asset["id"],
                                   asset.get("mime", "image/png"))
        else:
            value = f"/api/projects/{project['id']}/assets/{asset['id']}"
        if not value:
            continue
        for key in keys:
            out[key] = value
    return out


def asset_for_claude(project_id: str, asset: dict):
    """An image block in the shape the Anthropic Messages API expects."""
    path = asset_path(project_id, asset["id"])
    if not path:
        return None
    mime = asset.get("mime", "image/png")
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        mime = "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def write_export(project_id: str, filename: str, data) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80]
    path = _dir(project_id) / "exports" / safe
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return safe


def export_path(project_id: str, filename: str):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "")
    path = _dir(project_id) / "exports" / safe
    return path if path.exists() else None


def list_exports(project_id: str) -> list:
    folder = _dir(project_id) / "exports"
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        if path.is_file():
            out.append({
                "name": path.name,
                "bytes": path.stat().st_size,
                "modified": datetime.datetime.fromtimestamp(
                    path.stat().st_mtime).isoformat(timespec="seconds"),
            })
    return out


def log(project: dict, kind: str, text: str):
    project.setdefault("log", []).append(
        {"at": _now(), "kind": kind, "text": str(text)[:600]}
    )
    project["log"] = project["log"][-200:]
