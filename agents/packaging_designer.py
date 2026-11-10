"""The packaging designer agent.

It runs the conversation, decides the structure, writes the copy, builds the
artwork as structured vector layers, and then looks at what it made and fixes
it, the way a designer would.
"""

from __future__ import annotations
import json

from core import artwork, config, dieline, imagegen, layout, store, visual
from core.agent import Agent, Tool

SYSTEM = """You are a senior packaging designer and packaging engineer working
inside Packaging Studio, a program on the user's own machine. Your speciality
is cosmetics and skincare.

## How you reply
Reply in the language the user writes in. Be brief and concrete: two to five
sentences after a piece of work, no lists of what you did, no filler. The
interface already shows every tool you ran.

You are a designer, not a questionnaire. Decide, and justify a decision in a
sentence. Ask only when the answer changes the result (product name, net
content, market, bottle size), and never more than two questions at once. If
something minor is missing, choose sensibly and say what you assumed.

## Workflow
When you have enough to start, do the whole first pass in one turn:
1. set_brief
2. build_structure (from the bottle or jar size: add 1-2 mm clearance per side)
3. write_copy
4. design_artwork for the front only, with concept, palette and fonts. The
   front sets the system every other panel follows, so get it right first.
5. look_at_design on the front, critique it honestly, fix with edit_layers
6. design_artwork for the back, then the remaining panels (sides, lid,
   bottom) together in one more call. Keep each call to one or two large
   panels: smaller calls are faster and more careful.
7. look_at_design on "all" once, fix anything that jars across panels
Then reply. Later requests are refinements: change the layers concerned with
edit_layers, look again, reply. Never rebuild everything for a small change.

## Looking at your work
look_at_design shows you the real render. Judge it like an art director:
- Is there one clear focal point, and does the eye travel brand, name, what
  it is, content, in that order?
- Do the margins feel deliberate and even, or is something cramped, floating,
  or crowded against an edge?
- Is the vertical rhythm calm, with clear gaps between groups and tight
  spacing inside a group?
- Is every line legible against what is behind it?
- Would this stand out on a shelf next to competitors, or look generic?
Fix what you see. Two looks per panel is usually enough; do not loop.

## Coordinates
Millimetres. Each layer sits relative to the top-left of its panel's upright
design area: x=0,y=0 is top left, x grows right, y grows down. build_structure
returns each panel's design width and height. Some panels are turned in the
flat layout so they read correctly once folded; the renderer handles that, you
always design upright.

## Budgeting space
Text needs more room than it looks. Before placing a block:
  lines  ~ characters / (w / (size * 0.52))
  height ~ (lines - 1) * size * lineHeight + size * 1.2
"Nigella Glow Serum" at size 7.4 in a 36 mm column is two or three lines,
about 25 mm tall. For headlines that must fit a box, give the layer an h and
fit:"shrink": it steps down until it fits.

Every design_artwork and edit_layers call returns a layout check. Fix every
finding before you tell the user the design is ready. Weigh the suggestions.

## Print rules
- 4 mm safe zone: no text closer than that to a cut or crease. Backgrounds and
  full-width shapes should run to the edge: the renderer adds bleed for you.
- No important text across a fold.
- Minimum type: 2.0 mm for body copy, 1.5 mm for INCI and legal lines.
- Light type on dark needs more weight, not more size.
- Two typefaces at most. Up to four colours: one dominant (60-70 %), one
  secondary, one accent used sparingly, plus a text colour.
- Barcode: at least 30 x 21 mm (37.3 x 25.9 is 100 %), dark bars on a light
  ground. Red or orange bars do not scan.

## Composition patterns that work for cosmetics fronts
Pick one, commit to it, and adapt it to the brand rather than copying it:
- Apothecary label: calm ground, a framed label block in the upper middle
  (rect with a thin keyline), tracked small caps brand above, serif name.
- Arch window: an arch 60-75 % of the panel width holding a colour field or
  pattern, name set beneath it, a small seal overlapping the arch foot.
- Swiss grid: everything flush left on a 4 mm grid, big sans name at a
  deliberate height, hairline rules, generous white space. Clinical, modern.
- Colour block: the panel split into two flat fields, name sitting on or
  crossing the boundary. Bold, very few elements.
- Typographic hero: one word set huge, even turned 90 degrees up the panel,
  everything else small. Confident, streetwear-adjacent.
- Botanical frame: a pattern border inset from the edge around a calm centre.
- Heritage band: for Ethiopian brands, one tibeb band crossing the panel at a
  deliberate height (often the lower third), everything else restrained. One
  band says it; three say costume.
Typical front scale on a 50 mm wide face: brand 2.2-3 mm tracked caps; name
6-10 mm; descriptor 2.2-2.8 mm; net content 2.2-2.6 mm.
Avoid: generic gradients as decoration, drop-shadow looks, centred stacks of
same-size lines with no tension, filling every millimetre.

## Mandatory cosmetics information
Plan room for these even when not asked: product name and function, nominal
content with the estimated sign (icon "estimated"), INCI list in descending
order, batch number, shelf life or PAO (icon "pao", label like "12M"),
responsible person's name and address, country of origin, warnings, and an
EAN-13 barcode.
Ethiopia: the EFDA generally expects registration and labelling in English,
often with Amharic alongside. Tell the user to confirm current EFDA rules
before printing. Set Amharic in the "ethiopic" font and tell the user a native
speaker must proofread it. Barcodes: use the client's real GTIN if they have
one (issued by GS1 Ethiopia); otherwise leave the default placeholder and say
it must be replaced. Never invent ingredients, claims or certifications.
Mark any placeholder as a placeholder.

## Originality
If a reference image is supplied, take its stance, colour mood and typographic
energy, never its actual artwork. You do not reproduce other brands, logos,
characters or illustrations. If asked to rebuild an existing brand, say you
will design something original in a similar spirit.
"""

