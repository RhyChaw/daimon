"""
env.py — load DAIMON_* and ANTHROPIC_* vars from a .env file (stdlib only).

Existing shell exports always win; .env only fills unset keys.
Search order (later files override earlier for keys still unset):
  1. ~/Library/Application Support/Daimon/.env  (Daimon.app)
  2. <project>/.env                             (dev / editable install)
  3. ./.env                                     (cwd)
Override path: DAIMON_DOTENV=/path/to/.env
"""

import os
import sys
from pathlib import Path


def _parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].strip()
    key, sep, val = line.partition("=")
    if not sep or not key.strip():
        return None
    key = key.strip()
    val = val.strip()
    if "#" in val:
        val = val.split("#", 1)[0].rstrip()
    val = val.strip().strip('"').strip("'")
    return key, val


def _candidates():
    paths = []
    explicit = os.environ.get("DAIMON_DOTENV")
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(Path.home() / "Library/Application Support/Daimon/.env")
    if not getattr(sys, "frozen", False):
        paths.append(Path(__file__).resolve().parent.parent / ".env")
    paths.append(Path.cwd() / ".env")
    seen = set()
    out = []
    for p in paths:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(p)
    return out


def load_dotenv():
    """Load .env files into os.environ without overriding existing vars."""
    loaded = []
    for path in _candidates():
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)
            if not parsed:
                continue
            key, val = parsed
            if key not in os.environ:
                os.environ[key] = val
        loaded.append(str(path))
    return loaded
