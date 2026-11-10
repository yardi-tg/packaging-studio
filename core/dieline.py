"""Dieline engine: real cutting contours from geometry, not from AI images.

Everything is in millimetres. Coordinates run x to the right and y downwards,
the same way SVG does. Every dieline returns:

  cut    - cut lines (solid)
  fold   - crease lines (dashed)
  panels - which rectangle of the flat layout is which face of the box, and
           which way up it reads once folded

That last point matters more than it looks. On a tray or a mailer, the front
wall folds up from the base, so its artwork has to stand on its head in the
flat layout or it ends up upside down on the shelf. Each panel therefore
carries a rotation (0, 90, 180 or 270) and a design size: the agent always
designs upright, and the renderer turns the panel into place.

Supported structures:
  ste       folding carton, straight tuck end (the cosmetics standard)
  rte       folding carton, reverse tuck end
  autolock  folding carton with a crash-lock bottom (jars, glass)
  sleeve    sleeve / wrap, open at both ends
  rsc       regular slotted container, FEFCO 0201 (the classic shipping box)
  mailer    mailer box with rolled walls, after FEFCO 0427
  twopiece  two-piece box: base tray and lid tray (gift sets)
  pillow    pillow box
"""

from __future__ import annotations
import html
import math
from collections import Counter


def _x(text) -> str:
    """Escape text for XML."""
    return html.escape(str(text), quote=True)

MARGIN = 14.0          # clear space around the blank
PAD = {"left": 10.0, "top": 0.0, "right": 12.0, "bottom": 22.0}   # room for dimensions
GLUE_ANGLE = 3.0
TUCK_TAPER = 2.5
DUST_ANGLE = 3.5
MIN_GLUE, MAX_GLUE = 10.0, 20.0

BOX_TYPES = {
    "ste": {
        "name": "Folding carton, straight tuck end",
        "short": "Straight Tuck End (STE)",
        "use": "The cosmetics standard: creams, serums, tubes, bottles.",
    },
    "rte": {
        "name": "Folding carton, reverse tuck end",
        "short": "Reverse Tuck End (RTE)",
        "use": "Cheaper to die-cut than STE. Flaps face opposite directions.",
    },
    "sleeve": {
        "name": "Sleeve / wrap",
        "short": "Sleeve",
        "use": "Soap bars, gift sets, wrapping an inner box.",
    },
    "rsc": {
        "name": "Regular slotted container (FEFCO 0201)",
        "short": "RSC",
        "use": "Corrugated shipping box. The classic cardboard carton.",
    },
    "mailer": {
        "name": "Mailer box (FEFCO 0427)",
        "short": "Mailer",
        "use": "Premium shipping box with a hinged lid and rolled walls.",
    },
    "autolock": {
        "name": "Folding carton, crash-lock bottom",
        "short": "Auto-lock bottom",
        "use": "Heavier products: jars, glass bottles. Bottom pops into place.",
    },
    "twopiece": {
        "name": "Two-piece box, lid and base",
        "short": "Lid & base",
        "use": "Gift sets and premium skincare. Lifts off rather than opens.",
    },
    "pillow": {
        "name": "Pillow box",
        "short": "Pillow",
        "use": "Flat products, samples, jewellery, gift inserts.",
    },
}


def _glue_width(h: float) -> float:
    return max(MIN_GLUE, min(MAX_GLUE, round(h * 0.11, 1)))


def _panel(pid, label, x, y, w, h, face=None, rotate=0, printable=True):
    """A rectangle of the flat layout. w and h are as laid flat; dw and dh are
    the upright design size the artwork is drawn in."""
    rotate = int(rotate) % 360
    dw, dh = (h, w) if rotate in (90, 270) else (w, h)
    return {
        "id": pid, "label": label,
        "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2),
        "dw": round(dw, 2), "dh": round(dh, 2),
        "face": face or pid, "rotate": rotate, "printable": printable,
    }


# ---------------------------------------------------------------------------
# Flap geometry
# ---------------------------------------------------------------------------

def _tuck_flap(x0, x1, y_base, depth, direction):
    """Tuck flap. direction -1 is upwards, +1 downwards. The base is a crease."""
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


def _side_flap(x_attach, y0, y1, extent, direction, chamfer=4.0):
    """A flap hinged on a vertical edge. direction -1 points left, +1 right."""
    c = min(chamfer, extent * 0.45, (y1 - y0) * 0.45)
    xo = x_attach + direction * extent
    return [
        (x_attach, y0),
        (xo - direction * c, y0),
        (xo, y0 + c),
        (xo, y1 - c),
        (xo - direction * c, y1),
        (x_attach, y1),
    ]


