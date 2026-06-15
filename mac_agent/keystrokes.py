"""
Spotify UI automation via in-process AppleScript on the main thread.

Subprocess osascript is not allowed to send keystrokes (error 1002).
CGEventPost often fails silently. NSAppleScript inside Daimon.app
uses Daimon's Automation + Accessibility grants.
"""

import sys
import threading

from .resources import script_path

_HANDLERS = None


class AccessibilityRequired(RuntimeError):
    pass


# AppleScript error numbers that indicate missing Accessibility permission.
_AX_ERROR_CODES = frozenset({-1719, -1743, 1002, -1002, -25211})


def _is_accessibility_error(err: dict) -> bool:
    if err.get("NSAppleScriptErrorNumber", 0) in _AX_ERROR_CODES:
        return True
    msg = str(err.get("NSAppleScriptErrorMessage", "")).lower()
    return any(p in msg for p in ("not allowed", "accessibility", "assistive"))


def check_accessibility() -> bool:
    """Return True if this process already has Accessibility access."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return True  # can't check — assume OK


def request_accessibility() -> bool:
    """Prompt macOS to show the Accessibility permission dialog (if not yet granted).

    Returns True if access is already or newly granted.
    The process must be restarted after the user grants access for it to take effect.
    """
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True}))
    except Exception:
        return True


def _escape_applescript(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _handlers():
    global _HANDLERS
    if _HANDLERS is None:
        with open(script_path("play_spotify.applescript"), encoding="utf-8") as handle:
            text = handle.read()
        _HANDLERS = text[text.index("on focusSpotify()") :]
    return _HANDLERS


def _run_applescript(source):
    from Foundation import NSAppleScript

    script = NSAppleScript.alloc().initWithSource_(source)
    _result, err = script.executeAndReturnError_(None)
    if err is not None:
        if _is_accessibility_error(err):
            raise AccessibilityRequired(
                "Daimon needs Accessibility access to send keystrokes. "
                "Open System Settings → Privacy & Security → Accessibility, "
                "remove Daimon if listed, then re-add it and restart the app."
            )
        msg = err.get("NSAppleScriptErrorMessage", str(err))
        brief = err.get("NSAppleScriptErrorBriefMessage", "")
        raise RuntimeError(f"{msg} {brief}".strip())


def _yield_to_spotify():
    if not getattr(sys, "frozen", False):
        return
    try:
        from AppKit import NSApplication

        for window in NSApplication.sharedApplication().windows():
            window.orderBack_(None)
    except Exception:
        pass


def _on_main_thread(fn, *args, **kwargs):
    if not getattr(sys, "frozen", False):
        return fn(*args, **kwargs)
    done = threading.Event()
    box = {}

    def run():
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as exc:
            box["error"] = exc
        finally:
            done.set()

    from PyObjCTools.AppHelper import callAfter

    callAfter(run)
    if not done.wait(timeout=90):
        raise RuntimeError("Spotify automation timed out")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _run_spotify(mode, query=None):
    if mode == "search":
        q = _escape_applescript(query)
        body = f'''
my focusSpotify()
my openSearch()
my typeInSearch("{q}")
my pressDown(5)
my pressReturn()
delay 2
my pressReturn()
'''
    else:
        body = """
my focusSpotify()
my openSearch()
delay 0.5
my pressDown(1)
my pressReturn()
"""
    _yield_to_spotify()
    _run_applescript(body + _handlers())


def play_spotify_search(query):
    _on_main_thread(_run_spotify, "search", query)


def play_spotify_recent():
    _on_main_thread(_run_spotify, "recent")
