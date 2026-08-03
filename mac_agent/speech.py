"""
speech.py — serialized text-to-speech with handles and a completion signal.

Concurrent speak() calls queue up instead of interrupting each other (macOS
stops the previous utterance when a new `say` process starts).

Two things this layer provides that did not exist before:

  1. A handle to what is speaking, which can be stopped. `speak()` returns an
     Utterance; nothing in agent.py calls its stop() yet. Step 3 builds the
     mechanism; step 7 decides the policy. Deliberately absent here: any epoch
     counter, any barge-in rule, any stale-chunk dropping, and any per-pop
     _MUTED re-check. A second cancellation path would have to be reconciled
     with step 7's epoch counter, which is worse than having none.

  2. A real completion signal — wait_idle() — so handle()'s `finally` can emit
     an honest `state: idle` instead of one that fires while audio is playing.

speak() stays non-blocking. It is called from the agent loop, which is the same
thread that reads user input, so blocking there would stall the turn machine.
Completion is therefore separately observable rather than something speak()
waits for.

MUTE is still checked once, at enqueue, exactly as before. Under mute nothing is
queued, so wait_idle() returns immediately and browser-driven voice mode behaves
bit-identically to before this change.

INVARIANT — one producer, enforced in speak(), not merely documented.
wait_idle() waits on *all* queued speech, and the caller treats that as "this
turn's speech". Those are the same set only because there is exactly one agent
thread. A second producer (a sidecar pushing its own audio, a background
narrator) breaks the equivalence *silently*: wait_idle returns on someone else's
speech and handle() emits exactly the premature idle this layer exists to
prevent. Because the failure is silent, prose is not enough — speak() records
the owning thread and raises if a second one enqueues while speech is pending.

Ownership is per-pending-set, not per-process: a quiet queue can be claimed by
whoever speaks next, so sequential use from different threads is fine. Only
genuine concurrency trips it, which is the harmful case.
"""

import os
import queue
import threading

from .chunking import split_utterance
from .playback import FallbackPlayer, FinishedHandle, KokoroPlayer, SayPlayer

_QUEUE = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_MUTED = False
_SAY_PLAYER = SayPlayer()
_SIDECAR = None
_PLAYER = _SAY_PLAYER

# The sidecar lives OUTSIDE the app bundle, in its own venv, because
# `pip install kokoro` would change pyproject.toml and trigger the rebuild that
# silently drops the macOS Accessibility grant. Python 3.12 specifically: 3.13
# has no cp313 wheel for spaCy's `blis` and its source build dies in Cython.
_DEFAULT_TTS_PYTHON = "~/.daimon/tts-venv/bin/python"
_DEFAULT_TTS_SOCKET = "~/.daimon/tts.sock"


def start_sidecar(python_bin=None, socket_path=None, voice=None, device=None):
    """Launch the Kokoro sidecar eagerly and route playback through it.

    Called at agent startup, not on first speak(). Cold start is 6.56 s
    measured — longer than most turns — so putting it in front of the first
    utterance would be the failure users notice most. Instead the first seconds
    after launch use `say` and the voice changes once, observably, when the
    sidecar reports ready.

    Degrades silently when the sidecar venv is absent, following the
    palace_memory.py precedent: no sidecar, no error, just `say`.
    """
    global _SIDECAR, _PLAYER
    python_bin = os.path.expanduser(
        python_bin or os.environ.get("DAIMON_TTS_PYTHON") or _DEFAULT_TTS_PYTHON)
    socket_path = os.path.expanduser(
        socket_path or os.environ.get("DAIMON_TTS_SOCKET") or _DEFAULT_TTS_SOCKET)
    # resources.script_path(), NOT a join on __file__. py2app puts mac_agent
    # inside Resources/lib/pythonX.Y/, and a manual __file__ join resolves to a
    # path that does not exist in the frozen bundle — so the .app would have
    # silently used `say` forever while dev runs looked fine. This is the same
    # resolver the AppleScripts use.
    from .resources import script_path as _script_path
    try:
        script_path = _script_path("tts_sidecar.py")
    except FileNotFoundError:
        script_path = ""
    if not os.path.exists(python_bin) or not os.path.exists(script_path):
        # Degrading to `say` is correct — but say which, because a missing venv
        # and a broken sidecar are otherwise indistinguishable from outside,
        # and both simply sound like the old voice.
        print(f"[tts] no sidecar venv at {python_bin} — using /usr/bin/say",
              flush=True)
        return None
    sidecar = KokoroPlayer(socket_path, python_bin, script_path,
                           voice=voice or os.environ.get("DAIMON_TTS_VOICE"),
                           device=device or os.environ.get("DAIMON_TTS_DEVICE"))
    sidecar.start()
    _SIDECAR = sidecar
    _PLAYER = FallbackPlayer(sidecar, _SAY_PLAYER)
    return sidecar