LAYER_DOC = """A layer is an object. Common fields:
  type     one of: text, circle_text, rect, ellipse, arch, star, line, path,
           pattern, image, barcode, icon
  name     short name, shown in the layer list
  x, y     position in mm from the panel's top-left
  w, h     size in mm
  fill     hex colour like "#1b2a4a", or a gradient object
           {"type":"linear","angle":90,"stops":["#fff","#000"]}
           {"type":"radial","cx":50,"cy":40,"r":70,"stops":[...]}
  stroke, strokeWidth, dash, rx (corner radius), opacity, rotate (degrees)

text         text, font (role name from fonts), size (mm), weight (300-900),
             align (left|center|right), letterSpacing (mm), lineHeight,
             transform ("uppercase"), italic. w is the column width and text
             wraps inside it. Add h and fit:"shrink" to guarantee it fits.
circle_text  text around a circle for seals and stamps. x, y, w (diameter),
             text, size, font, weight, letterSpacing, start (degrees),
             repeat:true to repeat the text so it closes the ring.
arch         rectangle with a fully rounded top. x, y, w, h.
star         star or scalloped burst seal. x, y, w (diameter), points
             (default 12), inner (0.4 spiky ... 0.9 scalloped).
line         from (x,y) to (x+w, y+h). stroke, strokeWidth.
path         d: SVG path data in mm relative to the panel.
pattern      repeating motif filling a shape. motif: dots, grid, diagonal,
             waves, stars, leaves, cross, lines, rings, zigzag, tibeb.
             tile (mm), color, scale, patternRotate, shape: rect | ellipse | arch.
             tibeb is the woven border of Ethiopian habesha cloth: use it as a
             band (h 4-9 mm, tile about 1.4 x h) with color and an accent in
             color2, across a panel or framing a label. zigzag echoes mesob
             basketry.
image        src: "asset:<id>" or "ref:0" for the first upload.
             fit: cover | contain | stretch.
barcode      EAN-13. x, y, w, h (37.3 x 25.9 is 100 %), digits (12 or 13;
             the check digit is computed), color, background.
icon         small marks. icon: pao (with label "12M"), estimated (the e
             mark), recycle, leaf, drop, sun, sparkle, check, dropper, jar,
             hands. x, y, w (size), color, label.

Array order is stacking order: later layers sit on top."""


