"""
settings.py — persisted Daimon preferences (backend selection, etc.).
"""

import json
import os
from pathlib import Path

_DEFAULT_PATH = Path.home() / "Library" / "Application Support" / "Daimon" / "settings.json"


def _path():
    return Path(os.environ.get("DAIMON_SETTINGS", _DEFAULT_PATH))


def load():
    path = _path()
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def get_backend():
    data = load()
    name = (data.get("backend") or os.environ.get("DAIMON_BACKEND") or "").strip().lower()
    if name in ("ollama", "claude"):
        return name
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "ollama"


def set_backend(name):
    name = name.strip().lower()
    if name not in ("ollama", "claude"):
        raise ValueError(f"unknown backend: {name!r}")
    data = load()
    data["backend"] = name
    save(data)
    os.environ["DAIMON_BACKEND"] = name
