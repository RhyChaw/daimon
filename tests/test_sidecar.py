"""
Tests for the Kokoro sidecar player: handles, bounded stop, and fallback.

Silent by construction, and there is NEVER a live sidecar. The handle tests use
socket.socketpair() — a real socket with both ends in this process — so the
protocol is exercised for real while nothing is spawned and nothing is audible.

The one test that touches Kokoro (test_ood_word_produces_audio) synthesises
without ever opening an output device, and skips unless the sidecar venv is
present. Synthesis makes no sound; only playback does.
"""

import importlib.util
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from mac_agent import playback, speech
from mac_agent.playback import (FallbackPlayer, KokoroHandle, KokoroPlayer,
                                SidecarUnavailable)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR_SCRIPT = os.path.join(REPO_ROOT, "mac_agent", "scripts", "tts_sidecar.py")


def _sidecar_python():
    """The external venv interpreter, or None if it is not installed."""
    path = os.path.expanduser(
        os.environ.get("DAIMON_TTS_PYTHON", "~/.daimon/tts-venv/bin/python"))
    return path if os.path.exists(path) else None


def _load_sidecar_module():
    """Import tts_sidecar for constants only. Safe: heavy imports are lazy."""
    spec = importlib.util.spec_from_file_location("tts_sidecar", SIDECAR_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HandlePair:
    """A KokoroHandle wired to a fake peer we drive by hand."""

    def __init__(self, on_wedged=None):
        self.ours, self.theirs = socket.socketpair()
        self.wedged = threading.Event()
        self.peer_rf = self.theirs.makefile("r", encoding="utf-8")
        rf = self.ours.makefile("r", encoding="utf-8")
        self.handle = KokoroHandle(
            self.ours, rf, 7, on_wedged or (lambda: self.wedged.set()))

    def peer_send(self, obj):
        self.theirs.sendall((json.dumps(obj) + "\n").encode())

    def peer_recv(self):
        return json.loads(self.peer_rf.readline())

    def close_peer(self):
        # makefile() dups the fd, so closing the socket alone leaves the peer
        # end open and the handle's reader never sees EOF.
        self.peer_rf.close()
        self.theirs.close()

    def cleanup(self):
        # shutdown() before close(), for the same reason KokoroHandle._close
        # does: the handle's reader thread may be blocked in readline() on
        # `ours`, and closing the buffered reader under it deadlocks.
        self.handle._close()
        self.handle.wait(2)
        for sock in (self.ours, self.theirs):
            try:
                sock.close()
            except OSError:
                pass


class KokoroHandleTests(unittest.TestCase):
    def tearDown(self):
        pair = getattr(self, "pair", None)
        if pair is not None:
            pair.cleanup()

    def test_done_frame_completes_the_handle(self):
        self.pair = HandlePair()
        self.assertFalse(self.pair.handle.done)
        self.pair.peer_send({"ev": "done", "id": 7})
        self.assertTrue(self.pair.handle.wait(2))
        self.assertTrue(self.pair.handle.done)
        self.assertFalse(self.pair.handle.cancelled)

    def test_wait_returns_false_while_still_playing(self):
        self.pair = HandlePair()
        self.assertFalse(self.pair.handle.wait(0.05))

    def test_cancelled_flag_survives_the_done_frame(self):
        self.pair = HandlePair()
        self.pair.peer_send({"ev": "done", "id": 7, "cancelled": True})
        self.assertTrue(self.pair.handle.wait(2))
        self.assertTrue(self.pair.handle.cancelled)

    def test_stop_sends_stop_and_completes(self):
        self.pair = HandlePair()

        def answer():
            self.pair.peer_recv()          # the {"cmd": "stop"} frame
            self.pair.peer_send({"ev": "done", "id": 7, "cancelled": True})

        threading.Thread(target=answer, daemon=True).start()
        self.pair.handle.stop()
        self.assertTrue(self.pair.handle.done)

    def test_stop_is_bounded_and_kills_a_wedged_sidecar(self):
        """The property SayHandle got from SIGKILL for free.

        A socket stop that waits on an unresponsive peer would hang on the WS
        thread in step 7. It must escalate instead: close the socket, complete
        the handle, and take the process down.
        """
        self.pair = HandlePair()
        with patch.object(playback, "_SIDECAR_STOP_GRACE", 0.1):
            started = time.monotonic()
            self.pair.handle.stop()          # peer never answers
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertTrue(self.pair.handle.done)
        self.assertTrue(self.pair.wedged.wait(1),
                        "a wedged sidecar must be taken down, not tolerated")

    def test_done_alone_settles_stop_without_burning_the_grace(self):
        """Regression: stop() must not block its full grace when done landed.

        Found end to end, not by these tests. stop() waited on the `stopped`
        ack alone, so a sidecar that answered with `done` first left stop()
        blocking the entire grace -- measured 502 ms against a 150 ms bar.
        """
        self.pair = HandlePair()
        self.pair.peer_send({"ev": "done", "id": 7, "cancelled": True})
        self.assertTrue(self.pair.handle.wait(2))
        with patch.object(playback, "_SIDECAR_STOP_GRACE", 5.0):
            started = time.monotonic()
            self.pair.handle.stop()
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.pair.wedged.is_set())

    def test_stopped_ack_alone_is_enough_and_spares_the_sidecar(self):
        """Regression: a stop during synthesis must not kill a healthy sidecar.

        `done` cannot arrive until synthesis finishes (~600 ms), so waiting for
        it blew the grace every time and escalated into shutting down a process
        that was working fine. The `stopped` ack means audio has ceased, which
        is what stop() actually promises.
        """
        self.pair = HandlePair()

        def answer():
            self.pair.peer_recv()                       # the stop frame
            self.pair.peer_send({"ev": "stopped", "id": 7})
            # `done` deliberately never sent: synthesis is still running.

        threading.Thread(target=answer, daemon=True).start()
        with patch.object(playback, "_SIDECAR_STOP_GRACE", 2.0):
            started = time.monotonic()
            self.pair.handle.stop()
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.pair.wedged.is_set(),
                         "a sidecar that acknowledged the stop is healthy")

    def test_stop_is_idempotent(self):
        self.pair = HandlePair()
        self.pair.peer_send({"ev": "done", "id": 7})
        self.assertTrue(self.pair.handle.wait(2))
        self.pair.handle.stop()
        self.pair.handle.stop()

    def test_dead_sidecar_completes_rather_than_hanging(self):
        """A lost chunk beats a wedged agent.

        If the sidecar dies mid-utterance the reader hits EOF. The handle must
        report finished so the speech worker moves on; hanging the agent as
        well would be strictly worse.
        """
        self.pair = HandlePair()
        self.pair.close_peer()
        self.assertTrue(self.pair.handle.wait(2))


