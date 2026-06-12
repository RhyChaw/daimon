"""
backend.py — swappable model backends for action JSON reasoning.

Both implementations expose chat(system, user) -> str with the same action contract.
"""

import json
import os
import re

from . import anthropic_client
from . import ollama_client
from . import settings

DEFAULT_OLLAMA_MODEL = "llama3.2"
OLLAMA_FALLBACKS = ("llama3.1:latest", "llama3.1:8b", "llama3:latest")


class BackendError(Exception):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def parse_action(content):
    """Parse model output into an action object."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise TypeError(f"expected str or dict, got {type(content).__name__}")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


class OllamaBackend:
    name = "ollama"

    def __init__(self, model):
        self.model = model

    def ping(self):
        return ollama_client.ping()

    def describe(self):
        return f"ollama · {self.model}"

    def chat(self, system, user):
        try:
            return ollama_client.chat(self.model, system, user)
        except Exception as e:
            raise BackendError(str(e), "run: ollama pull " + self.model) from e

    @classmethod
    def create(cls):
        requested = os.environ.get("MACAGENT_MODEL", DEFAULT_OLLAMA_MODEL)
        resolved, available = ollama_client.resolve_model(requested)
        if resolved:
            return cls(resolved), None
        for candidate in OLLAMA_FALLBACKS:
            resolved, _ = ollama_client.resolve_model(candidate)
            if resolved:
                note = (
                    f"note: {requested!r} not installed — using {resolved!r}\n"
                    f"        (run `ollama pull {requested}` to use the default)"
                )
                return cls(resolved), note
        hint = "run: ollama pull " + requested
        if available:
            hint = f"available: {', '.join(available)}\n  " + hint
        return None, BackendError(f"Model {requested!r} not found.", hint)


class ClaudeBackend:
    name = "claude"

    def __init__(self, model=None):
        self.model = model or anthropic_client._model()

    def ping(self):
        return anthropic_client.ping()

    def describe(self):
        return f"claude · {self.model}"

    def chat(self, system, user):
        try:
            return anthropic_client.chat(system, user, model=self.model)
        except Exception as e:
            raise BackendError(str(e), "set ANTHROPIC_API_KEY in .env or your environment") from e

    @classmethod
    def create(cls):
        if not anthropic_client.ping():
            return None, BackendError(
                "ANTHROPIC_API_KEY is not set.",
                "add it to .env or switch backend to Ollama",
            )
        return cls(), None


def create_backend(name=None):
    name = (name or settings.get_backend()).strip().lower()
    if name == "claude":
        backend, err = ClaudeBackend.create()
    else:
        backend, err = OllamaBackend.create()
    if backend is None:
        if isinstance(err, BackendError):
            raise err
        raise BackendError(str(err or "backend unavailable"))
    if isinstance(err, str) and err.strip():
        print(f"  {err}")
    return backend


def check_backend_ready(backend=None):
    backend = backend or create_backend()
    if backend.name == "ollama":
        if backend.ping():
            return True, None
        return False, (
            "Could not reach Ollama at http://localhost:11434\n"
            "  1. install Ollama:  https://ollama.com\n"
            "  2. pull a model:    ollama pull llama3.2\n"
            "  3. make sure it's running, then try again."
        )
    if backend.ping():
        return True, None
    return False, (
        "ANTHROPIC_API_KEY is not set.\n"
        "  add ANTHROPIC_API_KEY to .env  or switch backend to Ollama (local)."
    )
