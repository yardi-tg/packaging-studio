"""Artwork renderer: turns a layer spec (JSON) into real vector SVG.

The important difference from a plain image generator: Claude describes the
design as structured layers in millimetres rather than painting pixels. That
buys you four things.

  * Text is real text, so it is always legible. Image models routinely
    mangle lettering on packaging.
  * Every layer can be edited on its own.
  * The output is print-ready vector art at actual size.
  * The same data supplies the textures for the 3D preview.

Layer coordinates are relative to the top-left corner of their panel, in
millimetres.
"""

from __future__ import annotations
import html
import math
import uuid

FONT_CATALOG = {
    "playfair": ("Playfair Display", "serif", "Elegant, premium, luxury beauty"),
    "cormorant": ("Cormorant Garamond", "serif", "Fine, apothecary, natural skincare"),
    "dmserif": ("DM Serif Display", "serif", "Modern classic, strong headlines"),
    "inter": ("Inter", "sans-serif", "Neutral, clean, highly legible"),
    "jost": ("Jost", "sans-serif", "Geometric, contemporary, clean beauty"),
    "worksans": ("Work Sans", "sans-serif", "Friendly, versatile"),
    "spacegrotesk": ("Space Grotesk", "sans-serif", "Technical, young, streetwear"),
    "bebas": ("Bebas Neue", "sans-serif", "Bold, condensed, all caps"),
    "archivo": ("Archivo Black", "sans-serif", "Very heavy, maximum presence"),
    "outfit": ("Outfit", "sans-serif", "Soft geometric, DTC look"),
    "lora": ("Lora", "serif", "Warm, narrative, body copy"),
    "ibmmono": ("IBM Plex Mono", "monospace", "Lab, actives, INCI lists"),
    "abril": ("Abril Fatface", "serif", "Decorative, retro, artisan"),
    "manrope": ("Manrope", "sans-serif", "Modern, balanced, body and headline"),
}

_GOOGLE_FAMILIES = [
    "Playfair+Display:ital,wght@0,400..900;1,400..900",
    "Cormorant+Garamond:ital,wght@0,300..700;1,300..700",
    "DM+Serif+Display:ital@0;1",
    "Inter:wght@100..900",
    "Jost:ital,wght@0,100..900;1,100..900",
    "Work+Sans:ital,wght@0,100..900;1,100..900",
    "Space+Grotesk:wght@300..700",
    "Bebas+Neue",
    "Archivo+Black",
    "Outfit:wght@100..900",
    "Lora:ital,wght@0,400..700;1,400..700",
    "IBM+Plex+Mono:ital,wght@0,100..700;1,100..700",
    "Abril+Fatface",
    "Manrope:wght@200..800",
]

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    + "&".join(f"family={f}" for f in _GOOGLE_FAMILIES)
    + "&display=swap');"
)

# Rough average character width as a fraction of font size, used for wrapping
_WIDTH_FACTOR = {
    "serif": 0.50,
    "sans-serif": 0.52,
    "monospace": 0.60,
}
_NARROW = {"Bebas Neue": 0.38, "Archivo Black": 0.62}


def font_stack(key: str) -> tuple:
    family, generic, _ = FONT_CATALOG.get(key, FONT_CATALOG["inter"])
    return family, generic


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _char_width(family: str, generic: str, size: float, weight) -> float:
    factor = _NARROW.get(family, _WIDTH_FACTOR.get(generic, 0.52))
    try:
        w = int(weight)
    except (TypeError, ValueError):
        w = 400
    factor *= 1.0 + max(0, w - 400) * 0.00018
    return size * factor


def wrap_text(text: str, max_width_mm: float, family: str, generic: str,
              size: float, weight, letter_spacing: float = 0.0) -> list:
    """Simple line wrapping based on estimated character widths."""
    if not text:
        return []
    cw = _char_width(family, generic, size, weight) + letter_spacing
    if cw <= 0 or max_width_mm <= 0:
        return str(text).split("\n")
    max_chars = max(1, int(max_width_mm / cw))
    lines = []
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            if len(current) + 1 + len(word) <= max_chars:
                current += " " + word
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Individual layer types
# ---------------------------------------------------------------------------

