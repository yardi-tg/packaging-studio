"""The packaging designer agent.

It runs the conversation, decides the structure, writes the copy, and builds
the artwork as structured vector layers.
"""

from __future__ import annotations
import json

from core import artwork, dieline, imagegen, layout, store
from core.agent import Agent, Tool

SYSTEM = """You are an experienced packaging designer and packaging engineer.
You work inside a program called Packaging Studio that runs on the user's own
machine. Your speciality is cosmetics packaging.

## How you reply
Reply in whatever language the user writes to you in. Be brief and concrete.
No filler, no narrating your own steps -- the interface already shows which
tools you ran.

You are a designer, not a questionnaire. Make decisions and justify them in a
sentence. Only ask when the answer genuinely changes the outcome: product name,
net content, market. Two questions at most in one go. If something minor is
missing, pick something sensible and say what you assumed.

## Your workflow
1. Record the brief (set_brief) as soon as you know the product and brand.
2. Decide the structure (build_structure). No artwork without a dieline.
3. Write the copy (write_copy): front, back, mandatory information.
4. Build the artwork (design_artwork), panel by panel.
5. Refine with edit_layers rather than rebuilding. When the user wants one
   small change, change that one layer.

After the first complete artwork, describe in two or three sentences what you
designed and why, and offer one concrete next improvement.

## The coordinate system
Everything is in millimetres. Each layer sits relative to the top-left corner
of its panel: x=0,y=0 is top left, x grows right, y grows down. build_structure
hands you the panel dimensions. Work from those numbers; do not guess.

## Budgeting space
The most common mistake is text that needs more room than you expected. Do the
arithmetic before you place anything:

  lines  = roughly  character count / (column width w / (size * 0.52))
  height = (lines - 1) * size * lineHeight  +  size * 1.2

Example: "Nigella Glow Serum" is 18 characters. At size 7.4 with w 36 you fit
about 9 characters per line, so two or three lines, so roughly 25 mm tall. The
next layer can therefore start no earlier than y + 25.

Every design_artwork and edit_layers call returns a layout check. If it reports
findings, fix them immediately with edit_layers. Do not tell the user the
design is finished while findings are open.

## Hard rules for printable design
- Safety margin: nothing important closer than 4 mm to a cut or crease.
  Backgrounds may and should run past the edge.
- No important text across a fold.
- Minimum type sizes: body copy 2.0 mm, mandatory text and INCI 1.5 mm. Below
  that it is unreadable in print.
- Contrast: light type on a dark ground needs more weight, not more size.
- Two typefaces at most. One for headlines, one for text.
- Four colours at most. Cosmetics lives on flat colour and white space, not
  decoration.
- The front panel is a hierarchy, in this order: brand, product name, what it
  is in two to four words, net content. Nothing more.
- Side panels carry ingredients, directions, origin, barcode area.
- Always leave a clear area of at least 40 x 25 mm for the barcode, usually on
  a side panel or low on the back.

## Mandatory cosmetics information
Always plan space for these, even when the user does not mention them: product
name and function, nominal content, INCI list in descending order, batch
number, shelf life or PAO symbol, name and address of the responsible person,
country of origin, warnings.

For the Ethiopian market: the EFDA generally requires registration and labelling
in English, with Amharic often expected alongside. Tell the user to verify the
current EFDA requirements before printing. You are a designer, not a regulator:
never invent ingredients, efficacy claims or certifications. Mark placeholders
clearly as placeholders.

## Design stance
Do not make template work. Commit to a position and carry it through: either
very reduced, with generous white space and one strong typographic moment, or
flat and colourful with bold graphics. Avoid generic gradients, drop shadows
and centred stacks with no tension. Work with a grid, asymmetric balance and
deliberate jumps in scale. One strong detail beats five weak ones.

If a reference image is supplied, take the stance, colour mood and typographic
energy from it, not the actual artwork. You do not copy other brands, logos or
illustrations. If the user asks you to rebuild an existing brand, explain
politely that you will design something original in a similar direction.
"""

