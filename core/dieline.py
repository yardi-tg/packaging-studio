"""Dieline engine: real cutting contours from geometry, not from AI images.

Everything is in millimetres. Coordinates run x to the right and y downwards,
the same way SVG does. Every dieline returns:

  cut    - cut lines (solid)
  fold   - crease lines (dashed)
  panels - which rectangle of the flat layout is which face of the box.
           The artwork places itself from this, and the 3D preview takes its
           textures from it.

Supported structures:
  ste     folding carton, straight tuck end (the cosmetics standard)
  rte     folding carton, reverse tuck end
  sleeve  sleeve / wrap, open at both ends
  rsc     regular slotted container, FEFCO 0201 (the classic shipping box)
  mailer  mailer box, FEFCO 0427 (premium shipping box)
  pillow  pillow box
"""

from __future__ import annotations
import math

MARGIN = 14.0          # margin around the layout, leaves room for dimensions
GLUE_ANGLE = 3.0       # chamfer on the glue flap
TUCK_TAPER = 2.5       # how much the tuck flap tapers
DUST_ANGLE = 3.5       # chamfer on the dust flaps
MIN_GLUE = 10.0
MAX_GLUE = 20.0

BOX_TYPES = {
    "ste": {
        "name": "Folding carton, straight tuck end",
        "short": "Straight Tuck End (STE)",
        "use": "The cosmetics standard: creams, serums, tubes, bottles.",
        "closed_both_ends": True,
    },
    "rte": {
        "name": "Folding carton, reverse tuck end",
        "short": "Reverse Tuck End (RTE)",
        "use": "Cheaper to die-cut than STE. Flaps face opposite directions.",
        "closed_both_ends": True,
    },
    "sleeve": {
        "name": "Sleeve / wrap",
        "short": "Sleeve",
        "use": "Soap bars, gift sets, wrapping an inner box.",
        "closed_both_ends": False,
    },
    "rsc": {
        "name": "Regular slotted container (FEFCO 0201)",
        "short": "RSC",
        "use": "Corrugated shipping box. The classic cardboard carton.",
        "closed_both_ends": True,
    },
    "mailer": {
        "name": "Mailer box (FEFCO 0427)",
        "short": "Mailer",
        "use": "Premium shipping box with a hinged lid, printable inside.",
        "closed_both_ends": True,
    },
    "pillow": {
        "name": "Pillow box",
        "short": "Pillow",
        "use": "Flat products, samples, jewellery, gift inserts.",
        "closed_both_ends": True,
    },
}


def _glue_width(h: float) -> float:
    return max(MIN_GLUE, min(MAX_GLUE, round(h * 0.11, 1)))


def _panel(pid, label, x, y, w, h, face=None, rotate=0, printable=True):
    return {
        "id": pid,
        "label": label,
        "x": round(x, 2),
        "y": round(y, 2),
        "w": round(w, 2),
        "h": round(h, 2),
        "face": face or pid,
        "rotate": rotate,
        "printable": printable,
    }


# ---------------------------------------------------------------------------
# Flap geometry
# ---------------------------------------------------------------------------

def _tuck_flap(x0, x1, y_base, depth, direction):
    """Tuck flap. direction -1 is upwards, +1 downwards.
    Returns the cut edges; the base of the flap is a crease."""
    yt = y_base + direction * depth
    nose = min(depth * 0.35, 6.0)
    return [
        (x0, y_base),
        (x0 + TUCK_TAPER, y_base + direction * (depth - nose)),
        (x0 + TUCK_TAPER + nose * 0.5, yt),
        (x1 - TUCK_TAPER - nose * 0.5, yt),
        (x1 - TUCK_TAPER, y_base + direction * (depth - nose)),
        (x1, y_base),
    ]