def _common_attrs(layer: dict) -> str:
    bits = []
    opacity = layer.get("opacity")
    if opacity is not None and float(opacity) < 1:
        bits.append(f'opacity="{float(opacity):.3f}"')
    rotate = float(layer.get("rotate") or 0)
    if rotate:
        cx = float(layer.get("x", 0)) + float(layer.get("w", 0)) / 2
        cy = float(layer.get("y", 0)) + float(layer.get("h", 0)) / 2
        bits.append(f'transform="rotate({rotate:.2f} {cx:.2f} {cy:.2f})"')
    if layer.get("blend"):
        bits.append(f'style="mix-blend-mode:{_esc(layer["blend"])}"')
    return " ".join(bits)


def _fill_attr(layer: dict, defs: list, key: str = "fill",
               default: str = "none") -> str:
    value = layer.get(key, default)
    if isinstance(value, dict):
        gid = "g" + uuid.uuid4().hex[:8]
        defs.append(_gradient_def(gid, value))
        return f'{key}="url(#{gid})"'
    return f'{key}="{_esc(value)}"'


def _gradient_def(gid: str, spec: dict) -> str:
    stops = spec.get("stops") or spec.get("colors") or ["#000000", "#ffffff"]
    normalized = []
    for i, stop in enumerate(stops):
        if isinstance(stop, dict):
            offset = float(stop.get("offset", i / max(1, len(stops) - 1)))
            color = stop.get("color", "#000000")
            op = stop.get("opacity", 1)
        else:
            offset = i / max(1, len(stops) - 1)
            color = stop
            op = 1
        normalized.append(
            f'<stop offset="{offset*100:.1f}%" stop-color="{_esc(color)}" '
            f'stop-opacity="{float(op):.3f}"/>'
        )
    joined = "".join(normalized)
    if spec.get("type") == "radial":
        cx = spec.get("cx", 50)
        cy = spec.get("cy", 50)
        r = spec.get("r", 70)
        return (f'<radialGradient id="{gid}" cx="{cx}%" cy="{cy}%" r="{r}%">'
                f'{joined}</radialGradient>')
    angle = float(spec.get("angle", 90))
    rad = math.radians(angle)
    x1 = 50 - math.cos(rad) * 50
    y1 = 50 - math.sin(rad) * 50
    x2 = 50 + math.cos(rad) * 50
    y2 = 50 + math.sin(rad) * 50
    return (f'<linearGradient id="{gid}" x1="{x1:.1f}%" y1="{y1:.1f}%" '
            f'x2="{x2:.1f}%" y2="{y2:.1f}%">{joined}</linearGradient>')


def _stroke_bits(layer: dict, defs: list) -> str:
    if not layer.get("stroke"):
        return ""
    bits = [_fill_attr(layer, defs, "stroke", "none")]
    bits.append(f'stroke-width="{float(layer.get("strokeWidth", 0.4)):.2f}"')
    if layer.get("dash"):
        bits.append(f'stroke-dasharray="{_esc(layer["dash"])}"')
    bits.append('stroke-linecap="round" stroke-linejoin="round"')
    return " ".join(bits)


