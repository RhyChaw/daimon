"""
ollama_serve.py — ensure the local Ollama API is reachable before using the Ollama backend.
"""

import os
import shutil
import subprocess
import time

from . import ollama_client

_START_WAIT_S = 20
_POLL_INTERVAL_S = 0.5


def _ollama_bin():
    path = shutil.which("ollama")
    if path:
        return path
    for candidate in (
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def ensure_serving(verbose=False):
    """
    Start Ollama if localhost:11434 is down. Returns True when the API responds.
    Raises RuntimeError if Ollama could not be started.
    """
    if ollama_client.ping():
        return True

    if verbose:
        print("  starting Ollama…")

    if _try_open_app():
        if _wait_for_api():
            return True

    if _try_serve_subprocess(verbose=verbose):
        if _wait_for_api():
            return True

    raise RuntimeError(
        "Could not reach Ollama at http://localhost:11434. "
        "Install from https://ollama.com or run `ollama serve` in a terminal."
    )


def _try_open_app():
    try:
        subprocess.run(
            ["open", "-a", "Ollama"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _try_serve_subprocess(verbose=False):
    ollama = _ollama_bin()
    if not ollama:
        return False
    try:
        subprocess.Popen(
            [ollama, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if verbose:
            print(f"  running: {ollama} serve")
        return True
    except Exception:
        return False


def _wait_for_api():
    deadline = time.time() + _START_WAIT_S
    while time.time() < deadline:
        if ollama_client.ping():
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False
