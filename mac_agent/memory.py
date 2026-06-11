"""
memory.py — simple personal fact store for resolving references like "my prof".
"""

import json
import os
import re

MEMORY = os.environ.get("MACAGENT_MEMORY", "memory.json")
_FIND_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\w*")
_IS_EMAIL = re.compile(r"^[\w.+-]+@[\w.-]+\w*$")

# "kevin@edu is my prof's email" / "my prof's email is kevin@edu"
_LABEL_BEFORE = re.compile(
    r"([\w.+-]+@[\w.-]+\w*)\s+is\s+my\s+(\w+)(?:'s)?\s+email",
    re.I,
)
_LABEL_AFTER = re.compile(
    r"my\s+(\w+)(?:'s)?\s+email\s+is\s+([\w.+-]+@[\w.-]+\w*)",
    re.I,
)
_POSSESSIVE_EMAIL = re.compile(
    r"(\w+)(?:'s)?\s+email\s+is\s+([\w.+-]+@[\w.-]+\w*)",
    re.I,
)
_MY_EMAIL = re.compile(
    r"my\s+email\s+is\s+([\w.+-]+@[\w.-]+\w*)",
    re.I,
)
_VAGUE_KEYS = frozenset({"his", "her", "their", "its", "he", "she", "him"})
_DONE_VALUES = frozenset({"finished", "done", "complete", "completed"})
_IGNORE_VALUES = frozenset({"ignore", "ignored", "skip", "skipped"})
_REMEMBER_NAME = re.compile(
    r"remember\s+(?:my\s+)?(\w+)\s+is\s+(.+)",
    re.I,
)


def is_email(s):
    return bool(s and _IS_EMAIL.match(s.strip()))


def find_emails(text):
    return _FIND_EMAIL.findall(text)


def _load():
    try:
        with open(MEMORY) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data):
    with open(MEMORY, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _extract_email(value):
    if not value:
        return None
    bracketed = re.search(r"<([^>]+)>", value)
    if bracketed and is_email(bracketed.group(1)):
        return bracketed.group(1)
    for candidate in _FIND_EMAIL.findall(value):
        if is_email(candidate):
            return candidate
    return None


def _normalize_value(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    email = _extract_email(value)
    if email:
        return email
    return value.strip()


def store(key, value):
    key = key.strip().lower()
    value = _normalize_value(value)
    data = _load()
    data[key] = value
    _save(data)
    return key, value


def parse_labeled_emails(text):
    """Return list of (key, email) for explicitly labeled addresses in text."""
    pairs = []
    seen = set()
    for pattern in (_LABEL_BEFORE, _LABEL_AFTER, _POSSESSIVE_EMAIL):
        for match in pattern.finditer(text):
            if pattern is _LABEL_BEFORE:
                email, key = match.group(1), match.group(2)
            else:
                key, email = match.group(1), match.group(2)
            key = key.lower()
            if key in ("my",) or key in _VAGUE_KEYS:
                continue
            pair = (key, email)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    my_match = _MY_EMAIL.search(text)
    if my_match:
        pair = ("me", my_match.group(1))
        if pair not in seen:
            pairs.append(pair)
    return pairs


def parse_remember(text):
    """
    Deterministic remember parsing.
    Returns None, "ambiguous", (key, value), or list[(key, value)].
    """
    if not re.search(r"\bremember\b", text, re.I) and not find_emails(text):
        return None

    labeled = parse_labeled_emails(text)
    emails = find_emails(text)

    if len(emails) > 1:
        if len(labeled) < len(emails):
            return "ambiguous"
        return labeled

    if len(emails) == 1:
        if labeled:
            return labeled[0]
        email = emails[0]
        for pattern in (_LABEL_BEFORE, _LABEL_AFTER, _POSSESSIVE_EMAIL):
            match = pattern.search(text)
            if match:
                if pattern is _LABEL_BEFORE:
                    return match.group(2).lower(), email
                return match.group(1).lower(), email

    name_match = _REMEMBER_NAME.search(text)
    if name_match:
        return name_match.group(1).lower(), name_match.group(2).strip()

    if re.search(r"\bremember\b", text, re.I):
        return None
    return None


def validate_remember(key, value, user_text):
    """
    Validate a model-proposed remember before storing.
    Returns "ambiguous", "invalid", or (key, value).
    """
    if not key or not isinstance(key, str) or not key.strip():
        return "invalid"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "invalid"
    emails_in_text = find_emails(user_text)
    if len(emails_in_text) > 1:
        labeled = parse_labeled_emails(user_text)
        if len(labeled) < len(emails_in_text):
            return "ambiguous"
        match = next((p for p in labeled if p[0] == key.lower()), None)
        if match:
            return match
        return "ambiguous"
    if len(emails_in_text) == 1:
        labeled = parse_labeled_emails(user_text)
        if labeled:
            match = next((p for p in labeled if p[0] == key.lower()), None)
            if match:
                return match
        if is_email(_normalize_value(value)):
            return key.lower(), _normalize_value(value)
    return key.lower(), _normalize_value(value)


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


def key_matches_text(key, text):
    """True when every token in key appears in text (e.g. bet 100 → BET 100 Review)."""
    key_tokens = _tokens(key)
    if not key_tokens:
        return False
    return key_tokens <= _tokens(text)


def _value_kind(value):
    v = str(value).strip().lower()
    if v in _DONE_VALUES:
        return "done"
    if v in _IGNORE_VALUES:
        return "ignore"
    return None


def is_marked_done(text):
    data = _load()
    for key, value in data.items():
        if _value_kind(value) == "done" and key_matches_text(key, text):
            return True
    return False


def is_ignored(text):
    data = _load()
    for key, value in data.items():
        if _value_kind(value) == "ignore" and key_matches_text(key, text):
            return True
    return False


def recall(text):
    data = _load()
    if not data:
        return ""
    words = _tokens(text)
    matches = []
    for key, value in data.items():
        if key.lower() not in words:
            continue
        email = _extract_email(value)
        if email:
            matches.append(f"{key} = {email}")
        else:
            matches.append(f"{key} = {value} (no email on file)")
    return "\n".join(matches)


def recall_for_schedule():
    """All completion/ignore facts — included on calendar queries regardless of wording."""
    data = _load()
    if not data:
        return ""
    lines = []
    for key, value in sorted(data.items()):
        kind = _value_kind(value)
        if kind == "done":
            lines.append(f"{key} = finished")
        elif kind == "ignore":
            lines.append(f"{key} = ignore (exclude from schedule)")
    return "\n".join(lines)


def lookup(key):
    data = _load()
    return data.get(key) or data.get(key.lower())


def resolve_to(to):
    """Resolve alias to a valid email, or None."""
    to = to.strip()
    if is_email(to):
        return to
    stored = lookup(to)
    if stored is None:
        return None
    email = _extract_email(stored)
    if email and is_email(email):
        return email
    if is_email(stored):
        return stored
    return None


def contact_label(key_or_address):
    stored = lookup(key_or_address)
    if stored:
        name = stored.split("<")[0].strip()
        if name and not is_email(name):
            return name
    return key_or_address


def recalled_keys_missing_email(text):
    data = _load()
    if not data:
        return []
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return [
        key for key, value in data.items()
        if key.lower() in words and not resolve_to(key)
    ]
