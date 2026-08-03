"""
Tests for the splitter's two consumers: the audio queue and the WS payload.

These run silently. speech._ensure_worker is patched out so no worker thread
starts and /usr/bin/say is never invoked — the assertion is the queue's
contents, not anything audible.
"""

import queue
import unittest
from unittest.mock import patch

from mac_agent import agent, events, speech


def _drain(q):
    """Pop every queued chunk, returning the text of each.

    Each item is released through the same accounting path the worker uses, so
    speech.pending() stays honest. A dedicated reset hook would be a second way
    to reach the same state, and would hide exactly the class of drift the
    accounting exists to catch.
    """
    out = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return out
        out.append(item.text)
        item._release()


class SpeechQueueTests(unittest.TestCase):
    def setUp(self):
        _drain(speech._QUEUE)
        speech.set_muted(False)

    def tearDown(self):
        _drain(speech._QUEUE)

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_speak_enqueues_one_item_per_chunk(self):
        speech.speak("Opening Spotify. Playing Tame Impala now.")
        self.assertEqual(
            _drain(speech._QUEUE),
            ["Opening Spotify.", "Playing Tame Impala now."],
        )

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_a_single_sentence_stays_a_single_queue_item(self):
        speech.speak("Opened Spotify.")
        self.assertEqual(_drain(speech._QUEUE), ["Opened Spotify."])

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_muted_speech_enqueues_nothing(self):
        speech.set_muted(True)
        try:
            speech.speak("Opening Spotify. Playing Tame Impala now.")
            self.assertEqual(_drain(speech._QUEUE), [])
        finally:
            speech.set_muted(False)


class SayPayloadTests(unittest.TestCase):
    """The WS say event carries transcript and audio in one payload.

    The browser must not re-implement the splitter, and must not truncate:
    speakDaimon previously cut every utterance at 600 chars (index.html).
    """

    def _capture(self, text, origin):
        seen = []
        unsub = events.subscribe(seen.append)
        try:
            agent._emit_say(text, origin)
        finally:
            unsub()
        return seen

    def test_say_event_carries_full_text_and_chunks_and_origin(self):
        seen = self._capture("Opening Spotify. Playing Tame Impala now.", "prose")
        self.assertEqual(seen, [{
            "type": "say",
            "text": "Opening Spotify. Playing Tame Impala now.",
            "chunks": ["Opening Spotify.", "Playing Tame Impala now."],
            "origin": "prose",
        }])

    def test_transcript_text_is_never_truncated(self):
        long_text = "This sentence is deliberately long. " * 30  # >600 chars
        seen = self._capture(long_text, "prose")
        # Verbatim, not normalised: the transcript shows exactly what was said.
        self.assertEqual(seen[0]["text"], long_text)
        self.assertGreater(len(seen[0]["text"]), 600)

    def test_system_origin_is_carried_through(self):
        seen = self._capture("Opened Spotify.", "system")
        self.assertEqual(seen[0]["origin"], "system")


class ToolResultAnnounceTests(unittest.TestCase):
    """ToolResult.say is pre-rendered system text; announce decides if it is spoken.

    Two fields because the pre-rendered text has two futures. Speaking it is
    step 2 plumbing (fork a). Answering *from* it without a model round-trip is
    fork b, which is out of scope — see docs/superpowers/specs/step-2-brief.md.
    Defaulting announce to False keeps read_calendar silent exactly as before,
    so nothing starts speaking by accident.
    """

    def test_say_defaults_to_not_announcing(self):
        from mac_agent.senses import ToolResult
        self.assertFalse(ToolResult("data", say="text").announce)

    def test_read_calendar_keeps_its_say_unspoken(self):
        # The pre-rendered calendar speech is the fork-b hook. It must stay
        # built and unspoken, or the answer is voiced twice: once here and
        # again when the model produces its final say from the same data.
        from mac_agent import senses
        result = senses.ToolResult("Now: 15:00\n\n(nothing remaining today)",
                                   say="It's 3:00 PM. Nothing left today.")
        self.assertTrue(result.say)
        self.assertFalse(result.announce)


class OpenAppTests(unittest.TestCase):
    """_open_app must not speak for itself — that path had no WS event and no
    audit row, which is a hole in the premise that every action is logged."""

    def test_open_app_returns_announceable_text_instead_of_speaking(self):
        from mac_agent import actions
        with patch("mac_agent.actions.subprocess.run") as run, \
             patch("mac_agent.actions._speak_async") as spoken:
            result = actions._open_app(app_name="Spotify")
        run.assert_called_once()
        spoken.assert_not_called()
        self.assertEqual(result.say, "Opened Spotify.")
        self.assertTrue(result.announce)


class FastPathOriginTests(unittest.TestCase):
    """Fast-path utterances reach both surfaces, tagged system."""

    def tearDown(self):
        # _say enqueues; without draining, speech.pending() leaks into any
        # later test that waits on it.
        _drain(speech._QUEUE)

    @patch("mac_agent.speech._ensure_worker", lambda: None)
    def test_fast_path_say_emits_a_system_tagged_event(self):
        seen = []
        unsub = events.subscribe(seen.append)
        try:
            agent._say("Okay — I'll ignore laundry on your schedule.")
        finally:
            unsub()
        says = [e for e in seen if e.get("type") == "say"]
        self.assertEqual(len(says), 1)
        self.assertEqual(says[0]["origin"], "system")
        self.assertEqual(says[0]["text"],
                         "Okay — I'll ignore laundry on your schedule.")


if __name__ == "__main__":
    unittest.main()
