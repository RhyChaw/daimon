"""
events.py — thread-safe in-process event bus.

The agent calls emit(); the WS server subscribes to forward events to the browser.
"""
import threading

_lock = threading.Lock()
_listeners: list = []


def emit(event: dict) -> None:
    with _lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(event)
        except Exception:
            pass


def subscribe(fn) -> callable:
    """Register fn(event) and return an unsubscribe callable."""
    with _lock:
        _listeners.append(fn)

    def _unsub():
        with _lock:
            try:
                _listeners.remove(fn)
            except ValueError:
                pass

    return _unsub
