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
import subprocess
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


# ── Which Claude Code binary ─────────────────────────────────────────────────
# Pinned to an absolute path rather than resolved from PATH. `zsh -l` re-sources
# .zprofile, whose `brew shellenv` prepends /opt/homebrew/bin ahead of anything
# _child_env() sets — so a bare `claude` silently ran Homebrew's 2.1.98 while
# ~/.local/bin held 2.1.220. Four months of drift, invisible from inside the app.
#
# That is a correctness problem, not a cosmetic one: the planned stream-json and
# hook work depends on feature availability and event shapes that moved across
# those versions, and it would have been developed against one version while
# being tested against another in a normal terminal.
_DEFAULT_CLAUDE_BIN = "~/.local/bin/claude"


def _resolve_claude_bin() -> str:
    """Absolute path to the Claude Code binary to spawn, or bare 'claude'."""
    path = os.path.expanduser(
        os.environ.get("DAIMON_CLAUDE_BIN") or _DEFAULT_CLAUDE_BIN)
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    # Falling back to PATH restores the drift this pin exists to prevent, so it
    # is never silent — an unpinned session must be visibly unpinned.
    print(f"  [term] no claude at {path} — falling back to PATH resolution",
          flush=True)
    return "claude"


def _log_claude_version(path: str) -> None:
    """Report the resolved binary and its version once, off the spawn path.

    Probed in a thread: a silently-wrong version is indistinguishable from a
    correct one until it isn't, but finding that out must not delay the session.
    """
    def _probe() -> None:
        try:
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=10)
            raw = (out.stdout or out.stderr).strip().splitlines()
            version = raw[0] if raw else "version unknown"
        except Exception as exc:
            version = f"version probe failed: {exc}"
        print(f"  claude  → {path}  ({version})", flush=True)

    threading.Thread(target=_probe, daemon=True, name="daimon-claude-ver").start()


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
        # Still a login shell — node/nvm and friends need it — but the binary is
        # named absolutely, so .zprofile no longer gets to choose the version.
        # Both arguments are shell-quoted: zsh -c re-parses this string, and an
        # unquoted payload loses its double quotes and reaches claude as a path.
        claude_bin = _resolve_claude_bin()
        cmd = ["zsh", "-l", "-c",
               f"exec {shlex.quote(claude_bin)} "
               f"--settings {shlex.quote(_QUIET_SETTINGS)}"]

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
        _log_claude_version(claude_bin)
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
