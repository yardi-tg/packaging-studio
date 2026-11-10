"""Image providers: generating pictures, and placing the package in a scene.

Claude does not generate pixel images. This module is the slot for a model that
does. With a key configured, two things become possible:

  generate()   a motif from a text prompt, to use as a design element
  photoshoot() take the 3D render of the finished package and put it somewhere
               real -- held in a hand, on a marble counter, on a shelf

The second is the interesting one. The package geometry and artwork stay exactly
as designed, because the model is editing a render rather than inventing a box.

Keys are never logged and never leave this machine except in the request to the
provider you configured.
"""

from __future__ import annotations
import base64
import json
import re
import urllib.error
import urllib.request

from . import config, store

GEMINI_HOST = "https://generativelanguage.googleapis.com"
OPENAI_HOST = "https://api.openai.com"

_ASPECT_TO_SIZE = {
    "1:1": "1024x1024", "3:4": "1024x1536", "4:3": "1536x1024",
    "9:16": "1024x1536", "16:9": "1536x1024",
}

# Best first. Whatever the account actually has access to wins.
GEMINI_PREFERENCE = [
    "gemini-3-pro-image", "gemini-3-flash-image",
    "gemini-2.5-flash-image", "gemini-2.0-flash-preview-image-generation",
    "imagen-4.0-ultra", "imagen-4.0", "imagen-3.0",
]


def provider() -> str:
    return config.image_provider()


