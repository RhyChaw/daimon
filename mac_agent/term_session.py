"""
term_session.py — PTY-backed Claude Code session.

Spawns `claude` in a pseudo-terminal at a project directory.
PTY output is base64-encoded and broadcast to WS clients via events.
WS clients send keystrokes as plain strings; resize events adjust TIOCSWINSZ.
"""

import base64
import fcntl
import json
import os
import resource
import select
import shlex
import struct
import termios
import threading
from pathlib import Path

from . import events

# ── Active session (one at a time) ───────────────────────────────────────────
_active: "TermSession | None" = None


# ── Window size ───────────────────────────────────────────────────────────────
def _set_winsize(fd: int, cols: int, rows: int) -> None:
    try:
        s = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, s)
    except OSError:
        pass


# ── Suppressing Claude Code's own chrome ─────────────────────────────────────
# This PTY is a surface inside Daimon's UI, not a terminal window. Claude Code's
# status line and update banner are another product's chrome showing through, and
# they are the main reason the panel reads as an embedded iframe.
#
# Both suppressions are scoped to this spawn only — they are process arguments
# and process environment, never written to ~/.claude/settings.json. The user's
# own terminal keeps whatever statusLine and updater behaviour they configured.
#
# statusLine is overridden rather than removed: `true` exits 0 with no output, so
# Claude Code renders an empty status line instead of the user's command.
_QUIET_SETTINGS = json.dumps({"statusLine": {"type": "command", "command": "true"}})


# ── Build environment with expanded PATH ─────────────────────────────────────
def _child_env() -> dict:
    env = dict(os.environ)
    home = str(Path.home())
    env["HOME"] = home
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Silences "Update available! Run: brew upgrade claude-code" inside the panel.
    # Scoped to this child: it does not stop the user updating Claude Code normally.
    env["DISABLE_AUTOUPDATER"] = "1"
    # Prepend common locations where `claude` may be installed.
    extras = [
        f"{home}/.claude/local",
        f"{home}/.nvm/versions/node/current/bin",
        f"{home}/.local/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    env["PATH"] = ":".join(extras) + ":" + env.get("PATH", "")
    return env


class TermSession:
    def __init__(self, cwd: str, cols: int = 220, rows: int = 50):
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.pid: int | None = None
        self.fd: int | None = None
        self._alive = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import pty
        env = _child_env()
        # Use login shell so PATH and nvm/etc. are configured; exec replaces it
        # with claude so the PTY shows the claude session directly.
        # The settings JSON is shell-quoted: zsh -c re-parses this string, and an
        # unquoted payload loses its double quotes and reaches claude as a path.
        cmd = ["zsh", "-l", "-c", f"exec claude --settings {shlex.quote(_QUIET_SETTINGS)}"]

        self.pid, self.fd = pty.fork()

        if self.pid == 0:
            # ── Child ──────────────────────────────────────────────────────
            try:
                # Close all fds > 2 so the parent's sockets don't leak.
                try:
                    maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
                    if maxfd == resource.RLIM_INFINITY or maxfd > 4096:
                        maxfd = 4096
                    os.closerange(3, maxfd)
                except Exception:
                    pass
                os.chdir(self.cwd)
                os.execvpe(cmd[0], cmd, env)
            except Exception:
                pass
            os._exit(1)

        # ── Parent ─────────────────────────────────────────────────────────
        _set_winsize(self.fd, self.cols, self.rows)
        self._alive = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True, name="daimon-pty"
        )
        self._thread.start()

    def _read_loop(self) -> None:
        while self._alive and self.fd is not None:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self.fd, 8192)
                if not data:
                    break
                events.emit({
                    "type": "term",
                    "data": base64.b64encode(data).decode("ascii"),
                })
            except OSError:
                break
            except Exception:
                continue

        self._alive = False
        if self.pid:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except Exception:
                pass
        events.emit({"type": "term_close"})

    def write(self, data: bytes) -> None:
        with self._lock:
            if self.fd is not None and self._alive:
                try:
                    os.write(self.fd, data)
                except OSError:
                    pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self.fd is not None:
            _set_winsize(self.fd, cols, rows)

    def kill(self) -> None:
        self._alive = False
        if self.pid:
            try:
                os.kill(self.pid, 15)
            except OSError:
                pass
        with self._lock:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None


# ── Public API ────────────────────────────────────────────────────────────────

def start_session(cwd: str, cols: int = 220, rows: int = 50) -> "TermSession":
    global _active
    if _active is not None and _active._alive:
        _active.kill()
    _active = TermSession(cwd=str(cwd), cols=cols, rows=rows)
    _active.start()
    return _active


def write_input(data: bytes) -> None:
    if _active is not None and _active._alive:
        _active.write(data)


def resize(cols: int, rows: int) -> None:
    if _active is not None:
        _active.resize(cols, rows)


def is_active() -> bool:
    return _active is not None and _active._alive


def close_session() -> None:
    global _active
    if _active is not None:
        _active.kill()
        _active = None