def _glue_flap_left(x_fold, y0, y1, width):
    x_out = x_fold - width
    return [(x_fold, y0), (x_out, y0 + GLUE_ANGLE),
            (x_out, y1 - GLUE_ANGLE), (x_fold, y1)]


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

def _arc(cx, cy, r, a0, a1, steps=7):
    """Points along a circular arc. Angles in degrees, y pointing down."""
    out = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def _tuck_dims(w, d):
    """Closure geometry for a tuck-end carton, sized the way converters do it."""
    tuck = max(10.0, min(18.0, 0.4 * d + 3.0))       # the tongue that tucks in
    dust = max(6.0, min(w * 0.45, d - 2.0))          # may not meet in the middle
    slit = max(1.5, min(3.0, w * 0.06))              # slit lock at each shoulder
    return tuck, dust, slit


def _closure(x0, x1, y_hinge, d, tuck, slit, direction, cut, fold):
    """Closure panel plus tuck flap, hinged on y_hinge.

    The closure panel is the face you see on top of the box (W x D). The tuck
    flap beyond it slides down inside the front. Short slits along the tuck
    crease at both shoulders catch behind the dust flaps: the slit lock that
    stops the lid springing open.
    """
    sgn = direction
    y_crease = y_hinge + sgn * d
    # closure panel: hinge is a crease, its two side edges are cuts
    fold.append([(x0, y_hinge), (x1, y_hinge)])
    cut.append([(x0, y_hinge), (x0, y_crease)])
    cut.append([(x1, y_hinge), (x1, y_crease)])
    # tuck crease, with the slit locks cut into both ends
    fold.append([(x0 + slit, y_crease), (x1 - slit, y_crease)])
    cut.append([(x0, y_crease), (x0 + slit, y_crease)])
    cut.append([(x1 - slit, y_crease), (x1, y_crease)])
    # tuck flap outline: 0.75 mm narrower each side, rounded nose
    inset = 0.75
    r = min(5.0, tuck * 0.45, (x1 - x0) * 0.2)
    ya = y_crease
    yt = y_crease + sgn * tuck
    left, right = x0 + inset, x1 - inset
    pts = [(x0, ya), (left, ya + sgn * 1.2)]
    if sgn < 0:
        pts += _arc(left + r, yt + r, r, 180, 270)
        pts += _arc(right - r, yt + r, r, 270, 360)
    else:
        pts += _arc(left + r, yt - r, r, 180, 90)
        pts += _arc(right - r, yt - r, r, 90, 0)
    pts += [(right, ya + sgn * 1.2), (x1, ya)]
    cut.append(pts)
    return y_crease, yt


def _dust(a0, a1, y_hinge, depth, direction, cut, fold):
    """Dust flap on a side wall: a small relief at the hinge, rounded corners."""
    sgn = direction
    relief = 1.2
    r = min(4.0, depth * 0.35, (a1 - a0) * 0.3)
    yt = y_hinge + sgn * depth
    pts = [(a0, y_hinge), (a0 + relief, y_hinge + sgn * relief)]
    if sgn < 0:
        pts += _arc(a0 + relief + r, yt + r, r, 180, 270)
        pts += _arc(a1 - relief - r, yt + r, r, 270, 360)
    else:
        pts += _arc(a0 + relief + r, yt - r, r, 180, 90)
        pts += _arc(a1 - relief - r, yt - r, r, 90, 0)
    pts += [(a1 - relief, y_hinge + sgn * relief), (a1, y_hinge)]
    cut.append(pts)
    fold.append([(a0, y_hinge), (a1, y_hinge)])