LAYER_DOC = """A layer is an object with these fields:
  type   text | rect | ellipse | line | path | pattern | image
  name   short name, shown in the layer list in the interface
  x,y    position in mm, relative to the panel's top-left corner
  w,h    width and height in mm. For text, w is the column width and
         wrapping happens automatically
  fill   a hex colour "#1b2a4a" OR a gradient object
         {"type":"linear","angle":90,"stops":["#fff","#000"]}
         {"type":"radial","cx":50,"cy":40,"r":70,"stops":[...]}
  stroke, strokeWidth, dash, rx, opacity, rotate, blend

  text also takes: text, font (a role name from fonts, e.g. "display"),
       size (mm, cap height plus descender), weight (300-900),
       align (left|center|right), letterSpacing (mm, may be negative),
       lineHeight, transform ("uppercase"), italic
  pattern also takes: motif (dots|grid|diagonal|waves|stars|leaves|cross),
       tile (mm), color, scale, patternRotate
  path also takes: d (SVG path data in mm, relative to the panel)
  image also takes: src ("asset:<id>", or "ref:0" for the first uploaded
       image), fit (cover|contain|stretch)

Array order is stacking order: later means in front."""


def _panel_table(project) -> str:
    dl = project.get("dieline")
    if not dl:
        return "No structure decided yet."
    return "\n".join(
        f'{p["id"]:10s} {p["label"]:22s} {p["w"]:.1f} x {p["h"]:.1f} mm'
        for p in dl["panels"] if p.get("printable", True)
    )


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
    emit({"type": "project_update", "field": "brief", "value": brief})
    return {"ok": True, "brief": brief,
            "next": "Brief saved. Next step: build_structure."}


def _t_build_structure(project, params, emit):
    dl = dieline.build(
        params.get("box_type", "ste"),
        params.get("width_mm", 50),
        params.get("depth_mm", 50),
        params.get("height_mm", 100),
        params.get("bleed_mm", 3),
    )
    project["dieline"] = dl
    if project.get("artwork"):
        project["artwork"] = None  # panel sizes changed, start the art again
    store.log(project, "structure", f'{dl["box_short"]} {dl["dims"]}')
    emit({"type": "project_update", "field": "dieline", "value": {"ready": True}})
    return {
        "ok": True,
        "box": dl["box_name"],
        "sheet_mm": dl["sheet"],
        "material": dl["material_hint"],
        "notes": dl["notes"],
        "panels": [
            {"id": p["id"], "label": p["label"], "w": p["w"], "h": p["h"],
             "printable": p.get("printable", True)}
            for p in dl["panels"]
        ],
        "next": "design_artwork for every printable panel. "
                "Use exactly these panel dimensions.",
    }


def _t_write_copy(project, params, emit):
    copy = project.setdefault("copy", {})
    copy.update({k: v for k, v in params.items() if v not in (None, "")})
    store.log(project, "copy", ", ".join(params.keys()))
    emit({"type": "project_update", "field": "copy", "value": copy})
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
    valid_ids = {p["id"] for p in project["dieline"]["panels"]}
    unknown = [pid for pid in incoming if pid not in valid_ids]
    for panel_id, panel in incoming.items():
        if panel_id in valid_ids:
            spec.setdefault("panels", {})[panel_id] = panel

    artwork.ensure_layer_ids(spec)
    project["artwork"] = spec
    store.log(project, "artwork", f"panels: {', '.join(incoming.keys())}")
    emit({"type": "artwork_update"})

    designed = sorted(spec.get("panels", {}).keys())
    missing = [p["id"] for p in project["dieline"]["panels"]
               if p.get("printable", True) and p["id"] not in designed]
    check = layout.check(spec, project["dieline"])
    result = {
        "ok": True,
        "panels_designed": designed,
        "panels_still_empty": missing,
        "total_layers": len(artwork.layer_index(spec)),
        "layout_check": check,
    }
    if unknown:
        result["ignored_unknown_panel_ids"] = unknown
        result["valid_ids"] = sorted(valid_ids)
    if missing:
        result["note"] = ("These panels have no artwork yet. Design them in your "
                          "next call before replying to the user.")
    if not check["ok"]:
        result["required"] = (
            "There are layout problems. Fix them now with edit_layers before "
            "replying. Do not tell the user the design is done while findings "
            "are open."
        )
    return result


def _t_list_layers(project, params, emit):
    spec = project.get("artwork")
    if not spec:
        return {"error": "There is no artwork yet."}
    layers = artwork.layer_index(spec)
    panel = params.get("panel")
    if panel:
        layers = [l for l in layers if l["panel"] == panel]
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
            out["required"] = ("Not clean yet. Fix the remaining findings "
                               "before replying.")
    if result["not_found"]:
        out["not_found"] = result["not_found"]
        out["note"] = "Call list_layers to see the valid layer ids."
    return out