def _render_text(layer: dict, defs: list, fonts: dict) -> str:
    font_key = layer.get("font", "body")
    resolved = fonts.get(font_key, font_key)
    family, generic = font_stack(resolved)
    size = float(layer.get("size", 4))
    weight = layer.get("weight", 400)
    spacing = float(layer.get("letterSpacing", 0) or 0)
    line_height = float(layer.get("lineHeight", 1.25) or 1.25)
    align = layer.get("align", "left")
    x = float(layer.get("x", 0))
    y = float(layer.get("y", 0))
    box_w = float(layer.get("w", 0) or 0)
    transform = layer.get("transform", "")
    text = layer.get("text", "")
    if transform == "uppercase":
        text = str(text).upper()
    elif transform == "lowercase":
        text = str(text).lower()

    lines = (wrap_text(text, box_w, family, generic, size, weight, spacing)
             if box_w > 0 else str(text).split("\n"))

    anchor = {"left": "start", "center": "middle", "right": "end"}.get(align, "start")
    if align == "center":
        tx = x + box_w / 2 if box_w else x
    elif align == "right":
        tx = x + box_w if box_w else x
    else:
        tx = x

    parts = [
        f'<text x="{tx:.2f}" y="{y + size * 0.82:.2f}" '
        f'font-family="{_esc(family)}, {generic}" font-size="{size:.2f}" '
        f'font-weight="{_esc(weight)}" text-anchor="{anchor}" '
        f'{_fill_attr(layer, defs, "fill", "#111111")} '
    ]
    if spacing:
        parts.append(f'letter-spacing="{spacing:.3f}" ')
    if layer.get("italic"):
        parts.append('font-style="italic" ')
    stroke = _stroke_bits(layer, defs)
    if stroke:
        parts.append(stroke + " ")
    parts.append(f'{_common_attrs(layer)}>')
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else size * line_height
        parts.append(
            f'<tspan x="{tx:.2f}" dy="{dy:.2f}">{_esc(line)}</tspan>'
        )
    parts.append("</text>")
    return "".join(parts)


def _render_rect(layer: dict, defs: list) -> str:
    return (
        f'<rect x="{float(layer.get("x",0)):.2f}" y="{float(layer.get("y",0)):.2f}" '
        f'width="{float(layer.get("w",0)):.2f}" height="{float(layer.get("h",0)):.2f}" '
        f'rx="{float(layer.get("rx",0) or 0):.2f}" '
        f'{_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
        f'{_common_attrs(layer)}/>'
    )


def _render_ellipse(layer: dict, defs: list) -> str:
    w = float(layer.get("w", 0))
    h = float(layer.get("h", w))
    return (
        f'<ellipse cx="{float(layer.get("x",0)) + w/2:.2f}" '
        f'cy="{float(layer.get("y",0)) + h/2:.2f}" '
        f'rx="{w/2:.2f}" ry="{h/2:.2f}" '
        f'{_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
        f'{_common_attrs(layer)}/>'
    )


def _render_line(layer: dict, defs: list) -> str:
    x = float(layer.get("x", 0))
    y = float(layer.get("y", 0))
    w = float(layer.get("w", 0))
    h = float(layer.get("h", 0))
    layer = dict(layer)
    layer.setdefault("stroke", "#111111")
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x+w:.2f}" y2="{y+h:.2f}" '
        f'{_stroke_bits(layer, defs)} {_common_attrs(layer)}/>'
    )


def _render_path(layer: dict, defs: list) -> str:
    d = layer.get("d", "")
    return (
        f'<path d="{_esc(d)}" {_fill_attr(layer, defs)} '
        f'{_stroke_bits(layer, defs)} {_common_attrs(layer)}/>'
    )


def _render_image(layer: dict, defs: list, assets: dict) -> str:
    src = layer.get("src", "")
    href = assets.get(src, src)
    if not href:
        return ""
    fit = layer.get("fit", "cover")
    par = "xMidYMid slice" if fit == "cover" else "xMidYMid meet"
    if fit == "stretch":
        par = "none"
    return (
        f'<image href="{_esc(href)}" x="{float(layer.get("x",0)):.2f}" '
        f'y="{float(layer.get("y",0)):.2f}" width="{float(layer.get("w",0)):.2f}" '
        f'height="{float(layer.get("h",0)):.2f}" preserveAspectRatio="{par}" '
        f'{_common_attrs(layer)}/>'
    )


