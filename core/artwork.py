"""Artwork renderer: turns a layer spec (JSON) into real vector SVG.

The important difference from a plain image generator: Claude describes the
design as structured layers in millimetres rather than painting pixels. That
buys you four things.

  * Text is real text, so it is always legible. Image models routinely
    mangle lettering on packaging.
  * Every layer can be edited on its own.
  * The output is print-ready vector art at actual size, with bleed.
  * The same data supplies the textures for the 3D preview.

Layer coordinates are relative to the top-left corner of their panel, in
millimetres.
"""

from __future__ import annotations
import html
import math
import re
import uuid

FONT_CATALOG = {
    "playfair": ("Playfair Display", "serif", "Elegant, premium, luxury beauty"),
    "cormorant": ("Cormorant Garamond", "serif", "Fine, apothecary, natural skincare"),
    "dmserif": ("DM Serif Display", "serif", "Modern classic, strong headlines"),
    "fraunces": ("Fraunces", "serif", "Soft, characterful, warm editorial"),
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
    "ethiopic": ("Noto Sans Ethiopic", "sans-serif",
                 "Amharic and Tigrinya (Ge'ez script), clean"),
    "ethiopicserif": ("Noto Serif Ethiopic", "serif",
                      "Amharic and Tigrinya (Ge'ez script), with serifs"),
}

# What to fall back on when the web font cannot be fetched. Kefa ships with
# macOS and Nyala with Windows, so Ge'ez script never turns into empty boxes.
_FALLBACKS = {
    "Noto Sans Ethiopic": "Kefa, Nyala, 'Abyssinica SIL'",
    "Noto Serif Ethiopic": "Kefa, Nyala, 'Abyssinica SIL'",
}

_GOOGLE_FAMILIES = [
    "Playfair+Display:ital,wght@0,400..900;1,400..900",
    "Cormorant+Garamond:ital,wght@0,300..700;1,300..700",
    "DM+Serif+Display:ital@0;1",
    "Fraunces:ital,wght@0,300..800;1,300..800",
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
    "Noto+Sans+Ethiopic:wght@100..900",
    "Noto+Serif+Ethiopic:wght@100..900",
]

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    + "&".join(f"family={f}" for f in _GOOGLE_FAMILIES)
    + "&display=swap');"
)

# Rough average character width as a fraction of font size, used for wrapping.
_WIDTH_FACTOR = {"serif": 0.50, "sans-serif": 0.52, "monospace": 0.60}
_NARROW = {"Bebas Neue": 0.38, "Archivo Black": 0.62, "Abril Fatface": 0.56}
_ETHIOPIC_FACTOR = 0.64   # Ge'ez syllables, averaged with the spaces between words

MIN_TYPE_MM = 1.5
SVG_NS = ('xmlns="http://www.w3.org/2000/svg" '
          'xmlns:xlink="http://www.w3.org/1999/xlink"')


def font_stack(key: str) -> tuple:
    family, generic, _ = FONT_CATALOG.get(key, FONT_CATALOG["inter"])
    return family, generic


def css_family(family: str, generic: str) -> str:
    extra = _FALLBACKS.get(family)
    return f"'{family}', {extra + ', ' if extra else ''}{generic}"


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _f(layer: dict, key: str, default: float = 0.0) -> float:
    try:
        value = layer.get(key, default)
        return float(default if value is None else value)
    except (TypeError, ValueError):
        return float(default)


def _has_ethiopic(text: str) -> bool:
    return any("ሀ" <= ch <= "᎟" or "ⶀ" <= ch <= "⷟"
               for ch in str(text))


def _char_width(family: str, generic: str, size: float, weight,
                text: str = "") -> float:
    if text and _has_ethiopic(text):
        factor = _ETHIOPIC_FACTOR
    else:
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
    cw = _char_width(family, generic, size, weight, text) + letter_spacing
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


def text_metrics(layer: dict, fonts: dict) -> dict:
    """Resolved size, lines and height for a text layer.

    Shared by the renderer and the layout checker so they always agree. With
    fit="shrink" and a box (w and h) the size steps down until the text fits,
    which removes a whole class of overflow errors before they happen.
    """
    key = layer.get("font", "body")
    family, generic = font_stack(fonts.get(key, key))
    size = _f(layer, "size", 4) or 4
    weight = layer.get("weight", 400)
    spacing = _f(layer, "letterSpacing", 0)
    line_height = _f(layer, "lineHeight", 1.25) or 1.25
    text = str(layer.get("text", "") or "")
    if layer.get("transform") == "uppercase":
        text = text.upper()
    elif layer.get("transform") == "lowercase":
        text = text.lower()
    box_w = _f(layer, "w", 0)
    box_h = _f(layer, "h", 0)

    def measure(s):
        lines = (wrap_text(text, box_w, family, generic, s, weight, spacing)
                 if box_w > 0 else text.split("\n")) or [""]
        height = (len(lines) - 1) * s * line_height + s * 1.22
        cw = _char_width(family, generic, s, weight, text) + spacing
        widest = max(len(l) for l in lines) * cw
        return lines, height, widest

    lines, height, widest = measure(size)
    if layer.get("fit") == "shrink" and box_w > 0:
        floor = max(MIN_TYPE_MM, size * 0.35)
        while size > floor and ((box_h > 0 and height > box_h) or widest > box_w * 1.02):
            size = round(size * 0.94, 3)
            lines, height, widest = measure(size)

    return {"family": family, "generic": generic, "size": size,
            "weight": weight, "spacing": spacing, "line_height": line_height,
            "lines": lines, "height": height, "widest": widest, "text": text}


# ---------------------------------------------------------------------------
# Shared attribute helpers
# ---------------------------------------------------------------------------