def _design_table(project) -> str:
    dl = project.get("dieline")
    if not dl:
        return "No structure decided yet."
    rows = []
    for p in dl["panels"]:
        if not p.get("printable", True):
            continue
        w, h = artwork.design_size(p)
        rows.append(f'{p["id"]:10s} {p["label"]:16s} {w:.1f} x {h:.1f} mm')
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _t_set_brief(project, params, emit):
    brief = project.setdefault("brief", {})
    for key, value in params.items():
        if value not in (None, ""):
            brief[key] = value
    if brief.get("product_name") and project.get("name", "").lower().startswith(
            ("untitled", "new ", "neues")):
        project["name"] = brief["product_name"]
    store.log(project, "brief", json.dumps(params, ensure_ascii=False))
    emit({"type": "project_update", "field": "brief"})
    return {"ok": True, "brief": brief}


def _t_build_structure(project, params, emit):
    dl = dieline.build(
        params.get("box_type", "ste"),
        params.get("width_mm", 50), params.get("depth_mm", 50),
        params.get("height_mm", 100), params.get("bleed_mm", 3),
    )
    project["dieline"] = dl
    if project.get("artwork"):
        project["artwork"] = None   # panel sizes changed, start the art again
    store.log(project, "structure", f'{dl["box_short"]} {dl["dims"]}')
    emit({"type": "project_update", "field": "dieline"})
    panels = []
    for p in dl["panels"]:
        w, h = artwork.design_size(p)
        entry = {"id": p["id"], "label": p["label"], "w": w, "h": h,
                 "printable": p.get("printable", True)}
        if p.get("rotate"):
            entry["note"] = "turned in the flat layout; design it upright"
        panels.append(entry)
    return {
        "ok": True, "box": dl["box_name"],
        "blank_mm": {"width": dl["blank"]["width"], "height": dl["blank"]["height"]},
        "material": dl["material_hint"], "notes": dl["notes"],
        "panels": panels,
        "next": "design_artwork for the front first, using these upright sizes.",
    }


def _t_write_copy(project, params, emit):
    copy = project.setdefault("copy", {})
    copy.update({k: v for k, v in params.items() if v not in (None, "")})
    store.log(project, "copy", ", ".join(params.keys()))
    emit({"type": "project_update", "field": "copy"})
    return {"ok": True, "copy": copy}


def _t_design_artwork(project, params, emit):
    if not project.get("dieline"):
        return {"error": "There is no structure yet. Call build_structure first."}
    spec = project.get("artwork") or {"panels": {}}
    if params.get("replace_all"):
        spec = {"panels": {}}
    if params.get("palette"):
        spec["palette"] = params["palette"]
    if params.get("fonts"):
        spec["fonts"] = {**(spec.get("fonts") or {}), **params["fonts"]}
    if params.get("concept"):
        spec["concept"] = params["concept"]

    incoming = params.get("panels") or {}
    valid = {p["id"] for p in project["dieline"]["panels"] if p.get("printable", True)}
    unknown = [pid for pid in incoming if pid not in valid]
    for panel_id, panel in incoming.items():
        if panel_id in valid:
            spec.setdefault("panels", {})[panel_id] = panel

    unknown_fonts = [k for k in (spec.get("fonts") or {}).values()
                     if k not in artwork.FONT_CATALOG]
    artwork.ensure_layer_ids(spec)
    project["artwork"] = spec
    store.log(project, "artwork", f"panels: {', '.join(incoming.keys())}")
    emit({"type": "artwork_update"})

    designed = sorted(spec.get("panels", {}).keys())
    missing = sorted(valid - set(designed))
    check = layout.check(spec, project["dieline"])
    result = {"ok": True, "panels_designed": designed, "panels_still_empty": missing,
              "total_layers": len(artwork.layer_index(spec)), "layout_check": check}
    if unknown:
        result["ignored_unknown_panel_ids"] = unknown
        result["valid_ids"] = sorted(valid)
    if unknown_fonts:
        result["unknown_fonts"] = unknown_fonts
        result["available_fonts"] = sorted(artwork.FONT_CATALOG)
    if missing:
        result["note"] = "Design the empty panels before replying to the user."
    if not check["ok"]:
        result["required"] = "Fix the layout findings with edit_layers before replying."
    return result