def _dust_flap(x0, x1, y_base, depth, direction):
    """Dust flap on the side panels."""
    yt = y_base + direction * depth
    return [
        (x0, y_base),
        (x0, y_base + direction * (depth - DUST_ANGLE)),
        (x0 + DUST_ANGLE, yt),
        (x1 - DUST_ANGLE, yt),
        (x1, y_base + direction * (depth - DUST_ANGLE)),
        (x1, y_base),
    ]


def _plain_flap(x0, x1, y_base, depth, direction, chamfer=0.0):
    yt = y_base + direction * depth
    if chamfer <= 0:
        return [(x0, y_base), (x0, yt), (x1, yt), (x1, y_base)]
    return [
        (x0, y_base),
        (x0, y_base + direction * (depth - chamfer)),
        (x0 + chamfer, yt),
        (x1 - chamfer, yt),
        (x1, y_base + direction * (depth - chamfer)),
        (x1, y_base),
    ]


def _glue_flap_left(x_fold, y0, y1, width):
    """Glue flap to the left of the first panel, drawn as a trapezoid."""
    x_out = x_fold - width
    return [
        (x_fold, y0),
        (x_out, y0 + GLUE_ANGLE),
        (x_out, y1 - GLUE_ANGLE),
        (x_fold, y1),
    ]


# ---------------------------------------------------------------------------
# Bauformen
# ---------------------------------------------------------------------------

def _tuck_carton(w, d, h, reverse=False):
    """STE and RTE. Layout: glue flap | back | side | front | side."""
    glue = _glue_width(h)
    tuck = max(6.0, d - 1.5)
    dust = max(5.0, d - 3.0)
    top_zone = max(tuck, dust)
    bot_zone = max(tuck, dust)

    ox = MARGIN + glue
    oy = MARGIN + top_zone

    xb0 = ox
    xb1 = xb0 + w            # back
    xl0, xl1 = xb1, xb1 + d  # left side
    xf0, xf1 = xl1, xl1 + w  # front
    xr0, xr1 = xf1, xf1 + d  # right side

    yb0 = oy
    yb1 = oy + h

    cut, fold = [], []

    # glue flap
    cut.append(_glue_flap_left(xb0, yb0, yb1, glue))
    fold.append([(xb0, yb0), (xb0, yb1)])

    # vertical creases between panels
    for x in (xl0, xf0, xr0):
        fold.append([(x, yb0), (x, yb1)])
    # outer edge on the right
    cut.append([(xr1, yb0), (xr1, yb1)])

    # --- top ---
    top_tuck_panel = (xb0, xb1)                 # top tuck comes off the back
    cut.append(_tuck_flap(*top_tuck_panel, yb0, tuck, -1))
    fold.append([(top_tuck_panel[0], yb0), (top_tuck_panel[1], yb0)])
    for (a, b) in ((xl0, xl1), (xr0, xr1)):     # dust flaps on top
        cut.append(_dust_flap(a, b, yb0, dust, -1))
        fold.append([(a, yb0), (b, yb0)])
    cut.append([(xf0, yb0), (xf1, yb0)])        # front top stays an open cut

    # --- bottom ---
    bot_tuck_panel = (xf0, xf1) if reverse else (xb0, xb1)
    bot_open_panel = (xb0, xb1) if reverse else (xf0, xf1)
    cut.append(_tuck_flap(*bot_tuck_panel, yb1, tuck, +1))
    fold.append([(bot_tuck_panel[0], yb1), (bot_tuck_panel[1], yb1)])
    for (a, b) in ((xl0, xl1), (xr0, xr1)):
        cut.append(_dust_flap(a, b, yb1, dust, +1))
        fold.append([(a, yb1), (b, yb1)])
    cut.append([(bot_open_panel[0], yb1), (bot_open_panel[1], yb1)])

    panels = [
        _panel("back", "Back", xb0, yb0, w, h),
        _panel("left", "Left side", xl0, yb0, d, h),
        _panel("front", "Front", xf0, yb0, w, h),
        _panel("right", "Right side", xr0, yb0, d, h),
        _panel("top", "Top (tuck)", top_tuck_panel[0], yb0 - tuck,
               top_tuck_panel[1] - top_tuck_panel[0], tuck, face="top"),
        _panel("bottom", "Bottom (tuck)", bot_tuck_panel[0], yb1,
               bot_tuck_panel[1] - bot_tuck_panel[0], tuck, face="bottom"),
        _panel("glue", "Glue flap", xb0 - glue, yb0, glue, h,
               face="glue", printable=False),
    ]

    sheet_w = xr1 + MARGIN
    sheet_h = yb1 + bot_zone + MARGIN
    notes = [
        f"Glue flap {glue} mm, glues behind the right side wall.",
        f"Tuck flaps {tuck} mm, dust flaps {dust} mm.",
        "Both tucks run in the same direction."
        if not reverse else
        "Top tuck comes off the back, bottom tuck off the front. "
        "Cheaper to die-cut.",
    ]
    return cut, fold, panels, sheet_w, sheet_h, notes