def available() -> bool:
    return provider() != "none"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, headers: dict, timeout: int = 240) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {str(detail)[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect: {exc.reason}") from exc


def _get(url: str, headers: dict, timeout: int = 40) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect: {exc.reason}") from exc


def _gemini_auth_variants(key: str):
    """Google has shipped several key formats. Try the plausible headers,
    starting with whichever one worked last time."""
    header = {"x-goog-api-key": key}
    bearer = {"Authorization": f"Bearer {key}"}
    return [bearer, header] if config.get("GEMINI_AUTH") == "bearer" else [header, bearer]


# ---------------------------------------------------------------------------
# Probing: what can this key actually do?
# ---------------------------------------------------------------------------

def probe() -> dict:
    """Check the configured key and find the best image model it can reach.

    Runs on the user's own machine, which is the only place with a free route
    to the provider. Returns something the settings panel can display.
    """
    which = provider()
    if which == "none":
        return {"ok": False, "provider": "none",
                "message": "No image provider key configured."}
    if which == "gemini":
        return _probe_gemini()
    return _probe_openai()


def _probe_gemini() -> dict:
    key = config.get("GEMINI_API_KEY")
    errors = []
    for headers in _gemini_auth_variants(key):
        try:
            data = _get(f"{GEMINI_HOST}/v1beta/models?pageSize=200", headers)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue

        names = []
        for model in data.get("models", []):
            name = model.get("name", "").split("/")[-1]
            methods = model.get("supportedGenerationMethods") or []
            if not name:
                continue
            looks_visual = "image" in name.lower() or name.startswith("imagen")
            usable = ("generateContent" in methods or "predict" in methods
                      or not methods)
            if looks_visual and usable:
                names.append(name)

        if not names:
            return {"ok": False, "provider": "gemini",
                    "message": "The key works, but no image model is enabled on "
                               "this account. Enable one in Google AI Studio.",
                    "all_models": len(data.get("models", []))}

        best = _rank_gemini(names)
        config.write_env({"GEMINI_IMAGE_MODEL": best,
                          "GEMINI_AUTH": "header" if "x-goog-api-key" in headers else "bearer"})
        return {
            "ok": True, "provider": "gemini", "model": best,
            "models": names[:25],
            "auth": "header" if "x-goog-api-key" in headers else "bearer",
            "message": f"Connected. Using {best}.",
        }
    return {"ok": False, "provider": "gemini",
            "message": "The key was rejected. " + (errors[0] if errors else ""),
            "detail": errors}


def _rank_gemini(names: list) -> str:
    for wanted in GEMINI_PREFERENCE:
        for name in names:
            if name.startswith(wanted):
                return name
    # Otherwise prefer the highest version number we can see.
    def score(n):
        m = re.search(r"(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    return sorted(names, key=score, reverse=True)[0]


def _probe_openai() -> dict:
    key = config.get("OPENAI_API_KEY")
    try:
        data = _get(f"{OPENAI_HOST}/v1/models", {"Authorization": f"Bearer {key}"})
    except RuntimeError as exc:
        return {"ok": False, "provider": "openai",
                "message": f"The key was rejected. {exc}"}
    names = [m.get("id", "") for m in data.get("data", [])]
    for wanted in ("gpt-image-1.5", "gpt-image-1", "dall-e-3"):
        if wanted in names:
            config.write_env({"OPENAI_IMAGE_MODEL": wanted})
            return {"ok": True, "provider": "openai", "model": wanted,
                    "message": f"Connected. Using {wanted}."}
    return {"ok": False, "provider": "openai",
            "message": "The key works, but no image model is available on it."}


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------

GUARDRAIL = (
    "Do not render any readable lettering that is not already present in the "
    "supplied image. No logos of existing brands. No recognisable real people: "
    "hands and bodies may appear, but never an identifiable face of a real "
    "individual."
)


def _gemini_call(parts: list, aspect: str, model: str = None) -> tuple:
    key = config.get("GEMINI_API_KEY")
    model = model or config.get("GEMINI_IMAGE_MODEL")
    url = f"{GEMINI_HOST}/v1beta/models/{model}:generateContent"

    bodies = [
        {"contents": [{"role": "user", "parts": parts}],
         "generationConfig": {"responseModalities": ["IMAGE"],
                              "imageConfig": {"aspectRatio": aspect}}},
        {"contents": [{"role": "user", "parts": parts}],
         "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
        {"contents": [{"role": "user", "parts": parts}]},
    ]
    last = None
    for headers in _gemini_auth_variants(key):
        for body in bodies:
            try:
                data = _post(url, body, headers)
            except RuntimeError as exc:
                last = exc
                continue
            found = _extract_gemini_image(data)
            if found:
                return found
            last = RuntimeError("The model replied without an image.")
    raise last or RuntimeError("Gemini returned nothing usable.")


def _extract_gemini_image(data: dict):
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = (inline.get("mimeType") or inline.get("mime_type")
                        or "image/png")
                return base64.b64decode(inline["data"]), mime
    return None


def _openai_call(prompt: str, aspect: str) -> tuple:
    key = config.get("OPENAI_API_KEY")
    data = _post(
        f"{OPENAI_HOST}/v1/images/generations",
        {"model": config.get("OPENAI_IMAGE_MODEL"), "prompt": prompt,
         "size": _ASPECT_TO_SIZE.get(aspect, "1024x1024"), "n": 1},
        {"authorization": f"Bearer {key}"},
    )
    items = data.get("data") or []
    if items and items[0].get("b64_json"):
        return base64.b64decode(items[0]["b64_json"]), "image/png"
    raise RuntimeError("OpenAI returned no image.")


def generate(project: dict, prompt: str, aspect: str = "1:1") -> dict:
    """A motif from a text prompt, stored as a project asset."""
    which = provider()
    if which == "none":
        raise RuntimeError("No image provider configured.")
    text = prompt.strip() + "\n\n" + GUARDRAIL
    if which == "gemini":
        data, mime = _gemini_call([{"text": text}], aspect)
    else:
        data, mime = _openai_call(text, aspect)
    ext = {"image/png": "png", "image/jpeg": "jpg",
           "image/webp": "webp"}.get(mime, "png")
    asset = store.add_asset(project, f"generated.{ext}", data, role="generated")
    asset["prompt"] = prompt[:400]
    return asset


# ---------------------------------------------------------------------------
# Photoshoot
# ---------------------------------------------------------------------------

SCENES = {
    "hand": {
        "label": "In hand",
        "blurb": "Held up, close, natural skin",
        "prompt": "A person holding this package up in one hand, photographed "
                  "close. Natural skin texture and realistic fingers wrapping "
                  "the box. Soft window light from the left, shallow depth of "
                  "field so the background falls away.",
    },
    "marble": {
        "label": "Marble counter",
        "blurb": "Bathroom, bright and clean",
        "prompt": "The package standing on a pale marble bathroom counter, with "
                  "a folded linen towel and a small eucalyptus sprig slightly "
                  "out of focus behind it. Bright, clean morning light, soft "
                  "reflection on the stone.",
    },
    "wood": {
        "label": "Wooden table",
        "blurb": "Warm, low afternoon sun",
        "prompt": "The package on a warm oak table in low afternoon sunlight, "
                  "long soft shadow falling to the right, a few dried botanicals "
                  "scattered nearby. Shot at table height.",
    },
    "shelf": {
        "label": "Retail shelf",
        "blurb": "In store, among neighbours",
        "prompt": "The package on a retail shelf in a well-lit beauty store, "
                  "with blurred unbranded products either side for context. "
                  "Straight-on eye-level shot, even store lighting.",
    },
    "studio": {
        "label": "Studio seamless",
        "blurb": "Clean e-commerce cutout",
        "prompt": "The package on a seamless studio backdrop in a soft neutral "
                  "tone, lit with a large softbox from the upper left and a "
                  "subtle reflection beneath. Clean commercial product shot, "
                  "nothing else in frame.",
    },
    "flatlay": {
        "label": "Flat lay",
        "blurb": "From above, styled",
        "prompt": "An overhead flat lay: the package lying on a textured linen "
                  "surface surrounded by a few carefully arranged props that "
                  "suit the product. Even diffuse daylight, shot straight down.",
    },
    "outdoor": {
        "label": "Outdoors",
        "blurb": "Daylight, dappled shade",
        "prompt": "The package outdoors on a weathered stone ledge in dappled "
                  "shade, leaves casting soft patterns across it. Natural "
                  "daylight, slight warmth, shallow depth of field.",
    },
    "heritage": {
        "label": "Ethiopian heritage",
        "blurb": "Woven basket, habesha cloth",
        "prompt": "The package standing on the lid of a hand-woven Ethiopian basket, "
                  "a white cotton habesha cloth with a colourful woven border draped "
                  "behind it, a clay coffee pot and a few roasted coffee beans softly "
                  "out of focus. Warm late-afternoon window light, natural and "
                  "respectful, editorial product photography.",
    },
    "vanity": {
        "label": "Vanity table",
        "blurb": "Mirror, warm glow, everyday",
        "prompt": "The package on a light wooden vanity table in front of a round "
                  "mirror, a ceramic dish and a small glass vase nearby, softly out "
                  "of focus. Warm, flattering morning light from a window to the "
                  "side. Lifestyle beauty photography.",
    },
    "dark": {
        "label": "Dark and moody",
        "blurb": "Hard light, deep shadow",
        "prompt": "The package on a dark textured surface lit by a single hard "
                  "light from the side, deep shadows, a rim of light along one "
                  "edge. Moody premium advertising look.",
    },
}

PRESERVE = (
    "This is a render of a real packaging design. Keep the package exactly as "
    "shown: identical proportions, identical artwork, identical colours, and "
    "identical text. Do not redesign it, do not re-letter it, do not add or "
    "remove anything printed on it. Only build the photographic scene around it "
    "and light it convincingly, with contact shadows where it meets the surface."
)


def _mime_of(data: bytes) -> str:
    """What a picture really is, whatever it was called on the way here."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def scene_catalog() -> list:
    return [{"id": key, "label": value["label"], "blurb": value["blurb"]}
            for key, value in SCENES.items()]


def photoshoot(project: dict, render_png: bytes, scene: str = "studio",
               notes: str = "", aspect: str = "1:1", front_png: bytes = None) -> dict:
    """Place the rendered package into a photographic scene."""
    which = provider()
    if which == "none":
        raise RuntimeError(
            "A photoshoot needs an image model. Add a Google Gemini key under "
            "Settings; Claude cannot produce photographs."
        )
    if which != "gemini":
        raise RuntimeError(
            "Photoshoot needs Gemini, because it edits the render rather than "
            "inventing a new box. Add a Gemini key under Settings."
        )

    preset = SCENES.get(scene) or SCENES["studio"]
    brief = project.get("brief") or {}
    context = ", ".join(filter(None, [
        brief.get("product_name"), brief.get("category"), brief.get("volume"),
    ]))

    prompt = PRESERVE + "\n\nScene: " + preset["prompt"]
    if context:
        prompt += f"\n\nThe product is {context}."
    if notes.strip():
        prompt += f"\n\nAlso: {notes.strip()}"
    prompt += "\n\n" + GUARDRAIL

    parts = [{"inline_data": {"mime_type": _mime_of(render_png),
                              "data": base64.b64encode(render_png).decode("ascii")}}]
    if front_png:
        # The 3D render is small and slightly angled, so fine lettering is soft.
        # The flat front at full resolution is the model's reference for what
        # the print must say and look like, letter for letter.
        parts.append({"inline_data": {"mime_type": _mime_of(front_png),
                                      "data": base64.b64encode(front_png).decode("ascii")}})
        prompt += ("\n\nThe second image is the exact flat artwork of the front "
                   "panel. Use it as the reference for every letter, colour and "
                   "graphic on the front of the package.")
    parts.append({"text": prompt})
    data, mime = _gemini_call(parts, aspect)
    ext = {"image/png": "png", "image/jpeg": "jpg",
           "image/webp": "webp"}.get(mime, "png")
    asset = store.add_asset(project, f"photo-{scene}.{ext}", data, role="photo")
    asset["scene"] = scene
    asset["prompt"] = preset["prompt"][:300]
    return asset