def _t_list_layers(project, params, emit):
    spec = project.get("artwork")
    if not spec:
        return {"error": "There is no artwork yet."}
    layers = artwork.layer_index(spec)
    if params.get("panel"):
        layers = [l for l in layers if l["panel"] == params["panel"]]
    return {"ok": True, "layers": layers}


def _t_edit_layers(project, params, emit):
    spec = project.get("artwork")
    if not spec:
        return {"error": "There is no artwork yet. Use design_artwork."}
    result = artwork.apply_patch(spec, params.get("patches") or [])
    project["artwork"] = result["spec"]
    store.log(project, "edit", "; ".join(result["applied"])[:300])
    emit({"type": "artwork_update"})
    out = {"ok": True, "changed": result["applied"]}
    if project.get("dieline"):
        check = layout.check(spec, project["dieline"])
        out["layout_check"] = check
        if not check["ok"]:
            out["required"] = "Not clean yet. Fix the remaining findings before replying."
    if result["not_found"]:
        out["not_found"] = result["not_found"]
        out["note"] = "Call list_layers to see valid layer ids."
    return out


def _t_check_layout(project, params, emit):
    if not project.get("artwork") or not project.get("dieline"):
        return {"error": "There is no artwork to check."}
    return layout.check(project["artwork"], project["dieline"])


def _t_look_at_design(project, params, emit):
    if not project.get("artwork") or not project.get("dieline"):
        return {"error": "There is nothing to look at yet."}
    panel = params.get("panel") or "front"
    valid = {p["id"] for p in project["dieline"]["panels"] if p.get("printable", True)}
    if panel != "all" and panel not in valid:
        return {"error": f"Unknown panel {panel}.", "valid": sorted(valid) + ["all"]}

    if config.HOSTED:
        # On the website nobody on this side can draw: the run pauses, the
        # browser renders the panel and sends the picture with the next
        # request (core/agent.py, core/hosted.py).
        return {"_await_render": panel}

    request_id = visual.request()
    emit({"type": "render_request", "request_id": request_id, "panel": panel})
    picture = visual.wait(request_id, timeout=35)
    if not picture:
        picture = visual.render_locally(project, panel)
    if not picture:
        return {"error": "No preview could be made (the studio tab may be closed). "
                         "Rely on the layout check for now."}
    import base64
    data, media_type = picture
    return {
        "ok": True, "panel": panel,
        "note": "This is the actual render. Critique it, then fix what needs it.",
        "_image": {"media_type": media_type,
                   "data": base64.b64encode(data).decode("ascii")},
    }


def _t_generate_image(project, params, emit):
    if imagegen.provider() == "none":
        return {"error": "No image model is configured, so no pixel images. Keep "
                         "designing with vector shapes, patterns and type, which is the "
                         "better answer for print. The user can add a Gemini key under "
                         "Settings for photographic motifs."}
    try:
        asset = imagegen.generate(project, params.get("prompt", ""), params.get("aspect", "1:1"))
    except Exception as exc:
        return {"error": f"Image generation failed: {exc}"}
    emit({"type": "assets_update"})
    return {"ok": True, "asset_id": asset["id"],
            "usage": f'Place it as an image layer with src "asset:{asset["id"]}".'}


# ---------------------------------------------------------------------------

