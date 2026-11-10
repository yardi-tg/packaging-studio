"""The website protocol, end to end, against the strict mock API.

Plays the browser's part (web/hosted.js) in Python: keeps the project on the
client side, sends it with each request, applies the checkpoints that come
back, renders when the designer asks to look, and carries a paused run on with
a fresh request. The mock refuses any history that is not byte-exact, so a
mistake in how the client rebuilds the conversation fails here.

Run:  python3 tests/test_hosted.py
"""

from __future__ import annotations
import base64
import gzip
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_anthropic as mock  # noqa: E402

MOCK_PORT = 8897
os.environ.update({
    "PACKAGING_HOSTED": "1",
    "ACCESS_CODES": "6767",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
})
os.environ.pop("ALLOWED_MODELS", None)

from core import hosted  # noqa: E402

CODE = {"x-access-code": "6767", "x-forwarded-for": "10.0.0.1"}
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 4096 + b"\xff\xd9").decode()
FRONT = {"background": "#123b33", "layers": [
    {"id": "front-brand", "type": "text", "name": "Brand", "text": "HABESHA",
     "x": 6, "y": 8, "w": 38, "size": 3, "font": "display", "fill": "#f4efe3", "align": "center"},
    {"id": "front-name", "type": "text", "name": "Name", "text": "Glow Serum",
     "x": 6, "y": 50, "w": 38, "size": 5.5, "font": "display", "fill": "#f4efe3", "align": "center"},
]}


def tool(name, input_, id_):
    return {"type": "tool_use", "name": name, "input": input_, "id": id_}


def post(route, payload, headers=CODE, gz=False):
    body = json.dumps(payload).encode()
    headers = dict(headers)
    if gz:
        body = gzip.compress(body)
        headers["x-body-encoding"] = "gzip"
    return hosted.handle("POST", "/api/" + route, "", headers, body)


class Browser:
    """What web/hosted.js does, minus the drawing."""

    def __init__(self):
        self.project = {"id": "p1", "name": "Test", "agent": "packaging_designer",
                        "brief": {}, "dieline": None, "artwork": None, "assets": [],
                        "messages": [], "usage": {}, "log": []}
        self.requests = 0
        self.suspends = []

    def apply(self, cp):
        msgs = self.project["messages"]
        assert len(msgs) == cp["from"], f"client has {len(msgs)} messages, server says {cp['from']}"
        for flag in cp["flags"]:
            m = msgs[flag["i"]]
            if flag["archived"]:
                m["archived"] = True
                m.pop("api", None)   # the client drops what will never be sent again
            if flag["cost"]:
                m["cost"] = flag["cost"]
        msgs.extend(json.loads(json.dumps(cp["append"])))
        fields = cp["fields"]
        for key in list(self.project):
            if key != "messages" and key not in fields:
                del self.project[key]
        self.project.update(json.loads(json.dumps(fields)))

    def chat(self, text, model=None, effort=None):
        payload = {"project": self.project, "text": text, "asset_ids": [], "attachments": [],
                   "model": model, "effort": effort}
        events = []
        for _ in range(30):
            self.requests += 1
            reply = post("h/chat", payload, gz=True)
            assert reply.status == 200, reply.body
            suspend = None
            for chunk in reply.stream:
                line = chunk.decode().strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event["type"] == "checkpoint":
                    self.apply(event)
                    continue
                events.append(event)
                if event["type"] == "suspend":
                    suspend = event
            if not suspend:
                return events
            self.suspends.append(suspend["reason"])
            pictures = {r["request_id"]: {"media_type": "image/jpeg", "data": JPEG}
                        for r in suspend["renders"]}
            payload = {"project": self.project, "resume": pictures, "model": model, "effort": effort}
        raise AssertionError("never finished")


