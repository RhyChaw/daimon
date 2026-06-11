"""
ollama_client.py — talk to the local Ollama server. Standard library only.

Ollama runs at http://localhost:11434. We force JSON output so the model's
reply is a parseable action object instead of prose.
"""

import json
import urllib.request

HOST = "http://localhost:11434"


def ping(host=HOST):
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def list_models(host=HOST):
    with urllib.request.urlopen(host + "/api/tags", timeout=5) as r:
        data = json.loads(r.read())
    return [m["name"] for m in data.get("models", [])]


def resolve_model(requested, host=HOST):
    """Return (resolved_name, available) or (None, available) if not found."""
    available = list_models(host)
    if requested in available:
        return requested, available
    for name in available:
        if name.split(":")[0] == requested:
            return name, available
    return None, available


def parse_action(content):
    """Parse Ollama message content into an action object. One json.loads only."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise TypeError(f"expected str or dict, got {type(content).__name__}")
    return json.loads(content.strip())


def chat(model, system, user, host=HOST):
    body = json.dumps({
        "model": model,
        "format": "json",       # force the reply to be valid JSON
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    req = urllib.request.Request(
        host + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return data["message"]["content"]
