"""
anthropic_client.py — Claude via the Anthropic Messages API (stdlib only).
"""

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"
FAST_MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"


def _api_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _model():
    return os.environ.get("DAIMON_CLAUDE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def ping():
    return bool(_api_key())


def chat(system, user, model=None):
    key = _api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = model or _model()
    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API {e.code}: {detail}") from e
    blocks = data.get("content") or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Anthropic API returned empty content")
    return text
