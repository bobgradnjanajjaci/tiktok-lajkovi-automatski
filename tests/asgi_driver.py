"""A tiny, bounded ASGI driver for tests.

Why this exists
---------------
``TestClient.stream("GET", "/api/events")`` does not terminate against an
endless Server-Sent Events response: the synchronous test client wants the body
to finish before the ``with`` block can close, and the SSE generator only
returns when the client disconnects. An isolated run of the snapshot test did
not finish inside six seconds and had to be killed.

That is a property of the test transport, not of the endpoint. So these helpers
speak ASGI directly, which lets a test:

* run the application's lifespan (database, worker) explicitly;
* read exactly as many body chunks as it needs;
* send a real ``http.disconnect`` at a moment it chooses;
* wrap everything in ``asyncio.wait_for`` and cancel leftovers.

Nothing here touches the network. Every call is an in-process ASGI call.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Iterable
from urllib.parse import urlencode

DEFAULT_TIMEOUT = 5.0


def _scope(method: str, path: str, headers: dict[str, str], query: str = "") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


@asynccontextmanager
async def lifespan(app):
    """Run the app's startup and shutdown, bounded by a timeout."""
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive():
        return await to_app.get()

    async def send(message):
        await from_app.put(message)

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )
    await to_app.put({"type": "lifespan.startup"})
    message = await asyncio.wait_for(from_app.get(), DEFAULT_TIMEOUT)
    if message["type"] != "lifespan.startup.complete":
        task.cancel()
        raise AssertionError(f"startup failed: {message}")
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(from_app.get(), DEFAULT_TIMEOUT)
            await asyncio.wait_for(task, DEFAULT_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
            task.cancel()


class Response:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        name = name.lower().encode()
        for key, value in self.headers:
            if key == name:
                return value.decode()
        return None

    def cookies(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for key, value in self.headers:
            if key == b"set-cookie":
                pair = value.decode().split(";", 1)[0]
                name, _, raw = pair.partition("=")
                jar[name.strip()] = raw.strip()
        return jar


async def call(
    app,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """One complete, bounded request/response cycle."""
    headers = dict(headers or {})
    if body and "content-length" not in {k.lower() for k in headers}:
        headers["content-length"] = str(len(body))

    sent: list[dict[str, Any]] = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await asyncio.wait_for(app(_scope(method, path, headers, query), receive, send), timeout)

    status = 500
    response_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []
    for message in sent:
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
    return Response(status, response_headers, b"".join(chunks))


def form(data: dict[str, str]) -> tuple[dict[str, str], bytes]:
    encoded = urlencode(data).encode()
    return {"content-type": "application/x-www-form-urlencoded"}, encoded


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in jar.items())


async def read_stream(
    app,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    until: Iterable[str] = ("\n\n",),
    wake: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Response, str]:
    """Open a streaming response, read until ``until`` is satisfied, disconnect.

    ``wake`` may be an ``asyncio.Event`` the endpoint waits on between chunks
    (here: ``Database.event_written``). Setting it lets the generator notice the
    disconnect immediately instead of sitting in its keep-alive wait, so the
    test never depends on that timeout elapsing.
    """
    headers = dict(headers or {})
    start: dict[str, Any] = {}
    chunks: list[str] = []
    got_enough = asyncio.Event()
    disconnected = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            piece = message.get("body", b"")
            if piece:
                chunks.append(piece.decode("utf-8", "replace"))
                if all(marker in "".join(chunks) for marker in until):
                    got_enough.set()

    task = asyncio.create_task(app(_scope("GET", path, headers), receive, send))
    try:
        done, _ = await asyncio.wait(
            {asyncio.create_task(got_enough.wait()), task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for finished in done:
            if finished is task and task.done():
                task.result()  # surface an application exception
    finally:
        # Explicit disconnect, then wake the generator so it observes it.
        disconnected.set()
        if wake is not None:
            wake.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                task.cancel()
            except asyncio.CancelledError:  # pragma: no cover
                pass
        for pending in [t for t in asyncio.all_tasks() if t is task and not t.done()]:
            pending.cancel()

    response = Response(
        start.get("status", 500), list(start.get("headers") or []), b""
    )
    return response, "".join(chunks)
