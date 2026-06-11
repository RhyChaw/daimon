"""
app_main.py — py2app entry point for Daimon.app.

Double-clicking the app opens a native console window. macOS attributes
Calendar / Mail automation and Touch ID to com.rhychaw.daimon, not Terminal
or Cursor.
"""

import os
import sys
from pathlib import Path


def _configure_app_paths():
    """Use Application Support when bundled (Finder launch cwd is unreliable)."""
    if not getattr(sys, "frozen", False):
        return
    support = Path.home() / "Library" / "Application Support" / "Daimon"
    support.mkdir(parents=True, exist_ok=True)
    os.chdir(support)
    os.environ.setdefault("MACAGENT_MEMORY", str(support / "memory.json"))
    os.environ.setdefault("MACAGENT_AUDIT", str(support / "audit.jsonl"))
    os.environ.setdefault("DAIMON_MEMORY_DIR", str(support / "memory"))


def _repl_with_input(input_fn):
    from mac_agent.agent import repl
    from mac_agent.ollama_client import ping

    if not ping():
        print("Could not reach Ollama at http://localhost:11434")
        print("  1. install Ollama:  https://ollama.com")
        print("  2. pull a model:    ollama pull llama3.2")
        print("  3. make sure it's running, then try again.")
        return
    repl(input_fn=input_fn)


def main():
    _configure_app_paths()
    from mac_agent.console_ui import run_console

    run_console(_repl_with_input)


if __name__ == "__main__":
    main()
