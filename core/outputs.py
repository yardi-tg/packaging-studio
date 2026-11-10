"""What the studio makes from a project: previews, 3D textures, production files.

Shared by the local server (server.py) and the website (core/hosted.py), so the
two always produce exactly the same thing.
"""

from __future__ import annotations
import re

from . import artwork, config, dieline, fonts, pdfwriter


def render(project: dict, mode: str = "flat", panel: str = None, labels: bool = False,
           assets: dict = None, scale: float = None) -> str:
    """One SVG view of the project.

    mode: flat (artwork on the dieline), print (artwork only), dieline, or
    panel (one face upright). Raises ValueError without a structure and
    LookupError for an unknown panel.
    """
    dl = project.get("dieline")
    if not dl:
        raise ValueError("No structure yet")
    spec = project.get("artwork") or {"panels": {}}
    assets = assets or {}

    if mode == "dieline":
        return dieline.to_svg(dl, show_labels=True, show_dimensions=True)
    if mode == "panel":
        geo = next((p for p in dl["panels"] if p["id"] == panel), None)
        if not geo:
            raise LookupError("Unknown panel")
        extra = {"scale": float(scale)} if scale else {}
        return artwork.render_panel_svg(spec, geo, assets,
                                        font_css=fonts.embed_css(spec, [geo["id"]]), **extra)
    css = fonts.embed_css(spec)
    if mode == "print":
        return artwork.full_svg(spec, dl, assets, with_dieline=False, font_css=css)
    return artwork.full_svg(spec, dl, assets, with_dieline=True, labels=labels, font_css=css)


def texture_panels(project: dict) -> list:
    """The faces that get printed, with the upright size each is drawn at."""
    out = []
    for geo in (project.get("dieline") or {}).get("panels", []):
        if geo.get("printable", True):
            w, h = artwork.design_size(geo)
            out.append({"id": geo["id"], "label": geo["label"], "w": w, "h": h})
    return out


def _base_name(project: dict) -> tuple:
    dl = project["dieline"]
    base = re.sub(r"[^A-Za-z0-9]+", "-",
                  (project.get("brief", {}).get("product_name")
                   or project.get("name", "package"))).strip("-").lower()[:40] or "package"
    dims = dl["dims"]
    return base, f'{dims["width"]:.0f}x{dims["depth"]:.0f}x{dims["height"]:.0f}'


def export_plan(project: dict) -> list:
    """Every production file as (file name, media type, maker). A maker takes
    the asset map and returns the file's content, so a caller can produce
    them all at once (local) or one per request (website)."""
    dl = project.get("dieline")
    if not dl:
        raise ValueError("No structure yet")
    spec = project.get("artwork") or {"panels": {}}
    base, size = _base_name(project)
    svg, pdf = "image/svg+xml", "application/pdf"
    plan = [
        (f"{base}-dieline-{size}.svg", svg, lambda a: dieline.to_svg(dl)),
        (f"{base}-dieline-{size}.pdf", pdf, lambda a: pdfwriter.dieline_pdf(dl)),
        (f"{base}-artwork-{size}.svg", svg,
         lambda a: artwork.full_svg(spec, dl, a, with_dieline=False,
                                    font_css=fonts.embed_css(spec))),
        (f"{base}-artwork-with-dieline-{size}.svg", svg,
         lambda a: artwork.full_svg(spec, dl, a, with_dieline=True,
                                    font_css=fonts.embed_css(spec))),
        (f"{base}-spec-sheet.pdf", pdf, lambda a: pdfwriter.spec_sheet_pdf(project)),
    ]
    for geo in dl["panels"]:
        if geo.get("printable", True):
            plan.append((f"{base}-panel-{geo['id']}.svg", svg,
                         lambda a, g=geo: artwork.render_panel_svg(
                             spec, g, a, font_css=fonts.embed_css(spec, [g["id"]]))))
    plan.append(("READ-ME-FIRST.txt", "text/plain; charset=utf-8",
                 lambda a: export_readme(project, dl)))
    return plan


def public(project: dict) -> dict:
    """The project without the raw API blocks the interface has no use for."""
    out = {k: v for k, v in project.items() if k != "messages"}
    out["messages"] = [
        {"role": m["role"], "text": m.get("display", ""),
         "tools": m.get("tools") or [], "cost": m.get("cost")}
        for m in project.get("messages", [])
        if not m.get("hidden") and m.get("display")
    ]
    if project.get("artwork"):
        out["layers"] = artwork.layer_index(project["artwork"])
    return out


def export_readme(project: dict, dl: dict) -> str:
    dims = dl["dims"]
    fonts_used = (project.get("artwork") or {}).get("fonts") or {}
    names = [artwork.FONT_CATALOG.get(v, (v,))[0] for v in fonts_used.values()]
    usage = project.get("usage") or {}
    where = "" if config.HOSTED else ", running on your own machine"
    return f"""PACKAGING STUDIO - EXPORT
=========================

Project:    {project.get('name')}
Structure:  {dl['box_name']} ({dl['box_short']})
Inside:     W {dims['width']:.0f} x D {dims['depth']:.0f} x H {dims['height']:.0f} mm
Flat sheet: {dl['sheet']['width']:.0f} x {dl['sheet']['height']:.0f} mm
Material:   {dl['material_hint']}
API cost:   ${usage.get('cost_usd', 0):.4f} across {usage.get('runs', 0)} requests

FILES
-----
*-dieline-*.svg / .pdf
    The cutting contour on its own, at 1:1.
    Solid red  = cut line.
    Dashed blue = crease line (fold).
    This is what you send to the box maker.

*-artwork-*.svg
    The print image on the flat layout, without cut lines.
    Opens in Adobe Illustrator, Affinity Designer and Inkscape with every
    element still editable.

*-artwork-with-dieline-*.svg
    Both on top of each other. Good for checking, not for printing.

*-panel-*.svg
    Each face of the box on its own.

*-spec-sheet.pdf
    Technical spec sheet for the printer and the box maker.

CHECK BEFORE PRINTING
---------------------
1. Fonts: this file uses {', '.join(names) or 'system fonts'}.
   They are all free via Google Fonts and are embedded in the SVG. If your
   printer wants certainty, convert text to outlines in your design app
   (Type > Create Outlines) before handing the file over.
2. Colour: the file is RGB. Your printer will convert to CMYK, or you can.
   Expect slight shifts, especially in strong blues and oranges.
3. Bleed: {dl['bleed']:.0f} mm is allowed for. Background areas must run past
   the cut line.
4. Material thickness: on corrugated, thickness is added outside the inside
   dimension.
5. Have the mandatory information checked. For cosmetics in Ethiopia that
   means the EFDA rules. The copy here is a design proposal, not legal advice.

Made with Packaging Studio{where}.
"""