def _sleeve(w, d, h):
    """Sleeve: four panels, open at top and bottom."""
    glue = _glue_width(h)
    ox = MARGIN + glue
    oy = MARGIN
    xf0, xf1 = ox, ox + w
    xl0, xl1 = xf1, xf1 + d
    xb0, xb1 = xl1, xl1 + w
    xr0, xr1 = xb1, xb1 + d
    yb0, yb1 = oy, oy + h

    cut, fold = [], []
    cut.append(_glue_flap_left(xf0, yb0, yb1, glue))
    fold.append([(xf0, yb0), (xf0, yb1)])
    for x in (xl0, xb0, xr0):
        fold.append([(x, yb0), (x, yb1)])
    cut.append([(xr1, yb0), (xr1, yb1)])
    cut.append([(xf0, yb0), (xr1, yb0)])
    cut.append([(xf0, yb1), (xr1, yb1)])

    panels = [
        _panel("front", "Front", xf0, yb0, w, h),
        _panel("left", "Left side", xl0, yb0, d, h),
        _panel("back", "Back", xb0, yb0, w, h),
        _panel("right", "Right side", xr0, yb0, d, h),
        _panel("glue", "Glue flap", xf0 - glue, yb0, glue, h,
               face="glue", printable=False),
    ]
    notes = [
        f"Glue flap {glue} mm.",
        "Open at both ends. The product or an inner box slides through.",
        "Allow roughly 0.3 mm per crease for material thickness.",
    ]
    return cut, fold, panels, xr1 + MARGIN, yb1 + MARGIN, notes


def _rsc(w, d, h):
    """FEFCO 0201. Top and bottom flaps are each half the depth."""
    glue = _glue_width(h)
    flap = d / 2.0
    ox = MARGIN + glue
    oy = MARGIN + flap
    xs = [ox]
    for width in (w, d, w, d):
        xs.append(xs[-1] + width)
    yb0, yb1 = oy, oy + h

    cut, fold = [], []
    cut.append(_glue_flap_left(xs[0], yb0, yb1, glue))
    fold.append([(xs[0], yb0), (xs[0], yb1)])
    for x in xs[1:-1]:
        fold.append([(x, yb0), (x, yb1)])
    cut.append([(xs[-1], yb0), (xs[-1], yb1)])

    # flaps top and bottom, with slits between them
    for i in range(4):
        a, b = xs[i], xs[i + 1]
        cut.append(_plain_flap(a, b, yb0, flap, -1))
        cut.append(_plain_flap(a, b, yb1, flap, +1))
        fold.append([(a, yb0), (b, yb0)])
        fold.append([(a, yb1), (b, yb1)])

    labels = ["Front", "Right side", "Back", "Left side"]
    ids = ["front", "right", "back", "left"]
    panels = [
        _panel(ids[i], labels[i], xs[i], yb0, xs[i + 1] - xs[i], h)
        for i in range(4)
    ]
    panels.append(_panel("top", "Top flap", xs[0], yb0 - flap, w, flap, face="top"))
    panels.append(_panel("bottom", "Bottom flap", xs[0], yb1, w, flap, face="bottom"))
    panels.append(_panel("glue", "Glue flap", xs[0] - glue, yb0, glue, h,
                         face="glue", printable=False))
    notes = [
        f"Flaps are {round(flap,1)} mm each and meet in the middle.",
        "Corrugated: quote the inside dimension, thickness is added outside.",
        "Budget about 1.5 mm for E-flute, 3 mm for B-flute.",
    ]
    return cut, fold, panels, xs[-1] + MARGIN, yb1 + flap + MARGIN, notes


