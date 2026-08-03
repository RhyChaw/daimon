"""
speech.py — serialized macOS text-to-speech.

Concurrent say() calls queue up instead of interrupting each other (macOS
stops the previous utterance when a new `say` process starts).
"""

import os
import queue
import subprocess
import tempfile
import threading

_SAY_BIN = "/usr/bin/say"
_QUEUE = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_MUTED = False


def set_muted(value):
    """Suppress macOS `say` output (e.g. while a browser client drives TTS)."""
    global _MUTED
    _MUTED = bool(value)


def _run_say(text):
    if not text or not str(text).strip():
        return
    text = str(text).strip()
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="daimon-say-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        subprocess.run([_SAY_BIN, "-f", path], check=False)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _worker():
    while True:
        text = _QUEUE.get()
        try:
            _run_say(text)
        finally:
            _QUEUE.task_done()


def _ensure_worker():
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker, name="daimon-say", daemon=True).start()
        _WORKER_STARTED = True


def speak(text):
    """Queue text for speech; waits behind any in-progress utterance."""
    if _MUTED:
        return
    _ensure_worker()
    _QUEUE.put(text)