def _common_attrs(layer: dict) -> str:
    bits = []
    opacity = layer.get("opacity")
    if opacity is not None and _f(layer, "opacity", 1) < 1:
        bits.append(f'opacity="{_f(layer, "opacity", 1):.3f}"')
    rotate = _f(layer, "rotate", 0)
    if rotate:
        w = _f(layer, "w", 0)
        h = _f(layer, "h", w)
        cx = _f(layer, "x") + w / 2
        cy = _f(layer, "y") + h / 2
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
            offset, color, op = i / max(1, len(stops) - 1), stop, 1
        if offset > 1:
            offset /= 100.0
        normalized.append(
            f'<stop offset="{offset*100:.1f}%" stop-color="{_esc(color)}" '
            f'stop-opacity="{float(op):.3f}"/>'
        )
    joined = "".join(normalized)
    if spec.get("type") == "radial":
        return (f'<radialGradient id="{gid}" cx="{spec.get("cx", 50)}%" '
                f'cy="{spec.get("cy", 50)}%" r="{spec.get("r", 70)}%">'
                f'{joined}</radialGradient>')
    rad = math.radians(float(spec.get("angle", 90)))
    x1, y1 = 50 - math.cos(rad) * 50, 50 - math.sin(rad) * 50
    x2, y2 = 50 + math.cos(rad) * 50, 50 + math.sin(rad) * 50
    return (f'<linearGradient id="{gid}" x1="{x1:.1f}%" y1="{y1:.1f}%" '
            f'x2="{x2:.1f}%" y2="{y2:.1f}%">{joined}</linearGradient>')


def _stroke_bits(layer: dict, defs: list) -> str:
    if not layer.get("stroke"):
        return ""
    bits = [_fill_attr(layer, defs, "stroke", "none"),
            f'stroke-width="{_f(layer, "strokeWidth", 0.4):.2f}"']
    if layer.get("dash"):
        bits.append(f'stroke-dasharray="{_esc(layer["dash"])}"')
    bits.append('stroke-linecap="round" stroke-linejoin="round"')
    return " ".join(bits)


def _shape_path(kind: str, x: float, y: float, w: float, h: float,
                rx: float = 0.0) -> str:
    """Outline of a rectangle, ellipse or arch, as SVG path data."""
    if kind == "ellipse":
        rx_, ry_ = w / 2, h / 2
        cx, cy = x + rx_, y + ry_
        return (f"M{cx - rx_:.2f} {cy:.2f} A{rx_:.2f} {ry_:.2f} 0 1 0 "
                f"{cx + rx_:.2f} {cy:.2f} A{rx_:.2f} {ry_:.2f} 0 1 0 "
                f"{cx - rx_:.2f} {cy:.2f} Z")
    if kind == "arch":
        r = min(w / 2, h)
        return (f"M{x:.2f} {y + h:.2f} V{y + r:.2f} "
                f"A{r:.2f} {r:.2f} 0 0 1 {x + w:.2f} {y + r:.2f} "
                f"V{y + h:.2f} Z")
    if rx > 0:
        r = min(rx, w / 2, h / 2)
        return (f"M{x + r:.2f} {y:.2f} H{x + w - r:.2f} "
                f"A{r:.2f} {r:.2f} 0 0 1 {x + w:.2f} {y + r:.2f} "
                f"V{y + h - r:.2f} A{r:.2f} {r:.2f} 0 0 1 {x + w - r:.2f} {y + h:.2f} "
                f"H{x + r:.2f} A{r:.2f} {r:.2f} 0 0 1 {x:.2f} {y + h - r:.2f} "
                f"V{y + r:.2f} A{r:.2f} {r:.2f} 0 0 1 {x + r:.2f} {y:.2f} Z")
    return f"M{x:.2f} {y:.2f} H{x + w:.2f} V{y + h:.2f} H{x:.2f} Z"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _render_text(layer: dict, defs: list, fonts: dict) -> str:
    m = text_metrics(layer, fonts)
    size, lines = m["size"], m["lines"]
    align = layer.get("align", "left")
    x, y = _f(layer, "x"), _f(layer, "y")
    box_w = _f(layer, "w", 0)

    anchor = {"left": "start", "center": "middle", "right": "end"}.get(align, "start")
    tx = x + (box_w / 2 if align == "center" else box_w if align == "right" else 0)

    if "fill" not in layer and layer.get("color"):
        layer = {**layer, "fill": layer["color"]}

    parts = [
        f'<text x="{tx:.2f}" y="{y + size * 0.82:.2f}" '
        f'font-family="{_esc(css_family(m["family"], m["generic"]))}" '
        f'font-size="{size:.2f}" font-weight="{_esc(m["weight"])}" '
        f'text-anchor="{anchor}" {_fill_attr(layer, defs, "fill", "#111111")} '
    ]
    if m["spacing"]:
        parts.append(f'letter-spacing="{m["spacing"]:.3f}" ')
    if layer.get("italic"):
        parts.append('font-style="italic" ')
    stroke = _stroke_bits(layer, defs)
    if stroke:
        parts.append(stroke + " ")
    parts.append(f'{_common_attrs(layer)}>')
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else size * m["line_height"]
        parts.append(f'<tspan x="{tx:.2f}" dy="{dy:.2f}">{_esc(line)}</tspan>')
    parts.append("</text>")
    return "".join(parts)