def sidecar_ready():
    """Whether the sidecar is warm. Observable state, not a timeout race."""
    return bool(_SIDECAR is not None and _SIDECAR.ready)

# Chunks queued or in flight, guarded by _PENDING_CV. This is what wait_idle()
# blocks on and what handle() reports when it times out.
_PENDING = 0
_PENDING_CV = threading.Condition()

# Thread that owns the currently-pending speech. Enforces the one-producer
# invariant in speak(); see the module docstring.
_PRODUCER = None

# The ceiling on how long handle() will wait for a whole utterance's audio
# before giving up and emitting idle anyway.
#
# Lowered from 120 s with the step-4 measurement, as that value's comment
# anticipated. Predicted wall time per utterance from the measured constants
# (527 ms + 7.36 ms/char synthesis, 54.8 ms/char of audio, 494 ms drain margin)
# over the 97-utterance corpus: median 3.5 s, p90 7.0 s, max 73.2 s. That max is
# a real 22-chunk calendar listing, so the bound has to clear it; 90 s gives it
# ~23% headroom. Anything lower would cut off a legitimate long utterance, which
# is why this did not drop further.
DRAIN_TIMEOUT = 90.0

# The ceiling on a SINGLE chunk. This is the one that did not exist before.
#
# `handle.wait()` was called with no timeout, which is safe for `say` — a local
# binary that always terminates — and unsafe for a socket, where an unresponsive
# sidecar wedges the speech worker forever and DRAIN_TIMEOUT is the only
# backstop. Worst legitimate chunk measured was 20.3 s end to end (a 133-char
# filesystem path that Kokoro spells out character by character); 45 s is over
# twice that and still bounded.
CHUNK_TIMEOUT = 45.0


def set_muted(value):
    """Suppress `say` output (e.g. while a browser client drives TTS)."""
    global _MUTED
    _MUTED = bool(value)


def pending():
    """Chunks queued or in flight. Reported by handle() when a drain times out."""
    with _PENDING_CV:
        return _PENDING


def _leave():
    global _PENDING
    with _PENDING_CV:
        _PENDING -= 1
        if _PENDING <= 0:
            _PENDING_CV.notify_all()


def wait_idle(timeout=None):
    """Block until nothing is queued or playing. False if it timed out."""
    with _PENDING_CV:
        return _PENDING_CV.wait_for(lambda: _PENDING <= 0, timeout)


