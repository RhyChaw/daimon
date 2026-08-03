"""
Tests for the playback layer: handles, stopping, and completion.

Silent by construction. `speech._PLAYER` is swapped for a fake for the whole
module, so no test can reach /usr/bin/say even by accident. The SayHandle tests
drive a real subprocess, but it is `sleep`, not `say`.

The abstraction under test is "an out-of-process speaker I hold a handle to and
can stop". Nothing here may assume the speaker is a subprocess — the Kokoro
sidecar in step 4 stops by sending a message over a unix socket.
"""

import io
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mac_agent import agent, events, speech
from mac_agent.playback import SayHandle, SayPlayer


class FakeHandle:
    """A stoppable speaker that is not a process."""

    def __init__(self):
        self._done = threading.Event()
        self.stopped = False

    @property
    def done(self):
        return self._done.is_set()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def stop(self):
        self.stopped = True
        self._done.set()

    def finish(self):
        self._done.set()


class FakePlayer:
    name = "fake"

    def __init__(self):
        self.spoken = []
        self.handles = []
        self.gate = None          # set to hold chunks mid-play

    def play(self, text):
        self.spoken.append(text)
        handle = FakeHandle()
        if self.gate is None:
            handle.finish()
        self.handles.append(handle)
        return handle


_patches = []


def setUpModule():
    # No real worker thread ever starts in this suite: _WORKER_STARTED is a
    # module global, so a single test that started one would leave every later
    # test racing a background consumer. Tests drive speech._run_one instead,
    # which is the worker's actual loop body.
    _patches.append(patch.object(speech, "_PLAYER", FakePlayer()))
    _patches.append(patch.object(speech, "_ensure_worker", lambda: None))
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


def _pump():
    """Play everything queued, through the production path. Returns the count."""
    played = 0
    while True:
        try:
            item = speech._QUEUE.get_nowait()
        except queue.Empty:
            return played
        speech._run_one(item)
        played += 1


def _pump_thread(stop_after=None):
    """Consume the queue on a background thread, for the concurrency tests."""
    def loop():
        seen = 0
        while stop_after is None or seen < stop_after:
            try:
                item = speech._QUEUE.get(timeout=0.05)
            except queue.Empty:
                if stop_after is None:
                    return
                continue
            speech._run_one(item)
            seen += 1
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def _reset():
    """Drain via the production accounting path, then wait for quiescence."""
    while True:
        try:
            speech._QUEUE.get_nowait()._release()
        except queue.Empty:
            break
    speech.set_muted(False)
    speech._PLAYER.spoken.clear()
    speech._PLAYER.handles.clear()
    speech._PLAYER.gate = None


