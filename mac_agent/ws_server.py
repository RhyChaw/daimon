"""
ws_server.py — WebSocket event bridge + integrated HTTP server.

  Port 8765: WebSocket  ws://hostname:8765   (agent ↔ browser)
             HTTP        http://hostname:8765  (serves web/index.html)

A single port handles both.  Plain HTTP GET requests get the page;
WebSocket upgrade requests get the live event stream.  This avoids
a separate HTTP server that breaks on VPN/Tailscale interfaces due to
TCP RST from Python's SimpleHTTPRequestHandler.

The shared `inbox` queue is the single source of user text for the agent loop.
Both the WS handler and the Cocoa console UI put into it; the agent reads from it.
"""
import asyncio
import json
import queue
import threading
from pathlib import Path

try:
    import websockets
    _HAVE_WS = True
except ImportError:
    _HAVE_WS = False

from . import events

WS_PORT = 8765

# Public — both console_ui and this module feed into it.
inbox: queue.Queue = queue.Queue()

_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None
_html_bytes: bytes | None = None  # loaded once at start()


def broadcast(event: dict) -> None:
    """Send event dict to all connected WS clients (thread-safe from any thread)."""
    if not _clients or _loop is None:
        return
    data = json.dumps(event)

    async def _send():
        for ws in list(_clients):
            try:
                await ws.send(data)
            except Exception:
                pass

    asyncio.run_coroutine_threadsafe(_send(), _loop)


async def _ws_handler(websocket):
    _clients.add(websocket)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if msg.get("type") == "user":
                text = str(msg.get("text", "")).strip()
                if text:
                    inbox.put(text)
    finally:
        _clients.discard(websocket)


async def _process_request(connection, request):
    """Serve index.html for plain HTTP GETs; let WebSocket upgrades through."""
    if request.headers.get("Upgrade", "").lower() != "websocket":
        import http as _http
        html = _html_bytes or b"<h1>Daimon</h1><p>index.html not found.</p>"
        try:
            from websockets.http11 import Response
            from websockets.datastructures import Headers
            return Response(
                status_code=_http.HTTPStatus.OK,
                reason_phrase="OK",
                headers=Headers([
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(html))),
                    ("Connection", "close"),
                ]),
                body=html,
            )
        except Exception:
            pass
    return None  # proceed with WebSocket handshake


def _run_ws_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)

    async def _serve():
        async with websockets.serve(
            _ws_handler,
            "0.0.0.0",
            WS_PORT,
            process_request=_process_request,
        ):
            await asyncio.Future()  # run forever

    loop.run_until_complete(_serve())


def start(web_dir: Path | None = None) -> None:
    """Start the combined WS+HTTP server and wire to the event bus."""
    global _loop, _html_bytes

    if not _HAVE_WS:
        print("  [ws] websockets not installed — run: pip install websockets")
        return

    if web_dir and (html_path := web_dir / "index.html").is_file():
        _html_bytes = html_path.read_bytes()

    _loop = asyncio.new_event_loop()
    events.subscribe(broadcast)

    threading.Thread(
        target=_run_ws_loop, args=(_loop,), daemon=True, name="daimon-ws"
    ).start()
    print(f"  web UI  → http://localhost:{WS_PORT}")
    print(f"  ws      → ws://localhost:{WS_PORT}")


def make_input_fn():
    """Return an input_fn that reads user text from the shared inbox queue."""
    def _read(prompt=""):
        return inbox.get()
    return _read
