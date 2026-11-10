"""Layout checking.

The usual failure in AI-assisted packaging design is not colour, it is
geometry: text overflows its panel, two blocks sit on top of each other, a
line creeps too close to a cut edge.

This module does the arithmetic and hands the agent concrete findings, so it
fixes the specific layers that are wrong instead of guessing.
"""

from __future__ import annotations
from . import artwork

SAFE_MARGIN = 4.0       # mm, minimum distance from text to a panel edge
OVERLAP_TOLERANCE = 0.6  # mm, a slight touch is not a collision


def _text_box(layer: dict, fonts: dict, panel_w: float) -> tuple:
    """Estimated bounding box of a text layer, in mm."""
    key = layer.get("font", "body")
    family, generic = artwork.font_stack(fonts.get(key, key))
    size = float(layer.get("size", 4) or 4)
    weight = layer.get("weight", 400)
    spacing = float(layer.get("letterSpacing", 0) or 0)
    line_height = float(layer.get("lineHeight", 1.25) or 1.25)
    text = str(layer.get("text", "") or "")
    if layer.get("transform") == "uppercase":
        text = text.upper()

    box_w = float(layer.get("w", 0) or 0)
    width_limit = box_w if box_w > 0 else panel_w
    lines = artwork.wrap_text(text, width_limit, family, generic, size, weight, spacing)
    lines = lines or [""]

    height = (len(lines) - 1) * size * line_height + size * 1.22
    if box_w > 0:
        widest = width_limit
    else:
        cw = artwork._char_width(family, generic, size, weight) + spacing
        widest = max((len(l) for l in lines), default=0) * cw

    x = float(layer.get("x", 0) or 0)
    y = float(layer.get("y", 0) or 0)
    align = layer.get("align", "left")
    if box_w <= 0 and align == "center":
        x -= widest / 2
    elif box_w <= 0 and align == "right":
        x -= widest
    return x, y, widest, height, len(lines)


def _box_of(layer: dict, fonts: dict, panel_w: float):
    if layer.get("type") == "text":
        x, y, w, h, lines = _text_box(layer, fonts, panel_w)
        return x, y, w, h, lines
    return (float(layer.get("x", 0) or 0), float(layer.get("y", 0) or 0),
            float(layer.get("w", 0) or 0), float(layer.get("h", 0) or 0), 0)


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    t = OVERLAP_TOLERANCE
    return not (ax + aw - t <= bx or bx + bw - t <= ax
                or ay + ah - t <= by or by + bh - t <= ay)


def check(spec: dict, dieline: dict) -> dict:
    """Check every panel. Returns findings the agent can act on."""
    fonts = (spec or {}).get("fonts") or {}
    geos = {p["id"]: p for p in dieline.get("panels", [])}
    problems = []

    for panel_id, panel in ((spec or {}).get("panels") or {}).items():
        geo = geos.get(panel_id)
        if not geo:
            continue
        pw, ph = geo["w"], geo["h"]
        texts = []

        for layer in panel.get("layers") or []:
            if layer.get("visible") is False:
                continue
            x, y, w, h, lines = _box_of(layer, fonts, pw)
            name = layer.get("name") or layer.get("id")
            lid = layer.get("id")
            is_text = layer.get("type") == "text"

            if is_text:
                if y + h > ph - 0.5:
                    problems.append({
                        "panel": panel_id, "layer": lid, "name": name,
                        "problem": "overflows the bottom of the panel",
                        "detail": f"needs {lines} lines and ends at "
                                  f"{y + h:.1f} mm, panel is {ph:.0f} mm tall",
                        "fix": "reduce size, shorten the text, lower y, "
                               "or widen w",
                    })
                if x < -0.5 or x + w > pw + 0.5:
                    problems.append({
                        "panel": panel_id, "layer": lid, "name": name,
                        "problem": "overflows the side of the panel",
                        "detail": f"spans x={x:.1f} to {x + w:.1f} mm, "
                                  f"panel is {pw:.0f} mm wide",
                        "fix": "adjust x or w",
                    })
                else:
                    if x < SAFE_MARGIN - 0.3 or x + w > pw - SAFE_MARGIN + 0.3:
                        problems.append({
                            "panel": panel_id, "layer": lid, "name": name,
                            "problem": "too close to the side cut edge",
                            "detail": f"{x:.1f} mm clear on the left, "
                                      f"{pw - (x + w):.1f} mm on the right, "
                                      f"{SAFE_MARGIN:.0f} mm required",
                            "fix": "increase x and reduce w to match",
                        })
                    if y < SAFE_MARGIN - 0.3 or y + h > ph - SAFE_MARGIN + 0.3:
                        problems.append({
                            "panel": panel_id, "layer": lid, "name": name,
                            "problem": "too close to the top or bottom crease",
                            "detail": f"{y:.1f} mm clear above, "
                                      f"{ph - (y + h):.1f} mm below, "
                                      f"{SAFE_MARGIN:.0f} mm required",
                            "fix": "adjust y",
                        })
                size = float(layer.get("size", 4) or 4)
                if size < 1.5:
                    problems.append({
                        "panel": panel_id, "layer": lid, "name": name,
                        "problem": "type too small to print",
                        "detail": f"{size:.2f} mm, the minimum is 1.5 mm",
                        "fix": "set size to at least 1.5",
                    })
                texts.append((lid, name, (x, y, w, h)))

        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if _overlaps(texts[i][2], texts[j][2]):
                    problems.append({
                        "panel": panel_id,
                        "layer": texts[j][0],
                        "name": texts[j][1],
                        "problem": f'overlaps "{texts[i][1]}"',
                        "detail": f"{texts[i][1]} occupies y "
                                  f"{texts[i][2][1]:.1f}-"
                                  f"{texts[i][2][1] + texts[i][2][3]:.1f} mm, "
                                  f"{texts[j][1]} y {texts[j][2][1]:.1f}-"
                                  f"{texts[j][2][1] + texts[j][2][3]:.1f} mm",
                        "fix": "move or shrink one of the two blocks",
                    })

    return {
        "ok": not problems,
        "count": len(problems),
        "findings": problems[:25],
    }


def summary(result: dict) -> str:
    if result["ok"]:
        return "Layout checked, nothing wrong."
    return f'{result["count"]} layout problems found.'
