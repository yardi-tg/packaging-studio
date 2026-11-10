"""A small PDF writer for dielines and spec sheets, standard library only.

Produces real vector PDFs at actual size (1:1), the way printers and diemakers
expect them. Cut and crease lines are drawn in named spot colours, CutContour
and Crease, which is what cutting tables and prepress software look for when
they separate the knife lines from the artwork.
"""

from __future__ import annotations
import math
import zlib

MM_TO_PT = 72.0 / 25.4

# Helvetica advance widths (per 1000 em) for printable ASCII, from the AFM.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
         278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
         584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
         500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
         667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
         278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
         278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def text_width(text: str, size_mm: float, bold: bool = False) -> float:
    total = 0
    for ch in str(text):
        code = ord(ch)
        total += _HELV[code - 32] if 32 <= code <= 126 else 556
    return total / 1000.0 * size_mm * (1.06 if bold else 1.0)


def _esc(text: str) -> str:
    return str(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _latin1(text: str) -> str:
    """The PDF base fonts only speak Latin-1. Replace anything else."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def hex_to_rgb(value: str):
    v = str(value or "").strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        return tuple(int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None


class PDF:
    def __init__(self, width_mm: float, height_mm: float, title: str = ""):
        self.w = width_mm * MM_TO_PT
        self.h = height_mm * MM_TO_PT
        self.height_mm = height_mm
        self.title = title
        self.ops: list = []
        self.spots: dict = {}       # resource name -> (spot name, CMYK)

    # input is mm with y downwards; PDF has y upwards
    def _x(self, x_mm: float) -> float:
        return x_mm * MM_TO_PT

    def _y(self, y_mm: float) -> float:
        return (self.height_mm - y_mm) * MM_TO_PT

    def set_stroke(self, r: float, g: float, b: float):
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")

    def set_fill(self, r: float, g: float, b: float):
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")

    def spot(self, name: str, cmyk: tuple) -> str:
        """Register a Separation colour space and return its resource name."""
        for res, (spot_name, _) in self.spots.items():
            if spot_name == name:
                return res
        res = f"CS{len(self.spots)}"
        self.spots[res] = (name, cmyk)
        return res

    def set_stroke_spot(self, resource: str):
        self.ops.append(f"/{resource} CS 1 SCN")

    def set_width(self, mm: float):
        self.ops.append(f"{mm * MM_TO_PT:.3f} w")

    def set_dash(self, pattern_mm=None):
        if not pattern_mm:
            self.ops.append("[] 0 d")
        else:
            self.ops.append("[" + " ".join(f"{v * MM_TO_PT:.2f}" for v in pattern_mm) + "] 0 d")

    def polyline(self, points, close: bool = False):
        if len(points) < 2:
            return
        x, y = points[0]
        self.ops.append(f"{self._x(x):.3f} {self._y(y):.3f} m")
        for x, y in points[1:]:
            self.ops.append(f"{self._x(x):.3f} {self._y(y):.3f} l")
        self.ops.append("h S" if close else "S")

    def rect_fill(self, x, y, w, h):
        self.ops.append(f"{self._x(x):.3f} {self._y(y + h):.3f} "
                        f"{w * MM_TO_PT:.3f} {h * MM_TO_PT:.3f} re f")

    def rect_stroke(self, x, y, w, h):
        self.ops.append(f"{self._x(x):.3f} {self._y(y + h):.3f} "
                        f"{w * MM_TO_PT:.3f} {h * MM_TO_PT:.3f} re S")

    def text(self, x, y, content, size_mm=3.0, font="F1", angle=0.0, align="left"):
        """Text at (x, y) baseline. angle in degrees, counter-clockwise on paper."""
        content = _latin1(content)
        width = text_width(content, size_mm, bold=(font == "F2"))
        shift = {"center": width / 2, "right": width}.get(align, 0.0)
        a = math.radians(angle)
        cos, sin = math.cos(a), math.sin(a)
        # move back along the text direction by the alignment shift
        bx = x - shift * cos
        by = y + shift * sin          # y grows downward in our input space
        pt = size_mm * MM_TO_PT
        self.ops.append("BT")
        self.ops.append(f"/{font} {pt:.2f} Tf")
        self.ops.append(f"{cos:.5f} {sin:.5f} {-sin:.5f} {cos:.5f} "
                        f"{self._x(bx):.3f} {self._y(by):.3f} Tm")
        self.ops.append(f"({_esc(content)}) Tj")
        self.ops.append("ET")

    def build(self) -> bytes:
        content = "\n".join(self.ops).encode("latin-1", "replace")
        compressed = zlib.compress(content)

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            None,   # page, filled in below once we know the resource numbers
            b"<< /Length " + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        cs_refs = []
        for res, (name, (c, m, y, k)) in self.spots.items():
            objects.append(
                (f"[/Separation /{name} /DeviceCMYK << /FunctionType 2 /Domain [0 1] "
                 f"/C0 [0 0 0 0] /C1 [{c} {m} {y} {k}] /N 1 >>]").encode("latin-1"))
            cs_refs.append(f"/{res} {len(objects)} 0 R")
        objects.append(b"<< /Title (" + _esc(_latin1(self.title)).encode("latin-1", "replace")
                       + b") /Producer (Packaging Studio) >>")
        info_ref = len(objects)

        colorspaces = f"/ColorSpace << {' '.join(cs_refs)} >> " if cs_refs else ""
        objects[2] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w:.3f} {self.h:.3f}] "
                      f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> {colorspaces}>> "
                      f"/Contents 4 0 R >>").encode("latin-1")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref_pos = len(out)
        count = len(objects) + 1
        out += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {count} /Root 1 0 R /Info {info_ref} 0 R >>\n"
                f"startxref\n{xref_pos}\n%%EOF\n").encode()
        return bytes(out)


# ---------------------------------------------------------------------------
# Dieline
# ---------------------------------------------------------------------------

def _draw_dimensions(pdf: PDF, marks: list):
    pdf.set_dash(None)
    pdf.set_stroke(0.54, 0.58, 0.65)
    pdf.set_width(0.2)
    tick = 1.1
    for m in marks:
        a, b, at = m["a"], m["b"], m["at"]
        if m["kind"] == "h":
            pdf.polyline([(a, at), (b, at)])
            for x in (a, b):
                pdf.polyline([(x - tick, at + tick), (x + tick, at - tick)])
                pdf.polyline([(x, at - 2.2), (x, at + 2.2)])
            pdf.set_fill(0.36, 0.40, 0.46)
            pdf.text((a + b) / 2, at - 1.3, m["label"], 2.5, align="center")
        else:
            pdf.polyline([(at, a), (at, b)])
            for y in (a, b):
                pdf.polyline([(at - tick, y + tick), (at + tick, y - tick)])
                pdf.polyline([(at - 2.2, y), (at + 2.2, y)])
            pdf.set_fill(0.36, 0.40, 0.46)
            pdf.text(at - 1.3, (a + b) / 2, m["label"], 2.5, angle=90, align="center")


def dieline_pdf(dieline: dict) -> bytes:
    """The dieline as a print-ready 1:1 vector PDF with spot-colour knife lines."""
    from .dieline import dimension_marks, PAD

    sheet = dieline["sheet"]
    dims = dieline["dims"]
    blank = dieline.get("blank") or sheet
    title = (f'Dieline {dieline["box_short"]} '
             f'{dims["width"]:.0f}x{dims["depth"]:.0f}x{dims["height"]:.0f}mm')
    pdf = PDF(sheet["width"], sheet["height"], title)
    cut_cs = pdf.spot("CutContour", (0, 1, 1, 0))
    crease_cs = pdf.spot("Crease", (1, 0.5, 0, 0))

    pdf.set_fill(1, 1, 1)
    pdf.rect_fill(0, 0, sheet["width"], sheet["height"])

    # panel labels, turned the way each face reads once folded
    for p in dieline["panels"]:
        if p["w"] < 12 or p["h"] < 10:
            continue
        cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
        span = p.get("dw", p["w"])
        size = max(2.2, min(3.6, span / 12))
        rot = int(p.get("rotate", 0)) % 360
        angle = -rot   # SVG rotates clockwise, PDF angles run counter-clockwise
        a = math.radians(rot)
        down = (-math.sin(a) * size * 1.5, math.cos(a) * size * 1.5)
        pdf.set_fill(0.56, 0.60, 0.66)
        pdf.text(cx, cy, p["label"], size, angle=angle, align="center")
        pdf.set_fill(0.70, 0.74, 0.79)
        pdf.text(cx + down[0], cy + down[1],
                 f'{p.get("dw", p["w"]):.0f} x {p.get("dh", p["h"]):.0f} mm',
                 size * 0.8, angle=angle, align="center")

    pdf.set_stroke_spot(crease_cs)
    pdf.set_width(0.35)
    pdf.set_dash([3.2, 2.2])
    for path in dieline["fold"]:
        pdf.polyline(path)

    pdf.set_dash(None)
    pdf.set_stroke_spot(cut_cs)
    pdf.set_width(0.45)
    for path in dieline["cut"]:
        pdf.polyline(path)

    _draw_dimensions(pdf, dimension_marks(dieline))

    pdf.set_fill(0.2, 0.25, 0.33)
    pdf.text(PAD["left"], sheet["height"] - 7,
             f'{dieline["box_name"]}  |  inside W {dims["width"]:.0f} x D {dims["depth"]:.0f} '
             f'x H {dims["height"]:.0f} mm  |  blank {blank["width"]:.0f} x '
             f'{blank["height"]:.0f} mm', 3.0, font="F2")
    pdf.set_fill(0.39, 0.45, 0.55)
    pdf.text(PAD["left"], sheet["height"] - 2.8,
             f'CutContour = cut  |  Crease = dashed  |  bleed {dieline["bleed"]:.0f} mm  |  '
             f'scale 1:1  |  {dieline["material_hint"]}', 2.5)
    return pdf.build()


# ---------------------------------------------------------------------------
# Spec sheet
# ---------------------------------------------------------------------------

def _draw_dieline_thumb(pdf: PDF, dieline: dict, x: float, y: float,
                        max_w: float, max_h: float):
    blank = dieline.get("blank")
    if not blank:
        return 0.0
    scale = min(max_w / blank["width"], max_h / blank["height"])
    ox, oy = blank["x"], blank["y"]

    def tr(path):
        return [(x + (px - ox) * scale, y + (py - oy) * scale) for px, py in path]

    pdf.set_stroke(0.18, 0.5, 0.91)
    pdf.set_width(0.2)
    pdf.set_dash([1.2, 0.9])
    for path in dieline["fold"]:
        pdf.polyline(tr(path))
    pdf.set_dash(None)
    pdf.set_stroke(0.9, 0.14, 0.23)
    pdf.set_width(0.28)
    for path in dieline["cut"]:
        pdf.polyline(tr(path))
    return blank["height"] * scale


def spec_sheet_pdf(project: dict) -> bytes:
    """A technical spec sheet for the printer and the box maker."""
    pdf = PDF(210, 297, "Packaging spec sheet")
    brief = project.get("brief") or {}
    dl = project.get("dieline") or {}
    dims = dl.get("dims", {})
    spec = project.get("artwork") or {}
    left = 18.0

    # header band
    pdf.set_fill(0.09, 0.10, 0.13)
    pdf.rect_fill(0, 0, 210, 34)
    pdf.set_fill(0.79, 0.64, 0.31)
    pdf.rect_fill(left, 11, 4, 4)
    pdf.set_fill(1, 1, 1)
    pdf.text(left + 7, 14.4, "Packaging spec sheet", 3.2, font="F2")
    pdf.text(left, 26, brief.get("product_name") or project.get("name", "Package"), 7.2, font="F2")
    pdf.set_fill(0.70, 0.73, 0.79)
    sub = "  |  ".join(filter(None, [brief.get("brand"), brief.get("volume"), brief.get("market")]))
    pdf.text(192, 26, sub, 3.0, align="right")

    y = 46.0

    def section(title):
        nonlocal y
        y += 3
        pdf.set_fill(0.79, 0.64, 0.31)
        pdf.text(left, y, title, 3.0, font="F2")
        y += 2.2
        pdf.set_stroke(0.88, 0.89, 0.91)
        pdf.set_width(0.25)
        pdf.polyline([(left, y), (192, y)])
        y += 5.2

    def row(label, value, bold=False):
        nonlocal y
        if value in (None, "", []):
            return
        pdf.set_fill(0.45, 0.5, 0.58)
        pdf.text(left, y, label, 3.0)
        pdf.set_fill(0.08, 0.10, 0.14)
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        chunks = list(_chunks(_latin1(text), 66))
        for i, chunk in enumerate(chunks):
            pdf.text(62, y + i * 4.5, chunk, 3.2, font="F2" if bold else "F1")
        y += max(1, len(chunks)) * 4.5 + 1.6

    section("Product")
    row("Product name", brief.get("product_name"), bold=True)
    row("Brand", brief.get("brand"))
    row("Category", brief.get("category"))
    row("Net content", brief.get("volume"))
    row("Market", brief.get("market"))
    row("Languages", brief.get("languages"))

    section("Structure")
    row("Style", f'{dl.get("box_name")} ({dl.get("box_short")})' if dl else None)
    row("Inside dimensions",
        f'W {dims.get("width",0):.0f} x D {dims.get("depth",0):.0f} x H {dims.get("height",0):.0f} mm'
        if dims else None, bold=True)
    blank = dl.get("blank") or {}
    row("Blank size", f'{blank.get("width",0):.0f} x {blank.get("height",0):.0f} mm' if blank else None)
    row("Material", dl.get("material_hint"))
    row("Bleed", f'{dl.get("bleed", 3)} mm on cut edges, 1 mm over folds into unprinted flaps')

    section("Print")
    row("Colour", brief.get("color_mode") or "CMYK, 4/0")
    fonts = spec.get("fonts") or {}
    if fonts:
        from .artwork import FONT_CATALOG
        row("Typefaces", ", ".join(sorted({FONT_CATALOG.get(key, (key,))[0]
                                           for key in fonts.values()})))
    row("Finishing", brief.get("finish"))

    palette = [(k, v) for k, v in (spec.get("palette") or {}).items() if hex_to_rgb(v)]
    if palette:
        pdf.set_fill(0.45, 0.5, 0.58)
        pdf.text(left, y, "Palette", 3.0)
        sx = 62.0
        for name, value in palette[:8]:
            r, g, b = hex_to_rgb(value)
            pdf.set_fill(r, g, b)
            pdf.rect_fill(sx, y - 3.6, 15, 9)
            pdf.set_stroke(0.85, 0.86, 0.88)
            pdf.set_width(0.2)
            pdf.rect_stroke(sx, y - 3.6, 15, 9)
            pdf.set_fill(0.25, 0.28, 0.34)
            pdf.text(sx, y + 9.0, str(name)[:10], 2.4)
            pdf.set_fill(0.5, 0.54, 0.6)
            pdf.text(sx, y + 12.2, str(value).upper(), 2.2)
            sx += 17
        y += 18

    if dl:
        section("Dieline")
        used = _draw_dieline_thumb(pdf, dl, left, y, 174, min(80, 284 - y - 40))
        y += used + 6

    if dl.get("notes"):
        section("Notes")
        for note in dl["notes"]:
            pdf.set_fill(0.2, 0.25, 0.33)
            for chunk in _chunks(_latin1("- " + note), 92):
                pdf.text(left, y, chunk, 3.0)
                y += 4.2
            y += 0.8

    pdf.set_fill(0.6, 0.65, 0.72)
    pdf.text(left, 288, "Made with Packaging Studio. Dieline at 1:1 as PDF (CutContour and "
                        "Crease spot colours) and SVG.", 2.6)
    return pdf.build()


def _chunks(text: str, size: int):
    words = str(text).split()
    if not words:
        yield ""
        return
    line = words[0]
    for word in words[1:]:
        if len(line) + 1 + len(word) <= size:
            line += " " + word
        else:
            yield line
            line = word
    yield line
