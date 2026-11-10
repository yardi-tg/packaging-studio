"""Anthropic Messages API, using nothing but the Python standard library.

No pip install needed. Talks straight to api.anthropic.com.
"""

from __future__ import annotations
import json
import time
import urllib.error
import urllib.request

from . import config

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class LLMError(Exception):
    """An error worth showing to the user as-is."""


def _post(payload: dict, api_key: str, timeout: int = 300) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call(messages: list, system: str = "", tools: list = None,
         model: str = None, max_tokens: int = 8000,
         temperature: float = 1.0, retries: int = 3) -> dict:
    """One call to Claude. Returns the raw response."""
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError(
            "No ANTHROPIC_API_KEY set. Add it under Settings, top right, "
            "or in the .env file next to server.py."
        )

    payload = {
        "model": model or config.get("ANTHROPIC_MODEL"),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        # Cache the system prompt: it is long, it barely changes between
        # steps, and cached input costs a tenth of fresh input.
        payload["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    if tools:
        payload["tools"] = tools

    last_error = None
    for attempt in range(retries):
        try:
            return _post(payload, api_key)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except Exception:
                pass
            if exc.code == 401:
                raise LLMError(
                    "Anthropic rejected the API key (401). Check that it is "
                    "correct and still active."
                ) from exc
            if exc.code == 400:
                raise LLMError(f"Bad request (400): {detail}") from exc
            if exc.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                last_error = LLMError(f"Anthropic returned {exc.code}: {detail}")
                time.sleep(2 ** attempt * 2)
                continue
            raise LLMError(f"Anthropic error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                last_error = LLMError(f"Could not reach Anthropic: {exc.reason}")
                time.sleep(2 ** attempt * 2)
                continue
            raise LLMError(
                f"Could not reach api.anthropic.com ({exc.reason}). "
                "Check your internet connection."
            ) from exc
    raise last_error or LLMError("Unknown error calling Anthropic.")


def text_of(response: dict) -> str:
    """All text blocks of a response, joined."""
    return "\n".join(
        block.get("text", "")
        for block in response.get("content", [])
        if block.get("type") == "text"
    ).strip()


def tool_calls_of(response: dict) -> list:
    return [b for b in response.get("content", []) if b.get("type") == "tool_use"]


def cost_of(response: dict) -> dict:
    """Token usage and a USD estimate for one response."""
    return config.price_usage(
        response.get("usage") or {},
        response.get("model") or config.get("ANTHROPIC_MODEL"),
    )


def parse_json(raw: str) -> dict:
    """Parse JSON even when the model wraps it in fences or prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        text = parts[1] if len(parts) > 1 else ""
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Claude did not return valid JSON.")
