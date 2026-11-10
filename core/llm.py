"""Anthropic Messages API over a streaming connection, standard library only.

No pip install needed. Talks straight to api.anthropic.com.

Why streaming: the current models think before they answer, and a full design
pass can think for minutes. A plain request would sit silent until it either
finished or hit a timeout. A stream stays alive, lets the interface show the
reply as it is written, and tells us exactly how a tool call is progressing.
"""

from __future__ import annotations
import json
import time
import urllib.error
import urllib.request

from . import config

API_VERSION = "2023-06-01"


def _api_url() -> str:
    base = (config.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    return base + "/v1/messages"

# Models that accept output_config.effort. Anything else gets no effort field,
# because an unknown field is a 400.
_EFFORT_PREFIXES = (
    "claude-fable-", "claude-mythos-", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-haiku-5",
)
EFFORT_LEVELS = ("medium", "high", "max")
_MAX_TOKENS = {"low": 16000, "medium": 24000, "high": 32000,
               "xhigh": 64000, "max": 64000}
MAX_OUTPUT_CEILING = 128000


class LLMError(Exception):
    """An error worth showing to the user as-is."""


class ThinkingMismatch(LLMError):
    """The API refused earlier thinking blocks: the conversation prefix changed."""


class Truncated(LLMError):
    """The answer hit max_tokens part-way through a tool call."""

    def __init__(self, message, max_tokens):
        super().__init__(message)
        self.max_tokens = max_tokens


def supports_effort(model: str) -> bool:
    return any((model or "").startswith(p) for p in _EFFORT_PREFIXES)


def max_tokens_for(effort: str) -> int:
    return _MAX_TOKENS.get(effort or "high", 32000)


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------

def _open(payload: dict, api_key: str, timeout: int):
    req = urllib.request.Request(
        _api_url(),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _classify_http(exc: urllib.error.HTTPError) -> Exception:
    raw = exc.read().decode("utf-8", "replace")
    detail = raw
    try:
        detail = json.loads(raw).get("error", {}).get("message", raw)
    except Exception:
        pass
    low = str(detail).lower()
    if exc.code == 401:
        return LLMError("Anthropic rejected the API key (401). Check that it is "
                        "correct and still active.")
    if exc.code == 403:
        return LLMError(f"Anthropic refused access (403): {detail}")
    if exc.code == 404 and "model" in low:
        return LLMError(f"This model is not available on your account: {detail}. "
                        "Pick another one under Settings.")
    if exc.code == 400 and "thinking" in low and any(
            w in low for w in ("signature", "prefix", "binding", "block")):
        return ThinkingMismatch(str(detail))
    if exc.code == 400:
        return LLMError(f"Bad request (400): {detail}")
    if exc.code in (429, 500, 502, 503, 504, 529):
        return _Retryable(f"Anthropic returned {exc.code}: {detail}")
    return LLMError(f"Anthropic error {exc.code}: {detail}")


class _Retryable(LLMError):
    pass


def stream(messages: list, system: str = "", tools: list = None,
           model: str = None, effort: str = None, max_tokens: int = None,
           on_event=None, retries: int = 4, timeout: int = 240) -> dict:
    """One model turn. Returns the message exactly as the API produced it.

    on_event receives small dicts while the turn is being written:
      {"kind": "text", "text": "..."}             a piece of visible reply
      {"kind": "thinking"}                         the model started thinking
      {"kind": "tool", "name": "...", "chars": n}  a tool call is being written
      {"kind": "restart"}                          a retry began; drop partial text
    """
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("No ANTHROPIC_API_KEY set. Add it under Settings, top "
                       "right, or in the .env file next to server.py.")
    model = model or config.get("ANTHROPIC_MODEL")
    effort = effort or config.get("ANTHROPIC_EFFORT") or "high"
    max_tokens = max_tokens or max_tokens_for(effort)
    notify = on_event or (lambda _e: None)

    payload = {"model": model, "max_tokens": max_tokens,
               "messages": messages, "stream": True}
    if system:
        # The system prompt never changes during a conversation, so it caches.
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
    if tools:
        payload["tools"] = tools
    if supports_effort(model):
        payload["output_config"] = {"effort": effort}

    last = None
    for attempt in range(retries):
        if attempt:
            notify({"kind": "restart"})
            time.sleep(min(30, 2 ** attempt * 2))
        try:
            resp = _open(payload, api_key, timeout)
        except urllib.error.HTTPError as exc:
            err = _classify_http(exc)
            if isinstance(err, _Retryable):
                last = err
                continue
            raise err from exc
        except urllib.error.URLError as exc:
            last = LLMError(f"Could not reach api.anthropic.com ({exc.reason}). "
                            "Check your internet connection.")
            continue
        try:
            message = _read_stream(resp, notify)
        except _Retryable as exc:
            last = exc
            continue
        except (OSError, ValueError) as exc:
            last = LLMError(f"The connection to Anthropic dropped: {exc}")
            continue
        finally:
            try:
                resp.close()
            except Exception:
                pass

        broken = [b for b in message["content"]
                  if b.get("type") == "tool_use" and b.get("_incomplete")]
        if broken:
            raise Truncated("The answer was cut off in the middle of a tool call.",
                            max_tokens)
        return message
    raise last or LLMError("Unknown error calling Anthropic.")


def _read_stream(resp, notify) -> dict:
    """Rebuild the final message from server-sent events."""
    message = {"role": "assistant", "content": [], "usage": {}}
    partial = {}
    names = {}
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body:
            continue
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")

        if kind == "message_start":
            start = event.get("message") or {}
            message["id"] = start.get("id")
            message["model"] = start.get("model")
            message["usage"] = dict(start.get("usage") or {})

        elif kind == "content_block_start":
            index = event.get("index", len(message["content"]))
            block = dict(event.get("content_block") or {})
            btype = block.get("type")
            if btype == "tool_use":
                partial[index] = ""
                names[index] = block.get("name", "")
                block["input"] = {}
                notify({"kind": "tool", "name": block.get("name", ""), "chars": 0})
            elif btype == "text":
                block.setdefault("text", "")
            elif btype == "thinking":
                block.setdefault("thinking", "")
                block.setdefault("signature", "")
                notify({"kind": "thinking"})
            while len(message["content"]) <= index:
                message["content"].append(None)
            message["content"][index] = block

        elif kind == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta") or {}
            block = message["content"][index] if index < len(message["content"]) else None
            if block is None:
                continue
            dtype = delta.get("type")
            if dtype == "text_delta":
                piece = delta.get("text", "")
                block["text"] = block.get("text", "") + piece
                notify({"kind": "text", "text": piece})
            elif dtype == "input_json_delta":
                partial[index] = partial.get(index, "") + delta.get("partial_json", "")
                size = len(partial[index])
                if size // 1500 != (size - len(delta.get("partial_json", ""))) // 1500:
                    notify({"kind": "tool", "name": names.get(index, ""), "chars": size})
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))

        elif kind == "content_block_stop":
            index = event.get("index", 0)
            if index in partial:
                text = partial.pop(index)
                try:
                    message["content"][index]["input"] = json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    message["content"][index]["_incomplete"] = True

        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if "stop_reason" in delta:
                message["stop_reason"] = delta.get("stop_reason")
            for key, value in (event.get("usage") or {}).items():
                if value is not None:
                    message["usage"][key] = value

        elif kind == "message_stop":
            break

        elif kind == "error":
            err = event.get("error") or {}
            text = f'{err.get("type", "error")}: {err.get("message", "")}'
            if err.get("type") in ("overloaded_error", "api_error", "rate_limit_error"):
                raise _Retryable(text)
            raise LLMError(text)

    # a tool call still open when the stream ended was cut off
    for index in list(partial):
        if message["content"][index] is not None:
            message["content"][index]["_incomplete"] = True
    message["content"] = [b for b in message["content"] if b is not None]
    return message


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def text_of(response: dict) -> str:
    """All visible text blocks of a response, joined."""
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
        text = text.strip().rstrip("`").strip()
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