def _t_check_layout(project, params, emit):
    if not project.get("artwork") or not project.get("dieline"):
        return {"error": "There is no artwork to check."}
    return layout.check(project["artwork"], project["dieline"])


def _t_generate_image(project, params, emit):
    if imagegen.provider() == "none":
        return {
            "error": "No image provider is configured. This studio runs on an "
                     "Anthropic key alone, and Claude does not generate pixel "
                     "images. Tell the user they can add a Gemini or OpenAI key "
                     "under Settings if they want photographic motifs. For now, "
                     "design the panel with vector shapes, flat colour, patterns "
                     "and type -- which is the better answer for print anyway."
        }
    try:
        asset = imagegen.generate(project, params.get("prompt", ""),
                                  params.get("aspect", "1:1"))
    except Exception as exc:
        return {"error": f"Image generation failed: {exc}"}
    emit({"type": "assets_update"})
    return {"ok": True, "asset_id": asset["id"],
            "usage": f'Place it as a layer with src "asset:{asset["id"]}".'}


# ---------------------------------------------------------------------------

class PackagingDesigner(Agent):
    key = "packaging_designer"
    title = "Packaging designer"
    icon = "box"
    description = ("Designs folding cartons and shipping boxes: structure, copy, "
                   "artwork as vector layers, dieline, 3D preview.")
    greeting = ("Tell me about your product: what it is, what the brand is called, "
                "and how big the bottle or tube is. If you have a reference image, "
                "drop it in with your first message.")

    def system_prompt(self, project: dict) -> str:
        parts = [SYSTEM, "\n## Layer format\n" + LAYER_DOC]
        parts.append("\n## Available typefaces (role names are yours to choose)\n"
                     + "\n".join(
                         f"  {key:14s} {name:22s} {hint}"
                         for key, (name, _, hint) in artwork.FONT_CATALOG.items()))
        parts.append("\n## Structures\n" + "\n".join(
            f'  {key:8s} {meta["short"]:24s} {meta["use"]}'
            for key, meta in dieline.BOX_TYPES.items()))
        parts.append("\n## State of this project")
        parts.append("Brief: " + json.dumps(project.get("brief") or {},
                                            ensure_ascii=False))
        parts.append("Panels:\n" + _panel_table(project))
        spec = project.get("artwork")
        if spec:
            parts.append(
                f"Artwork: {len(artwork.layer_index(spec))} layers across "
                f"{len(spec.get('panels') or {})} panels. Use edit_layers for "
                "changes rather than rebuilding.")
        else:
            parts.append("Artwork: none yet.")
        assets = project.get("assets") or []
        if assets:
            parts.append("Uploaded images: " + ", ".join(
                f'{a["name"]} (src "asset:{a["id"]}")' for a in assets))
        if imagegen.provider() == "none":
            parts.append("Image generation: unavailable (Anthropic key only). "
                         "Design with vectors.")
        else:
            parts.append(f"Image generation: available via {imagegen.provider()}.")
        return "\n".join(parts)

    def tools(self) -> list:
        return [
            Tool(
                "set_brief", "Record the key facts of the project.",
                {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "brand": {"type": "string"},
                        "category": {"type": "string",
                                     "description": "e.g. face serum, body butter, soap"},
                        "volume": {"type": "string", "description": "e.g. 30 ml, 100 g"},
                        "market": {"type": "string", "description": "target market or country"},
                        "languages": {"type": "array", "items": {"type": "string"}},
                        "positioning": {"type": "string",
                                        "description": "price tier and stance, one sentence"},
                        "target": {"type": "string"},
                        "finish": {"type": "string",
                                   "description": "soft-touch, hot foil, spot UV"},
                        "color_mode": {"type": "string", "description": "e.g. CMYK 4/0"},
                        "notes": {"type": "string"},
                    },
                },
                _t_set_brief, "Recording brief",
            ),
            Tool(
                "build_structure",
                "Set the style and dimensions and compute the dieline. Returns "
                "the exact panel sizes you must design against. Dimensions are "
                "inside measurements in mm.",
                {
                    "type": "object",
                    "properties": {
                        "box_type": {"type": "string",
                                     "enum": list(dieline.BOX_TYPES.keys())},
                        "width_mm": {"type": "number", "description": "width of the front"},
                        "depth_mm": {"type": "number", "description": "depth / side wall"},
                        "height_mm": {"type": "number", "description": "height"},
                        "bleed_mm": {"type": "number", "default": 3},
                    },
                    "required": ["box_type", "width_mm", "depth_mm", "height_mm"],
                },
                _t_build_structure, "Computing structure",
            ),
            Tool(
                "write_copy",
                "Store the packaging copy. Write it finished, not as filler, "
                "except for facts only the user can supply.",
                {
                    "type": "object",
                    "properties": {
                        "front_headline": {"type": "string"},
                        "descriptor": {"type": "string",
                                       "description": "what it is, two to four words"},
                        "tagline": {"type": "string"},
                        "benefits": {"type": "array", "items": {"type": "string"}},
                        "back_text": {"type": "string"},
                        "usage": {"type": "string"},
                        "ingredients_inci": {"type": "string"},
                        "warnings": {"type": "string"},
                        "net_content": {"type": "string"},
                        "responsible_person": {"type": "string"},
                        "origin": {"type": "string"},
                    },
                },
                _t_write_copy, "Writing copy",
            ),
            Tool(
                "design_artwork",
                "Build or replace the artwork of individual panels as vector "
                "layers. You can pass several panels at once or work through "
                "them one at a time. Panels you leave out are untouched.\n\n"
                + LAYER_DOC,
                {
                    "type": "object",
                    "properties": {
                        "concept": {"type": "string",
                                    "description": "The design idea in one sentence."},
                        "palette": {
                            "type": "object",
                            "description": "role name to hex colour, e.g. "
                                           "{\"bg\":\"#0f2a24\",\"accent\":\"#d9a441\"}",
                            "additionalProperties": {"type": "string"},
                        },
                        "fonts": {
                            "type": "object",
                            "description": "role name to typeface key, e.g. "
                                           "{\"display\":\"playfair\",\"body\":\"inter\"}",
                            "additionalProperties": {"type": "string"},
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "true wipes every panel first. Only for "
                                           "a genuine redesign.",
                        },
                        "panels": {
                            "type": "object",
                            "description": "panel id to panel content, where panel "
                                           "content is {\"background\": <colour or "
                                           "gradient>, \"layers\": [<layer>, ...]}",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "background": {},
                                    "layers": {"type": "array",
                                               "items": {"type": "object"}},
                                },
                            },
                        },
                    },
                    "required": ["panels"],
                },
                _t_design_artwork, "Designing artwork",
            ),
            Tool(
                "list_layers",
                "List every existing layer with its id. Call this before making "
                "a targeted change.",
                {"type": "object", "properties": {"panel": {"type": "string"}}},
                _t_list_layers, "Listing layers",
            ),
            Tool(
                "edit_layers",
                "Change individual layers instead of rebuilding the artwork. "
                "This is the right way to make corrections.",
                {
                    "type": "object",
                    "properties": {
                        "patches": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string",
                                           "enum": ["update", "add", "delete", "move"]},
                                    "id": {"type": "string",
                                           "description": "layer id from list_layers"},
                                    "panel": {"type": "string",
                                              "description": "op=add only"},
                                    "layer": {"type": "object",
                                              "description": "op=add only: the new layer"},
                                    "set": {"type": "object",
                                            "description": "op=update only: fields to change"},
                                    "to": {"type": "integer",
                                           "description": "op=move only: new index"},
                                },
                                "required": ["op"],
                            },
                        }
                    },
                    "required": ["patches"],
                },
                _t_edit_layers, "Editing layers",
            ),
            Tool(
                "check_layout",
                "Work out whether text overflows a panel, blocks overlap, or "
                "anything sits too close to an edge.",
                {"type": "object", "properties": {}},
                _t_check_layout, "Checking layout",
            ),
            Tool(
                "generate_image",
                "Generate a pixel image as a design element. Only works when a "
                "Gemini or OpenAI key is configured. Without one, keep designing "
                "with vectors.",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "aspect": {"type": "string",
                                   "enum": ["1:1", "3:4", "4:3", "9:16", "16:9"]},
                    },
                    "required": ["prompt"],
                },
                _t_generate_image, "Generating image",
            ),
        ]
