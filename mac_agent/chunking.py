"""
chunking.py — split one utterance into independently speakable chunks.

Pure function, no I/O. See docs/superpowers/specs/2026-08-02-*.md §4.3.
"""

import re

# A sentence ends at . ? ! followed by whitespace. The lookbehind keeps the
# terminator attached to the chunk it belongs to.
# A sentence ends at . ? ! optionally followed by a closing quote or bracket.
# Python has no variable-length lookbehind, so this is an alternation of two
# fixed-width ones with the longer first, letting `He said "stop." Then…` split.
_SENTENCE_END = re.compile(r"(?<=[.?!][\"')\]])\s+|(?<=[.?!])\s+")

# A list marker at the start of a line: "1." or "a.". Anchored to a newline so
# a sentence ending in a number ("It happened in 1999. Then we left.") is not
# mistaken for one.
_LIST_MARKER = re.compile(r"(?:(?<=\n)|^)((?:\d+|[a-z])\.)$")

# Clause boundaries, used only to break a sentence that is too long to speak
# as one chunk. §4.3 puts that threshold at 120 characters.
_CLAUSE_END = re.compile(r"(?<=[,;:—–])\s+")
MAX_CHARS = 120

# Words whose trailing period is part of the word, not a sentence terminator.
# Python's re has no variable-length lookbehind, so these are handled by
# re-joining after the split rather than by guarding the pattern.
_ABBREVIATIONS = frozenset({
    "dr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "jr.", "st.", "mt.",
    "a.m.", "p.m.", "e.g.", "i.e.", "vs.", "etc.", "approx.",
})
# Deliberately absent: "no.". As numero it is vanishingly rare in speech; as
# the word it ends sentences constantly ("No. Let me check that again."). The
# 100-utterance corpus contained neither, so this is failure-mode hedging.
#
# The corpus is the source of truth for this list, not prose convention. A new
# entry needs corpus evidence or an explicit asymmetry argument like the above.


def _ends_with_abbreviation(chunk):
    words = chunk.split()
    return bool(words) and words[-1].lower() in _ABBREVIATIONS


def _merge_abbreviations(parts):
    """Re-join parts that were split at an abbreviation's period."""
    merged = []
    for part in parts:
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def _rebalance_list_markers(parts):
    """Move a trailing list marker onto the chunk it labels.

    Splitting "…events:\\n1. 9:00 AM" at the marker's period leaves the marker
    stranded on the previous chunk, which is spoken as "…your remaining
    events, one." — it sounds like a finished sentence, so the listener gets no
    cue that it was mangled. Carry the marker forward instead of merging, since
    the text before it is a legitimate chunk of its own.
    """
    out, carry = [], ""
    for part in parts:
        part = carry + part
        carry = ""
        match = _LIST_MARKER.search(part)
        if match:
            head = part[:match.start()].rstrip()
            carry = match.group(1) + " "
            if head:
                out.append(head)
            continue
        out.append(part)
    if carry.strip():
        out.append(carry.strip())
    return out


def _pack(clauses, max_chars):
    """Greedily group clauses into chunks no longer than max_chars."""
    packed = []
    for clause in clauses:
        if packed and len(packed[-1]) + 1 + len(clause) <= max_chars:
            packed[-1] = f"{packed[-1]} {clause}"
        else:
            packed.append(clause)
    return packed


def _split_long(chunk, max_chars):
    """Break an over-long sentence at clause boundaries.

    If there is no boundary to cut at, the chunk is returned whole. Speaking a
    long chunk is a quality problem; dropping half of it is a correctness one.
    """
    if len(chunk) <= max_chars:
        return [chunk]
    clauses = [c.strip() for c in _CLAUSE_END.split(chunk) if c.strip()]
    if len(clauses) <= 1:
        return [chunk]
    return _pack(clauses, max_chars)


def split_utterance(text, max_chars=MAX_CHARS):
    """Split *text* into independently speakable chunks."""
    if not text or not text.strip():
        return []
    parts = [c.strip() for c in _SENTENCE_END.split(text.strip()) if c.strip()]
    chunks = []
    for part in _rebalance_list_markers(_merge_abbreviations(parts)):
        chunks.extend(_split_long(part, max_chars))
    return chunks
