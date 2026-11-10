"""A minimal PDF writer for vector dielines, standard library only.

Produces a real vector PDF at actual size (1:1), which is what printers and
diemakers expect. Lines stay lines; nothing is rasterised.
"""

from __future__ import annotations
import zlib

MM_TO_PT = 72.0 / 25.4


def _esc(text: str) -> str:
    return (str(text).replace("\\", r"\\")
            .replace("(", r"\(").replace(")", r"\)"))


def _latin1(text: str) -> str:
    """The PDF base fonts only speak Latin-1. Replace anything else."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


class PDF:
    def __init__(self, width_mm: float, height_mm: float, title: str = ""):
        self.w = width_mm * MM_TO_PT
        self.h = height_mm * MM_TO_PT
        self.height_mm = height_mm
        self.title = title
        self.ops: list = []

    # -- input is mm with y downwards; PDF has y upwards --
    def _x(self, x_mm: float) -> float:
        return x_mm * MM_TO_PT

    def _y(self, y_mm: float) -> float:
        return (self.height_mm - y_mm) * MM_TO_PT

    def set_stroke(self, r: float, g: float, b: float):
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")

    def set_fill(self, r: float, g: float, b: float):
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")

    def set_width(self, mm: float):
        self.ops.append(f"{mm * MM_TO_PT:.3f} w")

    def set_dash(self, pattern_mm=None):
        if not pattern_mm:
            self.ops.append("[] 0 d")
        else:
            vals = " ".join(f"{v * MM_TO_PT:.2f}" for v in pattern_mm)
            self.ops.append(f"[{vals}] 0 d")

    def polyline(self, points, close: bool = False):
        if len(points) < 2:
            return
        x, y = points[0]
        self.ops.append(f"{self._x(x):.3f} {self._y(y):.3f} m")
        for x, y in points[1:]:
            self.ops.append(f"{self._x(x):.3f} {self._y(y):.3f} l")
        self.ops.append("h S" if close else "S")

    def rect_fill(self, x, y, w, h):
        self.ops.append(
            f"{self._x(x):.3f} {self._y(y + h):.3f} "
            f"{w * MM_TO_PT:.3f} {h * MM_TO_PT:.3f} re f"
        )

    def text(self, x, y, content, size_mm=3.0, font="F1"):
        pt = size_mm * MM_TO_PT
        self.ops.append("BT")
        self.ops.append(f"/{font} {pt:.2f} Tf")
        self.ops.append(f"{self._x(x):.3f} {self._y(y):.3f} Td")
        self.ops.append(f"({_esc(_latin1(content))}) Tj")
        self.ops.append("ET")

    def build(self) -> bytes:
        content = "\n".join(self.ops).encode("latin-1", "replace")
        compressed = zlib.compress(content)

        objects = []
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w:.3f} {self.h:.3f}] "
            f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
            .encode("latin-1")
        )
        objects.append(
            b"<< /Length " + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
        )
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        objects.append(
            b"<< /Title (" + _esc(_latin1(self.title)).encode("latin-1", "replace")
            + b") /Producer (Packaging Studio) >>"
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode()
            out += obj
            out += b"\nendobj\n"

        xref_pos = len(out)
        count = len(objects) + 1
        out += f"xref\n0 {count}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {count} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode()
        return bytes(out)


def dieline_pdf(dieline: dict) -> bytes:
    """The dieline as a print-ready 1:1 vector PDF."""
    sheet = dieline["sheet"]
    dims = dieline["dims"]
    title = (f'Dieline {dieline["box_short"]} '
             f'{dims["width"]:.0f}x{dims["depth"]:.0f}x{dims["height"]:.0f}mm')
    pdf = PDF(sheet["width"], sheet["height"], title)

    pdf.set_fill(1, 1, 1)
    pdf.rect_fill(0, 0, sheet["width"], sheet["height"])

    # panel labels
    pdf.set_fill(0.62, 0.66, 0.72)
    for p in dieline["panels"]:
        if p["w"] < 12 or p["h"] < 10:
            continue
        size = max(2.2, min(3.6, p["w"] / 12))
        label = p["label"]
        pdf.text(p["x"] + p["w"] / 2 - len(label) * size * 0.26,
                 p["y"] + p["h"] / 2, label, size)
        meas = f'{p["w"]:.0f} x {p["h"]:.0f} mm'
        pdf.text(p["x"] + p["w"] / 2 - len(meas) * size * 0.22,
                 p["y"] + p["h"] / 2 + size * 1.5, meas, size * 0.82)

    # creases: blue, dashed
    pdf.set_stroke(0.04, 0.49, 1.0)
    pdf.set_width(0.35)
    pdf.set_dash([3.2, 2.2])
    for path in dieline["fold"]:
        pdf.polyline(path)

    # cuts: red, solid
    pdf.set_dash(None)
    pdf.set_stroke(0.90, 0.14, 0.23)
    pdf.set_width(0.45)
    for path in dieline["cut"]:
        pdf.polyline(path)

    # legend
    pdf.set_fill(0.2, 0.25, 0.33)
    pdf.text(4, sheet["height"] - 7,
             f'{dieline["box_name"]}  |  W {dims["width"]:.0f} x D {dims["depth"]:.0f} '
             f'x H {dims["height"]:.0f} mm  |  sheet {sheet["width"]:.0f} x '
             f'{sheet["height"]:.0f} mm', 3.2, font="F2")
    pdf.set_fill(0.39, 0.45, 0.55)
    pdf.text(4, sheet["height"] - 2.8,
             f'Red = cut  |  blue dashed = crease  |  '
             f'bleed {dieline["bleed"]:.0f} mm  |  scale 1:1  |  '
             f'{dieline["material_hint"]}', 2.7)
    return pdf.build()


def spec_sheet_pdf(project: dict) -> bytes:
    """A technical spec sheet for the printer and the box maker."""
    pdf = PDF(210, 297, "Packaging spec sheet")
    brief = project.get("brief") or {}
    dl = project.get("dieline") or {}
    dims = dl.get("dims", {})
    spec = project.get("artwork") or {}

    y = 22
    pdf.set_fill(0.07, 0.09, 0.13)
    pdf.text(18, y, _latin1(brief.get("product_name") or project.get("name", "Package")),
             7.0, font="F2")
    y += 8
    pdf.set_fill(0.39, 0.45, 0.55)
    pdf.text(18, y, "Technical spec sheet for the printer and box maker", 3.4)
    y += 4
    pdf.set_stroke(0.85, 0.87, 0.9)
    pdf.set_width(0.3)
    pdf.polyline([(18, y), (192, y)])
    y += 10

    def row(label, value, bold=False):
        nonlocal y
        if value in (None, "", []):
            return
        pdf.set_fill(0.45, 0.5, 0.58)
        pdf.text(18, y, _latin1(label), 3.1)
        pdf.set_fill(0.07, 0.09, 0.13)
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        for i, chunk in enumerate(_chunks(_latin1(text), 62)):
            pdf.text(68, y + i * 4.6, chunk, 3.3, font="F2" if bold else "F1")
        y += max(1, len(list(_chunks(_latin1(text), 62)))) * 4.6 + 2.4

    def section(title):
        nonlocal y
        y += 4
        pdf.set_fill(0.04, 0.49, 1.0)
        pdf.text(18, y, _latin1(title.upper()), 3.2, font="F2")
        y += 5.5

    section("Product")
    row("Product name", brief.get("product_name"), bold=True)
    row("Brand", brief.get("brand"))
    row("Category", brief.get("category"))
    row("Net content", brief.get("volume"))
    row("Market", brief.get("market"))
    row("Languages", brief.get("languages"))

    section("Structure")
    row("Style", dl.get("box_name"))
    row("Standard", dl.get("box_short"))
    row("Inside dimensions",
        f'W {dims.get("width",0):.0f} x D {dims.get("depth",0):.0f} '
        f'x H {dims.get("height",0):.0f} mm' if dims else None, bold=True)
    sheet = dl.get("sheet") or {}
    row("Flat sheet", f'{sheet.get("width",0):.0f} x {sheet.get("height",0):.0f} mm'
        if sheet else None)
    row("Material", dl.get("material_hint"))
    row("Bleed", f'{dl.get("bleed", 3)} mm')

    section("Print")
    row("Colour", brief.get("color_mode") or "CMYK, 4/0")
    palette = (spec.get("palette") or {})
    if palette:
        row("Palette", ", ".join(f"{k}: {v}" for k, v in palette.items()
                                 if isinstance(v, str)))
    fonts = spec.get("fonts") or {}
    if fonts:
        from .artwork import FONT_CATALOG
        names = [f"{role}: {FONT_CATALOG.get(key, (key,))[0]}"
                 for role, key in fonts.items()]
        row("Typefaces", ", ".join(names))
    row("Finishing", brief.get("finish"))

    section("Notes")
    for note in (dl.get("notes") or []):
        pdf.set_fill(0.2, 0.25, 0.33)
        for chunk in _chunks(_latin1("- " + note), 86):
            pdf.text(18, y, chunk, 3.1)
            y += 4.4
        y += 1

    pdf.set_fill(0.6, 0.65, 0.72)
    pdf.text(18, 284, "Made with Packaging Studio. Dieline included at 1:1 "
                      "as both PDF and SVG.", 2.8)
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
