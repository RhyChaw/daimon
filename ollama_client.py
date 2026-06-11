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
