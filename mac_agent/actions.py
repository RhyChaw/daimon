"""
actions.py — the action whitelist.

This file IS the security boundary. The model can only ever request one of the
verbs defined in ACTION_SCHEMA. Anything else is rejected by code before it can
run — not by the model's judgement. Want the agent to be able to do something
new? You add a verb here, on purpose. That's the whole design.

Each verb has:
  - risk:    "auto"    -> runs without asking
             "confirm" -> must pass Touch ID in Daimon.app (y/N fallback in terminal dev)
  Risk audit:
    auto    — say, open_url, open_app, play_music, read_calendar, get_weather, remember, draft_email
    confirm — send_email; any future irreversible / outbound / delete / private-data verb
  - args:    the exact argument names the handler expects
  - handler: the function that performs it (kept tiny and inspectable)
  - desc:    one line, also shown to the model so it knows what's available
"""

import re
import subprocess
import urllib.parse

from . import memory
from .keystrokes import AccessibilityRequired, play_spotify_recent, play_spotify_search
from .resources import script_path
from .senses import NeedsUserInput, get_weather, read_calendar
from .speech import speak as _speak_async
from . import projects as _projects


def _say(text):
    _speak_async(text)


def _open_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    subprocess.run(["open", url], check=True)


def _open_app(app_name):
    app_name = str(app_name).strip()
    if not app_name or "/" in app_name or app_name.startswith("-"):
        raise ValueError("invalid app name")
    subprocess.run(["open", "-a", app_name], check=True)
    _speak_async(f"Opened {app_name}.")


_RECENT_PLAY_QUERIES = frozenset({"recent", "last", "latest", "again"})


def _is_recent_play_query(query):
    q = str(query or "").strip().lower().rstrip(".")
    if not q:
        return False
    if q in _RECENT_PLAY_QUERIES:
        return True
    return bool(re.match(r"^(?:my\s+)?(?:most\s+recent|last|latest)(?:\s+(?:song|track))?$", q, re.I))


def _play_music(query, app_name="Spotify"):
    app_name = str(app_name or "Spotify").strip()
    if app_name.lower() != "spotify":
        raise ValueError("play_music UI automation currently supports Spotify only")
    try:
        if _is_recent_play_query(query):
            play_spotify_recent()
        else:
            query = str(query).strip()
            if not query:
                raise ValueError("query required")
            play_spotify_search(query)
    except AccessibilityRequired:
        raise
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _draft_email(to, subject, body):
    # Creates a VISIBLE draft in Mail. Does not send. Reversible -> auto-allowed.
    # `to` must already be a resolved email — agent.py validates before calling.
    subprocess.run(
        ["osascript", script_path("draft_email.applescript"), to, subject, body],
        check=True,
    )


def _send_email(to, subject, body):
    # Actually sends. Irreversible + leaves the device -> risk "confirm".
    subprocess.run(
        ["osascript", script_path("send_email.applescript"), to, subject, body],
        check=True,
    )


def _remember(key, value):
    memory.store(key, value)


def _read_calendar(query=None):
    return read_calendar(query=query)


def _get_weather(location=None):
    return get_weather(location=location)


def _play_music_action(query, app_name="Spotify"):
    return _play_music(query=query, app_name=app_name)


def _open_in_claude_code(project: str) -> str:
    name = str(project).strip()
    path = _projects.resolve(name)
    if path is None:
        known = _projects.known_aliases()
        known_str = ", ".join(known) if known else "none yet"
        raise NeedsUserInput(
            f"I don't have a project called {name}. "
            f"Known projects: {known_str}."
        )
    if not path.is_dir():
        raise NeedsUserInput(
            f"The path for {name} doesn't exist on disk: {path}"
        )
    # AppleScript: open a new Terminal window at the project path and start claude.
    # Path is single-quoted for the shell cd command inside the do script string.
    script = (
        'tell application "Terminal"\n'
        f"    do script \"cd '{path}' && claude\"\n"
        '    activate\n'
        'end tell'
    )
    subprocess.run(["osascript", "-e", script], check=True)
    return f"Opened {name} in Claude Code."


ACTION_SCHEMA = {
    "say":            {"risk": "auto",    "args": ["text"],                 "handler": _say,            "desc": "speak text aloud"},
    "open_url":       {"risk": "auto",    "args": ["url"],                  "handler": _open_url,       "desc": "open an http/https URL in the browser"},
    "open_app":       {"risk": "auto",    "args": ["app_name"],             "handler": _open_app,       "desc": "open a Mac application by name (e.g. Spotify, Notes, Calendar)"},
    "play_music":     {"risk": "auto",    "args": ["query", "app_name"],    "handler": _play_music_action, "desc": "play in Spotify: pass a track query to search, or query 'recent' for most recent song", "optional_args": ["app_name"]},
    "draft_email":    {"risk": "auto",    "args": ["to", "subject", "body"], "handler": _draft_email,    "desc": "create a visible email draft (does NOT send)"},
    "send_email":     {"risk": "confirm", "args": ["to", "subject", "body"], "handler": _send_email,     "desc": "send an email (irreversible; requires Touch ID)"},
    "remember":       {"risk": "auto",    "args": ["key", "value"],          "handler": _remember,       "desc": "save a personal fact the user stated (e.g. prof → contact) for later recall"},
    "read_calendar":         {"risk": "auto",    "args": ["query"],                    "handler": _read_calendar,          "desc": "read today's remaining Calendar events", "optional_args": ["query"]},
    "get_weather":           {"risk": "auto",    "args": ["location"],                 "handler": _get_weather,             "desc": "current weather for location (optional; uses location fact if omitted)", "optional_args": ["location"]},
    "open_in_claude_code":   {"risk": "auto",    "args": ["project"],                  "handler": _open_in_claude_code,     "desc": "open a registered project in Claude Code in a new Terminal window"},
}