class PlayerSelectionTests(unittest.TestCase):
    def test_play_raises_when_sidecar_not_ready(self):
        player = KokoroPlayer("/nonexistent.sock", "/nonexistent/python",
                              SIDECAR_SCRIPT)
        self.assertFalse(player.ready)
        with self.assertRaises(SidecarUnavailable):
            player.play("hello")

    def test_fallback_uses_say_when_sidecar_unavailable(self):
        class Primary:
            name = "kokoro"
            ready = False

            def play(self, text):
                raise SidecarUnavailable("not ready")

        class Fallback:
            name = "say"

            def __init__(self):
                self.spoken = []

            def play(self, text):
                self.spoken.append(text)
                return "say-handle"

        fallback = Fallback()
        player = FallbackPlayer(Primary(), fallback)
        self.assertEqual(player.play("hello"), "say-handle")
        self.assertEqual(fallback.spoken, ["hello"])
        self.assertEqual(player.name, "say")

    def test_fallback_prefers_the_sidecar_when_it_works(self):
        class Primary:
            name = "kokoro"
            ready = True

            def play(self, text):
                return "kokoro-handle"

        class Fallback:
            name = "say"

            def play(self, text):
                raise AssertionError("must not fall back when sidecar works")

        player = FallbackPlayer(Primary(), Fallback())
        self.assertEqual(player.play("hello"), "kokoro-handle")
        self.assertEqual(player.name, "kokoro")

    def test_busy_falls_back_rather_than_dropping_the_chunk(self):
        """MUST 7: the sidecar rejects concurrent speak; the chunk still speaks.

        A queue inside the sidecar would be a second place speech is pending,
        which breaks the one-producer reasoning wait_idle depends on. Rejecting
        is right — but the rejected chunk must not be silently lost.
        """
        spoken = []

        class BusyPrimary:
            name = "kokoro"
            ready = True

            def play(self, text):
                raise SidecarUnavailable("sidecar refused: 'busy'")

        class Fallback:
            name = "say"

            def play(self, text):
                spoken.append(text)
                return "say-handle"

        player = FallbackPlayer(BusyPrimary(), Fallback())
        player.play("second utterance")
        self.assertEqual(spoken, ["second utterance"])


