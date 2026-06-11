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

import os
import subprocess
import urllib.parse

SCRIPTS = os.path.join(os.path.dirname(__file__), "scripts")


def _say(text):
    # macOS built-in text-to-speech. Your "read it aloud / talk to me" verb.
    subprocess.run(["say", text], check=True)


def _open_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    subprocess.run(["open", url], check=True)


def _draft_email(to, subject, body):
    # Creates a VISIBLE draft in Mail. Does not send. Reversible -> auto-allowed.
    # Args are passed to the AppleScript via argv, never string-interpolated,
    # so the model can't inject AppleScript.
    subprocess.run(
        ["osascript", os.path.join(SCRIPTS, "draft_email.applescript"), to, subject, body],
        check=True,
    )


def _send_email(to, subject, body):
    # Actually sends. Irreversible + leaves the device -> risk "confirm".
    subprocess.run(
        ["osascript", os.path.join(SCRIPTS, "send_email.applescript"), to, subject, body],
        check=True,
    )


ACTION_SCHEMA = {
    "say":         {"risk": "auto",    "args": ["text"],                 "handler": _say,         "desc": "speak text aloud"},
    "open_url":    {"risk": "auto",    "args": ["url"],                  "handler": _open_url,    "desc": "open an http/https URL in the browser"},
    "draft_email": {"risk": "auto",    "args": ["to", "subject", "body"], "handler": _draft_email, "desc": "create a visible email draft (does NOT send)"},
    "send_email":  {"risk": "confirm", "args": ["to", "subject", "body"], "handler": _send_email,  "desc": "send an email (irreversible)"},
}