def _crash_lock(segments, yb1, d, cut, fold):
    """Auto-lock (crash-lock) bottom across the four panels.

    Each wide panel carries a hook flap with a 45-degree crease that lets the
    bottom collapse flat with the carton; each narrow panel carries a glue
    flap with a 45-degree edge. Glued on the folder-gluer, the bottom springs
    into place when the carton is squared up. Drawn to the standard proportions;
    the converter tunes the hook to their machine, as they always do.
    """
    half = d / 2.0
    for kind, x0, x1 in segments:
        fold.append([(x0, yb1), (x1, yb1)])
        if kind == "wide":
            hook = min(half * 0.55, (x1 - x0) * 0.25)
            pts = [(x0, yb1), (x0 + 0.8, yb1 + half + 1.5),
                   (x1 - half - hook * 0.4, yb1 + half + 1.5)]
            pts += _arc(x1 - half + hook * 0.6, yb1 + half + 1.5, hook, 180, 0, 6)
            pts += [(x1 - 1.0, yb1 + half * 0.45), (x1, yb1)]
            cut.append(pts)
            fold.append([(x0, yb1), (x0 + half, yb1 + half)])
        else:
            cut.append([(x0, yb1), (x0 + 0.8, yb1 + half - 0.8),
                        (x1 - half + 0.8, yb1 + half - 0.8), (x1, yb1)])


def _tuck_carton(w, d, h, reverse=False, autolock=False):
    """Tuck-end cartons. Layout: glue flap | back | side | front | side.

    STE: both closures hinge on the back, so both tucks run the same way.
    RTE: the top closes from the back, the bottom from the front.
    Auto-lock: tuck top, crash-lock bottom for heavier products such as jars.
    """
    glue = _glue_width(h)
    tuck, dust, slit = _tuck_dims(w, d)
    top_zone = d + tuck
    bottom_zone = d / 2.0 + 6.0 if autolock else d + tuck

    ox, oy = MARGIN + glue, MARGIN + top_zone
    xb0, xb1 = ox, ox + w
    xl0, xl1 = xb1, xb1 + d
    xf0, xf1 = xl1, xl1 + w
    xr0, xr1 = xf1, xf1 + d
    yb0, yb1 = oy, oy + h

    cut, fold = [], []
    cut.append(_glue_flap_left(xb0, yb0, yb1, glue))
    fold.append([(xb0, yb0), (xb0, yb1)])
    for x in (xl0, xf0, xr0):
        fold.append([(x, yb0), (x, yb1)])
    cut.append([(xr1, yb0), (xr1, yb1)])

    # top: closure + tuck off the back, dust flaps on the sides, front edge open
    _closure(xb0, xb1, yb0, d, tuck, slit, -1, cut, fold)
    for a, b in ((xl0, xl1), (xr0, xr1)):
        _dust(a, b, yb0, dust, -1, cut, fold)
    cut.append([(xf0, yb0), (xf1, yb0)])

    panels = [
        _panel("back", "Back", xb0, yb0, w, h),
        _panel("left", "Left side", xl0, yb0, d, h),
        _panel("front", "Front", xf0, yb0, w, h),
        _panel("right", "Right side", xr0, yb0, d, h),
        # The lid hinges at the back and closes towards you, so seen from the
        # front it reads the other way up to how it lies in the flat.
        _panel("top", "Top", xb0, yb0 - d, w, d, face="top", rotate=180),
        _panel("top_tuck", "Top tuck", xb0, yb0 - d - tuck, w, tuck,
               face="top_tuck", printable=False),
        _panel("glue", "Glue flap", xb0 - glue, yb0, glue, h, face="glue", printable=False),
    ]

    if autolock:
        _crash_lock([("wide", xb0, xb1), ("narrow", xl0, xl1),
                     ("wide", xf0, xf1), ("narrow", xr0, xr1)], yb1, d, cut, fold)
        notes = [
            f"Glue flap {glue} mm, glues behind the right side wall.",
            f"Lid {d:.0f} mm, tuck {tuck:.0f} mm with slit locks; dust flaps {dust:.0f} mm.",
            "Crash-lock bottom: pre-glued, pops open when the carton is squared. "
            "Strong enough for jars and glass. Your converter will tune the hook "
            "geometry to their folder-gluer.",
        ]
        return cut, fold, panels, xr1 + MARGIN, yb1 + bottom_zone + MARGIN, notes

    base = (xf0, xf1) if reverse else (xb0, xb1)
    open_edge = (xb0, xb1) if reverse else (xf0, xf1)
    _closure(base[0], base[1], yb1, d, tuck, slit, +1, cut, fold)
    for a, b in ((xl0, xl1), (xr0, xr1)):
        _dust(a, b, yb1, dust, +1, cut, fold)
    cut.append([(open_edge[0], yb1), (open_edge[1], yb1)])
    panels.insert(5, _panel("bottom", "Bottom", base[0], yb1, w, d, face="bottom"))
    panels.insert(6, _panel("bottom_tuck", "Bottom tuck", base[0], yb1 + d, w, tuck,
                            face="bottom_tuck", printable=False))

    notes = [
        f"Glue flap {glue} mm, glues behind the right side wall.",
        f"Lids {d:.0f} mm with {tuck:.0f} mm tucks and slit locks at the shoulders; "
        f"dust flaps {dust:.0f} mm.",
        "Both closures hinge on the back panel." if not reverse else
        "Top closes from the back, bottom from the front: nests tighter on the "
        "sheet, so it is cheaper to cut.",
    ]
    return cut, fold, panels, xr1 + MARGIN, yb1 + bottom_zone + MARGIN, notes


