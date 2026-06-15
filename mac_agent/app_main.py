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
    os.environ.setdefault("DAIMON_SETTINGS", str(support / "settings.json"))


def _check_accessibility():
    """Verify Accessibility trust and guide the user if it's missing or stale.

    Ad-hoc–signed builds change their binary hash on every rebuild, which causes
    macOS TCC to silently reject the stale entry even though System Settings shows
    the app as 'checked'. We detect this and print actionable instructions.
    """
    try:
        from mac_agent.keystrokes import check_accessibility, request_accessibility
    except Exception:
        return

    if check_accessibility():
        return  # already trusted

    # Try to trigger the system dialog (works when there is no entry yet).
    granted = request_accessibility()
    if granted:
        # Granted synchronously — rare, usually needs a restart.
        return

    # Dialog may not have appeared because macOS found a stale TCC entry for an
    # older binary. Print clear instructions instead of silently failing later.
    print()
    print("  [!] Daimon does not have Accessibility access.")
    print("      Keystrokes (e.g. play_music) will not work until you fix this.")
    print()
    print("      This happens after every rebuild because the app binary changes.")
    print("      To fix:")
    print("        1. System Settings → Privacy & Security → Accessibility")
    print("        2. Find Daimon in the list → click the − button to remove it")
    print("        3. Click + and add Daimon.app again  (or drag it in)")
    print("        4. Restart Daimon")
    print()


def _repl_with_input(input_fn):
    from pathlib import Path
    from mac_agent import ws_server
    from mac_agent.agent import repl
    from mac_agent.backend import create_backend
    from mac_agent.resources import web_dir

    # Start here — sys.stdout is now the Daimon console window.
    ws_server.start(web_dir=Path(web_dir()))
    repl(input_fn=input_fn, backend_getter=create_backend)


def main():
    from mac_agent.env import load_dotenv

    load_dotenv()
    _configure_app_paths()
    load_dotenv()
    _check_accessibility()
    from mac_agent.console_ui import run_console

    run_console(_repl_with_input)


if __name__ == "__main__":
    main()
