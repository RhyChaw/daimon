"""
episodic.py — append-only experience log (palace-ai v2 attempts.jsonl compatible).

Security decisions live in audit.jsonl; this file is institutional memory for
continuity and later convergence with palace reflect.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("DAIMON_MEMORY_DIR", Path.home() / ".daimon" / "memory"))
ATTEMPTS_PATH = MEMORY_DIR / "attempts.jsonl"

_session_id = ""


def init_session():
    global _session_id
    _session_id = str(uuid.uuid4())
    return _session_id


def session_id():
    return _session_id


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_dir():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def log_event(
    *,
    task,
    outcome,
    action,
    args=None,
    agent_notes="",
    session_id=None,
):
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": _utc_now(),
        "task": task,
        "outcome": outcome,
        "action": action,
        "args": args or {},
        "agent_notes": agent_notes,
        "session_id": session_id or _session_id,
    }
    _ensure_dir()
    with ATTEMPTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def recent(n=10):
    if not ATTEMPTS_PATH.exists():
        return []
    lines = ATTEMPTS_PATH.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-n:]


def _describe(entry):
    notes = (entry.get("agent_notes") or "").strip()
    if notes:
        return notes
    action = entry.get("action") or "?"
    args = entry.get("args") or {}
    if action == "say":
        text = str(args.get("text", ""))
        return f"said {text[:50]}" if text else "said something"
    if action in ("draft_email", "send_email"):
        verb = "drafted email" if action == "draft_email" else "sent email"
        return f"{verb} to {args.get('to', '?')}"
    if action == "remember":
        return f"remembered {args.get('key', '?')}"
    if action == "open_url":
        return f"opened {args.get('url', '?')}"
    return action.replace("_", " ")


def recent_summary(n=3):
    events = recent(n)
    if not events:
        return ""
    parts = [f"{_describe(e)} ({e.get('outcome', '?')})" for e in events]
    return "Recently: " + "; ".join(parts) + "."