def _sleeve(w, d, h):
    glue = _glue_width(h)
    ox, oy = MARGIN + glue, MARGIN
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
        _panel("glue", "Glue flap", xf0 - glue, yb0, glue, h, face="glue", printable=False),
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
    ox, oy = MARGIN + glue, MARGIN + flap
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
    for i in range(4):
        a, b = xs[i], xs[i + 1]
        cut.append(_plain_flap(a, b, yb0, flap, -1))
        cut.append(_plain_flap(a, b, yb1, flap, +1))
        fold.append([(a, yb0), (b, yb0)])
        fold.append([(a, yb1), (b, yb1)])

    labels = ["Front", "Right side", "Back", "Left side"]
    ids = ["front", "right", "back", "left"]
    panels = [_panel(ids[i], labels[i], xs[i], yb0, xs[i + 1] - xs[i], h) for i in range(4)]
    panels.append(_panel("top", "Top flap", xs[0], yb0 - flap, w, flap, face="top"))
    panels.append(_panel("bottom", "Bottom flap", xs[0], yb1, w, flap, face="bottom"))
    panels.append(_panel("glue", "Glue flap", xs[0] - glue, yb0, glue, h,
                         face="glue", printable=False))
    notes = [
        f"Flaps are {round(flap, 1)} mm each and meet in the middle.",
        "Corrugated: quote the inside dimension, thickness is added outside.",
        "Budget about 1.5 mm for E-flute, 3 mm for B-flute.",
    ]
    return cut, fold, panels, xs[-1] + MARGIN, yb1 + flap + MARGIN, notes


def _mailer(w, d, h):
    """After FEFCO 0427: a centre spine of lid tuck, lid, back wall, base and a
    rolled front wall, with rolled side walls off the base and dust ears on
    the lid and back wall. No glue: it folds and locks."""
    roll = max(4.0, h - 1.5)
    lid_tuck = max(12.0, min(h - 2.0, 40.0))
    lid_ear = max(8.0, min(h - 3.0, 25.0))
    back_ear = max(8.0, min(d - 4.0, 30.0))
    side = h
    reach = max(side + roll, back_ear, lid_ear)
    x0 = MARGIN + reach
    x1 = x0 + w

    y = MARGIN
    y_lid = y + lid_tuck
    y_back = y_lid + d
    y_base = y_back + h
    y_front = y_base + d
    y_roll = y_front + h
    y_end = y_roll + roll

    cut, fold = [], []
    cut.append(_plain_flap(x0, x1, y_lid, lid_tuck, -1, chamfer=4.0))
    for yy in (y_lid, y_back, y_base, y_front):
        fold.append([(x0, yy), (x1, yy)])

    # lid ears and back-wall ears
    for xa, direction in ((x0, -1), (x1, +1)):
        cut.append(_side_flap(xa, y_lid, y_back, lid_ear, direction, chamfer=6.0))
        fold.append([(xa, y_lid), (xa, y_back)])
        cut.append(_side_flap(xa, y_back, y_base, back_ear, direction, chamfer=5.0))
        fold.append([(xa, y_back), (xa, y_base)])

    # rolled side walls off the base: outer wall, then the inner half that
    # folds over the rim and locks into the base
    for xa, direction in ((x0, -1), (x1, +1)):
        xs = xa + direction * side
        fold.append([(xa, y_base), (xa, y_front)])
        fold.append([(xs, y_base + 1.5), (xs, y_front - 1.5)])
        cut.append([(xa, y_base), (xs, y_base), (xs, y_base + 1.5)])
        cut.append(_side_flap(xs, y_base + 1.5, y_front - 1.5, roll, direction, chamfer=3.0))
        cut.append([(xs, y_front - 1.5), (xs, y_front), (xa, y_front)])

    # rolled front wall
    cut.append([(x0, y_front), (x0, y_roll), (x0 + 1, y_roll)])
    cut.append([(x1, y_front), (x1, y_roll), (x1 - 1, y_roll)])
    fold.append([(x0 + 1, y_roll), (x1 - 1, y_roll)])
    cut.append(_plain_flap(x0 + 1, x1 - 1, y_roll, roll, +1, chamfer=3.0))

    panels = [
        _panel("top", "Lid", x0, y_lid, w, d, face="top", rotate=180),
        _panel("back", "Back wall", x0, y_back, w, h, face="back"),
        _panel("bottom", "Base", x0, y_base, w, d, face="bottom"),
        _panel("front", "Front wall", x0, y_front, w, h, face="front", rotate=180),
        # The base is seen from below once folded, so the wall on the right of
        # the flat layout ends up on the left of the box.
        _panel("left", "Left wall", x1, y_base, side, d, face="left", rotate=90),
        _panel("right", "Right wall", x0 - side, y_base, side, d, face="right", rotate=270),
        _panel("lid_tuck", "Lid tuck", x0, y_lid - lid_tuck, w, lid_tuck,
               face="lid_tuck", printable=False),
        _panel("front_inner", "Inner front", x0 + 1, y_roll, w - 2, roll,
               face="front_inner", printable=False),
    ]
    notes = [
        "No glue. The walls roll over and lock into the base.",
        f"Lid tuck {round(lid_tuck, 1)} mm, lid ears {round(lid_ear, 1)} mm, "
        f"back ears {round(back_ear, 1)} mm.",
        "The inside faces are not printed in this layout. Inside print is a "
        "second pass on the reverse of the board.",
    ]
    return cut, fold, panels, x1 + reach + MARGIN, y_end + MARGIN, notes


