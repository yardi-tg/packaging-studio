"""Optional image provider.

Claude does not generate pixel images. This module is the slot for one: put a
GEMINI_API_KEY or OPENAI_API_KEY in .env and the agent can additionally produce
photographic motifs. With no key everything stays vector-based and the studio
works exactly as before.
"""

from __future__ import annotations
import base64
import json
import urllib.error
import urllib.request

from . import config, store

_ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
}


def provider() -> str:
    return config.image_provider()


def _post(url: str, payload: dict, headers: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect: {exc.reason}") from exc


def _gemini(prompt: str, aspect: str) -> tuple:
    key = config.get("GEMINI_API_KEY")
    model = config.get("GEMINI_IMAGE_MODEL")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    data = _post(
        url,
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": aspect},
            },
        },
        {"x-goog-api-key": key},
    )
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(inline["data"]), mime
    raise RuntimeError("Gemini returned no image.")


def _openai(prompt: str, aspect: str) -> tuple:
    key = config.get("OPENAI_API_KEY")
    model = config.get("OPENAI_IMAGE_MODEL")
    data = _post(
        "https://api.openai.com/v1/images/generations",
        {
            "model": model,
            "prompt": prompt,
            "size": _ASPECT_TO_SIZE.get(aspect, "1024x1024"),
            "n": 1,
        },
        {"authorization": f"Bearer {key}"},
    )
    items = data.get("data") or []
    if items and items[0].get("b64_json"):
        return base64.b64decode(items[0]["b64_json"]), "image/png"
    raise RuntimeError("OpenAI returned no image.")


def generate(project: dict, prompt: str, aspect: str = "1:1") -> dict:
    """Generate an image and store it as a project asset."""
    which = provider()
    if which == "none":
        raise RuntimeError("No image provider configured.")
    guarded = (
        prompt.strip()
        + "\n\nImportant: no readable lettering in the image, no logos, no "
          "existing brands, no recognisable real people. The image is a "
          "graphic element on a package."
    )
    data, mime = (_gemini if which == "gemini" else _openai)(guarded, aspect)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    asset = store.add_asset(project, f"generated.{ext}", data, role="generated")
    asset["prompt"] = prompt[:400]
    return asset
