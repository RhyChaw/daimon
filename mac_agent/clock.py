"""
clock.py — cached local time, refreshed in the background every 15 minutes.

Handlers read clock.now() instead of calling datetime.now() on the request path.
"""

import os
import threading
from datetime import datetime

_INTERVAL = int(os.environ.get("DAIMON_CLOCK_INTERVAL", 15 * 60))
_lock = threading.Lock()
_now = datetime.now()
_started = False


def _refresh():
    global _now
    with _lock:
        _now = datetime.now()


def now():
    with _lock:
        return _now


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    _refresh()

    def _loop():
        while True:
            if threading.Event().wait(_INTERVAL):
                break
            _refresh()

    threading.Thread(target=_loop, name="daimon-clock", daemon=True).start()