class PackagingDesigner(Agent):
    key = "packaging_designer"
    title = "Packaging designer"
    icon = "box"
    description = ("Designs folding cartons, mailers and gift boxes: structure, copy, "
                   "artwork as vector layers, dieline, 3D preview.")
    greeting = ("Tell me about your product: what it is, the brand name, and the size "
                "of the bottle, jar or tube. A reference image helps, drop it in with "
                "your first message.")
    starters = [
        "Face serum, 30 ml dropper bottle, 33 mm wide, 105 mm tall. Brand: ",
        "Body butter in a 200 ml jar, 75 mm across, 60 mm tall. Brand: ",
        "Soap bar, 90 x 60 x 25 mm, needs a sleeve. Brand: ",
        "Gift set: serum and cream together in a lift-off box. Brand: ",
    ]

    def system_prompt(self) -> str:
        # Fixed text only. Anything about the current project goes in
        # state_block, which travels with each new message instead.
        parts = [SYSTEM, "\n## Layer format\n" + LAYER_DOC]
        parts.append("\n## Typefaces (choose role names yourself, e.g. display, body, mono)\n"
                     + "\n".join(f"  {key:14s} {name:22s} {hint}"
                                 for key, (name, _, hint) in artwork.FONT_CATALOG.items()))
        parts.append("\n## Structures\n" + "\n".join(
            f'  {key:9s} {meta["short"]:24s} {meta["use"]}'
            for key, meta in dieline.BOX_TYPES.items()))
        parts.append(
            "\n## Project state\nEvery message from the user arrives with a "
            "<project_state> block: brief, panel sizes, the current layers with "
            "their ids, uploads, and whether image generation is available. It is "
            "always current, so trust it over anything earlier in the conversation. "
            "Layer ids in it can be used directly with edit_layers.")
        return "\n".join(parts)

    def state_block(self, project: dict) -> str:
        lines = ["Brief: " + json.dumps(project.get("brief") or {}, ensure_ascii=False)]
        dl = project.get("dieline")
        if dl:
            lines.append(f'Structure: {dl["box_name"]}, inside '
                         f'{dl["dims"]["width"]:.0f} x {dl["dims"]["depth"]:.0f} x '
                         f'{dl["dims"]["height"]:.0f} mm')
        lines.append("Panels (upright design size):\n" + _design_table(project))
        spec = project.get("artwork")
        if spec:
            lines.append("Concept: " + str(spec.get("concept") or "n/a"))
            if spec.get("palette"):
                lines.append("Palette: " + json.dumps(spec["palette"]))
            if spec.get("fonts"):
                lines.append("Fonts: " + json.dumps(spec["fonts"]))
            lines.append("Layers (panel / id / type / name):")
            for layer in artwork.layer_index(spec):
                label = layer.get("text") or layer.get("name") or ""
                label = " ".join(str(label).split())[:48]
                hidden = "" if layer.get("visible", True) else " [hidden]"
                lines.append(f'  {layer["panel"]:9s} {layer["id"]:22s} '
                             f'{layer["type"]:11s} {label}{hidden}')
        else:
            lines.append("Artwork: none yet.")
        assets = project.get("assets") or []
        uploads = [a for a in assets if a.get("role") != "photo"]
        if uploads:
            lines.append("Uploaded images: " + ", ".join(
                f'{a["name"]} (src "asset:{a["id"]}")' for a in uploads[-12:]))
        lines.append("Image generation: " + (
            "unavailable, design with vectors." if imagegen.provider() == "none"
            else f"available via {imagegen.provider()}."))
        return "\n".join(lines)

    def tools(self) -> list:
        return [
            Tool("set_brief", "Record the key facts of the project.", {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "brand": {"type": "string"},
                    "category": {"type": "string", "description": "e.g. face serum, body butter"},
                    "volume": {"type": "string", "description": "e.g. 30 ml, 100 g"},
                    "container": {"type": "string",
                                  "description": "the primary pack and its size, e.g. 30 ml "
                                                 "dropper bottle, 33 mm x 105 mm"},
                    "market": {"type": "string"},
                    "languages": {"type": "array", "items": {"type": "string"}},
                    "positioning": {"type": "string", "description": "price tier and stance"},
                    "target": {"type": "string"},
                    "finish": {"type": "string", "description": "soft-touch, hot foil, spot UV"},
                    "color_mode": {"type": "string", "description": "e.g. CMYK 4/0"},
                    "notes": {"type": "string"},
                },
            }, _t_set_brief, "Recording brief"),

            Tool("build_structure",
                 "Set the structure and inside dimensions (mm) and compute the dieline. "
                 "Returns the upright design size of every panel.", {
                     "type": "object",
                     "properties": {
                         "box_type": {"type": "string", "enum": list(dieline.BOX_TYPES)},
                         "width_mm": {"type": "number", "description": "width of the front"},
                         "depth_mm": {"type": "number", "description": "depth, front to back"},
                         "height_mm": {"type": "number"},
                         "bleed_mm": {"type": "number", "default": 3},
                     },
                     "required": ["box_type", "width_mm", "depth_mm", "height_mm"],
                 }, _t_build_structure, "Computing structure"),

            Tool("write_copy",
                 "Store the packaging copy, written out in full. Placeholders only for "
                 "facts only the user can supply.", {
                     "type": "object",
                     "properties": {
                         "front_headline": {"type": "string"},
                         "descriptor": {"type": "string"},
                         "tagline": {"type": "string"},
                         "benefits": {"type": "array", "items": {"type": "string"}},
                         "back_text": {"type": "string"},
                         "usage": {"type": "string"},
                         "ingredients_inci": {"type": "string"},
                         "warnings": {"type": "string"},
                         "net_content": {"type": "string"},
                         "responsible_person": {"type": "string"},
                         "origin": {"type": "string"},
                         "amharic": {"type": "string",
                                     "description": "Amharic lines, if used, for native review"},
                     },
                 }, _t_write_copy, "Writing copy"),

            Tool("design_artwork",
                 "Build or replace the artwork of one or more panels as vector layers. "
                 "Panels you leave out are untouched.\n\n" + LAYER_DOC, {
                     "type": "object",
                     "properties": {
                         "concept": {"type": "string",
                                     "description": "the design idea in one sentence"},
                         "palette": {"type": "object",
                                     "description": 'role to hex, e.g. {"bg":"#0f2a24","accent":"#d9a441"}',
                                     "additionalProperties": {"type": "string"}},
                         "fonts": {"type": "object",
                                   "description": 'role to typeface key, e.g. {"display":"cormorant","body":"jost"}',
                                   "additionalProperties": {"type": "string"}},
                         "replace_all": {"type": "boolean",
                                         "description": "wipe every panel first; only for a real redesign"},
                         "panels": {
                             "type": "object",
                             "description": 'panel id to {"background": <colour or gradient>, "layers": [...]}',
                             "additionalProperties": {
                                 "type": "object",
                                 "properties": {"background": {},
                                                "layers": {"type": "array", "items": {"type": "object"}}},
                             },
                         },
                     },
                     "required": ["panels"],
                 }, _t_design_artwork, "Designing artwork"),

            Tool("look_at_design",
                 "See the actual render of a panel, or 'all' for the whole flat layout, "
                 "so you can judge it with your own eyes and fix it.", {
                     "type": "object",
                     "properties": {"panel": {"type": "string",
                                              "description": "panel id, or 'all'"}},
                 }, _t_look_at_design, "Looking at the design"),

            Tool("list_layers", "List existing layers with their ids.",
                 {"type": "object", "properties": {"panel": {"type": "string"}}},
                 _t_list_layers, "Listing layers"),

            Tool("edit_layers",
                 "Change individual layers instead of rebuilding. The right way to fix things.", {
                     "type": "object",
                     "properties": {
                         "patches": {
                             "type": "array",
                             "items": {
                                 "type": "object",
                                 "properties": {
                                     "op": {"type": "string", "enum": ["update", "add", "delete", "move"]},
                                     "id": {"type": "string", "description": "layer id"},
                                     "panel": {"type": "string", "description": "op=add only"},
                                     "layer": {"type": "object", "description": "op=add only"},
                                     "set": {"type": "object", "description": "op=update only"},
                                     "to": {"type": "integer", "description": "op=move only: new index"},
                                 },
                                 "required": ["op"],
                             },
                         }
                     },
                     "required": ["patches"],
                 }, _t_edit_layers, "Editing layers"),

            Tool("check_layout",
                 "Recheck overflow, collisions, margins, contrast and barcode rules.",
                 {"type": "object", "properties": {}}, _t_check_layout, "Checking layout"),

            Tool("generate_image",
                 "Generate a pixel image to use as a design element (needs a Gemini or "
                 "OpenAI key). Lettering always stays vector.", {
                     "type": "object",
                     "properties": {"prompt": {"type": "string"},
                                    "aspect": {"type": "string",
                                               "enum": ["1:1", "3:4", "4:3", "9:16", "16:9"]}},
                     "required": ["prompt"],
                 }, _t_generate_image, "Generating image"),
        ]
