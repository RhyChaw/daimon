"""
projects.py — project registry at ~/.daimon/projects.json.

Maps short aliases ("foundry") to absolute paths on disk.
Loaded fresh each call so edits to the JSON file take effect without restart.
"""

import json
from pathlib import Path

_REGISTRY = Path.home() / ".daimon" / "projects.json"


def load() -> dict[str, str]:
    """Return {alias_lower: path_str} from the registry file."""
    if not _REGISTRY.is_file():
        return {}
    try:
        data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k).strip().lower(): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def resolve(name: str) -> Path | None:
    """Return the resolved Path for alias *name*, or None if unknown."""
    entry = load().get(name.strip().lower())
    return Path(entry) if entry else None


def known_aliases() -> list[str]:
    """Return sorted list of known aliases (for error messages)."""
    return sorted(load().keys())
