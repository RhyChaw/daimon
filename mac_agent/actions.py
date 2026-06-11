"""
actions.py — the action whitelist.

This file IS the security boundary. The model can only ever request one of the
verbs defined in ACTION_SCHEMA. Anything else is rejected by code before it can
run — not by the model's judgement. Want the agent to be able to do something
new? You add a verb here, on purpose. That's the whole design.

Each verb has:
  - risk:    "auto"    -> runs without asking
             "confirm" -> must pass the gate (terminal y/N now; Touch ID later)
  - args:    the exact argument names the handler expects
  - handler: the function that performs it (kept tiny and inspectable)
  - desc:    one line, also shown to the model so it knows what's available
"""

import subprocess
import threading
import urllib.parse

from . import memory
from .resources import script_path
from .senses import get_weather, read_calendar
_SAY_BIN = "/usr/bin/say"


def _say(text):
    # macOS built-in text-to-speech. Fire-and-forget so the REPL stays responsive.
    def speak():
        try:
            subprocess.run([_SAY_BIN, text], check=False, timeout=120)
        except Exception:
            pass

    threading.Thread(target=speak, daemon=True).start()


def _open_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    subprocess.run(["open", url], check=True)


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


def _read_calendar():
    return read_calendar()


def _get_weather(location=None):
    return get_weather(location=location)


ACTION_SCHEMA = {
    "say":            {"risk": "auto",    "args": ["text"],                 "handler": _say,            "desc": "speak text aloud"},
    "open_url":       {"risk": "auto",    "args": ["url"],                  "handler": _open_url,       "desc": "open an http/https URL in the browser"},
    "draft_email":    {"risk": "auto",    "args": ["to", "subject", "body"], "handler": _draft_email,    "desc": "create a visible email draft (does NOT send)"},
    "send_email":     {"risk": "confirm", "args": ["to", "subject", "body"], "handler": _send_email,     "desc": "send an email (irreversible)"},
    "remember":       {"risk": "auto",    "args": ["key", "value"],          "handler": _remember,       "desc": "save a personal fact the user stated (e.g. prof → contact) for later recall"},
    "read_calendar":  {"risk": "auto",    "args": [],                        "handler": _read_calendar,  "desc": "read today's Calendar events (returns data for you to summarize)"},
    "get_weather":    {"risk": "auto",    "args": ["location"],              "handler": _get_weather,    "desc": "current weather for location (optional; uses location fact if omitted)", "optional_args": ["location"]},
}