def main():
    mock.serve(MOCK_PORT)
    failures = []

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            failures.append(label)

    print("access")
    locked = json.loads(hosted.handle("GET", "/api/status", "", {}, b"").body)
    check(locked["hosted"] and locked["locked"], "no code: locked")
    check("models" not in locked, "nothing else is revealed while locked")
    wrong = json.loads(hosted.handle("GET", "/api/status", "",
                                     {"x-access-code": "1234", "x-forwarded-for": "10.9.9.9"}, b"").body)
    check(wrong["locked"] and "not right" in wrong["message"], "wrong code: refused with a message")
    status = json.loads(hosted.handle("GET", "/api/status", "", CODE, b"").body)
    check(not status["locked"], "6767 unlocks")
    check([m["id"] for m in status["models"]] == ["claude-sonnet-5-5", "claude-haiku-5-5"],
          "the site offers Sonnet and Haiku, not Opus")
    check(status["config"]["model"] == "claude-sonnet-5-5", "Sonnet is the default")
    check(hosted.handle("POST", "/api/h/render", "", {}, b"{}").status == 401,
          "API refuses requests without the code")
    via_rewrite = hosted.handle("GET", "/api/index", "route=status", CODE, b"")
    check(json.loads(via_rewrite.body).get("locked") is False, "route survives the Vercel rewrite")

    print("design run with a look, paused and resumed")
    browser = Browser()
    mock.SCRIPT[:] = [
        [{"type": "text", "text": "Straight tuck end it is."},
         tool("set_brief", {"product_name": "Glow Serum", "brand": "Habesha"}, "t1"),
         tool("build_structure", {"box_type": "ste", "width_mm": 40, "depth_mm": 36,
                                  "height_mm": 112}, "t2")],
        [tool("design_artwork", {"concept": "dark green, quiet",
                                 "fonts": {"display": "cormorant", "body": "jost"},
                                 "panels": {"front": FRONT}}, "t3")],
        [tool("look_at_design", {"panel": "front"}, "t4")],
        [tool("edit_layers", {"patches": [{"op": "update", "id": "front-name",
                                           "set": {"size": 6}}]}, "t5"),
         tool("look_at_design", {"panel": "all"}, "t6")],
        [{"type": "text", "text": "Done. Calm, one gold accent."}],
    ]
    mock.LOG.clear()
    events = browser.chat("Face serum, 30 ml, brand Habesha", model="claude-opus-5-5")
    kinds = [e["type"] for e in events]
    problems = [p for entry in mock.LOG for p in entry["problems"]]
    check(not problems, "the strict mock accepted every request" + (f": {problems}" if problems else ""))
    check("done" in kinds and "error" not in kinds, "run completes without error")
    check(browser.suspends == ["render", "render"], "paused twice for the browser to render")
    check(browser.requests == 3, "three requests for one run")
    bodies = [e["body"] for e in mock.LOG]
    check(all(b["model"] == "claude-sonnet-5-5" for b in bodies),
          "a model the site does not offer falls back to Sonnet")
    check(all((b.get("output_config") or {}).get("effort") == "high" for b in bodies),
          "effort sent on every call")
    last = bodies[-1]["messages"]
    images = sum(1 for m in last if isinstance(m.get("content"), list)
                 for b in m["content"] if b.get("type") == "tool_result"
                 and isinstance(b.get("content"), list)
                 and any(i.get("type") == "image" for i in b["content"]))
    check(images == 2, "both rendered pictures reached the model")
    p = browser.project
    check(bool(p.get("artwork")) and bool(p.get("dieline")), "artwork and structure came back")
    check("run_state" not in p and "pending_tools" not in p, "no run left open")
    check(p["usage"].get("runs") == 1 and p["usage"]["cost_usd"] > 0, "cost recorded once")
    visible = [m for m in p["messages"] if not m.get("hidden") and m.get("display")]
    check(visible[-1].get("cost", {}).get("steps") == 5, "the reply carries the cost of all steps")

    print("follow-up keeps the conversation")
    mock.LOG.clear()
    mock.SCRIPT[:] = [[{"type": "text", "text": "Made it warmer."}]]
    events = browser.chat("Make it warmer")
    problems = [p for entry in mock.LOG for p in entry["problems"]]
    check(not problems and "done" in [e["type"] for e in events], "follow-up accepted")
    check(len(mock.LOG[-1]["body"]["messages"]) > 6, "earlier turns were sent again")

    print("model switch folds the history")
    mock.LOG.clear()
    mock.SCRIPT[:] = [[{"type": "text", "text": "Haiku here."}]]
    events = browser.chat("Small fix please", model="claude-haiku-5-5")
    sent = mock.LOG[-1]["body"]
    check(sent["model"] == "claude-haiku-5-5", "Haiku used when chosen")
    check(len(sent["messages"]) == 1 and "Earlier conversation" in json.dumps(sent["messages"][0]),
          "fresh conversation with a summary")
    archived = [m for m in browser.project["messages"] if m.get("archived")]
    check(archived and all("api" not in m for m in archived), "archived turns no longer travel")

    print("time budget: a paused run carries on")
    hosted.START_WINDOW, saved = -1.0, hosted.START_WINDOW   # no time to start a call
    browser2 = Browser()
    mock.LOG.clear()
    mock.SCRIPT[:] = [[{"type": "text", "text": "Hello."}]]
    original = hosted.Clock.can_start
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return False          # first request: out of time before the first call
        self.call_started = self.started
        return True
    hosted.Clock.can_start = flaky
    events = browser2.chat("Hi")
    hosted.Clock.can_start = original
    hosted.START_WINDOW = saved
    check(browser2.suspends == ["continue"], "paused for time, then continued")
    check("done" in [e["type"] for e in events], "and finished")
    check(len(mock.LOG) == 1 and not mock.LOG[0]["problems"], "the model was called once, cleanly")

    print("files")
    lean = {k: v for k, v in browser.project.items() if k != "messages"}
    svg = post("h/render", {"project": lean, "mode": "panel", "panel": "front", "scale": 10})
    check(svg.status == 200 and b"<svg" in svg.body[:400], "panel renders")
    flat = post("h/render", {"project": lean, "mode": "flat"})
    check(flat.status == 200 and b"<svg" in flat.body[:400], "flat layout renders")
    listing = json.loads(post("h/export", {"project": lean}).body)
    names = [f["name"] for f in listing["files"]]
    check(any(n.endswith(".pdf") for n in names) and "READ-ME-FIRST.txt" in names, "export list")
    pdf = post("h/export", {"project": lean, "file": next(n for n in names if "dieline" in n and n.endswith(".pdf"))})
    check(pdf.status == 200 and pdf.body.startswith(b"%PDF"), "one file per request, raw bytes")
    structure = json.loads(post("h/structure", {"box_type": "autolock", "width_mm": 60,
                                                "depth_mm": 60, "height_mm": 70}).body)
    check(structure["dieline"]["box_type"] == "autolock", "structure computed")
    patched = json.loads(post("h/layers", {"artwork": browser.project["artwork"], "patches": [
        {"op": "update", "id": "front-brand", "set": {"text": "HABESHA NATURALS"}}]}).body)
    check(any(l["text"] == "HABESHA NATURALS" for l in patched["layers"]), "layer patched")
    example = json.loads(post("h/example", {}).body)
    check(example["project"]["artwork"], "example project available")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