def _render_pattern(layer: dict, defs: list) -> str:
    """A repeating motif used as a surface pattern."""
    pid = "p" + uuid.uuid4().hex[:8]
    size = float(layer.get("tile", 10) or 10)
    motif = layer.get("motif", "dots")
    color = _esc(layer.get("color", "#000000"))
    scale = float(layer.get("scale", 1) or 1)
    rot = float(layer.get("patternRotate", 0) or 0)
    inner = {
        "dots": f'<circle cx="{size/2:.2f}" cy="{size/2:.2f}" r="{size*0.09*scale:.2f}" fill="{color}"/>',
        "grid": (f'<path d="M0 0 H{size:.2f} M0 0 V{size:.2f}" stroke="{color}" '
                 f'stroke-width="{0.25*scale:.2f}" fill="none"/>'),
        "diagonal": (f'<path d="M0 {size:.2f} L{size:.2f} 0" stroke="{color}" '
                     f'stroke-width="{0.5*scale:.2f}" fill="none"/>'),
        "waves": (f'<path d="M0 {size/2:.2f} Q {size/4:.2f} 0 {size/2:.2f} {size/2:.2f} '
                  f'T {size:.2f} {size/2:.2f}" stroke="{color}" '
                  f'stroke-width="{0.4*scale:.2f}" fill="none"/>'),
        "stars": (f'<path d="M{size/2:.2f} {size*0.2:.2f} L{size*0.58:.2f} {size*0.42:.2f} '
                  f'L{size*0.8:.2f} {size/2:.2f} L{size*0.58:.2f} {size*0.58:.2f} '
                  f'L{size/2:.2f} {size*0.8:.2f} L{size*0.42:.2f} {size*0.58:.2f} '
                  f'L{size*0.2:.2f} {size/2:.2f} L{size*0.42:.2f} {size*0.42:.2f} Z" '
                  f'fill="{color}"/>'),
        "leaves": (f'<path d="M{size/2:.2f} {size*0.15:.2f} C {size*0.85:.2f} {size*0.35:.2f} '
                   f'{size*0.85:.2f} {size*0.7:.2f} {size/2:.2f} {size*0.85:.2f} '
                   f'C {size*0.15:.2f} {size*0.7:.2f} {size*0.15:.2f} {size*0.35:.2f} '
                   f'{size/2:.2f} {size*0.15:.2f} Z" fill="{color}"/>'),
        "cross": (f'<path d="M{size/2:.2f} {size*0.3:.2f} V{size*0.7:.2f} '
                  f'M{size*0.3:.2f} {size/2:.2f} H{size*0.7:.2f}" stroke="{color}" '
                  f'stroke-width="{0.35*scale:.2f}" fill="none"/>'),
    }.get(motif, f'<circle cx="{size/2:.2f}" cy="{size/2:.2f}" r="{size*0.1:.2f}" fill="{color}"/>')

    defs.append(
        f'<pattern id="{pid}" width="{size:.2f}" height="{size:.2f}" '
        f'patternUnits="userSpaceOnUse" patternTransform="rotate({rot:.1f})">'
        f'{inner}</pattern>'
    )
    return (
        f'<rect x="{float(layer.get("x",0)):.2f}" y="{float(layer.get("y",0)):.2f}" '
        f'width="{float(layer.get("w",0)):.2f}" height="{float(layer.get("h",0)):.2f}" '
        f'fill="url(#{pid})" {_common_attrs(layer)}/>'
    )


_RENDERERS = {
    "text": lambda l, d, ctx: _render_text(l, d, ctx["fonts"]),
    "rect": lambda l, d, ctx: _render_rect(l, d),
    "ellipse": lambda l, d, ctx: _render_ellipse(l, d),
    "circle": lambda l, d, ctx: _render_ellipse(l, d),
    "line": lambda l, d, ctx: _render_line(l, d),
    "path": lambda l, d, ctx: _render_path(l, d),
    "image": lambda l, d, ctx: _render_image(l, d, ctx["assets"]),
    "pattern": lambda l, d, ctx: _render_pattern(l, d),
}


