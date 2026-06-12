"""Bring a target app forward before UI keystrokes — never hide Daimon (that looks like a quit)."""

import subprocess
import time


def prepare_for_keystrokes(app_name="Spotify", wait=2.0):
    subprocess.run(["open", "-a", app_name], check=False)
    subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'], check=False)
    time.sleep(wait)
