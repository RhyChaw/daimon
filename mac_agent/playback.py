"""
playback.py — the out-of-process speaker, behind a handle that can be stopped.

The abstraction is "a speaker running outside this process that I hold a handle
to and can stop". That is true of /usr/bin/say today and has to stay true of the
Kokoro sidecar in step 4, which stops by sending {"cmd": "stop"} over a unix
socket rather than by signalling a process. So nothing here exposes a pid, a
Popen, or a socket: callers get play / wait / stop / done and nothing else. An
interface that only makes sense for subprocess.run would be the wrong one.

`play()` returns once playback has *started* — never once it has finished. It is
allowed to be slow (the sidecar will do a socket round-trip); it is only ever
called from the speech worker thread, never from the agent loop.

Measured on this machine, 2026-08-02, capturing the real output stream through a
BlackHole loopback device (process exit is not the quantity of interest: `say`
hands samples to CoreAudio and can exit while buffered audio still plays):

    signal -> process exit       2.5-3.9 ms   terminate and kill alike
    signal -> audible silence    <= ~150 ms   two independent methods agreed
    SIGKILL does not wedge the audio device   interleaved trials stayed clean

terminate and kill were indistinguishable, so we send SIGTERM first — it is the
gentler signal and it is not slower — and escalate to SIGKILL only if the
process does not go away, which in measurement it always did within ~4 ms.
"""

import atexit
import os
import subprocess
import tempfile
import threading

_SAY_BIN = "/usr/bin/say"

# Temp files belonging to handles that have not been reaped yet.
#
# The normal and stopped paths both unlink, but neither runs if the interpreter
# goes away while the worker is blocked in handle.wait() — the worker is a
# daemon thread, so it is killed outright. That leaks one file per interrupted
# utterance, and it is not hypothetical: killing the process mid-utterance
# reproduces it every time. The old subprocess.run/finally shape leaked the same
# way, which is where the stale file found during step 3 came from.
#
# atexit covers ordinary exit (`exit`, EOF, Ctrl-C). Nothing can cover SIGKILL.
# Unlinking a file `say` still has open is safe: the inode survives the open fd,
# so playback is unaffected and this changes no audible behaviour.
_LIVE_PATHS = set()
_LIVE_LOCK = threading.Lock()


def _track(path):
    with _LIVE_LOCK:
        _LIVE_PATHS.add(path)


def _untrack(path):
    with _LIVE_LOCK:
        _LIVE_PATHS.discard(path)


@atexit.register
def _sweep_temp_files():
    with _LIVE_LOCK:
        paths = list(_LIVE_PATHS)
        _LIVE_PATHS.clear()
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass

# Measured signal->exit was 2.5-3.9 ms; this is two orders of magnitude of slack
# before we escalate to SIGKILL.
_STOP_GRACE = 0.25


class SayHandle:
    """A handle to one in-flight `say` process and the temp file it is reading.

    The handle owns the temp file. It unlinks on both the normal path and the
    stopped path, and the unlink is idempotent because stop() is idempotent and
    may race the worker's own reap from another thread.
    """

    __slots__ = ("_proc", "_temp_path", "_lock", "_cleaned")

    def __init__(self, proc, temp_path):
        self._proc = proc
        self._temp_path = temp_path
        self._lock = threading.Lock()
        self._cleaned = False

    @property
    def done(self):
        return self._proc.poll() is not None

    def wait(self, timeout=None):
        """Block until playback finishes. True if it finished, False on timeout."""
        if not self._reap(timeout):
            return False
        self._cleanup()
        return True

    def stop(self):
        """Stop playback. Idempotent, and safe from a thread that is not wait()ing."""
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            if not self._reap(_STOP_GRACE):
                try:
                    self._proc.kill()
                except OSError:
                    pass
                self._reap(_STOP_GRACE)
        self._cleanup()

    def _reap(self, timeout):
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _cleanup(self):
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            path = self._temp_path
        _untrack(path)
        try:
            os.unlink(path)
        except OSError:
            pass


class SayPlayer:
    """macOS `say`, one process per chunk.

    Note for step 4: `say` costs ~1.35 s from exec to first audio on this
    machine, so a five-chunk utterance pays that five times — measured at
    +4.9 s of dead air versus a single process. That is an argument for the
    long-lived sidecar, not something this layer can fix.
    """

    name = "say"

    def play(self, text):
        """Start speaking `text`. Returns a handle; does not wait for audio."""
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="daimon-say-")
        _track(path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            proc = subprocess.Popen(
                [_SAY_BIN, "-f", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            _untrack(path)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return SayHandle(proc, path)


class FinishedHandle:
    """A handle to nothing, already complete.

    Returned where there is genuinely nothing to play, so callers never have to
    branch on None.
    """

    __slots__ = ()

    done = True

    def wait(self, timeout=None):
        return True

    def stop(self):
        pass
