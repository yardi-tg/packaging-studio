"""A stand-in for api.anthropic.com, strict where the real one is strict.

It streams scripted turns (thinking, text, tool calls) and checks every
request the way current models do:

  * no sampling parameters (temperature, top_p, top_k)
  * output_config.effort on models that take it
  * every thinking block replayed must still match what came before it --
    system prompt, tools, model and earlier messages. Edit history, drop an
    image from a tool result, change the system prompt mid-conversation, and
    the request is refused with a 400, as the real API does for new accounts.
  * cache_control never on a thinking block, at most four breakpoints

Run it on its own:  python3 tests/mock_anthropic.py 8899
"""

from __future__ import annotations
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG: list = []
SCRIPT: list = []
_LOCK = threading.Lock()


def _strip_cache(value):
    if isinstance(value, dict):
        return {k: _strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip_cache(v) for v in value]
    return value


def _sign(model, system, tools, messages_before) -> str:
    blob = json.dumps(_strip_cache({"m": model, "s": system, "t": tools,
                                    "p": messages_before}), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _problems(body: dict) -> list:
    out = []
    for key in ("temperature", "top_p", "top_k"):
        if key in body:
            out.append(f"{key} is not supported for this model")
    if body.get("model", "").startswith(("claude-opus-5", "claude-sonnet-5")):
        if (body.get("output_config") or {}).get("effort") not in (
                "low", "medium", "high", "xhigh", "max"):
            out.append("expected output_config.effort")
    marks = 0
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if "cache_control" in block:
                    marks += 1
                    if block.get("type") in ("thinking", "redacted_thinking"):
                        out.append("cache_control cannot be set on thinking blocks")
                if block.get("_incomplete"):
                    out.append("unknown field _incomplete")
    for block in body.get("system") or []:
        if isinstance(block, dict) and "cache_control" in block:
            marks += 1
    if marks > 4:
        out.append("A maximum of 4 blocks with cache_control may be provided")
    # the prefix check
    messages = body.get("messages", [])
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if block.get("type") == "thinking":
                expected = _sign(body.get("model"), body.get("system"),
                                 body.get("tools"), messages[:index])
                if block.get("signature") != expected:
                    out.append("thinking block signature does not match its "
                               "prefix (thinking blocks cannot be modified)")
    return out


def _sse(handler, event: dict):
    handler.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
    handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        problems = _problems(body)
        with _LOCK:
            LOG.append({"body": body, "problems": problems})
            turn = SCRIPT.pop(0) if (SCRIPT and not problems) else None
        if problems:
            payload = json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "; ".join(problems)}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if turn is None:
            turn = [{"type": "text", "text": "Nothing more scripted."}]

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        signature = _sign(body.get("model"), body.get("system"),
                          body.get("tools"), body.get("messages"))
        _sse(self, {"type": "message_start", "message": {
            "id": "msg_mock", "type": "message", "role": "assistant", "content": [],
            "model": body.get("model"), "stop_reason": None,
            "usage": {"input_tokens": 1200, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 300, "output_tokens": 1}}})
        index = 0
        _sse(self, {"type": "content_block_start", "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        _sse(self, {"type": "content_block_delta", "index": index,
                    "delta": {"type": "thinking_delta", "thinking": "Considering the brief."}})
        _sse(self, {"type": "content_block_delta", "index": index,
                    "delta": {"type": "signature_delta", "signature": signature}})
        _sse(self, {"type": "content_block_stop", "index": index})
        stop = "end_turn"
        for block in turn:
            index += 1
            if block["type"] == "text":
                _sse(self, {"type": "content_block_start", "index": index,
                            "content_block": {"type": "text", "text": ""}})
                for word in block["text"].split(" "):
                    _sse(self, {"type": "content_block_delta", "index": index,
                                "delta": {"type": "text_delta", "text": word + " "}})
                _sse(self, {"type": "content_block_stop", "index": index})
            else:
                stop = "tool_use"
                _sse(self, {"type": "content_block_start", "index": index,
                            "content_block": {"type": "tool_use", "id": block["id"],
                                              "name": block["name"], "input": {}}})
                raw = json.dumps(block["input"])
                for i in range(0, len(raw), 400):
                    _sse(self, {"type": "content_block_delta", "index": index,
                                "delta": {"type": "input_json_delta",
                                          "partial_json": raw[i:i + 400]}})
                _sse(self, {"type": "content_block_stop", "index": index})
        _sse(self, {"type": "message_delta", "delta": {"stop_reason": stop},
                    "usage": {"output_tokens": 850}})
        _sse(self, {"type": "message_stop"})


def serve(port: int):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8899)
    print("mock running")
    threading.Event().wait()