def _render_circle_text(layer: dict, defs: list, fonts: dict) -> str:
    """Text running around a circle: seals, stamps, badges.

    The layer's box is x, y, w (the circle's diameter). Letters sit inside that
    circle, reading clockwise and centred at the top. With repeat=true the text
    is repeated to close the ring exactly, which is the classic seal.
    """
    key = layer.get("font", "body")
    family, generic = font_stack(fonts.get(key, key))
    size = _f(layer, "size", 2.4) or 2.4
    weight = layer.get("weight", 500)
    spacing = _f(layer, "letterSpacing", 0.2)
    d = _f(layer, "w", 20) or 20
    cx, cy = _f(layer, "x") + d / 2, _f(layer, "y") + d / 2
    r = max(1.0, d / 2 - size * 0.86)
    text = str(layer.get("text", "") or "")
    if layer.get("transform", "uppercase") == "uppercase":
        text = text.upper()

    circumference = 2 * math.pi * r
    text_length = ""
    if layer.get("repeat"):
        unit = text.strip()
        separator = layer.get("separator", " · ")
        unit = unit + separator
        unit_w = len(unit) * (_char_width(family, generic, size, weight, unit) + spacing)
        count = max(1, int(circumference / max(unit_w, 0.1)))
        text = unit * count
        text_length = (f' textLength="{circumference * 0.995:.2f}" '
                       f'lengthAdjust="spacing"')

    pid = "tp" + uuid.uuid4().hex[:8]
    defs.append(
        f'<path id="{pid}" fill="none" d="M{cx:.2f} {cy + r:.2f} '
        f'A{r:.2f} {r:.2f} 0 1 1 {cx:.2f} {cy - r:.2f} '
        f'A{r:.2f} {r:.2f} 0 1 1 {cx:.2f} {cy + r:.2f}"/>'
    )
    start = _f(layer, "start", 0)
    rotate = f' transform="rotate({start:.2f} {cx:.2f} {cy:.2f})"' if start else ""
    fill_layer = layer if "fill" in layer else {**layer, "fill": layer.get("color", "#111111")}
    return (
        f'<text font-family="{_esc(css_family(family, generic))}" '
        f'font-size="{size:.2f}" font-weight="{_esc(weight)}" '
        f'letter-spacing="{spacing:.3f}" {_fill_attr(fill_layer, defs, "fill", "#111111")}'
        f'{rotate} {_common_attrs({k: v for k, v in layer.items() if k != "rotate"})}>'
        f'<textPath href="#{pid}" xlink:href="#{pid}" startOffset="50%" '
        f'text-anchor="middle"{text_length}>{_esc(text)}</textPath></text>'
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def _render_rect(layer: dict, defs: list) -> str:
    return (
        f'<rect x="{_f(layer, "x"):.2f}" y="{_f(layer, "y"):.2f}" '
        f'width="{_f(layer, "w"):.2f}" height="{_f(layer, "h"):.2f}" '
        f'rx="{_f(layer, "rx"):.2f}" '
        f'{_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
        f'{_common_attrs(layer)}/>'
    )


def _render_ellipse(layer: dict, defs: list) -> str:
    w = _f(layer, "w")
    h = _f(layer, "h", w) or w
    return (
        f'<ellipse cx="{_f(layer, "x") + w/2:.2f}" cy="{_f(layer, "y") + h/2:.2f}" '
        f'rx="{w/2:.2f}" ry="{h/2:.2f}" '
        f'{_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
        f'{_common_attrs(layer)}/>'
    )


def _render_arch(layer: dict, defs: list) -> str:
    """A rectangle with a fully rounded top. A staple of current beauty design."""
    d = _shape_path("arch", _f(layer, "x"), _f(layer, "y"),
                    _f(layer, "w"), _f(layer, "h"))
    return (f'<path d="{d}" {_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
            f'{_common_attrs(layer)}/>')


def _render_star(layer: dict, defs: list) -> str:
    """Star or starburst seal. inner near 1 gives a scalloped burst."""
    d = _f(layer, "w", 12) or 12
    points = max(3, int(_f(layer, "points", 12)))
    inner = min(0.98, max(0.1, _f(layer, "inner", 0.84)))
    cx, cy = _f(layer, "x") + d / 2, _f(layer, "y") + d / 2
    outer_r, inner_r = d / 2, d / 2 * inner
    pts = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        radius = outer_r if i % 2 == 0 else inner_r
        pts.append(f"{cx + radius * math.cos(angle):.2f} {cy + radius * math.sin(angle):.2f}")
    path = "M" + " L".join(pts) + " Z"
    return (f'<path d="{path}" {_fill_attr(layer, defs)} {_stroke_bits(layer, defs)} '
            f'stroke-linejoin="round" {_common_attrs(layer)}/>')


def _render_line(layer: dict, defs: list) -> str:
    x, y, w, h = _f(layer, "x"), _f(layer, "y"), _f(layer, "w"), _f(layer, "h")
    layer = dict(layer)
    layer.setdefault("stroke", layer.get("color", "#111111"))
    return (f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x+w:.2f}" y2="{y+h:.2f}" '
            f'{_stroke_bits(layer, defs)} {_common_attrs(layer)}/>')


def _render_path(layer: dict, defs: list) -> str:
    return (f'<path d="{_esc(layer.get("d", ""))}" {_fill_attr(layer, defs)} '
            f'{_stroke_bits(layer, defs)} {_common_attrs(layer)}/>')


def _render_image(layer: dict, defs: list, assets: dict) -> str:
    src = layer.get("src", "")
    href = assets.get(src, src)
    if not href:
        return ""
    fit = layer.get("fit", "cover")
    par = {"cover": "xMidYMid slice", "contain": "xMidYMid meet",
           "stretch": "none"}.get(fit, "xMidYMid slice")
    return (
        f'<image href="{_esc(href)}" xlink:href="{_esc(href)}" '
        f'x="{_f(layer, "x"):.2f}" y="{_f(layer, "y"):.2f}" '
        f'width="{_f(layer, "w"):.2f}" height="{_f(layer, "h"):.2f}" '
        f'preserveAspectRatio="{par}" {_common_attrs(layer)}/>'
    )


