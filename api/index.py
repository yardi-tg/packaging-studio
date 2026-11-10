"""Vercel entry point: the website's API as a plain ASGI app.

Every /api/... request lands here (see vercel.json) and goes to core/hosted.py.
Answers stream, so the conversation appears as it is written. No framework and
no dependencies: the standard library is enough.
"""

from __future__ import annotations
import asyncio
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("PACKAGING_HOSTED", "1")

from core import hosted  # noqa: E402


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return

    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    query = (scope.get("query_string") or b"").decode("latin-1")

    loop = asyncio.get_running_loop()
    reply = await loop.run_in_executor(None, hosted.handle, scope.get("method", "GET"),
                                       scope.get("path", "/"), query, headers, b"".join(chunks))
    await send({"type": "http.response.start", "status": reply.status,
                "headers": [(k.lower().encode("latin-1"), str(v).encode("latin-1"))
                            for k, v in reply.header_list()]})
    if reply.stream is None:
        await send({"type": "http.response.body", "body": reply.body})
        return

    # The stream is a blocking generator (the designer runs in a thread);
    # pump it through a queue so the event loop stays free to send.
    pipe: asyncio.Queue = asyncio.Queue()

    def pump():
        try:
            for chunk in reply.stream:
                loop.call_soon_threadsafe(pipe.put_nowait, chunk)
        finally:
            loop.call_soon_threadsafe(pipe.put_nowait, None)

    threading.Thread(target=pump, daemon=True).start()
    while True:
        chunk = await pipe.get()
        if chunk is None:
            break
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})