def _mailer(w, d, h):
    """FEFCO 0427, simplified: base, front, lid with a front tuck,
    side walls with fold-in wings."""
    wing = max(14.0, min(d, h) * 0.55)       # side wings
    lid_tuck = max(12.0, h * 0.85)
    ox = MARGIN + wing
    oy = MARGIN

    # vertical order: lid tuck | lid | back wall | base | front wall
    y = oy
    y_lidtuck = y;            y += lid_tuck
    y_lid = y;                y += d
    y_back = y;               y += h
    y_base = y;               y += d
    y_front = y;              y += h
    sheet_h = y + MARGIN

    x0, x1 = ox, ox + w
    cut, fold = [], []

    # outer contour either side of the centre strip
    cut.append([(x0, y_lidtuck), (x1, y_lidtuck)])
    cut.append([(x0, y_lidtuck), (x0, y_lid)])
    cut.append([(x1, y_lidtuck), (x1, y_lid)])
    cut.append([(x0, y_front + h), (x1, y_front + h)])

    for yy in (y_lid, y_back, y_base, y_front):
        fold.append([(x0, yy), (x1, yy)])

    # side wings on the walls
    def wing_pair(y_top, height, chamfer=4.0):
        left = [
            (x0, y_top),
            (x0 - height + chamfer, y_top),
            (x0 - height, y_top + chamfer),
            (x0 - height, y_top + height - chamfer),
            (x0 - height + chamfer, y_top + height),
            (x0, y_top + height),
        ]
        right = [
            (x1, y_top),
            (x1 + height - chamfer, y_top),
            (x1 + height, y_top + chamfer),
            (x1 + height, y_top + height - chamfer),
            (x1 + height - chamfer, y_top + height),
            (x1, y_top + height),
        ]
        return left, right

    for y_top, hh in ((y_back, h), (y_front, h)):
        lf, rf = wing_pair(y_top, min(wing, hh))
        cut.append(lf)
        cut.append(rf)
        fold.append([(x0, y_top), (x0, y_top + hh)])
        fold.append([(x1, y_top), (x1, y_top + hh)])
    # lid and base: their side edges are cuts
    for y_top, hh in ((y_lid, d), (y_base, d)):
        cut.append([(x0, y_top), (x0, y_top + hh)])
        cut.append([(x1, y_top), (x1, y_top + hh)])

    panels = [
        _panel("top", "Lid", x0, y_lid, w, d, face="top"),
        _panel("back", "Back wall", x0, y_back, w, h, face="back"),
        _panel("bottom", "Base (printable inside)", x0, y_base, w, d, face="bottom"),
        _panel("front", "Front wall", x0, y_front, w, h, face="front"),
        _panel("lid_tuck", "Lid tuck", x0, y_lidtuck, w, lid_tuck,
               face="lid_tuck", printable=False),
        _panel("left", "Left wall", x0 - min(wing, h), y_front, min(wing, h), h,
               face="left"),
        _panel("right", "Right wall", x1, y_front, min(wing, h), h,
               face="right"),
    ]
    notes = [
        "No glue. The mailer folds and tucks together.",
        f"Lid tuck {round(lid_tuck,1)} mm, side wings {round(wing,1)} mm.",
        "The inside of the base and lid is what the customer sees on opening. "
        "Worth printing.",
    ]
    return cut, fold, panels, x1 + wing + MARGIN, sheet_h, notes