def _tray(ox, oy, tw, td, th):
    """One glued-corner tray: floor, four walls, corner tabs on the long walls.
    Returns cut, fold and the five rectangles as (x, y, w, h)."""
    fx0, fy0 = ox + th, oy + th
    fx1, fy1 = fx0 + tw, fy0 + td
    tab = max(6.0, min(th - 1.0, td * 0.45, 20.0))
    ch = min(2.5, th * 0.2)

    contour = [
        (fx0, oy), (fx1, oy),
        (fx1 + tab, oy + ch), (fx1 + tab, fy0),
        (fx1 + th, fy0), (fx1 + th, fy1),
        (fx1 + tab, fy1), (fx1 + tab, fy1 + th - ch),
        (fx1, fy1 + th), (fx0, fy1 + th),
        (fx0 - tab, fy1 + th - ch), (fx0 - tab, fy1),
        (fx0 - th, fy1), (fx0 - th, fy0),
        (fx0 - tab, fy0), (fx0 - tab, oy + ch),
        (fx0, oy),
    ]
    cut = [contour,
           [(fx1, fy0), (fx1 + tab, fy0)], [(fx1, fy1), (fx1 + tab, fy1)],
           [(fx0 - tab, fy1), (fx0, fy1)], [(fx0 - tab, fy0), (fx0, fy0)]]
    fold = [
        [(fx0, fy0), (fx1, fy0)], [(fx0, fy1), (fx1, fy1)],
        [(fx0, fy0), (fx0, fy1)], [(fx1, fy0), (fx1, fy1)],
        [(fx0, oy), (fx0, fy0)], [(fx1, oy), (fx1, fy0)],
        [(fx0, fy1), (fx0, fy1 + th)], [(fx1, fy1), (fx1, fy1 + th)],
    ]
    rects = {
        "floor": (fx0, fy0, tw, td),
        "top_wall": (fx0, oy, tw, th),
        "bottom_wall": (fx0, fy1, tw, th),
        "left_wall": (fx0 - th, fy0, th, td),
        "right_wall": (fx1, fy0, th, td),
    }
    return cut, fold, rects, tab


