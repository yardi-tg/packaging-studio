"""Configuration: reads .env, knows which providers are available."""

from __future__ import annotations
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"

# The website build (Vercel, or `python3 server.py --hosted` to try it locally).
# Projects then live in the visitor's browser, the keys in the server's
# environment, and access needs a code. See core/hosted.py.
HOSTED = os.environ.get("PACKAGING_HOSTED", "").strip().lower() not in ("", "0", "false", "no")

# Settings learnt at run time where there is no .env to write to (hosted).
_runtime: dict = {}

_DEFAULTS = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "claude-opus-5-5",
    # How hard the model thinks: medium, high or max. Higher designs better
    # and costs more, because thinking is billed as output.
    "ANTHROPIC_EFFORT": "high",
    # Only change this if you route traffic through a proxy.
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    # Optional image providers. Leave empty to disable.
    "GEMINI_API_KEY": "",
    "GEMINI_IMAGE_MODEL": "gemini-3-pro-image-preview",
    "GEMINI_AUTH": "",
    "OPENAI_API_KEY": "",
    "OPENAI_IMAGE_MODEL": "gpt-image-1",
    "PORT": "8777",
    "MAX_AGENT_STEPS": "24",
    "CURRENCY": "USD",
}

# Price per million tokens, in USD, used for the cost read-out in the app.
#
# From Anthropic's model pages, November 2026. Prices change, so treat the
# numbers in the app as an estimate and check https://www.anthropic.com/pricing
# if they ever look off. Haiku 5.5 is the short-prompt rate (under 100k tokens).
MODEL_PRICING = {
    "claude-fable-5-2":  {"input": 8.00, "output": 40.00, "cache_read": 0.80,
                          "cache_write": 10.00, "label": "Fable 5.2"},
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_read": 0.20,
                          "cache_write": 5.00, "label": "Opus 5.5"},
    "claude-opus-5":     {"input": 5.00, "output": 25.00, "cache_read": 0.50,
                          "cache_write": 6.25, "label": "Opus 5"},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10,
                          "cache_write": 2.50, "label": "Sonnet 5.5"},
    "claude-haiku-5-5":  {"input": 0.10, "output": 0.50, "cache_read": 0.01,
                          "cache_write": 0.125, "label": "Haiku 5.5"},
    # older ids, in case an .env still names one
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50,
                          "cache_write": 6.25, "label": "Opus 4.5"},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30,
                          "cache_write": 3.75, "label": "Sonnet 4.5"},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00, "cache_read": 0.10,
                          "cache_write": 1.25, "label": "Haiku 4.5"},
}
# Used only when a model's table entry has no explicit cache prices.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25

_FALLBACK_PRICE = {"input": 4.00, "output": 20.00, "cache_read": 0.20,
                   "cache_write": 5.00, "label": "unknown model"}

# The choices offered in Settings, best first.
MODEL_CHOICES = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5",
     "note": "best design judgement, recommended"},
    {"id": "claude-fable-5-2", "label": "Fable 5.2",
     "note": "most capable, priciest"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5",
     "note": "good balance for everyday edits"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5",
     "note": "very fast and cheap, for small fixes"},
]