def _pillow(w, d, h):
    """Pillow box: two panels plus curved tuck edges."""
    glue = _glue_width(h)
    arc = d * 0.75
    ox = MARGIN + glue
    oy = MARGIN + arc
    xf0, xf1 = ox, ox + w
    xb0, xb1 = xf1, xf1 + w
    yb0, yb1 = oy, oy + h

    cut, fold = [], []
    cut.append(_glue_flap_left(xf0, yb0, yb1, glue))
    fold.append([(xf0, yb0), (xf0, yb1)])
    fold.append([(xb0, yb0), (xb0, yb1)])
    cut.append([(xb1, yb0), (xb1, yb1)])

    def arc_pts(x_start, x_end, y_base, direction):
        pts = []
        steps = 24
        for i in range(steps + 1):
            t = i / steps
            px = x_start + (x_end - x_start) * t
            py = y_base + direction * arc * math.sin(math.pi * t)
            pts.append((px, py))
        return pts

    for (a, b) in ((xf0, xf1), (xb0, xb1)):
        cut.append(arc_pts(a, b, yb0, -1))
        cut.append(arc_pts(a, b, yb1, +1))
        fold.append([(a, yb0), (b, yb0)])
        fold.append([(a, yb1), (b, yb1)])

    panels = [
        _panel("front", "Front", xf0, yb0, w, h),
        _panel("back", "Back", xb0, yb0, w, h),
        _panel("glue", "Glue flap", xf0 - glue, yb0, glue, h,
               face="glue", printable=False),
    ]
    notes = [
        "The curved edges interlock as the box is folded.",
        f"Arc height {round(arc,1)} mm.",
        "Only suitable for flat products.",
    ]
    return cut, fold, panels, xb1 + MARGIN, yb1 + arc + MARGIN, notes


_BUILDERS = {
    "ste": lambda w, d, h: _tuck_carton(w, d, h, reverse=False),
    "rte": lambda w, d, h: _tuck_carton(w, d, h, reverse=True),
    "sleeve": _sleeve,
    "rsc": _rsc,
    "mailer": _mailer,
    "pillow": _pillow,
}


def build(box_type: str, width: float, depth: float, height: float,
          bleed: float = 3.0) -> dict:
    """Build the whole dieline. Dimensions in mm, inside measurements."""
    box_type = (box_type or "ste").lower().strip()
    if box_type not in _BUILDERS:
        box_type = "ste"
    w, d, h = [max(5.0, float(v)) for v in (width, depth, height)]
    cut, fold, panels, sheet_w, sheet_h, notes = _BUILDERS[box_type](w, d, h)

    def rnd(seq):
        return [[(round(x, 2), round(y, 2)) for x, y in path] for path in seq]

    return {
        "box_type": box_type,
        "box_name": BOX_TYPES[box_type]["name"],
        "box_short": BOX_TYPES[box_type]["short"],
        "dims": {"width": w, "depth": d, "height": h},
        "bleed": bleed,
        "sheet": {"width": round(sheet_w, 2), "height": round(sheet_h, 2)},
        "cut": rnd(cut),
        "fold": rnd(fold),
        "panels": panels,
        "notes": notes,
        "material_hint": _material_hint(box_type, w, d, h),
    }


def _material_hint(box_type, w, d, h):
    volume = w * d * h / 1000.0  # cm3
    if box_type in ("rsc", "mailer"):
        return ("Corrugated E-flute (1.5 mm) for light products, "
                "B-flute (3 mm) for shipping.")
    if volume < 60:
        return "Folding boxboard GC1/GZ, 300-350 gsm."
    if volume < 400:
        return "Folding boxboard GC1, 350-400 gsm."
    return "Folding boxboard GC1 400 gsm, or micro-flute."


