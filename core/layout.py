"""Layout checking.

The usual failure in AI-assisted packaging design is not colour, it is
geometry: text overflows its panel, two blocks sit on top of each other, a
line creeps too close to a cut edge, small type disappears into a background
of almost the same tone, a barcode is printed too small or in a colour no
scanner can read.

This module does the arithmetic and hands the agent concrete findings, so it
fixes the specific layers that are wrong instead of guessing. Findings must be
fixed; suggestions are worth considering.
"""

from __future__ import annotations
from . import artwork

SAFE_MARGIN = 4.0          # mm, text to panel edge
ICON_MARGIN = 3.0          # mm, icons and barcodes to panel edge
OVERLAP_TOLERANCE = 0.6    # mm, a slight touch is not a collision
EAN_MIN_W, EAN_MIN_H = 29.8, 20.0   # 80 % magnification, the scannable floor


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def _hex(value):
    if isinstance(value, dict):
        stops = value.get("stops") or value.get("colors") or []
        if stops:
            first = stops[0]
            value = first.get("color") if isinstance(first, dict) else first
        else:
            return None
    if not isinstance(value, str):
        return None
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        return tuple(int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None


def _luminance(rgb) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    ca, cb = _hex(a), _hex(b)
    if not ca or not cb:
        return None
    la, lb = _luminance(ca), _luminance(cb)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _f(layer, key, default=0.0):
    try:
        value = layer.get(key, default)
        return float(default if value is None else value)
    except (TypeError, ValueError):
        return float(default)


def _ink_box(layer: dict, fonts: dict, panel_w: float):
    """Bounding box of what actually gets printed, in mm, plus line count."""
    kind = layer.get("type")
    x, y = _f(layer, "x"), _f(layer, "y")
    if kind == "text":
        m = artwork.text_metrics(layer, fonts)
        box_w = _f(layer, "w")
        ink_w = min(m["widest"], box_w) if box_w > 0 else m["widest"]
        align = layer.get("align", "left")
        if box_w > 0:
            if align == "center":
                x += (box_w - ink_w) / 2
            elif align == "right":
                x += box_w - ink_w
        else:
            if align == "center":
                x -= ink_w / 2
            elif align == "right":
                x -= ink_w
        box = (x, y, ink_w, m["height"])
        rot = _f(layer, "rotate") % 180
        if abs(rot - 90) < 5:
            # turned a quarter: same centre, width and height swap
            lw = _f(layer, "w") or ink_w
            lh = _f(layer, "h") or m["height"]
            cx, cy = _f(layer, "x") + lw / 2, _f(layer, "y") + lh / 2
            box = (cx - m["height"] / 2, cy - ink_w / 2, m["height"], ink_w)
        return box, len(m["lines"]), m["size"]
    w = _f(layer, "w")
    h = _f(layer, "h", w if kind in ("icon", "circle_text", "star") else 0)
    return (x, y, w, h), 0, None


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    t = OVERLAP_TOLERANCE
    return not (ax + aw - t <= bx or bx + bw - t <= ax
                or ay + ah - t <= by or by + bh - t <= ay)


def _contains(outer, point) -> bool:
    ox, oy, ow, oh = outer
    px, py = point
    return ox <= px <= ox + ow and oy <= py <= oy + oh


def _backdrop(panel: dict, layers_below: list, fonts: dict, pw: float, centre):
    """The colour a text layer actually sits on."""
    for layer in reversed(layers_below):
        if layer.get("visible") is False:
            continue
        if layer.get("type") not in ("rect", "arch", "ellipse", "circle", "star"):
            continue
        if _f(layer, "opacity", 1) < 0.85:
            continue
        colour = layer.get("fill")
        if not _hex(colour):
            continue
        box, _, _ = _ink_box(layer, fonts, pw)
        if _contains(box, centre):
            return colour
    return panel.get("background")


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def check(spec: dict, dieline: dict) -> dict:
    """Check every panel. Returns findings to fix and suggestions to weigh."""
    fonts = (spec or {}).get("fonts") or {}
    geos = {p["id"]: p for p in dieline.get("panels", [])}
    findings, suggestions = [], []
    seen_types = set()

    def add(panel_id, layer, problem, detail, fix):
        findings.append({
            "panel": panel_id, "layer": layer.get("id"),
            "name": layer.get("name") or layer.get("id"),
            "problem": problem, "detail": detail, "fix": fix,
        })

    for panel_id, panel in ((spec or {}).get("panels") or {}).items():
        geo = geos.get(panel_id)
        if not geo:
            continue
        pw, ph = artwork.design_size(geo)
        blocking = []    # boxes that must not collide: text and barcodes
        layers = panel.get("layers") or []

        for index, layer in enumerate(layers):
            if layer.get("visible") is False:
                continue
            kind = layer.get("type")
            seen_types.add(kind)
            if kind == "icon":
                seen_types.add("icon:" + str(layer.get("icon")))
            box, lines, size = _ink_box(layer, fonts, pw)
            x, y, w, h = box

            if kind == "text":
                if y + h > ph - 0.5:
                    add(panel_id, layer, "overflows the bottom of the panel",
                        f"needs {lines} lines and ends at {y + h:.1f} mm, panel is {ph:.0f} mm tall",
                        "reduce size, shorten the text, raise y, widen w, or set fit:'shrink' with an h")
                elif y < SAFE_MARGIN - 0.3 or y + h > ph - SAFE_MARGIN + 0.3:
                    add(panel_id, layer, "too close to the top or bottom crease",
                        f"{y:.1f} mm clear above, {ph - (y + h):.1f} mm below, {SAFE_MARGIN:.0f} mm required",
                        "adjust y")
                if x < -0.5 or x + w > pw + 0.5:
                    add(panel_id, layer, "overflows the side of the panel",
                        f"spans x={x:.1f} to {x + w:.1f} mm, panel is {pw:.0f} mm wide",
                        "adjust x or w, or reduce size")
                elif x < SAFE_MARGIN - 0.3 or x + w > pw - SAFE_MARGIN + 0.3:
                    add(panel_id, layer, "too close to the side edge",
                        f"{x:.1f} mm clear on the left, {pw - (x + w):.1f} mm on the right, "
                        f"{SAFE_MARGIN:.0f} mm required",
                        "move inward or narrow the column")
                if size is not None and size < artwork.MIN_TYPE_MM - 0.01:
                    add(panel_id, layer, "type too small to print",
                        f"{size:.2f} mm, the minimum is {artwork.MIN_TYPE_MM} mm",
                        f"set size to at least {artwork.MIN_TYPE_MM}")

                colour = layer.get("fill", layer.get("color", "#111111"))
                ground = _backdrop(panel, layers[:index], fonts, pw, (x + w / 2, y + h / 2))
                ratio = contrast(colour, ground)
                if ratio is not None and size is not None:
                    needed = 3.0 if size < 3.0 else 2.0
                    if ratio < needed:
                        add(panel_id, layer, "too little contrast to read",
                            f"{ratio:.1f}:1 against its background, {needed:.0f}:1 is the floor at this size",
                            "darken or lighten the text, or put it on a calmer area")
                blocking.append((layer, box))

            elif kind == "barcode":
                if w < EAN_MIN_W or h < EAN_MIN_H:
                    add(panel_id, layer, "barcode too small to scan reliably",
                        f"{w:.1f} x {h:.1f} mm, the floor is {EAN_MIN_W} x {EAN_MIN_H} mm",
                        "set w to at least 30 and h to at least 21 (37.3 x 25.9 is 100 %)")
                bars = layer.get("color", "#111111")
                ground = layer.get("background", "#ffffff")
                bc, gc = _hex(bars), _hex(ground)
                if bc and gc and (_luminance(bc) > 0.25 or _luminance(gc) < 0.45):
                    add(panel_id, layer, "barcode colours will not scan",
                        "scanners read dark bars on a light ground; red, orange and pale bars are invisible to them",
                        "use near-black or a deep navy or green for bars, on white or cream")
                if x < ICON_MARGIN or y < ICON_MARGIN or x + w > pw - ICON_MARGIN or y + h > ph - ICON_MARGIN:
                    add(panel_id, layer, "barcode too close to an edge",
                        f"keep {ICON_MARGIN:.0f} mm clear around it",
                        "move it inward")
                blocking.append((layer, box))

            elif kind in ("icon", "circle_text"):
                if x < ICON_MARGIN - 0.3 or y < ICON_MARGIN - 0.3 or \
                        x + w > pw - ICON_MARGIN + 0.3 or y + h > ph - ICON_MARGIN + 0.3:
                    add(panel_id, layer, f"{kind.replace('_', ' ')} too close to an edge",
                        f"keep {ICON_MARGIN:.0f} mm clear", "move it inward")
                if kind == "icon" and w < 4.0:
                    add(panel_id, layer, "icon too small to read", f"{w:.1f} mm wide",
                        "make it at least 4 mm, 6 mm for the PAO jar")

        for i in range(len(blocking)):
            for j in range(i + 1, len(blocking)):
                (la, a), (lb, b) = blocking[i], blocking[j]
                if _overlaps(a, b):
                    add(panel_id, lb, f'overlaps "{la.get("name") or la.get("id")}"',
                        f'{la.get("name")} occupies y {a[1]:.1f}-{a[1] + a[3]:.1f} mm, '
                        f'{lb.get("name")} y {b[1]:.1f}-{b[1] + b[3]:.1f} mm',
                        "move or shrink one of the two")

    if spec and spec.get("panels"):
        if "barcode" not in seen_types:
            suggestions.append("No barcode yet. Retail needs an EAN-13: add a barcode "
                               "layer, at least 30 x 21 mm, on a side or the back.")
        if "icon:pao" not in seen_types:
            suggestions.append("No PAO symbol. Cosmetics with a shelf life over 30 months "
                               "need the open-jar mark, e.g. icon 'pao' with label '12M'.")

    return {
        "ok": not findings,
        "count": len(findings),
        "findings": findings[:25],
        "suggestions": suggestions,
    }


def summary(result: dict) -> str:
    if result["ok"]:
        return "Layout checked, nothing wrong."
    return f'{result["count"]} layout problems found.'
