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
_html_bytes: bytes | None = None   # loaded once at start()
_static: dict[str, tuple[bytes, str]] = {}  # path → (bytes, mime)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".glb":  "model/gltf-binary",
}


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
        # Tell a freshly connected client about any live PTY session.
        try:
            from . import term_session as _ts
            if _ts.is_active():
                await websocket.send(json.dumps({"type": "term_open"}))
        except Exception:
            pass

        # Tell the client which model backend is currently selected.
        try:
            from . import settings as _settings
            await websocket.send(json.dumps({"type": "model", "model": _settings.get_backend()}))
        except Exception:
            pass

        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue

            t = msg.get("type")
            if t == "user":
                text = str(msg.get("text", "")).strip()
                if text:
                    inbox.put(text)
            elif t == "term_in":
                data = msg.get("data", "")
                if data:
                    try:
                        from . import term_session as _ts
                        _ts.write_input(data.encode("utf-8"))
                    except Exception:
                        pass
            elif t == "term_resize":
                try:
                    from . import term_session as _ts
                    _ts.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                except Exception:
                    pass
            elif t == "set_model":
                name = str(msg.get("model", "")).strip().lower()
                if name in ("claude", "ollama"):
                    try:
                        from . import settings as _settings
                        _settings.set_backend(name)
                        # Echo to every client so all UIs reflect the change.
                        broadcast({"type": "model", "model": name})
                    except Exception:
                        pass
            elif t == "voice":
                # Browser drives TTS during hands-free conversation — mute the
                # Mac's `say` so audio isn't doubled.
                try:
                    from . import speech as _speech
                    _speech.set_muted(bool(msg.get("on")))
                except Exception:
                    pass
    finally:
        _clients.discard(websocket)
        # Don't leave the Mac voice muted if the controlling client drops.
        if not _clients:
            try:
                from . import speech as _speech
                _speech.set_muted(False)
            except Exception:
                pass


async def _process_request(connection, request):
    """Serve static files for plain HTTP GETs; let WebSocket upgrades through."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None  # proceed with WebSocket handshake

    import http as _http
    try:
        from websockets.http11 import Response
        from websockets.datastructures import Headers

        path = getattr(request, "path", "/").split("?")[0]
        if path in ("", "/", "/index.html"):
            body = _html_bytes or b"<h1>Daimon</h1>"
            mime = "text/html; charset=utf-8"
        elif path.lstrip("/") in _static:
            body, mime = _static[path.lstrip("/")]
        else:
            body = b"Not found"
            mime = "text/plain"
            return Response(
                status_code=_http.HTTPStatus.NOT_FOUND,
                reason_phrase="Not Found",
                headers=Headers([("Content-Length", str(len(body))), ("Connection", "close")]),
                body=body,
            )

        return Response(
            status_code=_http.HTTPStatus.OK,
            reason_phrase="OK",
            headers=Headers([
                ("Content-Type", mime),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ]),
            body=body,
        )
    except Exception:
        pass
    return None


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
        # Load any other static files in web_dir (js, css) into memory.
        for p in web_dir.iterdir():
            if p.suffix in _MIME and p.name != "index.html":
                mime = _MIME.get(p.suffix, "application/octet-stream")
                _static[p.name] = (p.read_bytes(), mime)
                print(f"  static  → /{p.name}  ({len(_static[p.name][0]):,}b)")

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