# ---------------------------------------------------------------------------
# Panel and sheet rendering
# ---------------------------------------------------------------------------

def ensure_layer_ids(spec: dict) -> dict:
    """Give every layer a stable id and name so the UI can address it."""
    for panel_id, panel in (spec.get("panels") or {}).items():
        for i, layer in enumerate(panel.get("layers") or []):
            if not layer.get("id"):
                layer["id"] = f"{panel_id}-{i+1}-{uuid.uuid4().hex[:4]}"
            if not layer.get("name"):
                kind = layer.get("type", "layer")
                hint = layer.get("text") or layer.get("motif") or kind
                layer["name"] = str(hint)[:38]
            layer.setdefault("visible", True)
    return spec


def render_panel(spec: dict, panel_geo: dict, defs: list,
                 assets: dict | None = None, clip: bool = True) -> str:
    """Render one panel in sheet coordinates."""
    panels = spec.get("panels") or {}
    panel = panels.get(panel_geo["id"]) or panels.get(panel_geo.get("face")) or {}
    fonts = spec.get("fonts") or {}
    ctx = {"fonts": fonts, "assets": assets or {}}

    x, y = panel_geo["x"], panel_geo["y"]
    w, h = panel_geo["w"], panel_geo["h"]
    cid = "c" + uuid.uuid4().hex[:8]
    out = []
    if clip:
        defs.append(
            f'<clipPath id="{cid}"><rect x="0" y="0" width="{w:.2f}" '
            f'height="{h:.2f}"/></clipPath>'
        )
    clip_attr = f' clip-path="url(#{cid})"' if clip else ""
    out.append(
        f'<g id="panel-{_esc(panel_geo["id"])}" '
        f'data-panel="{_esc(panel_geo["id"])}" '
        f'transform="translate({x:.2f} {y:.2f})"{clip_attr}>'
    )

    bg = panel.get("background")
    if bg:
        bg_layer = bg if isinstance(bg, dict) else {"fill": bg}
        if "fill" not in bg_layer and bg_layer.get("type") in ("linear", "radial"):
            bg_layer = {"fill": bg_layer}
        bg_layer = {**bg_layer, "x": 0, "y": 0, "w": w, "h": h}
        out.append(_render_rect(bg_layer, defs))

    for layer in panel.get("layers") or []:
        if layer.get("visible") is False:
            continue
        renderer = _RENDERERS.get(layer.get("type"))
        if not renderer:
            continue
        try:
            out.append(
                f'<g data-layer="{_esc(layer.get("id",""))}" '
                f'data-name="{_esc(layer.get("name",""))}">'
                + renderer(layer, defs, ctx) + "</g>"
            )
        except Exception as exc:  # one broken layer must never kill the page
            out.append(f'<!-- layer error {_esc(layer.get("id"))}: {_esc(exc)} -->')
    out.append("</g>")
    return "".join(out)


def render_flat(spec: dict, dieline: dict, assets: dict | None = None,
                only_printable: bool = True) -> str:
    """Every panel rendered onto the flat layout, without the SVG wrapper."""
    defs: list = []
    body = []
    for panel_geo in dieline["panels"]:
        if only_printable and not panel_geo.get("printable", True):
            continue
        body.append(render_panel(spec, panel_geo, defs, assets))
    return f"<defs>{''.join(defs)}</defs>" + "".join(body)


def _style_block(font_css: str | None) -> str:
    """Embedded fonts, falling back to an @import.

    The content has to sit in CDATA. When an SVG is loaded as an image the
    browser parses it strictly as XML, and a bare & in a font URL makes the
    file invalid -- the texture would come out black.
    """
    css = font_css or FONT_IMPORT
    return f"<style><![CDATA[{css}]]></style>"


