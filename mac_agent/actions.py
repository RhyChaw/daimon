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

import subprocess
import urllib.parse

from . import memory
from .resources import script_path
from .senses import get_weather, read_calendar
from .speech import speak as _speak_async


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


def _escape_applescript(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _play_music(query, app_name="Spotify"):
    query = str(query).strip()
    app_name = str(app_name or "Spotify").strip()
    if not query:
        raise ValueError("query required")
    if not app_name or "/" in app_name or app_name.startswith("-"):
        raise ValueError("invalid app name")
    script = (
        f'tell application "{_escape_applescript(app_name)}" to activate\n'
        f'tell application "{_escape_applescript(app_name)}" to play track "{_escape_applescript(query)}"'
    )
    subprocess.run(["osascript", "-e", script], check=True)


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


ACTION_SCHEMA = {
    "say":            {"risk": "auto",    "args": ["text"],                 "handler": _say,            "desc": "speak text aloud"},
    "open_url":       {"risk": "auto",    "args": ["url"],                  "handler": _open_url,       "desc": "open an http/https URL in the browser"},
    "open_app":       {"risk": "auto",    "args": ["app_name"],             "handler": _open_app,       "desc": "open a Mac application by name (e.g. Spotify, Notes, Calendar)"},
    "play_music":     {"risk": "auto",    "args": ["query", "app_name"],    "handler": _play_music_action, "desc": "play a song or track in a music app (default Spotify)", "optional_args": ["app_name"]},
    "draft_email":    {"risk": "auto",    "args": ["to", "subject", "body"], "handler": _draft_email,    "desc": "create a visible email draft (does NOT send)"},
    "send_email":     {"risk": "confirm", "args": ["to", "subject", "body"], "handler": _send_email,     "desc": "send an email (irreversible; requires Touch ID)"},
    "remember":       {"risk": "auto",    "args": ["key", "value"],          "handler": _remember,       "desc": "save a personal fact the user stated (e.g. prof → contact) for later recall"},
    "read_calendar":  {"risk": "auto",    "args": ["query"],                   "handler": _read_calendar,  "desc": "read today's remaining Calendar events", "optional_args": ["query"]},
    "get_weather":    {"risk": "auto",    "args": ["location"],              "handler": _get_weather,    "desc": "current weather for location (optional; uses location fact if omitted)", "optional_args": ["location"]},
}