class SayHandleTests(unittest.TestCase):
    """The one concrete Player we ship. Driven with `sleep`, never `say`."""

    def _handle(self, seconds="30"):
        fd, path = tempfile.mkstemp(prefix="daimon-test-")
        os.close(fd)
        proc = subprocess.Popen(["/bin/sleep", seconds],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return SayHandle(proc, path), path

    def test_stop_ends_playback_and_unlinks_the_temp_file(self):
        handle, path = self._handle()
        self.assertTrue(os.path.exists(path))
        handle.stop()
        self.assertTrue(handle.done)
        self.assertFalse(os.path.exists(path), "temp file leaked on the stop path")

    def test_natural_completion_unlinks_the_temp_file(self):
        handle, path = self._handle(seconds="0")
        self.assertTrue(handle.wait(timeout=5))
        self.assertFalse(os.path.exists(path), "temp file leaked on the normal path")

    def test_stop_is_idempotent_and_survives_racing_the_reaper(self):
        # stop() may be called from the WS thread while the worker sits in
        # wait(). Both paths unlink; the second must not raise.
        handle, path = self._handle()
        handle.stop()
        handle.stop()
        self.assertTrue(handle.wait(timeout=5))
        self.assertFalse(os.path.exists(path))

    def test_wait_reports_false_while_still_playing(self):
        handle, path = self._handle()
        try:
            self.assertFalse(handle.wait(timeout=0.05))
            self.assertFalse(handle.done)
        finally:
            handle.stop()

    def test_player_exposes_no_process_details(self):
        # If the interface only makes sense for subprocess.run it is wrong.
        # A sidecar handle has no pid and no returncode to expose.
        for banned in ("pid", "proc", "returncode", "poll"):
            self.assertFalse(
                hasattr(SayPlayer, banned),
                f"SayPlayer leaks {banned} into the interface",
            )


class SpeakReturnsAHandleTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_speak_returns_an_utterance(self):
        utt = speech.speak("Opening Spotify. Playing Tame Impala now.")
        self.assertTrue(hasattr(utt, "wait"))
        self.assertTrue(hasattr(utt, "stop"))
        self.assertFalse(utt.done)

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_muted_speak_returns_a_finished_utterance_not_none(self):
        # A None return would force null checks at every call site and hand
        # step 7 a guaranteed null-handle bug.
        speech.set_muted(True)
        try:
            utt = speech.speak("Opening Spotify.")
            self.assertIsNotNone(utt)
            self.assertTrue(utt.done)
            self.assertTrue(utt.wait(timeout=0))
        finally:
            speech.set_muted(False)

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_empty_text_returns_a_finished_utterance(self):
        for text in ("", "   \n  ", None):
            utt = speech.speak(text)
            self.assertIsNotNone(utt)
            self.assertTrue(utt.done, f"{text!r} should yield a finished utterance")


class CompletionTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_wait_idle_returns_true_once_the_queue_drains(self):
        utt = speech.speak("Opening Spotify. Playing Tame Impala now.")
        self.assertFalse(speech.wait_idle(0), "idle before anything was played")
        self.assertEqual(_pump(), 2)
        self.assertTrue(speech.wait_idle(5.0))
        self.assertTrue(utt.done)
        self.assertEqual(speech.pending(), 0)

    def test_utterance_wait_blocks_until_its_chunks_are_played(self):
        speech._PLAYER.gate = threading.Event()
        _pump_thread(stop_after=2)
        try:
            utt = speech.speak("One. Two.")
            self.assertFalse(utt.wait(timeout=0.1))
            deadline = time.monotonic() + 5
            while not utt.done and time.monotonic() < deadline:
                for handle in list(speech._PLAYER.handles):
                    handle.finish()
                time.sleep(0.01)
            self.assertTrue(utt.done)
        finally:
            speech._PLAYER.gate = None
            for handle in list(speech._PLAYER.handles):
                handle.finish()

    def test_wait_idle_reports_false_when_speech_never_drains(self):
        # Nothing is consuming, so the chunk sits in the queue. This is the
        # timeout that handle() must treat as a bug signal, not as normal.
        speech.speak("Opening Spotify.")
        self.assertFalse(speech.wait_idle(0.05))
        self.assertEqual(speech.pending(), 1)

    def test_wait_idle_reports_false_while_a_chunk_hangs_mid_playback(self):
        # The shape of a wedged sidecar in step 4: the chunk left the queue but
        # playback never returns. wait_idle must not call that idle.
        speech._PLAYER.gate = threading.Event()
        _pump_thread(stop_after=1)
        try:
            speech.speak("Opening Spotify.")
            deadline = time.monotonic() + 5
            while not speech._PLAYER.handles and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(speech.wait_idle(0.05))
            self.assertEqual(speech.pending(), 1)
        finally:
            speech._PLAYER.gate = None
            for handle in list(speech._PLAYER.handles):
                handle.finish()

    def test_pending_counts_chunks_not_utterances(self):
        speech.speak("One. Two. Three.")
        self.assertEqual(speech.pending(), 3)


class StopMechanismTests(unittest.TestCase):
    """Step 3 builds the mechanism only. No caller in agent.py invokes it."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_stopping_before_playback_starts_drops_queued_chunks_unspoken(self):
        # A queued-but-not-started chunk must never reach the speaker at all.
        utt = speech.speak("One. Two. Three.")
        utt.stop()
        self.assertEqual(_pump(), 3, "chunks must still be drained from the queue")
        self.assertEqual(speech._PLAYER.spoken, [],
                         "a stopped utterance still handed text to the speaker")
        self.assertTrue(utt.done)
        self.assertEqual(speech.pending(), 0,
                         "dropped chunks must still be accounted for")

    def test_stop_stops_the_chunk_currently_playing(self):
        speech._PLAYER.gate = threading.Event()
        _pump_thread(stop_after=2)
        try:
            utt = speech.speak("One. Two.")
            deadline = time.monotonic() + 5
            while not speech._PLAYER.handles and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(speech._PLAYER.handles, "playback never started")
            utt.stop()
            self.assertTrue(speech._PLAYER.handles[0].stopped)
            self.assertTrue(speech.wait_idle(5.0))
            # The second chunk was queued behind the first: dropped, not spoken.
            self.assertEqual(len(speech._PLAYER.spoken), 1)
        finally:
            speech._PLAYER.gate = None
            for handle in list(speech._PLAYER.handles):
                handle.finish()

    def test_stop_is_idempotent(self):
        utt = speech.speak("One. Two.")
        utt.stop()
        utt.stop()
        _pump()
        self.assertTrue(utt.done)
        self.assertEqual(speech._PLAYER.spoken, [])

    def test_stopping_a_finished_utterance_is_harmless(self):
        utt = speech.speak("Opening Spotify.")
        _pump()
        self.assertTrue(speech.wait_idle(5.0))
        utt.stop()
        self.assertTrue(utt.done)


class AccountingTests(unittest.TestCase):
    """The pending count is what handle()'s finally trusts. Guard it."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_release_is_idempotent(self):
        speech.speak("One. Two.")
        item = speech._QUEUE.get_nowait()
        item._release()
        item._release()
        self.assertEqual(speech.pending(), 1, "double release double-counted")

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_draining_the_queue_by_hand_keeps_the_count_honest(self):
        # Tests drain the queue directly; that must exercise the same
        # accounting as the worker, or pending() silently drifts.
        speech.speak("One. Two. Three.")
        self.assertEqual(speech.pending(), 3)
        while True:
            try:
                speech._QUEUE.get_nowait()._release()
            except Exception:
                break
        self.assertEqual(speech.pending(), 0)
        self.assertTrue(speech.wait_idle(1.0))


class OneProducerTests(unittest.TestCase):
    """wait_idle() is only meaningful if one thread owns the pending set.

    A second producer makes wait_idle return on someone else's speech, which
    reproduces the premature idle this whole layer exists to prevent. The
    failure is silent, so it is enforced rather than documented.
    """

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_a_second_concurrent_producer_is_rejected(self):
        speech.speak("One. Two.")          # main thread owns the pending set
        self.assertGreater(speech.pending(), 0)
        box = {}

        def other():
            try:
                speech.speak("Three.")
            except RuntimeError as exc:
                box["err"] = exc

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        self.assertIn("err", box, "a second producer was allowed to enqueue")
        self.assertIn("single producer", str(box["err"]))

    def test_a_rejected_producer_enqueues_nothing(self):
        speech.speak("One. Two.")
        before = speech.pending()

        def other():
            try:
                speech.speak("Three. Four.")
            except RuntimeError:
                pass

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        self.assertEqual(speech.pending(), before,
                         "the rejected producer still altered the pending count")

    def test_a_quiet_queue_can_be_claimed_by_another_thread(self):
        # Sequential ownership is fine and must stay fine: the REPL thread
        # differs between cli.py and the .app, and the tests use both.
        speech.speak("One.")
        _pump()
        self.assertEqual(speech.pending(), 0)
        box = {}

        def other():
            try:
                speech.speak("Two.")
                box["ok"] = True
            except RuntimeError as exc:
                box["err"] = exc

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        self.assertNotIn("err", box, f"sequential handoff rejected: {box.get('err')}")
        self.assertTrue(box.get("ok"))


class TempFileSweepTests(unittest.TestCase):
    """Neither unlink path runs if the interpreter dies mid-playback."""

    def test_live_temp_files_are_tracked_and_released(self):
        import mac_agent.playback as pb
        fd, path = tempfile.mkstemp(prefix="daimon-test-")
        os.close(fd)
        proc = subprocess.Popen(["/bin/sleep", "30"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        pb._track(path)
        handle = pb.SayHandle(proc, path)
        try:
            self.assertIn(path, pb._LIVE_PATHS)
            handle.stop()
            self.assertNotIn(path, pb._LIVE_PATHS,
                             "a reaped handle stayed on the sweep list")
            self.assertFalse(os.path.exists(path))
        finally:
            handle.stop()

    def test_the_sweep_unlinks_what_the_handles_never_reaped(self):
        # Stands in for the interpreter exiting while the worker is blocked in
        # handle.wait(): the daemon worker is killed, so _cleanup never runs.
        import mac_agent.playback as pb
        fd, path = tempfile.mkstemp(prefix="daimon-test-")
        os.close(fd)
        pb._track(path)
        self.assertTrue(os.path.exists(path))
        pb._sweep_temp_files()
        self.assertFalse(os.path.exists(path), "abandoned temp file survived exit")
        self.assertNotIn(path, pb._LIVE_PATHS)


class HonestIdleTests(unittest.TestCase):
    """handle()'s finally must emit idle when audio ends, not when it starts.

    The old finally emitted idle as soon as the text was enqueued, so the
    server announced idle while audio was still playing.
    """

    def setUp(self):
        _reset()
        self.seen = []
        self._unsub = events.subscribe(self.seen.append)
        # Keep audit.jsonl and the episodic log out of the test run.
        self._patches = [patch.object(agent, "log", lambda **kw: None),
                         patch.object(agent, "_episode", lambda *a, **kw: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._unsub()
        _reset()

    def _states(self):
        return [e["state"] for e in self.seen if e.get("type") == "state"]

    def _handle(self, text="say hello there"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            agent.handle(text, backend=None)
        return buf.getvalue()

    def test_idle_is_not_emitted_while_audio_is_still_pending(self):
        finished = threading.Event()

        def run():
            with redirect_stdout(io.StringIO()):
                agent.handle("say hello there", backend=None)
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 5
        while speech.pending() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(speech.pending(), 0, "nothing was ever enqueued")
        self.assertNotIn("idle", self._states(),
                         "idle emitted while audio was still pending")
        self.assertFalse(finished.wait(0.1),
                         "handle() returned before audio finished")
        _pump()
        self.assertTrue(finished.wait(5), "handle() never returned")
        self.assertEqual(self._states()[-1], "idle")

    def test_muted_turn_emits_idle_without_waiting(self):
        # Browser-driven voice mode mutes the Mac voice, so nothing is queued
        # and the wait is a no-op. This is the property that makes step 3
        # bit-identical under mute.
        speech.set_muted(True)
        try:
            started = time.monotonic()
            self._handle()
            elapsed = time.monotonic() - started
        finally:
            speech.set_muted(False)
        self.assertLess(elapsed, 1.0, "muted turn waited on speech")
        self.assertEqual(speech.pending(), 0)
        self.assertEqual(self._states()[-1], "idle")

    def test_drain_timeout_warns_and_still_emits_idle(self):
        # Nothing consumes the queue, so the wait times out. A timeout is a bug
        # signal: it must be reported, and idle must still be emitted.
        with patch.object(speech, "DRAIN_TIMEOUT", 0.05):
            out = self._handle()
        self.assertIn("WARNING", out)
        self.assertIn("chunk(s) still pending", out)
        self.assertEqual(self._states()[-1], "idle",
                         "a drain timeout must not strand the UI off idle")

    def test_speaking_precedes_idle(self):
        self.assertEqual(speech.pending(), 0)
        finished = threading.Event()

        def run():
            with redirect_stdout(io.StringIO()):
                agent.handle("say hello there", backend=None)
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 5
        while speech.pending() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        _pump()
        self.assertTrue(finished.wait(5))
        states = self._states()
        self.assertIn("speaking", states)
        self.assertLess(states.index("speaking"), states.index("idle"))


if __name__ == "__main__":
    unittest.main()