class ChunkTimeoutTests(unittest.TestCase):
    """speech.py must never wait on a chunk forever."""

    def test_worker_gives_up_and_stops_a_chunk_that_never_finishes(self):
        class NeverFinishes:
            done = False

            def __init__(self):
                self.stopped = threading.Event()

            def wait(self, timeout=None):
                return False          # never completes, exactly like a wedge

            def stop(self):
                self.stopped.set()

        handle = NeverFinishes()

        class Player:
            name = "stub"

            def play(self, text):
                return handle

        utterance = speech.Utterance(1)
        item = speech._Chunk(utterance, "hello")
        with patch.object(speech, "_PLAYER", Player()), \
                patch.object(speech, "CHUNK_TIMEOUT", 0.05):
            speech._run_one(item)

        self.assertTrue(handle.stopped.is_set(),
                        "a chunk that never completes must be stopped")
        self.assertTrue(utterance.done)

    def test_timeouts_are_bounded_and_ordered(self):
        self.assertLess(speech.CHUNK_TIMEOUT, speech.DRAIN_TIMEOUT)
        self.assertLess(speech.DRAIN_TIMEOUT, 120.0,
                        "step 4 lowered this from 120 s with evidence")


class OutOfDictionaryTests(unittest.TestCase):
    """The regression guard for the silent-truncation bug.

    Without a working espeak fallback, KPipeline runs en.G2P(unk=''), and every
    out-of-dictionary word — calendar titles, contact names, app names —
    phonemises to '' and vanishes from the audio with no error. This is the
    600-char browser truncation wearing a different hat.

    Synthesis only. No output device is opened, so this makes no sound.
    """

    DRIVER = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("tts_sidecar", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
probe = mod._wire_espeak(sys.argv[2], sys.argv[3])
from kokoro import KPipeline
pipe = KPipeline(lang_code="a")
pipe.load_voice("af_heart")
samples = sum(len(r.audio) for r in pipe(mod.OOD_PROBE, voice="af_heart")
              if r.audio is not None)
print(json.dumps({"probe": probe, "samples": samples, "word": mod.OOD_PROBE}))
"""

    def test_ood_word_produces_audio(self):
        python = _sidecar_python()
        if python is None:
            self.skipTest("sidecar venv absent (set DAIMON_TTS_PYTHON)")
        out = subprocess.run(
            [python, "-c", self.DRIVER, SIDECAR_SCRIPT,
             "/opt/homebrew/lib/libespeak-ng.dylib",
             "/opt/homebrew/share/espeak-ng-data"],
            capture_output=True, text=True, timeout=600)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        result = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(
            result["probe"],
            f"{result['word']} phonemised to nothing — it would be dropped "
            "from speech silently")
        self.assertGreater(
            result["samples"], 0,
            f"{result['word']} produced no audio; out-of-dictionary words are "
            "being silently dropped")

    def test_startup_refuses_when_espeak_is_broken(self):
        """The sidecar must refuse to start rather than degrade.

        espeak's C code can exit() instead of raising, so 'the constructor did
        not throw' proves nothing. The check is a positive assertion, and a
        sidecar that starts successfully and drops words is worse than no
        sidecar at all.
        """
        python = _sidecar_python()
        if python is None:
            self.skipTest("sidecar venv absent (set DAIMON_TTS_PYTHON)")
        out = subprocess.run(
            [python, "-c", self.DRIVER, SIDECAR_SCRIPT,
             "/nonexistent/libespeak-ng.dylib", "/nonexistent/espeak-ng-data"],
            capture_output=True, text=True, timeout=600)
        self.assertNotEqual(out.returncode, 0,
                            "a broken espeak must stop the sidecar starting")


if __name__ == "__main__":
    unittest.main()