class Utterance:
    """One speak() call: a handle to its chunks, stoppable as a unit.

    Chunk-level playback handles are owned here and never handed out — a caller
    holding one could stop half an utterance, which is a policy decision that
    does not belong in step 3.
    """

    __slots__ = ("_lock", "_cv", "_remaining", "_stopped", "_current")

    def __init__(self, chunk_count):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._remaining = chunk_count
        self._stopped = False
        self._current = None

    @property
    def done(self):
        with self._lock:
            return self._remaining <= 0

    def wait(self, timeout=None):
        """Block until every chunk has been played or dropped."""
        with self._cv:
            return self._cv.wait_for(lambda: self._remaining <= 0, timeout)

    def stop(self):
        """Stop this utterance: drop its queued chunks, stop the one playing.

        Idempotent and safe from any thread. Nothing in agent.py calls this in
        step 3 — the callers arrive with step 7's cancellation policy.
        """
        with self._lock:
            self._stopped = True
            current = self._current
        if current is not None:
            current.stop()

    def _play(self, text):
        """Play one chunk. Called only from the worker thread."""
        with self._lock:
            if self._stopped:
                return          # queued but never started: dropped unspoken

        # play() is called *outside* the lock on purpose. It may be slow — the
        # step 4 sidecar does a socket round-trip — and holding the lock across
        # it would leave stop() blocked behind exactly the call it is trying to
        # cancel.
        handle = _PLAYER.play(text)

        with self._lock:
            # stop() may have landed while play() was in flight. It saw
            # _current as None, so it could not have stopped this handle.
            late_stop = self._stopped
            if not late_stop:
                self._current = handle
        if late_stop:
            handle.stop()
            return

        if not handle.wait(CHUNK_TIMEOUT):
            # A socket peer can hang where a `say` process could not. Give the
            # chunk up rather than wedging the worker thread forever; stop() is
            # itself bounded, so this cannot trade one hang for another.
            handle.stop()
        with self._lock:
            if self._current is handle:
                self._current = None

    def _chunk_finished(self):
        with self._cv:
            self._remaining -= 1
            if self._remaining <= 0:
                self._cv.notify_all()


class _Chunk:
    """One queue item: a chunk of text and the utterance it belongs to.

    _release() is the single accounting path. The worker calls it in its
    `finally`; tests that drain the queue by hand call the same thing, so the
    test path cannot drift from production.
    """

    __slots__ = ("utterance", "text", "_lock", "_released")

    def __init__(self, utterance, text):
        self.utterance = utterance
        self.text = text
        self._lock = threading.Lock()
        self._released = False

    def _release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
        self.utterance._chunk_finished()
        _leave()


def _run_one(item):
    """Play one queue item and account for it. The worker's whole loop body.

    Separate from _worker so tests can drive playback deterministically through
    the production path instead of racing a background thread.
    """
    try:
        item.utterance._play(item.text)
    except Exception:
        pass
    finally:
        item._release()


def _worker():
    while True:
        item = _QUEUE.get()
        try:
            _run_one(item)
        finally:
            _QUEUE.task_done()


def _ensure_worker():
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker, name="daimon-say", daemon=True).start()
        _WORKER_STARTED = True


def speak(text):
    """Queue text for speech, one queue item per speakable chunk.

    Returns an Utterance — always, never None. Muted or empty text yields one
    that is already finished, so callers never branch on None and step 7 cannot
    inherit a null-handle bug.

    Chunking is what gives playback a clean place to stop mid-utterance. It does
    not reduce time-to-first-audio — no backend streams, so the whole utterance
    already exists before the first chunk is queued.
    """
    global _PENDING, _PRODUCER
    chunks = [] if _MUTED else split_utterance(text)
    utterance = Utterance(len(chunks))
    if not chunks:
        return utterance
    _ensure_worker()
    ident = threading.get_ident()
    with _PENDING_CV:
        # Enforce the one-producer invariant. Sequential ownership is fine — a
        # quiet queue can be claimed by whoever speaks next — but two threads
        # with speech in flight at once is the failure this guards.
        if _PENDING == 0:
            _PRODUCER = ident
        elif _PRODUCER != ident:
            raise RuntimeError(
                f"speech.speak() called from thread {ident} while thread "
                f"{_PRODUCER} still has {_PENDING} chunk(s) pending. "
                "wait_idle() assumes a single producer: handle() treats 'all "
                "queued speech' as 'this turn's speech', so a second producer "
                "makes it return early and emit exactly the premature idle "
                "this layer exists to prevent. If a sidecar or narrator now "
                "enqueues its own audio, wait_idle needs a scoped replacement "
                "— see docs/superpowers/specs/step-4-brief.md."
            )
        # Count before queueing, so wait_idle() can never observe zero in the
        # gap between the put and the accounting.
        _PENDING += len(chunks)
    for chunk in chunks:
        _QUEUE.put(_Chunk(utterance, chunk))
    return utterance


__all__ = ["CHUNK_TIMEOUT", "DRAIN_TIMEOUT", "FinishedHandle", "Utterance",
           "pending", "set_muted", "sidecar_ready", "speak", "start_sidecar",
           "wait_idle"]
