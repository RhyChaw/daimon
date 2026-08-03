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
import itertools
import json
import os
import socket
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


class SidecarUnavailable(Exception):
    """The Kokoro sidecar cannot take this chunk. Fall back to `say`.

    Raised for every reason the sidecar might not serve a request: not warm
    yet, process gone, socket refused, protocol error, or busy. The caller
    treats them identically because the remedy is identical.
    """


# The sidecar acknowledges stop in 2-6 ms measured mid-playback, so this is two
# orders of magnitude of slack before we treat it as wedged. Unlike
# SayHandle.stop(), which can always fall back to SIGKILL, a socket stop has no
# floor of its own -- so escalation is explicit: no ack -> close socket -> kill.
_SIDECAR_STOP_GRACE = 0.5
_SIDECAR_ACK_TIMEOUT = 2.0


def _log(message):
    """Which backend is live, on one line.

    Falling back to `say` is correct behaviour when the sidecar is absent — but
    correct-and-silent is indistinguishable from broken-and-silent, and this
    layer's whole design stance is that failures should be visible. A panel
    comes later; a line is enough now.
    """
    print(f"[tts] {message}", flush=True)


class KokoroHandle:
    """A handle to one chunk in flight inside the sidecar.

    Owns one socket, the way SayHandle owns one process. That is deliberate:
    stop() is called from a thread that is not the one blocked in wait(), so a
    single half-duplex connection shared by both would make the writer contend
    with a blocked reader. Sockets are full-duplex, so one connection per chunk
    lets stop() send while the reader thread sits in recv.
    """

    __slots__ = ("_sock", "_rf", "_req_id", "_done", "_lock", "_closed",
                 "_send_lock", "_on_wedged", "_stop_ack", "cancelled")

    def __init__(self, sock, rf, req_id, on_wedged):
        self._sock = sock
        self._rf = rf
        self._req_id = req_id
        self._on_wedged = on_wedged
        self._done = threading.Event()
        self._stop_ack = threading.Event()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        self.cancelled = False
        threading.Thread(target=self._read, name="daimon-tts-rx",
                         daemon=True).start()

    def _read(self):
        try:
            for line in self._rf:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                event = msg.get("ev")
                if event == "stopped":
                    # Audio has already ceased: the sidecar aborts the stream
                    # synchronously before replying. `done` follows once the
                    # speak thread notices, which may be much later.
                    self._stop_ack.set()
                elif event == "done":
                    self.cancelled = bool(msg.get("cancelled"))
                    # `done` settles the stop question too. Without this,
                    # stop() blocks its full grace waiting for a `stopped`
                    # frame that is already moot -- measured 502 ms against a
                    # 150 ms bar when `done` had landed at ~50 ms.
                    self._stop_ack.set()
                    break
        except OSError:
            pass
        finally:
            # Setting the event in `finally` is what makes a dead sidecar
            # surface as "finished" rather than as a worker thread wedged
            # forever in wait(). The chunk is lost either way; hanging the
            # agent as well is strictly worse.
            self._done.set()
            # This thread OWNS _rf and is the only one allowed to close it --
            # see _close().
            for closer in (self._rf.close, self._sock.close):
                try:
                    closer()
                except OSError:
                    pass

    def _send(self, obj):
        with self._send_lock:
            self._sock.sendall((json.dumps(obj) + "\n").encode())

    def _close(self):
        """Unblock the reader from another thread, without deadlocking on it.

        Do NOT close self._rf here. A BufferedReader's close() acquires the
        same lock its blocked readline() already holds, so closing it from the
        stopping thread while the reader sits in recv deadlocks both -- which
        is exactly the hang stop()'s bounded escalation exists to prevent,
        reintroduced one level down. Reproduced: the test suite wedged
        indefinitely until this became a shutdown().

        shutdown() needs no such lock: it makes the in-flight recv return EOF,
        the reader's `finally` runs, and the reader closes what it owns.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass          # already closed, or peer gone

    @property
    def done(self):
        return self._done.is_set()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def stop(self):
        """Idempotent, safe from any thread, and bounded.

        Bounded is the part that does not come for free. SayHandle.stop() can
        always escalate to SIGKILL; a socket stop that waits on a wedged
        sidecar would hang on the WS thread in step 7.
        """
        if self._done.is_set():
            self._close()
            return
        try:
            self._send({"cmd": "stop", "id": self._req_id})
        except OSError:
            self._done.set()
            self._close()
            return
        # Wait for the ACKNOWLEDGEMENT, not for completion. The sidecar aborts
        # the output stream before replying `stopped`, so once that lands the
        # audio has already ceased -- which is what stop() promises. `done`
        # arrives later, whenever the speak thread next checks.
        #
        # Waiting for `done` here instead was wrong and measurably so: a stop
        # sent during synthesis cannot be answered until synthesis finishes
        # (~600 ms for a short sentence), which blew the grace every time and
        # escalated into killing a perfectly healthy sidecar.
        if self._stop_ack.wait(_SIDECAR_STOP_GRACE) or self._done.is_set():
            return
        # No acknowledgement at all. Closing the socket unblocks our reader,
        # whose `finally` sets the event; then take the process down so the
        # next play() falls back to `say` instead of hanging again.
        self._close()
        self._done.set()
        if self._on_wedged is not None:
            self._on_wedged()


class KokoroPlayer:
    """The Kokoro sidecar, behind the same play/wait/stop/done interface.

    Nothing above this line knows a socket exists. `play()` raises
    SidecarUnavailable when it cannot serve the chunk; selecting a backend on
    that signal is FallbackPlayer's job and happens inside play(), never by
    re-entering speech.speak() -- see the note there.
    """

    name = "kokoro"

    def __init__(self, socket_path, python_bin, script_path, voice=None,
                 device=None):
        self._socket_path = socket_path
        self._python_bin = python_bin
        self._script_path = script_path
        self._voice = voice
        self._device = device
        self._proc = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._next_id = itertools.count(1)

    @property
    def ready(self):
        """Non-blocking. Readiness is a state, not a race resolved by timeout."""
        return (self._ready.is_set() and self._proc is not None
                and self._proc.poll() is None)

    def start(self):
        """Launch the sidecar eagerly, at agent startup.

        Cold start is 6.56 s measured -- longer than most turns. Starting on
        first speak() would put that in front of the first utterance; starting
        here means the first seconds after launch use `say` and the voice
        changes once, observably, when `ready` flips.
        """
        with self._lock:
            if self._proc is not None:
                return
            cmd = [self._python_bin, self._script_path,
                   "--socket", self._socket_path]
            if self._voice:
                cmd += ["--voice", self._voice]
            if self._device:
                cmd += ["--device", self._device]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
            except OSError:
                self._proc = None
                return
        threading.Thread(target=self._await_ready, name="daimon-tts-ready",
                         daemon=True).start()

    def _await_ready(self):
        proc = self._proc
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue          # library banners on stdout; ignore
                if msg.get("ev") == "ready":
                    self._ready.set()
                    _log(f"kokoro sidecar ready in {msg.get('load_s')}s "
                         f"(voice {msg.get('voice')}) — speech switches from "
                         "`say` to kokoro now")
                    return
        except (OSError, ValueError):
            pass
        # stdout closed without ready: the sidecar failed to start. Most likely
        # the espeak check refused, which is the intended behaviour -- running
        # with unk='' would silently drop words. Stay unready; play() falls back.
        #
        # Say so. Falling back to `say` is correct behaviour, but silence here
        # is indistinguishable from a broken sidecar, and the whole point of
        # the espeak refusal is that failures are visible rather than silent.
        _log("sidecar did not become ready — speech stays on /usr/bin/say")

    def shutdown(self):
        with self._lock:
            proc, self._proc = self._proc, None
        was_ready = self._ready.is_set()
        self._ready.clear()
        if proc is None:
            return
        if was_ready:
            _log("sidecar stopped — speech falls back to /usr/bin/say")
        try:
            proc.terminate()
            proc.wait(timeout=_STOP_GRACE)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def play(self, text):
        if not self.ready:
            raise SidecarUnavailable("sidecar not ready")
        req_id = next(self._next_id)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_SIDECAR_ACK_TIMEOUT)
            sock.connect(self._socket_path)
            sock.sendall(
                (json.dumps({"cmd": "speak", "id": req_id, "text": text})
                 + "\n").encode())
            rf = sock.makefile("r", encoding="utf-8")
            ack = json.loads(rf.readline() or "{}")
        except (OSError, ValueError) as exc:
            raise SidecarUnavailable(f"sidecar speak failed: {exc}") from exc

        if ack.get("ev") != "ack":
            # "busy" lands here. The speech worker serialises, so a second
            # concurrent speak means an assumption broke -- speak this chunk
            # through `say` rather than dropping it, and let the sidecar's
            # refusal stay loud rather than being absorbed by a queue.
            try:
                sock.close()
            except OSError:
                pass
            raise SidecarUnavailable(f"sidecar refused: {ack.get('ev')!r}")

        sock.settimeout(None)          # reader thread blocks; ack window is over
        return KokoroHandle(sock, rf, req_id, self.shutdown)


class FallbackPlayer:
    """Selects a backend per chunk. THIS is where fallback belongs.

    The tempting alternative -- catching SidecarUnavailable somewhere and
    calling speech.speak() again -- is a trap. That call happens on the speech
    worker thread, which is not the pending set's producer, so the one-producer
    guard raises RuntimeError; _run_one's `except Exception: pass` swallows it;
    the chunk is silently dropped and the accounting still balances, so nothing
    looks wrong. Backend selection stays here, inside play().
    """

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    @property
    def name(self):
        return (self._primary.name if getattr(self._primary, "ready", False)
                else self._fallback.name)

    def play(self, text):
        try:
            return self._primary.play(text)
        except SidecarUnavailable:
            return self._fallback.play(text)


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