def _twopiece(w, d, h):
    """Base tray plus a lid tray a little larger, side by side on one sheet."""
    clear = 1.5
    lid_h = round(max(12.0, min(h * 0.35, 30.0)), 1)
    gap = 14.0
    bx, by = MARGIN, MARGIN
    base_cut, base_fold, base, tab = _tray(bx, by, w, d, h)
    lw, ld = w + 2 * clear, d + 2 * clear
    lx = bx + w + 2 * h + gap
    lid_cut, lid_fold, lid, _ = _tray(lx, by, lw, ld, lid_h)

    def P(pid, label, rect, face, rot, printable=True):
        x, y, rw, rh = rect
        return _panel(pid, label, x, y, rw, rh, face=face, rotate=rot, printable=printable)

    # Rules of thumb that fall out of the folding:
    #  - every wall's top edge is its free edge (the rim), so artwork reads
    #    away from the floor on the base and towards the floor on the lid
    #  - the base is seen from below, so its left and right swap over
    panels = [
        P("bottom", "Base floor", base["floor"], "bottom", 0),
        P("back", "Back wall", base["top_wall"], "back", 0),
        P("front", "Front wall", base["bottom_wall"], "front", 180),
        P("right", "Right wall", base["left_wall"], "right", 270),
        P("left", "Left wall", base["right_wall"], "left", 90),
        P("top", "Lid top", lid["floor"], "top", 0),
        P("lid_back", "Lid back", lid["top_wall"], "lid_back", 180),
        P("lid_front", "Lid front", lid["bottom_wall"], "lid_front", 0),
        P("lid_left", "Lid left", lid["left_wall"], "lid_left", 90),
        P("lid_right", "Lid right", lid["right_wall"], "lid_right", 270),
    ]
    notes = [
        f"Lid is {clear} mm larger per side than the base, {lid_h} mm deep.",
        f"Corner tabs {round(tab, 1)} mm glue inside the side walls.",
        "For a rigid gift box, use these as wraps over 1.5-2 mm greyboard.",
    ]
    sheet_w = lx + lw + 2 * lid_h + MARGIN
    sheet_h = by + max(d + 2 * h, ld + 2 * lid_h) + MARGIN
    return (base_cut + lid_cut, base_fold + lid_fold, panels, sheet_w, sheet_h, notes)


def _pillow(w, d, h):
    glue = _glue_width(h)
    arc = d * 0.75
    ox, oy = MARGIN + glue, MARGIN + arc
    xf0, xf1 = ox, ox + w
    xb0, xb1 = xf1, xf1 + w
    yb0, yb1 = oy, oy + h

    cut, fold = [], []
    cut.append(_glue_flap_left(xf0, yb0, yb1, glue))
    fold.append([(xf0, yb0), (xf0, yb1)])
    fold.append([(xb0, yb0), (xb0, yb1)])
    cut.append([(xb1, yb0), (xb1, yb1)])

    def arc_pts(x_start, x_end, y_base, direction):
        return [(x_start + (x_end - x_start) * i / 24,
                 y_base + direction * arc * math.sin(math.pi * i / 24))
                for i in range(25)]

    for a, b in ((xf0, xf1), (xb0, xb1)):
        cut.append(arc_pts(a, b, yb0, -1))
        cut.append(arc_pts(a, b, yb1, +1))
        fold.append([(a, yb0), (b, yb0)])
        fold.append([(a, yb1), (b, yb1)])

    panels = [
        _panel("front", "Front", xf0, yb0, w, h),
        _panel("back", "Back", xb0, yb0, w, h),
        _panel("glue", "Glue flap", xf0 - glue, yb0, glue, h, face="glue", printable=False),
    ]
    notes = [
        "The curved edges interlock as the box is folded.",
        f"Arc height {round(arc, 1)} mm.",
        "Only suitable for flat products.",
    ]
    return cut, fold, panels, xb1 + MARGIN, yb1 + arc + MARGIN, notes


