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
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


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


if __name__ == "__main__":
    unittest.main()
