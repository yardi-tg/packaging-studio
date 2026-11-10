"""Configuration: reads .env, knows which providers are available."""

from __future__ import annotations
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"

_DEFAULTS = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "claude-opus-4-5-20251101",
    # Optional image providers. Leave empty to disable.
    "GEMINI_API_KEY": "",
    "GEMINI_IMAGE_MODEL": "gemini-3-pro-image-preview",
    "OPENAI_API_KEY": "",
    "OPENAI_IMAGE_MODEL": "gpt-image-1",
    "PORT": "8777",
    "MAX_AGENT_STEPS": "24",
    "CURRENCY": "USD",
}

# Price per million tokens, in USD.
#
# These are hard-coded so the app can show you a cost estimate without calling
# out to anything. Prices change -- check the current list at
# https://www.anthropic.com/pricing and edit this table if it drifts.
MODEL_PRICING = {
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "label": "Opus 4.5"},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "label": "Sonnet 4.5"},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "label": "Haiku 4.5"},
}
_FALLBACK_PRICE = {"input": 3.00, "output": 15.00, "label": "unknown model"}

# Cache reads are much cheaper than fresh input, cache writes slightly dearer.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


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
    """Priority: real environment variable > .env file > built-in default."""
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
    for prefix, price in MODEL_PRICING.items():
        if model.startswith(prefix):
            return price
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
        + cache_read * price["input"] * CACHE_READ_FACTOR
        + cache_write * price["input"] * CACHE_WRITE_FACTOR
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


def status() -> dict:
    model = get("ANTHROPIC_MODEL")
    return {
        "anthropic": bool(get("ANTHROPIC_API_KEY")),
        "model": model,
        "model_label": pricing_for(model)["label"],
        "image_provider": image_provider(),
        "port": get_int("PORT"),
        "pricing": pricing_for(model),
    }