_BUILDERS = {
    "ste": lambda w, d, h: _tuck_carton(w, d, h, reverse=False),
    "rte": lambda w, d, h: _tuck_carton(w, d, h, reverse=True),
    "autolock": lambda w, d, h: _tuck_carton(w, d, h, autolock=True),
    "sleeve": _sleeve,
    "rsc": _rsc,
    "mailer": _mailer,
    "twopiece": _twopiece,
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

    # Shift everything to leave room for the dimension lines.
    dx, dy = PAD["left"], PAD["top"]

    def shift(seq):
        return [[(round(x + dx, 2), round(y + dy, 2)) for x, y in path] for path in seq]

    cut, fold = shift(cut), shift(fold)
    for p in panels:
        p["x"] = round(p["x"] + dx, 2)
        p["y"] = round(p["y"] + dy, 2)

    xs = [x for path in cut for x, _ in path]
    ys = [y for path in cut for _, y in path]
    blank = {"width": round(max(xs) - min(xs), 1), "height": round(max(ys) - min(ys), 1),
             "x": round(min(xs), 2), "y": round(min(ys), 2)}

    return {
        "box_type": box_type,
        "box_name": BOX_TYPES[box_type]["name"],
        "box_short": BOX_TYPES[box_type]["short"],
        "dims": {"width": w, "depth": d, "height": h},
        "bleed": bleed,
        "sheet": {"width": round(sheet_w + dx + PAD["right"], 2),
                  "height": round(sheet_h + dy + PAD["bottom"], 2)},
        "blank": blank,
        "cut": cut,
        "fold": fold,
        "panels": panels,
        "notes": notes,
        "material_hint": _material_hint(box_type, w, d, h),
    }


def _material_hint(box_type, w, d, h):
    volume = w * d * h / 1000.0
    if box_type in ("rsc", "mailer"):
        return "Corrugated E-flute (1.5 mm) for light products, B-flute (3 mm) for shipping."
    if box_type == "autolock":
        return "Folding boxboard GC1 400 gsm; E-flute micro-flute for glass over 250 g."
    if box_type == "twopiece":
        return "Folding boxboard 400 gsm, or printed paper wrapped over 1.5 mm greyboard."
    if volume < 60:
        return "Folding boxboard GC1/GZ, 300-350 gsm."
    if volume < 400:
        return "Folding boxboard GC1, 350-400 gsm."
    return "Folding boxboard GC1 400 gsm, or micro-flute."


def area_m2(dieline: dict) -> float:
    b = dieline.get("blank") or dieline["sheet"]
    return round(b["width"] * b["height"] / 1_000_000.0, 5)


# ---------------------------------------------------------------------------
# Dimension lines, shared by the SVG and the PDF writer
# ---------------------------------------------------------------------------

def dimension_marks(dieline: dict) -> list:
    """Measured chains around the blank: panel widths and overall size below,
    overall height on the left, wall height on the right."""
    blank = dieline.get("blank")
    if not blank:
        return []
    x0, y0 = blank["x"], blank["y"]
    x1, y1 = x0 + blank["width"], y0 + blank["height"]
    marks = []

    walls = [p for p in dieline["panels"] if p.get("printable", True)]
    rows = Counter(round(p["y"], 1) for p in walls)
    if rows:
        row_y = rows.most_common(1)[0][0]
        row = sorted((p for p in walls if round(p["y"], 1) == row_y), key=lambda p: p["x"])
        if len(row) >= 2:
            for p in row:
                marks.append({"kind": "h", "a": p["x"], "b": p["x"] + p["w"],
                              "at": y1 + 6.5, "label": f'{p["w"]:.0f}'})
            first = row[0]
            marks.append({"kind": "v", "a": first["y"], "b": first["y"] + first["h"],
                          "at": x1 + 7.5, "label": f'{first["h"]:.0f}'})

    marks.append({"kind": "h", "a": x0, "b": x1, "at": y1 + 14,
                  "label": f"{x1 - x0:.0f}"})
    marks.append({"kind": "v", "a": y0, "b": y1, "at": x0 - 7.5,
                  "label": f"{y1 - y0:.0f}"})
    return marks


def _dims_svg(marks: list) -> str:
    out = ['<g id="dimensions" stroke="#8a94a6" stroke-width="0.2" fill="none" '
           'font-family="Helvetica, Arial, sans-serif">']
    tick = 1.1
    for m in marks:
        a, b, at = m["a"], m["b"], m["at"]
        if m["kind"] == "h":
            out.append(f'<path d="M{a:.2f} {at:.2f} H{b:.2f}'
                       f' M{a - tick:.2f} {at + tick:.2f} L{a + tick:.2f} {at - tick:.2f}'
                       f' M{b - tick:.2f} {at + tick:.2f} L{b + tick:.2f} {at - tick:.2f}'
                       f' M{a:.2f} {at - 2.2:.2f} V{at + 2.2:.2f}'
                       f' M{b:.2f} {at - 2.2:.2f} V{at + 2.2:.2f}"/>')
            out.append(f'<text x="{(a + b) / 2:.2f}" y="{at - 1.3:.2f}" font-size="2.5" '
                       f'fill="#5b6576" stroke="none" text-anchor="middle">{m["label"]}</text>')
        else:
            out.append(f'<path d="M{at:.2f} {a:.2f} V{b:.2f}'
                       f' M{at - tick:.2f} {a + tick:.2f} L{at + tick:.2f} {a - tick:.2f}'
                       f' M{at - tick:.2f} {b + tick:.2f} L{at + tick:.2f} {b - tick:.2f}'
                       f' M{at - 2.2:.2f} {a:.2f} H{at + 2.2:.2f}'
                       f' M{at - 2.2:.2f} {b:.2f} H{at + 2.2:.2f}"/>')
            cy = (a + b) / 2
            out.append(f'<text x="{at - 1.3:.2f}" y="{cy:.2f}" font-size="2.5" '
                       f'fill="#5b6576" stroke="none" text-anchor="middle" '
                       f'transform="rotate(-90 {at - 1.3:.2f} {cy:.2f})">{m["label"]}</text>')
    out.append("</g>")
    return "".join(out)


# ---------------------------------------------------------------------------
# SVG output
# ---------------------------------------------------------------------------

def _path_d(points, close=False):
    if not points:
        return ""
    d = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    for x, y in points[1:]:
        d += f" L {x:.2f} {y:.2f}"
    return d + (" Z" if close else "")


def to_svg(dieline: dict, show_labels: bool = True,
           show_dimensions: bool = True, artwork_svg: str = "") -> str:
    sheet = dieline["sheet"]
    W, H = sheet["width"], sheet["height"]
    dims = dieline["dims"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}" '
        f'data-units="mm" data-box-type="{dieline["box_type"]}">',
        f'<title>Dieline {_x(dieline["box_short"])} {dims["width"]:.0f}x{dims["depth"]:.0f}x'
        f'{dims["height"]:.0f} mm</title>',
        f'<rect x="0" y="0" width="{W:.2f}" height="{H:.2f}" fill="#ffffff"/>',
    ]
    if artwork_svg:
        out.append(f'<g id="artwork">{artwork_svg}</g>')

    if show_labels:
        out.append('<g id="panel-labels" font-family="Helvetica, Arial, sans-serif">')
        for p in dieline["panels"]:
            if p["w"] < 8 or p["h"] < 8:
                continue
            cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
            span = p.get("dw", p["w"])
            size = max(2.2, min(4.0, span / 10))
            rot = p.get("rotate", 0)
            turn = f' transform="rotate({rot} {cx:.2f} {cy:.2f})"' if rot else ""
            # The label turns with the panel, so the drawing shows which way
            # up each face reads once folded.
            out.append(f'<g{turn}>'
                       f'<text x="{cx:.2f}" y="{cy:.2f}" font-size="{size:.2f}" fill="#8f99a8" '
                       f'text-anchor="middle">{_x(p["label"])}</text>'
                       f'<text x="{cx:.2f}" y="{cy + size * 1.3:.2f}" font-size="{size * 0.78:.2f}" '
                       f'fill="#b3bcc8" text-anchor="middle">'
                       f'{p.get("dw", p["w"]):.0f} x {p.get("dh", p["h"]):.0f} mm'
                       f'{"  ↑" if rot else ""}</text></g>')
        out.append("</g>")

    out.append('<g id="fold-lines" stroke="#2f7fe8" stroke-width="0.35" fill="none" '
               'stroke-dasharray="3.2 2.2" stroke-linecap="round">')
    for path in dieline["fold"]:
        out.append(f'<path d="{_path_d(path)}"/>')
    out.append("</g>")

    out.append('<g id="cut-lines" stroke="#e5243b" stroke-width="0.45" fill="none" '
               'stroke-linejoin="round" stroke-linecap="round">')
    for path in dieline["cut"]:
        out.append(f'<path d="{_path_d(path)}"/>')
    out.append("</g>")

    if show_dimensions:
        out.append(_dims_svg(dimension_marks(dieline)))
        blank = dieline.get("blank") or sheet
        out.append(
            '<g id="legend" font-family="Helvetica, Arial, sans-serif">'
            f'<text x="{PAD["left"]:.2f}" y="{H - 7:.2f}" font-size="3.2" fill="#334155">'
            f'{_x(dieline["box_name"])} &#183; inside W {dims["width"]:.0f} x D {dims["depth"]:.0f} '
            f'x H {dims["height"]:.0f} mm &#183; blank {blank["width"]:.0f} x '
            f'{blank["height"]:.0f} mm</text>'
            f'<text x="{PAD["left"]:.2f}" y="{H - 2.6:.2f}" font-size="2.7" fill="#64748b">'
            'Red = cut &#183; blue dashed = crease &#183; '
            f'bleed {dieline["bleed"]:.0f} mm &#183; all in mm, scale 1:1</text>'
            "</g>"
        )
    out.append("</svg>")
    return "\n".join(out)