def _render_pattern(layer: dict, defs: list) -> str:
    """A repeating motif filling a rectangle, ellipse or arch."""
    pid = "p" + uuid.uuid4().hex[:8]
    size = _f(layer, "tile", 10) or 10
    motif = layer.get("motif", "dots")
    color = _esc(layer.get("color", "#000000"))
    scale = _f(layer, "scale", 1) or 1
    rot = _f(layer, "patternRotate", 0)
    s = size
    inner = {
        "dots": f'<circle cx="{s/2:.2f}" cy="{s/2:.2f}" r="{s*0.09*scale:.2f}" fill="{color}"/>',
        "grid": (f'<path d="M0 0 H{s:.2f} M0 0 V{s:.2f}" stroke="{color}" '
                 f'stroke-width="{0.25*scale:.2f}" fill="none"/>'),
        "diagonal": (f'<path d="M0 {s:.2f} L{s:.2f} 0" stroke="{color}" '
                     f'stroke-width="{0.5*scale:.2f}" fill="none"/>'),
        "waves": (f'<path d="M0 {s/2:.2f} Q {s/4:.2f} 0 {s/2:.2f} {s/2:.2f} '
                  f'T {s:.2f} {s/2:.2f}" stroke="{color}" '
                  f'stroke-width="{0.4*scale:.2f}" fill="none"/>'),
        "stars": (f'<path d="M{s/2:.2f} {s*0.2:.2f} L{s*0.58:.2f} {s*0.42:.2f} '
                  f'L{s*0.8:.2f} {s/2:.2f} L{s*0.58:.2f} {s*0.58:.2f} '
                  f'L{s/2:.2f} {s*0.8:.2f} L{s*0.42:.2f} {s*0.58:.2f} '
                  f'L{s*0.2:.2f} {s/2:.2f} L{s*0.42:.2f} {s*0.42:.2f} Z" fill="{color}"/>'),
        "leaves": (f'<path d="M{s/2:.2f} {s*0.15:.2f} C {s*0.85:.2f} {s*0.35:.2f} '
                   f'{s*0.85:.2f} {s*0.7:.2f} {s/2:.2f} {s*0.85:.2f} '
                   f'C {s*0.15:.2f} {s*0.7:.2f} {s*0.15:.2f} {s*0.35:.2f} '
                   f'{s/2:.2f} {s*0.15:.2f} Z" fill="{color}"/>'),
        "cross": (f'<path d="M{s/2:.2f} {s*0.3:.2f} V{s*0.7:.2f} '
                  f'M{s*0.3:.2f} {s/2:.2f} H{s*0.7:.2f}" stroke="{color}" '
                  f'stroke-width="{0.35*scale:.2f}" fill="none"/>'),
        "lines": (f'<path d="M0 {s/2:.2f} H{s:.2f}" stroke="{color}" '
                  f'stroke-width="{0.3*scale:.2f}" fill="none"/>'),
        "rings": (f'<circle cx="{s/2:.2f}" cy="{s/2:.2f}" r="{s*0.3:.2f}" fill="none" '
                  f'stroke="{color}" stroke-width="{0.3*scale:.2f}"/>'),
        # Mesob basketry: a stepped zigzag band.
        "zigzag": (f'<path d="M0 {s*0.65:.2f} L{s*0.25:.2f} {s*0.35:.2f} '
                   f'L{s*0.5:.2f} {s*0.65:.2f} L{s*0.75:.2f} {s*0.35:.2f} '
                   f'L{s:.2f} {s*0.65:.2f}" stroke="{color}" '
                   f'stroke-width="{0.45*scale:.2f}" fill="none" '
                   f'stroke-linejoin="miter"/>'),
    }.get(motif, f'<circle cx="{s/2:.2f}" cy="{s/2:.2f}" r="{s*0.1:.2f}" fill="{color}"/>')

    tile_w, tile_h, offset_y = s, s, 0.0
    if motif == "tibeb":
        # The woven border of a habesha kemis or netela: a chain of nested
        # lozenges between two rules, read as one continuous band. The tile is
        # as tall as the layer so the band always sits exactly inside it.
        band = max(2.0, _f(layer, "h") or s)
        tile_w, tile_h, offset_y = s, band, _f(layer, "y")
        accent = _esc(layer.get("color2") or layer.get("color", "#000000"))
        w2, h2 = s / 2, band / 2
        rule = max(0.18, band * 0.045) * scale
        inner = (
            f'<path d="M0 {band*0.1:.2f} H{s:.2f} M0 {band*0.9:.2f} H{s:.2f}" '
            f'stroke="{color}" stroke-width="{rule:.2f}" fill="none"/>'
            f'<path d="M{w2:.2f} {band*0.2:.2f} L{s*0.86:.2f} {h2:.2f} '
            f'L{w2:.2f} {band*0.8:.2f} L{s*0.14:.2f} {h2:.2f} Z" fill="{color}"/>'
            f'<path d="M{w2:.2f} {band*0.34:.2f} L{s*0.7:.2f} {h2:.2f} '
            f'L{w2:.2f} {band*0.66:.2f} L{s*0.3:.2f} {h2:.2f} Z" fill="{accent}"/>'
            f'<circle cx="{w2:.2f}" cy="{h2:.2f}" r="{band*0.05:.2f}" fill="{color}"/>'
            f'<path d="M0 {h2:.2f} L{s*0.08:.2f} {band*0.38:.2f} L{s*0.08:.2f} '
            f'{band*0.62:.2f} Z M{s:.2f} {h2:.2f} L{s*0.92:.2f} {band*0.38:.2f} '
            f'L{s*0.92:.2f} {band*0.62:.2f} Z" fill="{color}"/>'
        )

    defs.append(
        f'<pattern id="{pid}" width="{tile_w:.2f}" height="{tile_h:.2f}" '
        f'y="{offset_y:.2f}" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate({rot:.1f})">'
        f'{inner}</pattern>'
    )
    d = _shape_path(layer.get("shape", "rect"), _f(layer, "x"), _f(layer, "y"),
                    _f(layer, "w"), _f(layer, "h"), _f(layer, "rx"))
    return f'<path d="{d}" fill="url(#{pid})" {_common_attrs(layer)}/>'


# ---------------------------------------------------------------------------
# EAN-13 barcode
# ---------------------------------------------------------------------------

_EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011"]
_EAN_G = ["0100111", "0110011", "0011011", "0100001", "0011101",
          "0111001", "0000101", "0010001", "0001001", "0010111"]
_EAN_R = ["1110010", "1100110", "1101100", "1000010", "1011100",
          "1001110", "1010000", "1000100", "1001000", "1110100"]
_EAN_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
               "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]

# GS1 prefix 2 is reserved for in-store use, so this placeholder can never
# collide with a real product on a real shelf.
EAN_PLACEHOLDER = "200000000000"


def ean13_check_digit(first12: str) -> int:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12[:12]))
    return (10 - total % 10) % 10


