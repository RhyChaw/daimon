"""
palace_memory.py — episodic recall via palace mem recall (subprocess + JSON).
"""

import json
import os
import subprocess
from pathlib import Path

from .episodic import MEMORY_DIR

def _palace_bin():
    return os.environ.get("DAIMON_PALACE_BIN", "palace")


def recall(query, root=None):
    """
    Run ``palace mem recall --root <root> --query <query> --json``.
    Returns matched events, or [] on any failure (palace missing, error, no matches).
    """
    root_path = str(MEMORY_DIR if root is None else Path(root).expanduser())
    try:
        proc = subprocess.run(
            [_palace_bin(), "mem", "recall", "--root", root_path, "--query", query, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not data.get("ok"):
        return []
    return data.get("events") or []


def recall_summary(query, n=3, root=None):
    """One-line summary of top n palace-recalled events for model context."""
    events = recall(query, root=root)
    if not events:
        return ""
    parts = []
    for event in events[:n]:
        notes = (event.get("agent_notes") or "").strip()
        action = event.get("action") or ""
        task = (event.get("task") or "").strip()
        outcome = event.get("outcome") or "?"
        if notes:
            desc = notes
        elif action and task:
            desc = f"{action}: {task}"
        elif task:
            desc = task
        else:
            desc = action or "past event"
        parts.append(f"{desc} ({outcome})")
    return "Past similar: " + "; ".join(parts) + "."