def _parse_env_file(path: pathlib.Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


_file_env = _parse_env_file(ENV_FILE)


def get(key: str, default=None):
    """Priority: learnt at run time > environment variable > .env > default."""
    if _runtime.get(key):
        return _runtime[key]
    if os.environ.get(key):
        return os.environ[key]
    if _file_env.get(key):
        return _file_env[key]
    if default is not None:
        return default
    return _DEFAULTS.get(key, "")


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(_DEFAULTS.get(key, "0"))


def reload_env():
    global _file_env
    _file_env = _parse_env_file(ENV_FILE)


def write_env(updates: dict):
    """Write keys back to .env without destroying what is already there."""
    if HOSTED:
        # Read-only disk, and keys belong in the host's environment settings.
        # Only what the program learns (which image model works) is kept, in
        # memory, for as long as this server instance lives.
        _runtime.update({k: v for k, v in updates.items()
                         if v and not k.endswith("_API_KEY")})
        return
    existing = _parse_env_file(ENV_FILE)
    existing.update({k: v for k, v in updates.items() if v is not None})
    lines = [
        "# Packaging Studio configuration",
        "# This file stays on your machine. Never upload it, never commit it.",
        "",
    ]
    for key in _DEFAULTS:
        lines.append(f"{key}={existing.get(key, _DEFAULTS[key])}")
    for key, value in existing.items():
        if key not in _DEFAULTS:
            lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    reload_env()


_UPGRADES = {
    "claude-opus-4-5": "claude-opus-5-5",
    "claude-sonnet-4-5": "claude-sonnet-5-5",
    "claude-haiku-4-5": "claude-haiku-5-5",
}


def migrate():
    """Move an older .env onto the current model generation."""
    current = _file_env.get("ANTHROPIC_MODEL", "")
    for old, new in _UPGRADES.items():
        if current.startswith(old):
            write_env({"ANTHROPIC_MODEL": new})
            return new
    return None


def image_provider() -> str:
    """Which image provider is active: 'gemini', 'openai' or 'none'."""
    if get("GEMINI_API_KEY"):
        return "gemini"
    if get("OPENAI_API_KEY"):
        return "openai"
    return "none"


def pricing_for(model: str) -> dict:
    """Look up the price table entry for a model id."""
    model = (model or "").strip()
    for prefix in sorted(MODEL_PRICING, key=len, reverse=True):
        if model.startswith(prefix):
            return MODEL_PRICING[prefix]
    return _FALLBACK_PRICE


def price_usage(usage: dict, model: str) -> dict:
    """Turn a usage block from the API into a cost estimate in USD."""
    price = pricing_for(model)
    plain_in = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)

    cost = (
        plain_in * price["input"]
        + cache_read * price.get("cache_read", price["input"] * CACHE_READ_FACTOR)
        + cache_write * price.get("cache_write", price["input"] * CACHE_WRITE_FACTOR)
        + out * price["output"]
    ) / 1_000_000.0

    return {
        "input_tokens": plain_in + cache_read + cache_write,
        "output_tokens": out,
        "cached_tokens": cache_read,
        "cost_usd": round(cost, 6),
        "model": model,
        "model_label": price["label"],
    }


# ---------------------------------------------------------------------------
# The website: which models a visitor may pick
# ---------------------------------------------------------------------------

HOSTED_NOTES = {
    "claude-sonnet-5-5": "recommended: strong design at a fair price",
    "claude-haiku-5-5": "fastest and cheapest, for small edits",
    "claude-opus-5-5": "best design judgement, about twice the price of Sonnet",
    "claude-fable-5-2": "most capable, about four times the price of Sonnet",
}


def hosted_models() -> list:
    """ALLOWED_MODELS, comma-separated. Opus is left out by default: on a
    shared site the owner pays, and Sonnet designs well for half the price."""
    wanted = [m.strip() for m in (get("ALLOWED_MODELS", "claude-sonnet-5-5,claude-haiku-5-5")
                                  or "").split(",") if m.strip()]
    out = []
    for model_id in wanted or ["claude-sonnet-5-5"]:
        known = next((m for m in MODEL_CHOICES if m["id"] == model_id), None)
        out.append({"id": model_id,
                    "label": known["label"] if known else pricing_for(model_id)["label"],
                    "note": HOSTED_NOTES.get(model_id, known["note"] if known else "")})
    return out


def hosted_default_model() -> str:
    ids = [m["id"] for m in hosted_models()]
    wanted = get("HOSTED_MODEL", "claude-sonnet-5-5")
    return wanted if wanted in ids else ids[0]


def hosted_efforts() -> tuple:
    allowed = [e.strip() for e in (get("HOSTED_EFFORTS", "medium,high") or "").split(",")]
    return tuple(e for e in allowed if e in ("low", "medium", "high", "xhigh", "max")) or ("high",)


def hosted_default_effort() -> str:
    wanted = get("HOSTED_EFFORT", "high")
    efforts = hosted_efforts()
    return wanted if wanted in efforts else efforts[-1]


def status() -> dict:
    model = get("ANTHROPIC_MODEL")
    return {
        "anthropic": bool(get("ANTHROPIC_API_KEY")),
        "effort": get("ANTHROPIC_EFFORT") or "high",
        "model": model,
        "model_label": pricing_for(model)["label"],
        "image_provider": image_provider(),
        "port": get_int("PORT"),
        "pricing": pricing_for(model),
    }
