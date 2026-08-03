"""
Tests for the utterance splitter.

The splitter's job is to cut one utterance into independently speakable chunks
so playback has a clean place to stop mid-utterance. It is a pure function —
no I/O, no engine, no queue — so it can be tested in isolation before it is
wired to anything.
"""

import re
import unittest

from mac_agent.chunking import split_utterance


class SentenceSplittingTests(unittest.TestCase):
    def test_splits_on_sentence_terminators(self):
        self.assertEqual(
            split_utterance("Opening Spotify. Playing Tame Impala now."),
            ["Opening Spotify.", "Playing Tame Impala now."],
        )

    def test_single_sentence_without_terminator_is_one_chunk(self):
        self.assertEqual(
            split_utterance("Opening Spotify"),
            ["Opening Spotify"],
        )

    def test_question_and_exclamation_terminate_sentences(self):
        self.assertEqual(
            split_utterance("Did that work? Good morning! Let's go."),
            ["Did that work?", "Good morning!", "Let's go."],
        )

    def test_empty_input_produces_no_chunks(self):
        self.assertEqual(split_utterance(""), [])
        self.assertEqual(split_utterance("   \n  "), [])


class FalsePositiveTests(unittest.TestCase):
    """A period is not always a sentence end.

    Every string here is shaped like something Daimon actually says today —
    _calendar_speech emits clock times, get_weather emits decimals, contact
    prompts emit titles, _open_app emits domain names.
    """

    def test_titles_do_not_end_a_sentence(self):
        self.assertEqual(
            split_utterance("Dr. Smith replied to your message."),
            ["Dr. Smith replied to your message."],
        )

    def test_lowercase_dotted_abbreviations_do_not_end_a_sentence(self):
        self.assertEqual(
            split_utterance("Your meeting is at 9 a.m. tomorrow."),
            ["Your meeting is at 9 a.m. tomorrow."],
        )

    def test_decimals_do_not_end_a_sentence(self):
        self.assertEqual(
            split_utterance("It's 21.5 degrees and clear."),
            ["It's 21.5 degrees and clear."],
        )

    def test_email_addresses_survive_intact(self):
        self.assertEqual(
            split_utterance("I'll email kevin@uni.edu about the deadline."),
            ["I'll email kevin@uni.edu about the deadline."],
        )

    def test_domain_names_survive_intact(self):
        self.assertEqual(
            split_utterance("Opened github.com in your browser."),
            ["Opened github.com in your browser."],
        )

    def test_clock_times_split_only_at_the_real_terminator(self):
        self.assertEqual(
            split_utterance("It's 3:30 PM. You still have poker at 7:00 PM."),
            ["It's 3:30 PM.", "You still have poker at 7:00 PM."],
        )

    def test_ellipsis_does_not_produce_empty_chunks(self):
        self.assertEqual(
            split_utterance("Hold on... checking your calendar now."),
            ["Hold on...", "checking your calendar now."],
        )


class ListMarkerTests(unittest.TestCase):
    """Amendment 5 — observed, not predicted.

    Found by running the splitter over 100 real utterances from
    ~/.daimon/memory/attempts.jsonl. One of the 100 was a numbered calendar
    list, and every chunk trailed the *next* chunk's marker: spoken aloud it
    became "...your remaining events, one." — which sounds like a completed
    sentence, so the listener gets no cue that it was mangled.
    """

    CORPUS_UTTERANCE = (
        "I have no upcoming events, but here are your remaining events:\n"
        "1. 9:00 AM - 10:00 AM\n"
        "2. 2:00 PM - 3:00 PM"
    )

    def test_numeric_list_markers_do_not_end_a_sentence(self):
        self.assertEqual(
            split_utterance(self.CORPUS_UTTERANCE),
            [
                "I have no upcoming events, but here are your remaining events:",
                "1. 9:00 AM - 10:00 AM",
                "2. 2:00 PM - 3:00 PM",
            ],
        )

    def test_a_sentence_ending_in_a_number_is_not_a_list_marker(self):
        # The marker pattern is anchored to a newline for this reason: without
        # the anchor, "1999." reads as a list marker and gets carried onto the
        # following chunk.
        self.assertEqual(
            split_utterance("It happened in 1999. Then we left."),
            ["It happened in 1999.", "Then we left."],
        )

    def test_lettered_list_markers_do_not_end_a_sentence(self):
        # Never seen in the corpus; covered because it fails by the same
        # mechanism if the model ever emits one.
        self.assertEqual(
            split_utterance("Here you go:\na. first item\nb. second item"),
            ["Here you go:", "a. first item", "b. second item"],
        )


