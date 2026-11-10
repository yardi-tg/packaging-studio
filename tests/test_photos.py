"""Photoshoot and provider detection against a mock Gemini endpoint.

Run:  python3 tests/test_photos.py
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = pathlib.Path(tempfile.mkdtemp(prefix="ps-photo-"))
os.environ["GEMINI_API_KEY"] = "AQ.test-key-new-format"

from core import config, imagegen, store  # noqa: E402

config.PROJECTS_DIR = WORK / "projects"
config.ENV_FILE = WORK / ".env"
SEEN = []
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 4000).decode()


class Gemini(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth_ok(self):
        # Behave like the newer key format: only the Bearer header works.
        return self.headers.get("Authorization") == "Bearer AQ.test-key-new-format"

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "API key not valid"}})
        self._send(200, {"models": [
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": ["generateContent"]},
        ]})

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "API key not valid"}})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "body": body})
        self._send(200, {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": PNG}}]}}]})


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", 8897), Gemini)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    imagegen.GEMINI_HOST = "http://127.0.0.1:8897"
    failures = []

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            failures.append(label)

    result = imagegen.probe()
    check(result.get("ok"), "probe connects with the new key format")
    check(result.get("model") == "gemini-3-pro-image-preview",
          "best image model chosen (" + str(result.get("model")) + ")")
    check(result.get("auth") == "bearer", "falls back to Bearer auth when needed")

    project = store.create("Photo test")
    project["brief"] = {"product_name": "Glow Serum", "category": "face serum"}
    asset = imagegen.photoshoot(project, base64.b64decode(PNG), scene="hand",
                                notes="warm skin", front_png=base64.b64decode(PNG))
    parts = SEEN[-1]["body"]["contents"][0]["parts"]
    images = [p for p in parts if "inline_data" in p]
    prompt = next(p["text"] for p in parts if "text" in p)
    check(len(images) == 2, "render and flat front both sent")
    check("second image is the exact flat artwork" in prompt, "prompt explains the reference")
    check("Keep the package exactly as" in prompt, "prompt protects the design")
    check("holding this package" in prompt, "scene preset used")
    check("gemini-3-pro-image-preview" in SEEN[-1]["path"], "uses the detected model")
    check(asset["role"] == "photo" and store.asset_path(project["id"], asset["id"]),
          "photo stored as a project asset")

    httpd.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    print()
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
