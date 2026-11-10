"""Font embedding.

An SVG that refers to a typeface by name alone loses it the moment the file is
loaded as an image or handed to someone else. So this module fetches the Google
Fonts a design actually uses, caches them locally, and embeds them as @font-face
rules with data URIs.

Which means:
  * the 3D preview shows the real typeface
  * exported SVGs are self-contained
  * with no internet it falls back cleanly to system fonts
"""

from __future__ import annotations
import base64
import pathlib
import re
import tempfile
import threading
import urllib.error
import urllib.request

from . import artwork, config

# On the website the program's own folder is read-only; the temp dir is not.
FONT_DIR = (pathlib.Path(tempfile.gettempdir()) / "packaging-studio-fonts"
            if config.HOSTED else config.ROOT / "fonts")
_CACHE: dict = {}
_LOCK = threading.Lock()
_FAILED: set = set()

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Which weights to fetch. More weights means bigger files.
_WEIGHTS = "400;700"
_WEIGHTS_VARIABLE = {
    "Bebas Neue": "",
    "Archivo Black": "",
    "Abril Fatface": "",
    "DM Serif Display": "",
}


def _family_query(family: str) -> str:
    name = family.replace(" ", "+")
    if family in _WEIGHTS_VARIABLE:
        return name
    return f"{name}:wght@{_WEIGHTS}"


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _load_family(family: str) -> str:
    """The @font-face rules for one family, with data URIs."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", family)
    cached = FONT_DIR / f"{safe}.css"
    for known in (cached, config.ROOT / "fonts" / f"{safe}.css"):
        if known.exists():
            return known.read_text(encoding="utf-8")
    if family in _FAILED:
        return ""

    try:
        css_url = (f"https://fonts.googleapis.com/css2?family="
                   f"{_family_query(family)}&display=swap")
        css = _fetch(css_url).decode("utf-8")
        blocks = []
        for match in re.finditer(r"@font-face\s*\{(.*?)\}", css, re.S):
            block = match.group(1)
            url_match = re.search(r"url\((https://[^)]+\.woff2)\)", block)
            if not url_match:
                continue
            # Basic Latin always; Ge'ez script for the Ethiopic families.
            # Everything else (Cyrillic, Vietnamese, ...) would only balloon
            # the file.
            unicode_range = re.search(r"unicode-range:\s*([^;]+);", block)
            ranges = unicode_range.group(1) if unicode_range else ""
            wanted = (not ranges or "U+0000-00FF" in ranges
                      or ("Ethiopic" in family and "U+12" in ranges))
            if not wanted:
                continue
            data = _fetch(url_match.group(1), timeout=30)
            if len(data) > 900_000:
                continue
            encoded = base64.b64encode(data).decode("ascii")
            weight = re.search(r"font-weight:\s*([^;]+);", block)
            style = re.search(r"font-style:\s*([^;]+);", block)
            # The unicode-range has to travel with the face, or two subsets of
            # the same weight overwrite each other.
            range_rule = f"unicode-range:{ranges.strip()};" if ranges else ""
            blocks.append(
                f"@font-face{{font-family:'{family}';"
                f"font-style:{style.group(1).strip() if style else 'normal'};"
                f"font-weight:{weight.group(1).strip() if weight else '400'};"
                f"{range_rule}"
                f"src:url(data:font/woff2;base64,{encoded}) format('woff2');}}"
            )
        result = "".join(blocks)
        if not result:
            _FAILED.add(family)
            return ""
        try:
            FONT_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_text(result, encoding="utf-8")
        except OSError:
            pass   # no disk cache; the in-memory one still holds it
        return result
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        _FAILED.add(family)
        return ""


def css_for(families) -> str:
    """Embedded @font-face rules for a set of families."""
    out = []
    with _LOCK:
        for family in sorted(set(families)):
            if family not in _CACHE:
                _CACHE[family] = _load_family(family)
            if _CACHE[family]:
                out.append(_CACHE[family])
    return "".join(out)


def families_in_spec(spec: dict, panel_ids=None) -> list:
    """Which font families does this artwork (or these panels of it) use?"""
    fonts = (spec or {}).get("fonts") or {}
    used = set()
    panels = (spec or {}).get("panels") or {}
    if panel_ids is not None:
        panels = {k: v for k, v in panels.items() if k in set(panel_ids)}
    for panel in panels.values():
        for layer in panel.get("layers") or []:
            kind = layer.get("type")
            if kind in ("text", "circle_text"):
                key = layer.get("font", "body")
            elif kind == "barcode":
                key = "mono"
            else:
                continue
            resolved = fonts.get(key, key if kind != "barcode" else "ibmmono")
            family, _ = artwork.font_stack(resolved)
            used.add(family)
    if not used and panel_ids is None:
        for key in fonts.values():
            used.add(artwork.font_stack(key)[0])
    return sorted(used)


def embed_css(spec: dict, panel_ids=None) -> str:
    """@font-face rules for the families in use. Pass panel_ids to embed only
    what one face needs: a lid with a single word should not carry a 500 KB
    Ethiopic font because the side panel uses it."""
    families = families_in_spec(spec, panel_ids)
    if not families:
        return ""
    return css_for(families)


def prewarm(spec: dict):
    """Warm the cache in the background so the first preview does not wait."""
    families = families_in_spec(spec)
    if families:
        threading.Thread(target=css_for, args=(families,), daemon=True).start()
