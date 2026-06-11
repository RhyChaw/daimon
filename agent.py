"""
agent.py — the core loop.

  you type  ->  local model proposes ONE action  ->  whitelist check
            ->  gate (if risky)  ->  execute  ->  audit log  ->  repeat

The model never touches your system directly. It can only *propose* a verb from
the whitelist; this code is what actually runs anything.
"""

import json
import os

from .actions import ACTION_SCHEMA
from .ollama_client import chat
from .gate import allow
from .audit import log

MODEL = os.environ.get("MACAGENT_MODEL", "llama3.2")


def _system_prompt():
    lines = [
        "You control a Mac through a FIXED set of actions.",
        'Reply with ONLY a JSON object of the form: {"action": <name>, "args": {...}}.',
        "Allowed actions:",
    ]
    for name, spec in ACTION_SCHEMA.items():
        lines.append(f'  - {name}(args: {", ".join(spec["args"])}) — {spec["desc"]}')
    lines.append('If the request does not fit an action, use "say" to ask a clarifying question.')
    lines.append("Output nothing except the JSON object.")
    return "\n".join(lines)


def handle(user_text):
    raw = chat(MODEL, _system_prompt(), user_text)
    try:
        msg = json.loads(raw)
        action = msg["action"]
        args = msg.get("args", {})
    except Exception:
        print("  [could not parse model output]:", raw)
        return

    spec = ACTION_SCHEMA.get(action)
    if spec is None:                                   # whitelist enforcement
        print(f"  [rejected] unknown action: {action!r}")
        log(action=action, args=args, decision="rejected-unknown", ok=False)
        return
    if set(args) != set(spec["args"]):
        print(f"  [rejected] wrong args for {action}: {list(args)}")
        log(action=action, args=args, decision="rejected-args", ok=False)
        return

    risk = spec["risk"]
    print(f"  -> proposed: {action}({args})   [risk: {risk}]")

    if risk == "confirm" and not allow(action, args):
        print("  x denied")
        log(action=action, args=args, decision="denied", ok=False)
        return

    try:
        spec["handler"](**args)
        print("  ok")
        log(action=action, args=args, decision="allowed", ok=True)
    except Exception as e:
        print("  ! error:", e)
        log(action=action, args=args, decision="allowed", ok=False, error=str(e))


def _banner():
    return (
        "\n  mac-agent v0  ·  local · gated · logged\n"
        f"  model: {MODEL}   (set MACAGENT_MODEL to change)\n"
        "  try: say good morning   /   draft an email to me@x.com about lunch\n"
        "  type 'exit' to quit.\n"
    )


def repl():
    print(_banner())
    while True:
        try:
            user = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not user:
            continue
        if user in ("exit", "quit"):
            break
        handle(user)