def area_m2(dieline: dict) -> float:
    s = dieline["sheet"]
    return round(s["width"] * s["height"] / 1_000_000.0, 5)


# ---------------------------------------------------------------------------
# SVG output
# ---------------------------------------------------------------------------

def _path_d(points, close=False):
    if not points:
        return ""
    d = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    for x, y in points[1:]:
        d += f" L {x:.2f} {y:.2f}"
    if close:
        d += " Z"
    return d


def to_svg(dieline: dict, show_labels: bool = True,
           show_dimensions: bool = True, artwork_svg: str = "") -> str:
    sheet = dieline["sheet"]
    W, H = sheet["width"], sheet["height"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}" '
        f'data-units="mm" data-box-type="{dieline["box_type"]}">',
        f'<title>Dieline {dieline["box_short"]} '
        f'{dieline["dims"]["width"]}x{dieline["dims"]["depth"]}x'
        f'{dieline["dims"]["height"]} mm</title>',
        '<rect x="0" y="0" width="%.2f" height="%.2f" fill="#ffffff"/>' % (W, H),
    ]

    if artwork_svg:
        out.append('<g id="artwork">')
        out.append(artwork_svg)
        out.append("</g>")

    if show_labels:
        out.append('<g id="panel-labels" font-family="Helvetica, Arial, sans-serif">')
        for p in dieline["panels"]:
            if p["w"] < 8 or p["h"] < 8:
                continue
            cx = p["x"] + p["w"] / 2
            cy = p["y"] + p["h"] / 2
            size = max(2.4, min(4.2, p["w"] / 10))
            out.append(
                f'<text x="{cx:.2f}" y="{cy:.2f}" font-size="{size:.2f}" '
                f'fill="#9aa4b2" text-anchor="middle" opacity="0.85">{p["label"]}</text>'
            )
            out.append(
                f'<text x="{cx:.2f}" y="{cy + size * 1.3:.2f}" font-size="{size * 0.8:.2f}" '
                f'fill="#b9c2cf" text-anchor="middle" opacity="0.8">'
                f'{p["w"]:.0f} x {p["h"]:.0f} mm</text>'
            )
        out.append("</g>")

    # crease lines
    out.append(
        '<g id="fold-lines" stroke="#0a7cff" stroke-width="0.35" fill="none" '
        'stroke-dasharray="3.2 2.2" stroke-linecap="round">'
    )
    for path in dieline["fold"]:
        out.append(f'<path d="{_path_d(path)}"/>')
    out.append("</g>")

    # cut lines
    out.append(
        '<g id="cut-lines" stroke="#e5243b" stroke-width="0.45" fill="none" '
        'stroke-linejoin="round" stroke-linecap="round">'
    )
    for path in dieline["cut"]:
        out.append(f'<path d="{_path_d(path)}"/>')
    out.append("</g>")

    if show_dimensions:
        d = dieline["dims"]
        out.append(
            '<g id="legend" font-family="Helvetica, Arial, sans-serif" font-size="3.4">'
            f'<text x="{MARGIN*0.3:.2f}" y="{H - 7:.2f}" fill="#334155">'
            f'{dieline["box_name"]} &#183; W {d["width"]:.0f} x D {d["depth"]:.0f} '
            f'x H {d["height"]:.0f} mm &#183; sheet {W:.0f} x {H:.0f} mm</text>'
            f'<text x="{MARGIN*0.3:.2f}" y="{H - 2.5:.2f}" fill="#64748b" font-size="2.9">'
            'Red = cut &#183; blue dashed = crease &#183; '
            f'bleed {dieline["bleed"]:.0f} mm &#183; all in mm, scale 1:1</text>'
            "</g>"
        )

    out.append("</svg>")
    return "\n".join(out)
