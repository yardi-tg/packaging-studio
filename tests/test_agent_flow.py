"""End-to-end test of the agent loop against the strict mock API.

Drives the real server over HTTP the way the browser does, including answering
render requests, and checks that the conversation survives what breaks naive
implementations on current models: tool loops with thinking, a picture in a
tool result, a follow-up message, a model switch, and a tampered history.

Run:  python3 tests/test_agent_flow.py
"""

from __future__ import annotations
import base64
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_anthropic as mock  # noqa: E402

MOCK_PORT, APP_PORT = 8899, 8798
WORK = pathlib.Path(tempfile.mkdtemp(prefix="ps-test-"))
os.environ.update({
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ANTHROPIC_MODEL": "claude-opus-5-5",
    "ANTHROPIC_EFFORT": "high",
    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
})

from core import config  # noqa: E402
config.PROJECTS_DIR = WORK / "projects"
config.ENV_FILE = WORK / ".env"
import server  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 2048 + b"\xff\xd9").decode()


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method=method or ("POST" if data else "GET"),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def chat(project_id, text):
    req = urllib.request.Request(f"{BASE}/api/projects/{project_id}/chat",
                                 data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json"})
    events = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event["type"] == "render_request":   # play the browser's part
                call(f"/api/projects/{project_id}/render_result",
                     {"request_id": event["request_id"], "png": JPEG,
                      "media_type": "image/jpeg"})
            if event["type"] == "end":
                break
    return events


def tool(name, input_, id_):
    return {"type": "tool_use", "name": name, "input": input_, "id": id_}


FRONT = {"background": "#123b33", "layers": [
    {"id": "front-brand", "type": "text", "name": "Brand", "text": "HABESHA",
     "x": 6, "y": 8, "w": 38, "size": 3, "font": "display", "fill": "#f4efe3",
     "align": "center"},
    {"id": "front-name", "type": "text", "name": "Name", "text": "Glow Serum",
     "x": 6, "y": 50, "w": 38, "size": 5.5, "font": "display", "fill": "#f4efe3",
     "align": "center"},
]}


def main():
    mock.serve(MOCK_PORT)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", APP_PORT), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    failures = []

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            failures.append(label)

    project = call("/api/projects", {"name": "Test"})["project"]
    pid = project["id"]

    # --- run 1: full first pass with a look at the design -------------------
    mock.SCRIPT[:] = [
        [{"type": "text", "text": "Straight tuck end it is."},
         tool("set_brief", {"product_name": "Glow Serum", "brand": "Habesha"}, "t1"),
         tool("build_structure", {"box_type": "ste", "width_mm": 50, "depth_mm": 40,
                                  "height_mm": 120}, "t2")],
        [tool("design_artwork", {"concept": "dark green, quiet",
                                 "fonts": {"display": "cormorant", "body": "jost"},
                                 "panels": {"front": FRONT}}, "t3")],
        [tool("look_at_design", {"panel": "front"}, "t4")],
        [{"type": "text", "text": "Done. Calm, one gold accent."}],
    ]
    events = chat(pid, "Face serum, 30 ml, brand Habesha")
    kinds = [e["type"] for e in events]
    print("run 1")
    check("done" in kinds and "error" not in kinds, "first pass completes without error")
    check("assistant_delta" in kinds, "reply streams in pieces")
    check("render_request" in kinds, "agent asked to see its design")
    check(any(e["type"] == "status" and "Designing" in e.get("text", "") for e in events),
          "tool progress is reported")
    bodies = [entry["body"] for entry in mock.LOG]
    check(all("temperature" not in b for b in bodies), "no temperature sent")
    check(all((b.get("output_config") or {}).get("effort") == "high" for b in bodies),
          "effort sent on every call")
    check(len({json.dumps(b.get("system")) for b in bodies}) == 1,
          "system prompt identical across the whole run")
    last = bodies[-1]["messages"]
    has_image = any(
        isinstance(block.get("content"), list)
        and any(inner.get("type") == "image" for inner in block["content"])
        for message in last if isinstance(message.get("content"), list)
        for block in message["content"] if block.get("type") == "tool_result")
    check(has_image, "the rendered picture reached the model")
    check("<project_state>" in json.dumps(bodies[0]["messages"][0]),
          "project state rides in the user message")
    stored = call(f"/api/projects/{pid}")["project"]
    check(bool(stored.get("artwork")), "artwork saved")
    check(stored["usage"]["cost_usd"] > 0, "cost recorded")
    check(not any(entry["problems"] for entry in mock.LOG), "mock saw no rule violations")

    # --- run 2: a follow-up must replay history exactly -----------------------
    mock.LOG.clear()
    mock.SCRIPT[:] = [
        [tool("edit_layers", {"patches": [{"op": "update", "id": "front-name",
                                            "set": {"size": 7}}]}, "t5")],
        [{"type": "text", "text": "Bigger now."}],
    ]
    events = chat(pid, "Make the name bigger")
    print("run 2")
    check(not any(entry["problems"] for entry in mock.LOG),
          "history replayed byte-exact, image included (prefix check passed)")
    check(any(e["type"] == "done" for e in events) and
          not any(e["type"] == "error" for e in events), "follow-up completes")
    layer = next(l for l in call(f"/api/projects/{pid}")["project"]["layers"]
                 if l["id"] == "front-name")
    check(layer is not None, "edit applied to the named layer")

    # --- run 3: switching model starts a fresh conversation -------------------
    mock.LOG.clear()
    call("/api/settings", {"ANTHROPIC_MODEL": "claude-sonnet-5-5"})
    os.environ["ANTHROPIC_MODEL"] = "claude-sonnet-5-5"
    mock.SCRIPT[:] = [[{"type": "text", "text": "Hello again."}]]
    events = chat(pid, "Still there?")
    print("run 3")
    first = mock.LOG[0]
    check(not first["problems"], "no 400 after the model switch")
    check(len(first["body"]["messages"]) == 1, "old conversation folded away")
    check("summarised" in json.dumps(first["body"]["messages"][0]),
          "a summary of the earlier chat came along")

    # --- run 4: a tampered history is recovered from --------------------------
    mock.LOG.clear()
    path = config.PROJECTS_DIR / pid / "project.json"
    data = json.loads(path.read_text())
    # Rewrite an earlier user message: every thinking block after it is now
    # signed over a prefix that no longer exists.
    for entry in data["messages"]:
        if entry.get("role") == "user" and entry.get("api") and not entry.get("archived"):
            entry["api"]["content"].append({"type": "text", "text": "edited after the fact"})
            break
    path.write_text(json.dumps(data))
    mock.SCRIPT[:] = [[{"type": "text", "text": "Recovered."}]]
    events = chat(pid, "And now?")
    print("run 4")
    check(any(entry["problems"] for entry in mock.LOG), "the mock refused the edited history")
    check(any(e["type"] == "assistant" and "Recovered" in e["text"] for e in events),
          "the agent folded the conversation and carried on")

    httpd.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    print()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