class SpeculativeHardeningTests(unittest.TestCase):
    """Fixes kept for their failure mode, NOT because the corpus showed them.

    Neither case appears in the 100-utterance corpus. Do not read the presence
    of these tests as evidence of a live bug.
    """

    def test_no_as_a_word_ends_a_sentence(self):
        # "no." was absent from the corpus, so this is failure-mode hedging.
        # The asymmetry justifies it: "no." as numero costs a missed merge on
        # text that basically never occurs, while "No." as the word costs a
        # broken split on text a voice assistant produces constantly.
        self.assertEqual(
            split_utterance("No. Let me check that again."),
            ["No.", "Let me check that again."],
        )

    def test_terminator_before_a_closing_quote_still_splits(self):
        # 0 of 100 corpus utterances contained a terminator followed by a
        # closing quote or bracket.
        self.assertEqual(
            split_utterance('He said "stop." Then he left.'),
            ['He said "stop."', "Then he left."],
        )

    def test_other_closing_delimiters_also_split(self):
        self.assertEqual(
            split_utterance("She asked 'why?' He shrugged."),
            ["She asked 'why?'", "He shrugged."],
        )
        self.assertEqual(
            split_utterance("It worked (finally.) Moving on."),
            ["It worked (finally.)", "Moving on."],
        )
        self.assertEqual(
            split_utterance('He shouted "go!" We went.'),
            ['He shouted "go!"', "We went."],
        )


class LongClauseTests(unittest.TestCase):
    def test_short_sentence_is_not_split_at_commas(self):
        self.assertEqual(
            split_utterance("Opening Spotify, then playing your song."),
            ["Opening Spotify, then playing your song."],
        )

    def test_long_sentence_splits_at_clause_boundaries(self):
        self.assertEqual(
            split_utterance("alpha beta, gamma delta, epsilon zeta", max_chars=20),
            ["alpha beta,", "gamma delta,", "epsilon zeta"],
        )

    def test_clause_split_respects_the_character_budget(self):
        long_line = (
            "Still on your calendar: poker at the club from 7 to 9 PM, "
            "the BET 100 review at the library from 9:30 to 11, "
            "and laundry which is marked all day."
        )
        chunks = split_utterance(long_line)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 120)

    def test_unsplittable_long_text_is_kept_whole_not_truncated(self):
        # NOTE: synthetic coverage only. 0 of the 100 real corpus utterances
        # exceeded 120 chars without a clause boundary, so the branch guarding
        # against the exact class of bug we're replacing has never seen
        # production input. If it is still untouched after a few weeks of use,
        # that is a signal the 120-char budget is too loose for the actual
        # utterance distribution — worth revisiting then, not now.
        run_on = "supercalifragilistic" * 10  # 200 chars, no boundary to cut at
        self.assertEqual(split_utterance(run_on, max_chars=20), [run_on])


class PreservationTests(unittest.TestCase):
    """Nothing the model said may be silently dropped.

    speakDaimon currently truncates at 600 chars (index.html:923). Whatever
    replaces it must not, so the splitter is held to the stronger invariant:
    the chunks contain exactly the input's non-whitespace content, in order.
    """

    CASES = [
        "Opening Spotify. Playing Tame Impala now.",
        "Dr. Smith replied. Your meeting is at 9 a.m. tomorrow.",
        "It's 3:30 PM. Still on your calendar: poker at the club from 7:00 PM "
        "to 9:00 PM, the BET 100 review at the library, and laundry all day.",
        "Hold on... checking your calendar now.",
        "supercalifragilistic" * 10,
    ]

    def test_chunks_preserve_every_non_whitespace_character(self):
        dense = lambda s: re.sub(r"\s+", "", s)
        for src in self.CASES:
            with self.subTest(src=src[:40]):
                self.assertEqual(
                    dense("".join(split_utterance(src))),
                    dense(src),
                )

    def test_no_chunk_is_empty_or_whitespace_only(self):
        for src in self.CASES:
            with self.subTest(src=src[:40]):
                for chunk in split_utterance(src):
                    self.assertTrue(chunk.strip())


if __name__ == "__main__":
    unittest.main()