def render_panel_svg(spec: dict, panel_geo: dict, assets: dict | None = None,
                     scale: float = 8.0, font_css: str | None = None) -> str:
    """One panel as a standalone SVG, for instance as a 3D texture."""
    defs: list = []
    w, h = panel_geo["w"], panel_geo["h"]
    local = {**panel_geo, "x": 0, "y": 0}
    body = render_panel(spec, local, defs, assets)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w*scale:.0f}" '
        f'height="{h*scale:.0f}" viewBox="0 0 {w:.2f} {h:.2f}">'
        f'{_style_block(font_css)}'
        f'<defs>{"".join(defs)}</defs>'
        f'<rect width="{w:.2f}" height="{h:.2f}" fill="#ffffff"/>'
        f'{body}</svg>'
    )


def full_svg(spec: dict, dieline: dict, assets: dict | None = None,
             with_dieline: bool = True, labels: bool = False,
             font_css: str | None = None) -> str:
    """Full print SVG: artwork on the flat layout, optionally with the dieline."""
    from . import dieline as dl

    art = render_flat(spec, dieline, assets)
    sheet = dieline["sheet"]
    if with_dieline:
        svg = dl.to_svg(dieline, show_labels=labels, show_dimensions=True,
                        artwork_svg=art)
        return svg.replace("<title>", _style_block(font_css) + "<title>", 1)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{sheet["width"]}mm" height="{sheet["height"]}mm" '
        f'viewBox="0 0 {sheet["width"]} {sheet["height"]}">'
        f'{_style_block(font_css)}'
        f'<rect width="{sheet["width"]}" height="{sheet["height"]}" fill="#fff"/>'
        f'{art}</svg>'
    )


def layer_index(spec: dict) -> list:
    """A flat list of all layers for the layer palette in the UI."""
    out = []
    for panel_id, panel in (spec.get("panels") or {}).items():
        for layer in panel.get("layers") or []:
            out.append({
                "panel": panel_id,
                "id": layer.get("id"),
                "name": layer.get("name"),
                "type": layer.get("type"),
                "text": layer.get("text"),
                "visible": layer.get("visible", True),
                "fill": layer.get("fill") if isinstance(layer.get("fill"), str) else None,
            })
    return out


def apply_patch(spec: dict, patches: list) -> dict:
    """Apply targeted changes to individual layers."""
    by_id = {}
    for panel_id, panel in (spec.get("panels") or {}).items():
        for i, layer in enumerate(panel.get("layers") or []):
            by_id[layer.get("id")] = (panel_id, i, layer)

    applied, failed = [], []
    for patch in patches or []:
        op = patch.get("op", "update")
        lid = patch.get("id")
        if op == "add":
            panel_id = patch.get("panel")
            panel = (spec.setdefault("panels", {})
                     .setdefault(panel_id, {"layers": []}))
            panel.setdefault("layers", []).append(patch.get("layer") or {})
            applied.append(f"added to {panel_id}")
            continue
        if lid not in by_id:
            failed.append(lid)
            continue
        panel_id, index, layer = by_id[lid]
        if op == "delete":
            spec["panels"][panel_id]["layers"].pop(index)
            by_id = {}
            for pid, p in (spec.get("panels") or {}).items():
                for i, l in enumerate(p.get("layers") or []):
                    by_id[l.get("id")] = (pid, i, l)
            applied.append(f"{lid} deleted")
            continue
        if op == "move":
            layers = spec["panels"][panel_id]["layers"]
            target = max(0, min(len(layers) - 1, int(patch.get("to", index))))
            layers.insert(target, layers.pop(index))
            applied.append(f"{lid} reordered")
            continue
        changes = patch.get("set") or {}
        layer.update(changes)
        applied.append(f"{lid}: {', '.join(changes.keys())}")
    ensure_layer_ids(spec)
    return {"spec": spec, "applied": applied, "not_found": failed}