def ean13(raw) -> str:
    """Normalise to 13 digits with a correct check digit."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) not in (12, 13):
        digits = EAN_PLACEHOLDER
    first12 = digits[:12]
    return first12 + str(ean13_check_digit(first12))


def ean13_modules(code: str) -> str:
    """The 95-module bar pattern for a 13-digit code."""
    parity = _EAN_PARITY[int(code[0])]
    left = "".join((_EAN_L if p == "L" else _EAN_G)[int(d)]
                   for p, d in zip(parity, code[1:7]))
    right = "".join(_EAN_R[int(d)] for d in code[7:13])
    return "101" + left + "01010" + right + "101"


def _render_barcode(layer: dict, defs: list, fonts: dict) -> str:
    code = ean13(layer.get("digits"))
    pattern = ean13_modules(code)
    x, y = _f(layer, "x"), _f(layer, "y")
    w = _f(layer, "w", 37.3) or 37.3
    h = _f(layer, "h", 25.9) or 25.9
    bars = _esc(layer.get("color", "#111111"))
    ground = layer.get("background", "#ffffff")
    show_digits = layer.get("showDigits", True)

    module = w / 113.0                      # 11 quiet + 95 + 7 quiet
    origin = x + 11 * module
    text_band = h * 0.15 if show_digits else 0
    bar_h = h - text_band - module * 2
    guard_h = bar_h + text_band * 0.55
    top = y + module

    guards = set(range(0, 3)) | set(range(45, 50)) | set(range(92, 95))
    parts = []
    run_start = None
    for i, bit in enumerate(pattern + "0"):
        if bit == "1" and run_start is None:
            run_start = i
        elif bit != "1" and run_start is not None:
            length = i - run_start
            tall = run_start in guards
            parts.append(f"M{origin + run_start * module:.3f} {top:.3f}"
                         f"h{length * module:.3f}v{guard_h if tall else bar_h:.3f}"
                         f"h{-length * module:.3f}z")
            run_start = None

    out = []
    if ground and ground != "none":
        out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                   f'fill="{_esc(ground)}"/>')
    out.append(f'<path d="{"".join(parts)}" fill="{bars}"/>')

    if show_digits:
        family, generic = font_stack(fonts.get("mono", "ibmmono"))
        size = text_band * 0.92
        base = top + bar_h + text_band * 0.86
        attrs = (f'font-family="{_esc(css_family(family, generic))}" '
                 f'font-size="{size:.2f}" fill="{bars}" text-anchor="middle"')
        out.append(f'<text x="{x + 5.5 * module:.2f}" y="{base:.2f}" {attrs}>{code[0]}</text>')
        out.append(f'<text x="{origin + 24 * module:.2f}" y="{base:.2f}" {attrs} '
                   f'textLength="{40 * module:.2f}" lengthAdjust="spacing">{code[1:7]}</text>')
        out.append(f'<text x="{origin + 71 * module:.2f}" y="{base:.2f}" {attrs} '
                   f'textLength="{40 * module:.2f}" lengthAdjust="spacing">{code[7:]}</text>')
    return f'<g {_common_attrs(layer)}>{"".join(out)}</g>'


# ---------------------------------------------------------------------------
# Icons: the small marks cosmetics packaging carries
# ---------------------------------------------------------------------------

def _recycle_paths(color: str) -> str:
    cx = cy = 12.0
    r = 7.0
    out = []
    for k in range(3):
        a0 = math.radians(-90 + k * 120 + 16)
        a1 = math.radians(-90 + k * 120 + 104)
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        out.append(f'<path d="M{x0:.2f} {y0:.2f} A{r} {r} 0 0 1 {x1:.2f} {y1:.2f}" '
                   f'fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round"/>')
        tx, ty = -math.sin(a1), math.cos(a1)
        nx, ny = math.cos(a1), math.sin(a1)
        tip = (x1 + tx * 2.6, y1 + ty * 2.6)
        b1 = (x1 + nx * 2.0, y1 + ny * 2.0)
        b2 = (x1 - nx * 2.0, y1 - ny * 2.0)
        out.append(f'<path d="M{tip[0]:.2f} {tip[1]:.2f} L{b1[0]:.2f} {b1[1]:.2f} '
                   f'L{b2[0]:.2f} {b2[1]:.2f} Z" fill="{color}"/>')
    return "".join(out)


def _icon_body(name: str, color: str, label: str) -> str:
    c = color
    stroke = f'fill="none" stroke="{c}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"'
    if name == "pao":
        return (f'<path d="M5.2 10.6 H18.8 V18.4 Q18.8 20.6 16.6 20.6 H7.4 '
                f'Q5.2 20.6 5.2 18.4 Z" {stroke}/>'
                f'<path d="M4.4 8.4 L18.8 4.6" stroke="{c}" stroke-width="2.4" stroke-linecap="round"/>'
                f'<text x="12" y="17.5" font-family="Helvetica, Arial, sans-serif" '
                f'font-weight="700" font-size="{5.4 if len(label) <= 3 else 4.2}" '
                f'text-anchor="middle" fill="{c}">{_esc(label or "12M")}</text>')
    if name == "estimated":
        return (f'<path d="M6.2 12 H17.8 A5.8 5.8 0 1 0 15.9 16.3" fill="none" '
                f'stroke="{c}" stroke-width="1.9" stroke-linecap="round"/>')
    if name == "recycle":
        return _recycle_paths(c)
    if name == "leaf":
        return (f'<path d="M5 19 C5 10.5 11 5.2 19.4 4.6 C18.9 13.2 13.8 19 5 19 Z" {stroke}/>'
                f'<path d="M5 19 L13.6 10.4" {stroke}/>')
    if name == "drop":
        return (f'<path d="M12 3.6 C12 3.6 6.2 10.4 6.2 14.6 A5.8 5.8 0 0 0 17.8 14.6 '
                f'C17.8 10.4 12 3.6 12 3.6 Z" {stroke}/>')
    if name == "sun":
        rays = "".join(
            f'<path d="M{12 + 6.6 * math.cos(a):.2f} {12 + 6.6 * math.sin(a):.2f} '
            f'L{12 + 9.2 * math.cos(a):.2f} {12 + 9.2 * math.sin(a):.2f}" {stroke}/>'
            for a in (math.radians(d) for d in range(0, 360, 45)))
        return f'<circle cx="12" cy="12" r="4.1" {stroke}/>' + rays
    if name == "sparkle":
        return (f'<path d="M12 2.8 C12.7 8.6 15.4 11.3 21.2 12 C15.4 12.7 12.7 15.4 12 21.2 '
                f'C11.3 15.4 8.6 12.7 2.8 12 C8.6 11.3 11.3 8.6 12 2.8 Z" fill="{c}"/>')
    if name == "check":
        return (f'<circle cx="12" cy="12" r="8.8" {stroke}/>'
                f'<path d="M7.9 12.3 L10.8 15.1 L16.3 9.3" {stroke}/>')
    if name == "dropper":
        return (f'<path d="M15.2 3.9 a2.7 2.7 0 0 1 3.8 0 l1.1 1.1 a2.7 2.7 0 0 1 0 3.8 '
                f'l-1.9 1.9 -4.9 -4.9 Z" {stroke}/>'
                f'<path d="M13.6 7.6 L5.9 15.3 L5.1 18.9 L8.7 18.1 L16.4 10.4" {stroke}/>'
                f'<path d="M4.3 21.3 a1 1 0 0 0 1.4 -1.4" {stroke}/>')
    if name == "jar":
        return (f'<path d="M6.6 9.6 H17.4 V18.4 Q17.4 20.4 15.4 20.4 H8.6 '
                f'Q6.6 20.4 6.6 18.4 Z" {stroke}/>'
                f'<rect x="5.6" y="5" width="12.8" height="4.6" rx="1.2" {stroke}/>')
    if name == "hands":
        return (f'<path d="M4 15 C6.5 15 8.5 13.6 10 12 L12.6 9.6 a1.3 1.3 0 0 1 1.9 1.8 '
                f'L12.4 13.6 H17.4 a1.5 1.5 0 0 1 0 3 H11 C9 16.6 7.2 18.8 4 18.8" {stroke}/>')
    return f'<circle cx="12" cy="12" r="8" {stroke}/>'


ICON_NAMES = ["pao", "estimated", "recycle", "leaf", "drop", "sun",
              "sparkle", "check", "dropper", "jar", "hands"]


def _render_icon(layer: dict, defs: list) -> str:
    size = _f(layer, "w", 8) or 8
    scale = size / 24.0
    color = layer.get("color") or layer.get("fill") or "#111111"
    if isinstance(color, dict):
        color = "#111111"
    body = _icon_body(layer.get("icon", "check"), _esc(color), str(layer.get("label", "") or ""))
    # Two groups, so placement and the optional rotation never compete for
    # the same transform attribute.
    inner = _common_attrs({**layer, "x": 0, "y": 0, "w": 24, "h": 24})
    return (f'<g transform="translate({_f(layer, "x"):.2f} {_f(layer, "y"):.2f}) '
            f'scale({scale:.4f})"><g {inner}>{body}</g></g>')


_RENDERERS = {
    "text": lambda l, d, ctx: _render_text(l, d, ctx["fonts"]),
    "circle_text": lambda l, d, ctx: _render_circle_text(l, d, ctx["fonts"]),
    "rect": lambda l, d, ctx: _render_rect(l, d),
    "ellipse": lambda l, d, ctx: _render_ellipse(l, d),
    "circle": lambda l, d, ctx: _render_ellipse(l, d),
    "arch": lambda l, d, ctx: _render_arch(l, d),
    "star": lambda l, d, ctx: _render_star(l, d),
    "line": lambda l, d, ctx: _render_line(l, d),
    "path": lambda l, d, ctx: _render_path(l, d),
    "image": lambda l, d, ctx: _render_image(l, d, ctx["assets"]),
    "pattern": lambda l, d, ctx: _render_pattern(l, d),
    "barcode": lambda l, d, ctx: _render_barcode(l, d, ctx["fonts"]),
    "icon": lambda l, d, ctx: _render_icon(l, d),
}

LAYER_TYPES = sorted(_RENDERERS.keys())


# ---------------------------------------------------------------------------
# Bleed
# ---------------------------------------------------------------------------

FOLD_OVERPRINT = 1.0   # mm of ink carried over a fold into an unprinted flap


def bleed_edges(dieline: dict) -> dict:
    """How far each printable panel's artwork should run past each edge.

    An edge shared with another printable panel gets nothing: the neighbour
    takes over there. An edge against an unprinted flap (the glue flap) gets a
    millimetre, so no white hairline shows on the fold but the glue strip stays
    clean. Every other edge is a cut, or a fold into a dust flap, and gets the
    full bleed so the knife never lands on white.
    """
    bleed = float(dieline.get("bleed", 3) or 3)
    panels = dieline.get("panels") or []
    tol = 0.35

    def overlap(a0, a1, b0, b1):
        return max(0.0, min(a1, b1) - max(a0, b0))

    result = {}
    for a in panels:
        if not a.get("printable", True):
            continue
        ax0, ay0 = a["x"], a["y"]
        ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
        edges = {}
        for edge in ("left", "right", "top", "bottom"):
            amount = bleed
            for b in panels:
                if b is a:
                    continue
                bx0, by0 = b["x"], b["y"]
                bx1, by1 = bx0 + b["w"], by0 + b["h"]
                if edge == "left":
                    touching = abs(bx1 - ax0) < tol and overlap(ay0, ay1, by0, by1) > a["h"] * 0.5
                elif edge == "right":
                    touching = abs(bx0 - ax1) < tol and overlap(ay0, ay1, by0, by1) > a["h"] * 0.5
                elif edge == "top":
                    touching = abs(by1 - ay0) < tol and overlap(ax0, ax1, bx0, bx1) > a["w"] * 0.5
                else:
                    touching = abs(by0 - ay1) < tol and overlap(ax0, ax1, bx0, bx1) > a["w"] * 0.5
                if touching:
                    amount = 0.0 if b.get("printable", True) else min(amount, FOLD_OVERPRINT)
                    if amount == 0.0:
                        break
            edges[edge] = amount
        result[a["id"]] = edges
    return result


def _extend_into_bleed(layer: dict, w: float, h: float, bleed: dict) -> dict:
    """Full-bleed elements that touch an edge are carried out into the bleed."""
    if layer.get("type") not in ("rect", "pattern", "image") or not bleed:
        return layer
    x0, y0 = _f(layer, "x"), _f(layer, "y")
    x1, y1 = x0 + _f(layer, "w"), y0 + _f(layer, "h")
    edge_tol = 0.6
    if x0 <= edge_tol:
        x0 -= bleed.get("left", 0)
    if x1 >= w - edge_tol:
        x1 += bleed.get("right", 0)
    if y0 <= edge_tol:
        y0 -= bleed.get("top", 0)
    if y1 >= h - edge_tol:
        y1 += bleed.get("bottom", 0)
    return {**layer, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


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
                hint = layer.get("text") or layer.get("icon") or layer.get("motif") or kind
                layer["name"] = str(hint)[:38]
            layer.setdefault("visible", True)
    return spec


# How a panel's flat-layout edges map onto its upright design edges, for each
# rotation: design edge -> flat edge.
_EDGE_MAP = {
    0:   {"top": "top", "bottom": "bottom", "left": "left", "right": "right"},
    180: {"top": "bottom", "bottom": "top", "left": "right", "right": "left"},
    90:  {"top": "right", "bottom": "left", "left": "top", "right": "bottom"},
    270: {"top": "left", "bottom": "right", "left": "bottom", "right": "top"},
}


def design_size(panel_geo: dict) -> tuple:
    """The upright size the artwork for this panel is drawn in."""
    return (panel_geo.get("dw", panel_geo["w"]), panel_geo.get("dh", panel_geo["h"]))


def _placement(panel_geo: dict) -> str:
    """SVG transform that turns upright design space into the flat layout."""
    x, y, w, h = panel_geo["x"], panel_geo["y"], panel_geo["w"], panel_geo["h"]
    rot = int(panel_geo.get("rotate", 0)) % 360
    if rot == 180:
        return f"translate({x + w:.2f} {y + h:.2f}) rotate(180)"
    if rot == 90:
        return f"translate({x + w:.2f} {y:.2f}) rotate(90)"
    if rot == 270:
        return f"translate({x:.2f} {y + h:.2f}) rotate(-90)"
    return f"translate({x:.2f} {y:.2f})"


# Print finishes: effects applied on top of the ink. Each becomes its own
# plate for the printer, and the 3D preview shows it as metal, gloss or relief.
FINISHES = ("foil", "spot_uv", "emboss")
_FINISH_WORDS = {
    "foil": "foil", "hot_foil": "foil", "hotfoil": "foil", "gold_foil": "foil",
    "silver_foil": "foil", "cold_foil": "foil", "metallic": "foil",
    "spot_uv": "spot_uv", "spotuv": "spot_uv", "uv": "spot_uv", "spot_gloss": "spot_uv",
    "gloss": "spot_uv", "varnish": "spot_uv",
    "emboss": "emboss", "embossed": "emboss", "deboss": "emboss", "debossed": "emboss",
    "blind_emboss": "emboss", "raised": "emboss",
}


def layer_finishes(layer: dict) -> set:
    """The finishes a layer asks for: "foil", ["foil", "emboss"], "spot UV"..."""
    raw = layer.get("finish")
    if isinstance(raw, str):
        words = re.split(r"[,+/&]|\band\b", raw.lower())
    elif isinstance(raw, (list, tuple)):
        words = [str(w).lower() for w in raw]
    else:
        return set()
    out = set()
    for word in words:
        key = re.sub(r"[\s-]+", "_", word.strip())
        if key in _FINISH_WORDS:
            out.add(_FINISH_WORDS[key])
    return out


def finishes_in(spec: dict, panel_ids=None) -> set:
    found = set()
    for panel_id, panel in ((spec or {}).get("panels") or {}).items():
        if panel_ids is not None and panel_id not in panel_ids:
            continue
        for layer in panel.get("layers") or []:
            if layer.get("visible") is not False:
                found |= layer_finishes(layer)
    return found


def _finish_paint(finish: str, on: str, off: str):
    """Paint one finish for a mask or a plate.

    Foil is opaque and goes on after the ink, so anything designed to sit on
    top of it must be knocked out of the foil: those layers paint "off".
    Varnish and emboss cover whatever is printed beneath and above them, so
    other layers are simply left out (None) and the shapes add up.
    """
    if finish == "foil":
        return lambda layer: on if "foil" in layer_finishes(layer) else off
    return lambda layer: on if finish in layer_finishes(layer) else None


def _recolor(defs: list, color: str, made: dict) -> str:
    """A filter that turns whatever a layer draws into one flat colour, edges
    and all, so text, patterns and images can all be masks."""
    if color not in made:
        fid = "rc" + color.lstrip("#")
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        defs.append(
            f'<filter id="{fid}" filterUnits="userSpaceOnUse" x="-2000" y="-2000" '
            f'width="5000" height="5000" color-interpolation-filters="sRGB">'
            f'<feColorMatrix type="matrix" values="0 0 0 0 {r:.4f} 0 0 0 0 {g:.4f} '
            f'0 0 0 0 {b:.4f} 0 0 0 1 0"/></filter>')
        made[color] = fid
    return made[color]


def render_panel(spec: dict, panel_geo: dict, defs: list, assets: dict = None,
                 clip: bool = True, bleed: dict = None, paint=None,
                 ground: str = None) -> str:
    """Render one panel, upright design space turned into place on the sheet.

    paint, if given, recolours every layer: paint(layer) -> "#rrggbb". With
    ground as the background colour, that makes finish masks and plates.
    """
    panels = spec.get("panels") or {}
    panel = panels.get(panel_geo["id"]) or panels.get(panel_geo.get("face")) or {}
    fonts = spec.get("fonts") or {}
    ctx = {"fonts": fonts, "assets": assets or {}}

    rot = int(panel_geo.get("rotate", 0)) % 360
    edge_map = _EDGE_MAP.get(rot, _EDGE_MAP[0])
    flat_bleed = bleed or {}
    bleed = {edge: flat_bleed.get(edge_map[edge], 0) for edge in edge_map}

    w, h = design_size(panel_geo)
    bl, br = bleed.get("left", 0), bleed.get("right", 0)
    bt, bb = bleed.get("top", 0), bleed.get("bottom", 0)

    cid = "c" + uuid.uuid4().hex[:8]
    out = []
    recolored = ctx.setdefault("_recolored", {})
    if clip:
        defs.append(f'<clipPath id="{cid}"><rect x="{-bl:.2f}" y="{-bt:.2f}" '
                    f'width="{w + bl + br:.2f}" height="{h + bt + bb:.2f}"/></clipPath>')
    clip_attr = f' clip-path="url(#{cid})"' if clip else ""
    out.append(f'<g id="panel-{_esc(panel_geo["id"])}" data-panel="{_esc(panel_geo["id"])}" '
               f'transform="{_placement(panel_geo)}"{clip_attr}>')

    bg = ground if paint else panel.get("background")
    if bg:
        bg_layer = bg if isinstance(bg, dict) else {"fill": bg}
        if "fill" not in bg_layer and bg_layer.get("type") in ("linear", "radial"):
            bg_layer = {"fill": bg_layer}
        bg_layer = {**bg_layer, "x": -bl, "y": -bt, "w": w + bl + br, "h": h + bt + bb}
        out.append(_render_rect(bg_layer, defs))

    for layer in panel.get("layers") or []:
        if layer.get("visible") is False:
            continue
        renderer = _RENDERERS.get(layer.get("type"))
        if not renderer:
            continue
        try:
            drawn = _extend_into_bleed(layer, w, h, bleed) if bleed else layer
            tint = ""
            if paint:
                colour = paint(layer)
                if colour is None:
                    continue
                tint = f' filter="url(#{_recolor(defs, colour, recolored)})"'
            out.append(f'<g data-layer="{_esc(layer.get("id",""))}" '
                       f'data-name="{_esc(layer.get("name",""))}"{tint}>'
                       + renderer(drawn, defs, ctx) + "</g>")
        except Exception as exc:  # one broken layer must never kill the page
            out.append(f'<!-- layer error {_esc(layer.get("id"))}: {_esc(exc)} -->')
    out.append("</g>")
    return "".join(out)


def render_flat(spec: dict, dieline: dict, assets: dict = None,
                only_printable: bool = True, with_bleed: bool = True,
                paint=None, ground: str = None) -> str:
    """Every panel rendered onto the flat layout, without the SVG wrapper."""
    defs: list = []
    body = []
    edges = bleed_edges(dieline) if with_bleed else {}
    for panel_geo in dieline["panels"]:
        if only_printable and not panel_geo.get("printable", True):
            continue
        body.append(render_panel(spec, panel_geo, defs, assets,
                                 bleed=edges.get(panel_geo["id"]),
                                 paint=paint, ground=ground))
    return f"<defs>{''.join(defs)}</defs>" + "".join(body)


def _style_block(font_css: str = None) -> str:
    """Embedded fonts, falling back to an @import.

    The content has to sit in CDATA. When an SVG is loaded as an image the
    browser parses it strictly as XML, and a bare & in a font URL makes the
    file invalid -- the texture would come out black.
    """
    return f"<style><![CDATA[{font_css or FONT_IMPORT}]]></style>"


def render_panel_svg(spec: dict, panel_geo: dict, assets: dict = None,
                     scale: float = 8.0, font_css: str = None,
                     finish_mask: bool = False) -> str:
    """One panel upright as a standalone SVG, for instance as a 3D texture.

    finish_mask=True draws the finish maps instead, three tiles stacked top
    to bottom: foil, spot UV, emboss, each white where it applies. The 3D view
    turns them into metal, gloss and relief.
    """
    defs: list = []
    w, h = design_size(panel_geo)
    upright = {**panel_geo, "x": 0, "y": 0, "w": w, "h": h, "dw": w, "dh": h, "rotate": 0}
    if not finish_mask:
        body = render_panel(spec, upright, defs, assets)
        return (
            f'<svg {SVG_NS} width="{w*scale:.0f}" height="{h*scale:.0f}" '
            f'viewBox="0 0 {w:.2f} {h:.2f}">'
            f'{_style_block(font_css)}<defs>{"".join(defs)}</defs>'
            f'<rect width="{w:.2f}" height="{h:.2f}" fill="#ffffff"/>{body}</svg>'
        )
    tiles = []
    for i, finish in enumerate(FINISHES):
        tiles.append(f'<g transform="translate(0 {i * h:.2f})">'
                     + render_panel(spec, upright, defs, assets,
                                    paint=_finish_paint(finish, "#ffffff", "#000000"),
                                    ground="#000000")
                     + "</g>")
    return (
        f'<svg {SVG_NS} width="{w*scale:.0f}" height="{3*h*scale:.0f}" '
        f'viewBox="0 0 {w:.2f} {3*h:.2f}">'
        f'{_style_block(font_css)}<defs>{"".join(defs)}</defs>'
        f'<rect width="{w:.2f}" height="{3*h:.2f}" fill="#000000"/>{"".join(tiles)}</svg>'
    )


def plate_svg(spec: dict, dieline: dict, finish: str, assets: dict = None,
              font_css: str = None) -> str:
    """One finish as a printer's plate on the flat layout: black where the
    foil, varnish or emboss goes, white elsewhere, in register 1:1 with the
    artwork file."""
    sheet = dieline["sheet"]
    art = render_flat(spec, dieline, assets, paint=_finish_paint(finish, "#000000", "#ffffff"),
                      ground="#ffffff")
    name = {"foil": "Hot foil", "spot_uv": "Spot UV varnish", "emboss": "Emboss"}.get(finish, finish)
    return (
        f'<svg {SVG_NS} width="{sheet["width"]}mm" height="{sheet["height"]}mm" '
        f'viewBox="0 0 {sheet["width"]} {sheet["height"]}">'
        f'<title>{_esc(name)} plate</title>{_style_block(font_css)}'
        f'<rect width="{sheet["width"]}" height="{sheet["height"]}" fill="#fff"/>'
        f'{art}</svg>'
    )


def full_svg(spec: dict, dieline: dict, assets: dict = None,
             with_dieline: bool = True, labels: bool = False,
             font_css: str = None) -> str:
    """Full print SVG: artwork with bleed on the flat layout, optionally with the dieline."""
    from . import dieline as dl

    art = render_flat(spec, dieline, assets)
    sheet = dieline["sheet"]
    if with_dieline:
        svg = dl.to_svg(dieline, show_labels=labels, show_dimensions=True,
                        artwork_svg=art)
        return svg.replace("<title>", _style_block(font_css) + "<title>", 1)
    return (
        f'<svg {SVG_NS} width="{sheet["width"]}mm" height="{sheet["height"]}mm" '
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
            fill = layer.get("fill")
            if not isinstance(fill, str):
                fill = layer.get("color") if isinstance(layer.get("color"), str) else None
            out.append({
                "panel": panel_id,
                "id": layer.get("id"),
                "name": layer.get("name"),
                "type": layer.get("type"),
                "text": layer.get("text"),
                "size": layer.get("size"),
                "font": layer.get("font"),
                "icon": layer.get("icon"),
                "digits": layer.get("digits"),
                "visible": layer.get("visible", True),
                "fill": fill,
            })
    return out


def _index_layers(spec: dict) -> dict:
    by_id = {}
    for panel_id, panel in (spec.get("panels") or {}).items():
        for i, layer in enumerate(panel.get("layers") or []):
            by_id[layer.get("id")] = (panel_id, i, layer)
    return by_id


def apply_patch(spec: dict, patches: list) -> dict:
    """Apply targeted changes to individual layers."""
    applied, failed = [], []
    for patch in patches or []:
        by_id = _index_layers(spec)       # always fresh: earlier patches move things
        op = patch.get("op", "update")
        lid = patch.get("id")
        if op == "add":
            panel_id = patch.get("panel")
            panel = spec.setdefault("panels", {}).setdefault(panel_id, {"layers": []})
            panel.setdefault("layers", []).append(dict(patch.get("layer") or {}))
            ensure_layer_ids(spec)
            applied.append(f"added to {panel_id}")
            continue
        if lid not in by_id:
            failed.append(lid)
            continue
        panel_id, index, layer = by_id[lid]
        layers = spec["panels"][panel_id]["layers"]
        if op == "delete":
            layers.pop(index)
            applied.append(f"{lid} deleted")
        elif op == "move":
            target = max(0, min(len(layers) - 1, int(patch.get("to", index))))
            layers.insert(target, layers.pop(index))
            applied.append(f"{lid} reordered")
        else:
            changes = patch.get("set") or {}
            changes.pop("id", None)
            layer.update(changes)
            applied.append(f"{lid}: {', '.join(changes.keys())}")
    ensure_layer_ids(spec)
    return {"spec": spec, "applied": applied, "not_found": failed}
