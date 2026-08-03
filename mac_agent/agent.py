"""
agent.py — the core loop.

  you type  ->  model proposes an action  ->  whitelist  ->  gate  ->  execute
            ->  append result to step history  ->  next action  ->  …  ->  say

The model never touches your system directly. It can only *propose* verbs from
the whitelist; this code is what actually runs anything. say ends the turn.
"""

import json
import re

from . import actions as actions_mod
from .actions import ACTION_SCHEMA
from .backend import BackendError, create_backend, parse_action
from .gate import allow
from .audit import log
from . import events
from . import memory
from . import episodic
from . import palace_memory
from . import clock
from .chunking import split_utterance
from .senses import NeedsUserInput, ToolResult

MAX_AGENT_STEPS = 5

# Set to True within a turn when play_music succeeds; cleared at turn start.
# Prevents the TTS "say" after music from pausing Spotify.
_music_played_this_turn: bool = False

_PROMPT_PREFIX = re.compile(r"^(?:you\s*>\s*)+", re.I)
_SMART_APOS = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u2032": "'",
})


def _clean_input(user_text):
    """Strip accidental pasted 'you >' prompts."""
    return _PROMPT_PREFIX.sub("", user_text).strip()


def _normalize_text(value):
    if not isinstance(value, str):
        return value
    return value.translate(_SMART_APOS)


def _normalize_args(args):
    if not isinstance(args, dict):
        return {}
    return {key: _normalize_text(val) for key, val in args.items()}


def _system_prompt():
    lines = [
        "You control a Mac through a FIXED set of actions.",
        'Reply with ONLY valid JSON. Example: {"action":"say","args":{"text":"I don\'t know"}}',
        "Allowed actions:",
    ]
    for name, spec in ACTION_SCHEMA.items():
        lines.append(f'  - {name}(args: {", ".join(spec["args"])}) — {spec["desc"]}')
    lines.append('When the user says "say <text>", use say with that exact text (keep apostrophes).')
    lines.append('For greetings, chat, math, facts, trivia, or any general question: use say to answer directly and helpfully. Example: "what is 1+1" → {"action":"say","args":{"text":"2"}}')
    lines.append('If the request is truly ambiguous and fits no action and has no clear answer, use say to ask one clarifying question.')
    lines.append('When the user says "remember …", use remember to store the fact (key + value).')
    lines.append(
        "If Known facts show a contact with no email on file, use say to ask for their email — "
        "never draft_email or send_email until an address is known. "
        "NEVER ask for email for Spotify, music, or play_music requests."
    )
    lines.append(
        "For email tasks ('send email to X', 'email X saying Y', 'draft email to X'): "
        "use draft_email(to, subject, body) to open a visible reviewable draft, "
        "or send_email(to, subject, body) to send immediately. "
        "These actions compose the message through Mail automatically — NEVER use open_app for email. "
        "Infer a clear subject and body from the user's description. "
        "For 'send an email saying X': subject = concise one-liner about X, body = polite full sentence."
    )
    lines.append("If the message includes a Recently or Past similar line and the user asks what you did before, use say to answer from those events.")
    lines.append(
        "For schedule or calendar questions, always use read_calendar — never invent events or answer from Recently lines. "
        "read_calendar returns only today's remaining events (after the current time)."
    )
    lines.append("For weather questions, always use get_weather — never invent temperatures or answer from Recently lines.")
    lines.append(
        "You may chain several actions before responding. Tool actions (get_weather, read_calendar) return "
        "data shown in step history — use only that data in your final answer."
    )
    lines.append(
        'Use say only when you are ready to respond to the user and STOP. say is always the last action in a turn.'
    )
    lines.append(
        "For compound requests (e.g. weather AND calendar), call each needed tool, then one combined say."
    )
    lines.append(
        "For Spotify or play requests: use play_music with the song query (no email needed). "
        "Then say to confirm. Do not use open_app for Spotify when play_music handles it."
    )
    lines.append(
        "To open a project in Claude Code: use open_in_claude_code(project=<alias>, prompt=<optional message>). "
        "This starts a live embedded Claude Code session in the daimon UI terminal panel. "
        "Examples: 'open foundry in claude code' → open_in_claude_code(project='foundry'). "
        "'open foundry and ask it to fix the login bug' → open_in_claude_code(project='foundry', prompt='fix the login bug'). "
        "The prompt is sent as the first message to claude — do NOT use say for it. "
        "Add mode='terminal' only if the user explicitly asks for a separate Terminal window. "
        "Relay the result with say."
    )
    lines.append("Output nothing except the JSON object.")
    return "\n".join(lines)


def _email_intent(text):
    return bool(re.search(r"\b(email|draft|send|mail)\b", text, re.I))


_WEATHER_IN = re.compile(
    r"\b(?:weather|temperature|forecast|temp)\s+(?:in|for|at)\s+([^?.]+)",
    re.I,
)
_WEATHER_LOC = re.compile(
    r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z\s,'-]+?)(?:\?|$|today|tonight|right now)",
    re.I,
)


def _parse_weather_location(text):
    for pattern in (_WEATHER_IN, _WEATHER_LOC):
        m = pattern.search(text)
        if not m:
            continue
        loc = m.group(1).strip().rstrip(".,!?")
        loc = re.sub(r"\b(today|tonight|right now|like|please)\b", "", loc, flags=re.I).strip()
        if loc and loc.lower() not in {"my location", "here", "local"}:
            return loc
    return None


def _weather_args(user_text):
    loc = _parse_weather_location(user_text)
    return {"location": loc} if loc else {}


def _sense_action(text):
    if re.search(r"\b(weather|temperature|temp\b|rain|snow|forecast|humid|wind)\b", text, re.I):
        return "get_weather"
    if re.search(
        r"\b(calendar|schedule|my day|today|meetings|agenda|poker|club|event|"
        r"do i have|am i|got)\b",
        text,
        re.I,
    ):
        return "read_calendar"
    return None


def _emit_say(text, origin):
    """Emit the WS say event: whole utterance for the transcript, chunks for audio.

    One payload rather than two paths, so the browser never re-implements the
    splitter and never has to truncate. `origin` is what the step-6 TTS router
    keys on alongside length — system utterances are Daimon's own fixed or
    derived phrases, prose is model-generated answer text.
    """
    events.emit({
        "type": "say",
        "text": text,
        "chunks": split_utterance(text),
        "origin": origin,
    })


def _say(text):
    """Speak a deterministic, Daimon-authored utterance.

    Denials, gate prompts, step-cap messages, contact questions. These used to
    speak on the Mac without ever reaching the event bus, so in the browser
    they simply did not exist.
    """
    events.emit({"type": "state", "state": "speaking"})
    _emit_say(text, "system")
    if not _music_played_this_turn:
        ACTION_SCHEMA["say"]["handler"](text=text)
    log(action="say", args={"text": text}, decision="allowed", auth_method="auto", ok=True)
    print(f"  -> {text}")


def _music_say(text):
    """Show confirmation in console/UI without TTS — avoids pausing Spotify."""
    print(f"  -> {text}")
    events.emit({"type": "state", "state": "speaking"})
    _emit_say(text, "system")
    log(action="say", args={"text": text}, decision="allowed", auth_method="auto", ok=True)


def _announce(task, action, text):
    """Speak a tool's pre-rendered text, on the bus and in the audit log.

    This is the (a) half of reviving ToolResult.say: the text is spoken, not
    used to answer in place of the model. The model still runs its own step.
    """
    events.emit({"type": "state", "state": "speaking"})
    _emit_say(text, "system")
    if not _music_played_this_turn:
        ACTION_SCHEMA["say"]["handler"](text=text)
    log(action="say", args={"text": text}, decision="allowed", auth_method="auto", ok=True)
    _episode(task, "success", "say", {"text": text}, f"announced {action}")
    print(f"  -> {text}")


def _ask_for_email(label):
    _say(f"What is {label}'s email address?")


def _episode(task, outcome, action, args=None, agent_notes=""):
    episodic.log_event(
        task=task,
        outcome=outcome,
        action=action,
        args=args or {},
        agent_notes=agent_notes,
    )


def _save_remember(task, key, value):
    key, value = memory.store(key, value)
    print(f"  saved: {key} → {value}")
    log(action="remember", args={"key": key, "value": value}, decision="allowed", auth_method="auto", ok=True)
    _episode(task, "success", "remember", {"key": key, "value": value}, f"remembered {key}")


_APP_ALIASES = {
    "calendar": "Calendar",
    "google calendar": "Calendar",
    "mail": "Mail",
    "notes": "Notes",
    "music": "Music",
    "spotify": "Spotify",
}

_OPEN_AND_PLAY = re.compile(
    r'\bopen\s+(?:the\s+)?(?:app\s+)?(.+?)\s+and\s+(?:play|search(?:\s+for)?)\s+(?:"([^"]+)"|\'([^\']+)\'|(.+?))\s*\.?\s*$',
    re.I,
)
_PLAY_TRACK = re.compile(
    r'\b(?:play|search(?:\s+for)?|listen\s+to)\s+(?:"([^"]+)"|\'([^\']+)\'|(.+?))(?:\s+on\s+spotify)?\s*\.?\s*$',
    re.I,
)
_SPOTIFY_PLAY = re.compile(
    r'\bspotify\s+(?:play|search(?:\s+for)?)\s+(?:"([^"]+)"|\'([^\']+)\'|(.+?))\s*\.?\s*$',
    re.I,
)
_BUNDLED_OPEN_PLAY = re.compile(
    r'^(.+?)\s+and\s+play\s+(?:"([^"]+)"|\'([^\']+)\'|(.+?))\s*\.?\s*$',
    re.I,
)


def _normalize_app_name(raw):
    name = str(raw).strip()
    if not name:
        return name
    key = re.sub(r"^my\s+", "", name, flags=re.I).strip().lower()
    return _APP_ALIASES.get(key, _APP_ALIASES.get(name.lower(), name))


def _music_app_for(app_name):
    return "Spotify" if str(app_name).strip().lower() == "spotify" else app_name


def _is_recent_track(track):
    return actions_mod._is_recent_play_query(track)


_PLAY_RECENT = re.compile(
    r"\b(?:play\s+again|play\s+(?:my\s+)?(?:most\s+recent|last|latest)(?:\s+(?:song|track))?)\b",
    re.I,
)


def _run_play_recent(user_text, app_name="Spotify"):
    music_app = _music_app_for(app_name)
    outcome = _execute_step(user_text, "play_music", {"query": "recent", "app_name": music_app})
    if outcome.status in ("done", "abort"):
        return True
    _music_say(f"Playing your most recent song on {music_app}.")
    return True


def _try_play_recent(user_text):
    if not _PLAY_RECENT.search(user_text):
        return False
    if re.search(r"\bopen\b.+\band\s+play\b", user_text, re.I):
        return False
    return _run_play_recent(user_text, "Spotify")


def _track_from_groups(match, start=1):
    if not match:
        return None
    track = (
        match.group(start)
        or match.group(start + 1)
        or match.group(start + 2)
        or ""
    ).strip().rstrip(".")
    return track or None


def _extract_play_track(user_text):
    text = user_text.strip()
    track = _track_from_groups(_OPEN_AND_PLAY.search(text), start=2)
    if track:
        return track
    for pattern in (_PLAY_TRACK, _SPOTIFY_PLAY):
        track = _track_from_groups(pattern.search(text))
        if track:
            return track
    m = re.search(
        r'\bopen\s+spotify\b.*?\b(?:play|search(?:\s+for)?)\s+(?:"([^"]+)"|\'([^\']+)\'|(.+?))\s*\.?\s*$',
        user_text.strip(),
        re.I | re.S,
    )
    return _track_from_groups(m)


def _music_intent(user_text):
    text = user_text.lower()
    if _extract_play_track(user_text):
        return True
    if _PLAY_RECENT.search(user_text):
        return True
    return bool(
        re.search(r"\bspotify\b", text)
        and re.search(r"\b(play|search|listen)\b", text)
    )


def _parse_open_and_play(user_text):
    m = _OPEN_AND_PLAY.search(user_text.strip())
    if not m:
        return None
    app_name = _normalize_app_name(m.group(1))
    track = _track_from_groups(m, start=2)
    if not app_name or not track:
        return None
    return app_name, track


def _split_bundled_open_play(app_name):
    m = _BUNDLED_OPEN_PLAY.match(str(app_name).strip())
    if not m:
        return None
    app = _normalize_app_name(m.group(1))
    track = _track_from_groups(m, start=2)
    if not app or not track:
        return None
    return app, track


def _run_open_and_play(user_text, app_name, track):
    music_app = _music_app_for(app_name)
    outcome = _execute_step(user_text, "play_music", {"query": track, "app_name": music_app})
    if outcome.status in ("done", "abort"):
        return True
    # Confirm in UI only — speaking would pause Spotify.
    _music_say(f"Playing {track} on {music_app}.")
    return True


def _try_open_and_play(user_text):
    parsed = _parse_open_and_play(user_text)
    if not parsed:
        return False
    app_name, track = parsed
    if _is_recent_track(track):
        return _run_play_recent(user_text, app_name)
    return _run_open_and_play(user_text, app_name, track)


def _try_spotify_play(user_text):
    if not _music_intent(user_text):
        return False
    track = _extract_play_track(user_text)
    if not track:
        return False
    if _is_recent_track(track):
        return _run_play_recent(user_text)
    return _run_open_and_play(user_text, "Spotify", track)


def _music_play_pending(user_text, steps):
    if not _music_intent(user_text):
        return None
    if any("play_music" in entry for entry in steps):
        return None
    return _extract_play_track(user_text)


def _redirect_music_action(user_text, steps, action, args):
    """If the model picked open_app/say instead of play_music, fix the plan."""
    track = _music_play_pending(user_text, steps)
    if not track:
        return action, args
    query = "recent" if _is_recent_track(track) else track
    play_args = {"query": query, "app_name": "Spotify"}
    if action == "say":
        return "play_music", play_args
    if action == "open_app":
        app = str(args.get("app_name", "")).strip().lower()
        if app in ("spotify",) or _normalize_app_name(app).lower() == "spotify":
            return "play_music", play_args
    return action, args


def _try_direct_say(user_text):
    m = re.match(r"^say\s+(.+)$", user_text, re.I | re.S)
    if not m:
        return False
    text = _normalize_text(m.group(1).strip())
    if not text:
        return False
    args = {"text": text}
    print(f"  -> proposed: say({json.dumps(args, ensure_ascii=False)})   [risk: auto]")
    events.emit({"type": "action", "verb": "say", "risk": "auto"})
    events.emit({"type": "state", "state": "speaking"})
    _emit_say(text, "prose")
    try:
        ACTION_SCHEMA["say"]["handler"](**args)
        print("  ok")
        log(action="say", args=args, decision="allowed", auth_method="auto", ok=True)
        _episode(user_text, "success", "say", args, _success_note("say", args))
    except Exception as e:
        print(f"  ! error: {e}")
        log(action="say", args=args, decision="allowed", auth_method="auto", ok=False, error=str(e))
        _episode(user_text, "failure", "say", args, str(e))
    return True


_FINISH = re.compile(
    r"(?:i\s+)?(?:have\s+)?(?:finished|completed|done\s+with)\s+(?:the\s+)?(.+)",
    re.I,
)
_IGNORE = re.compile(
    r"(?:ignore|skip|don'?t\s+(?:show|include))\s+(?:the\s+)?(.+)",
    re.I,
)
_MULTI_CAL_IGNORE = re.compile(
    r"^(.+?)\s+(?:are\s+)?(?:in\s+)?(?:a\s+)?different\s+calendar.*?\bignore\b",
    re.I,
)


def _split_ignore_keys(text):
    text = text.strip().rstrip(".")
    return [p.strip() for p in re.split(r"\s+and\s+", text, flags=re.I) if p.strip()]


def _try_status_update(user_text):
    """Deterministic finished/ignore updates (no model round-trip)."""
    m = _MULTI_CAL_IGNORE.search(user_text)
    if m:
        keys = _split_ignore_keys(m.group(1))
        for key in keys:
            _save_remember(user_text, key, "ignore")
        if keys:
            joined = " and ".join(keys)
            _say(f"Okay — I'll ignore {joined} on your schedule.")
            return True
    m = _FINISH.search(user_text)
    if m:
        key = m.group(1).strip().rstrip(".")
        if key:
            _save_remember(user_text, key, "finished")
            _say(f"Got it — marked {key} as finished.")
            return True
    m = _IGNORE.search(user_text)
    if m:
        key = m.group(1).strip().rstrip(".")
        if key:
            _save_remember(user_text, key, "ignore")
            _say(f"Okay — I'll ignore {key} on your schedule.")
            return True
    return False


def _try_remember(user_text):
    parsed = memory.parse_remember(user_text)
    if parsed == "ambiguous":
        _say("I heard more than one email address. Which email belongs to whom?")
        log(action="remember", args={}, decision="ambiguous", auth_method="auto", ok=False)
        _episode(user_text, "failure", "remember", {}, "ambiguous multi-email remember request")
        return True
    if not parsed:
        return False
    items = parsed if isinstance(parsed, list) else [parsed]
    for key, value in items:
        _save_remember(user_text, key, value)
    return True


def _resolve_recipient(to):
    """Return a valid email address, or None."""
    return memory.resolve_to(to)


def _coerce_args(spec, args):
    allowed = set(spec["args"])
    out = {}
    for key, val in args.items():
        if key not in allowed or val is None:
            continue
        if isinstance(val, str):
            val = val.strip()
            if not val:
                continue
        out[key] = val
    return out


def _valid_args(spec, args):
    allowed = set(spec["args"])
    optional = set(spec.get("optional_args", []))
    required = allowed - optional
    keys = set(args.keys())
    return keys <= allowed and required <= keys


class StepOutcome:
    __slots__ = ("status", "history_line")

    def __init__(self, status, history_line=""):
        self.status = status  # "continue" | "done" | "abort"
        self.history_line = history_line


def _format_step_history(steps):
    if not steps:
        return ""
    lines = ["Steps taken so far:"]
    for i, entry in enumerate(steps, 1):
        lines.append(f"  {i}. {entry}")
    lines.append("")
    lines.append("Choose the NEXT action to fulfill the request. Use say only when ready to respond and stop.")
    return "\n".join(lines)


def _build_step_prompt(base_prompt, steps):
    history = _format_step_history(steps)
    if history:
        return f"{base_prompt}\n\n{history}"
    return (
        f"{base_prompt}\n\n"
        "Choose the first action to fulfill the request. Use say only when ready to respond and stop."
    )


def _tool_history_line(action, args, tool_data, ok):
    args_json = json.dumps(args, ensure_ascii=False)
    if ok:
        return f"{action}({args_json}) returned:\n{tool_data}"
    return f"{action}({args_json}) error:\n{tool_data}"


_DENIED_MESSAGES = {
    "send_email": "I didn't send that — you cancelled.",
}


def _denied_say(action):
    _say(_DENIED_MESSAGES.get(action, "I didn't do that — you cancelled."))


def _gate_label(action, args):
    """Human-readable label for the Touch ID / gate card in the UI."""
    parts = [action.replace("_", " ")]
    if "to" in args and args["to"]:
        parts.append(f"to {args['to']}")
    if "subject" in args and args["subject"]:
        parts.append(f"— {args['subject']}")
    return " ".join(parts)


def _execute_step(user_text, action, args):
    """Run one whitelisted action; return whether the turn continues, ends, or aborts."""
    global _music_played_this_turn
    spec = ACTION_SCHEMA[action]
    risk = spec["risk"]
    auth_method = "auto"
    print(f"  -> proposed: {action}({json.dumps(args, ensure_ascii=False)})   [risk: {risk}]")
    events.emit({"type": "action", "verb": action, "risk": risk})

    if action == "remember":
        validated = memory.validate_remember(args.get("key"), args.get("value"), user_text)
        if validated == "ambiguous":
            _say("I heard more than one email address. Which email belongs to whom?")
            log(action=action, args=args, decision="ambiguous", auth_method="auto", ok=False)
            _episode(user_text, "failure", action, args, "ambiguous multi-email remember request")
            return StepOutcome("abort")
        if validated == "invalid":
            print("  [rejected] remember requires a non-empty key and value")
            log(action=action, args=args, decision="rejected-args", auth_method="auto", ok=False)
            _episode(user_text, "failure", action, args, "remember missing key or value")
            return StepOutcome("abort")
        key, value = validated
        _save_remember(user_text, key, value)
        return StepOutcome("continue", f"remember({json.dumps({'key': key, 'value': value}, ensure_ascii=False)}) → saved")

    if risk == "confirm":
        gate_label = _gate_label(action, args)
        events.emit({"type": "state", "state": "gate"})
        events.emit({"type": "gate", "label": gate_label})
        granted, auth_method = allow(action, args)
        events.emit({"type": "gate_close"})
        if not granted:
            print("  x denied")
            log(action=action, args=args, decision="denied", auth_method="denied", ok=False)
            _episode(user_text, "failure", action, args, f"denied {action}")
            _denied_say(action)
            return StepOutcome("abort")

    if "to" in args:
        email = _resolve_recipient(args["to"])
        if not email:
            label = memory.contact_label(args["to"])
            _ask_for_email(label)
            log(action=action, args=args, decision="needs-email", auth_method=auth_method, ok=False)
            _episode(user_text, "partial", action, args, f"asked for {label}'s email")
            return StepOutcome("abort")
        args = {**args, "to": email}
        print(f"  -> to: {email}")

    if action == "say":
        text = args.get("text", "")
        print(f"  -> {text}")
        events.emit({"type": "state", "state": "speaking"})
        _emit_say(text, "prose")
        try:
            if not _music_played_this_turn:
                handler = getattr(actions_mod, spec["handler"].__name__)
                handler(**args)
        except Exception as e:
            print(f"  ! error: {e}")
            log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=False, error=str(e))
            _episode(user_text, "failure", action, args, str(e))
            return StepOutcome("abort")
        print("  ok")
        log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=True)
        _episode(user_text, "success", action, args, _success_note(action, args))
        return StepOutcome("done")

    events.emit({"type": "state", "state": "acting"})
    try:
        handler = getattr(actions_mod, spec["handler"].__name__)
        result = handler(**args)
        if action == "play_music":
            _music_played_this_turn = True
    except NeedsUserInput as e:
        _say(e.message)
        log(action=action, args=args, decision="needs-input", auth_method=auth_method, ok=False)
        _episode(user_text, "partial", action, args, e.message)
        return StepOutcome("abort")
    except Exception as e:
        print(f"  ! error: {e}")
        log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=False, error=str(e))
        _episode(user_text, "failure", action, args, str(e))
        return StepOutcome("abort")

    if isinstance(result, ToolResult):
        tool_data = result.data
        tool_ok = getattr(result, "ok", True)
        if tool_ok and result.say and getattr(result, "announce", False):
            _announce(user_text, action, result.say)
        if not tool_data:
            # Nothing for the model to read — keep the compact history line the
            # non-ToolResult path produced, so the model's context is unchanged.
            print("  ok")
            log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=tool_ok)
            _episode(user_text, "success" if tool_ok else "failure", action, args,
                     _success_note(action, args))
            return StepOutcome("continue", f"{action}({json.dumps(args, ensure_ascii=False)}) → ok")
        if tool_ok:
            print("  -> data received")
            log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=True)
            _episode(user_text, "success", action, args, _success_note(action, args, tool_data))
        else:
            print(f"  -> tool error: {tool_data}")
            log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=False, error=str(tool_data))
            _episode(user_text, "failure", action, args, str(tool_data)[:120])
        return StepOutcome(
            "continue",
            _tool_history_line(action, args, tool_data, tool_ok),
        )

    if isinstance(result, str):
        print("  -> data received")
        log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=True)
        _episode(user_text, "success", action, args, _success_note(action, args, result))
        return StepOutcome("continue", _tool_history_line(action, args, result, True))

    print("  ok")
    log(action=action, args=args, decision="allowed", auth_method=auth_method, ok=True)
    _episode(user_text, "success", action, args, _success_note(action, args))
    return StepOutcome("continue", f"{action}({json.dumps(args, ensure_ascii=False)}) → ok")


def _compound_hints(user_text):
    hints = []
    weather = bool(re.search(r"\b(weather|temperature|forecast|temp)\b", user_text, re.I))
    calendar = _sense_action(user_text) == "read_calendar" or bool(
        re.search(r"\b(calendar|schedule|my day|meetings|agenda)\b", user_text, re.I)
    )
    if weather and calendar and not _email_intent(user_text):
        hints.append(
            "This request needs both get_weather and read_calendar before your final say."
        )
    if re.search(r"\bopen\b", user_text, re.I) and re.search(r"\bplay\b", user_text, re.I):
        hints.append("Open the app first with open_app, then play_music with the track, then say.")
    if _email_intent(user_text) and not _music_intent(user_text):
        hints.append(
            "Email task: use draft_email(to, subject, body) or send_email(to, subject, body). "
            "Do NOT use open_app — Mail is opened automatically by these actions."
        )
    return hints


def _redirect_email_action(user_text, steps, action, args):
    """Block open_app(Mail/Calendar) during email tasks and inject a corrective step note."""
    if not _email_intent(user_text) or _music_intent(user_text):
        return action, args, None
    if action != "open_app":
        return action, args, None
    app = str(args.get("app_name", "")).strip().lower()
    if app not in ("mail", "calendar"):
        return action, args, None
    hint = (
        f"open_app({app}) was skipped for this email task — "
        "use draft_email(to, subject, body) or send_email(to, subject, body) instead."
    )
    return None, None, hint


def _coerce_action_args(action, args, user_text):
    spec = ACTION_SCHEMA.get(action)
    if spec is None:
        return None, None
    args = _coerce_args(spec, args)
    if _valid_args(spec, args):
        return action, args
    if action == "get_weather":
        return action, _weather_args(user_text)
    if action == "read_calendar":
        return action, {"query": user_text}
    return None, None


def _request_action(backend, user_prompt):
    """Ask the model for the next action JSON."""
    return backend.chat(_system_prompt(), user_prompt)


def _build_prompt(user_text):
    parts = []
    recent = episodic.recent_summary(3)
    if recent:
        parts.append(recent)
    past = palace_memory.recall_summary(user_text, n=3)
    if past:
        parts.append(past)
    # Don't inject schedule context for pure email requests — "today" in
    # "email my prof saying I can't make it today" triggers read_calendar
    # incorrectly, confusing the model into opening Calendar.
    if _sense_action(user_text) == "read_calendar" and not _email_intent(user_text):
        schedule_facts = memory.recall_for_schedule()
        if schedule_facts:
            parts.append(f"Schedule notes:\n{schedule_facts}")
    facts = memory.recall(user_text)
    if facts:
        parts.append(f"Known facts:\n{facts}")
    parts.append(f"Request: {user_text}")
    hints = _compound_hints(user_text)
    if hints:
        parts.extend(hints)
    return "\n\n".join(parts)


def handle(user_text, backend):
    global _music_played_this_turn
    user_text = _clean_input(user_text)
    if not user_text:
        return
    _music_played_this_turn = False
    events.emit({"type": "state", "state": "thinking"})
    try:
        _handle_inner(user_text, backend)
    finally:
        _music_played_this_turn = False
        events.emit({"type": "state", "state": "idle"})


def _handle_inner(user_text, backend):

    if _try_remember(user_text):
        return

    if _try_status_update(user_text):
        return

    if _try_direct_say(user_text):
        return

    if _try_open_and_play(user_text):
        return

    if _try_play_recent(user_text):
        return

    if _try_spotify_play(user_text):
        return

    if _email_intent(user_text) and not _music_intent(user_text):
        for key in memory.recalled_keys_missing_email(user_text):
            label = memory.contact_label(key)
            _ask_for_email(label)
            log(action="draft_email", args={"to": key}, decision="needs-email", auth_method="auto", ok=False)
            _episode(
                user_text,
                "partial",
                "draft_email",
                {"to": key},
                f"asked for {label}'s email",
            )
            return

    base_prompt = _build_prompt(user_text)
    steps = []

    for step_num in range(1, MAX_AGENT_STEPS + 1):
        user_prompt = _build_step_prompt(base_prompt, steps)
        try:
            raw = _request_action(backend, user_prompt)
        except BackendError as e:
            print(f"  [{backend.name} error]: {e.message}")
            if e.hint:
                print(f"        {e.hint}")
            return
        except Exception as e:
            print(f"  [{backend.name} error]: {e}")
            return

        try:
            msg = parse_action(raw)
            action = msg["action"]
            args = _normalize_args(msg.get("args", {}))
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as e:
            print("  [could not parse model output]:", raw)
            print(f"  ({e})")
            _say("Sorry, I didn't understand that request.")
            _episode(user_text, "failure", "parse", {}, f"unparseable model output: {raw!r}")
            return

        coerced = _coerce_action_args(action, args, user_text)

        if action == "open_app" and coerced != (None, None):
            bundled = _split_bundled_open_play(coerced[1].get("app_name", ""))
            if bundled:
                app_name, track = bundled
                _run_open_and_play(user_text, app_name, track)
                return

        if coerced == (None, None):
            if ACTION_SCHEMA.get(action) is None:
                print(f"  [rejected] unknown action: {action!r}")
                log(action=action, args=args, decision="rejected-unknown", auth_method="auto", ok=False)
                _episode(user_text, "failure", action, args, f"rejected unknown action {action!r}")
            else:
                print(f"  [rejected] wrong args for {action}: {list(args)}")
                log(action=action, args=args, decision="rejected-args", auth_method="auto", ok=False)
                _episode(user_text, "failure", action, args, f"rejected wrong args for {action}")
            return

        action, args = coerced
        action, args, email_hint = _redirect_email_action(user_text, steps, action, args)
        if email_hint:
            print(f"  [email redirect] {email_hint}")
            steps.append(email_hint)
            continue
        action, args = _redirect_music_action(user_text, steps, action, args)
        if action == "play_music":
            spec = ACTION_SCHEMA["play_music"]
            args = _coerce_args(spec, args)
            if not _valid_args(spec, args):
                print(f"  [rejected] wrong args for play_music: {list(args)}")
                return
        outcome = _execute_step(user_text, action, args)
        if outcome.status == "done":
            return
        if outcome.status == "abort":
            return
        if outcome.history_line:
            steps.append(outcome.history_line)

    _say("Sorry, I couldn't finish that in time. Try breaking it into smaller steps.")
    log(action="say", args={"text": "step cap"}, decision="step-cap", auth_method="auto", ok=False)
    _episode(user_text, "failure", "step-cap", {}, f"hit {MAX_AGENT_STEPS} step limit")


def _success_note(action, args, tool_result=None):
    if tool_result is not None:
        if action in ("read_calendar", "get_weather"):
            return f"{action} data fetched"
        return f"{action}: {tool_result[:80]}"
    if action == "say":
        return f"said {str(args.get('text', ''))[:80]}"
    if action == "draft_email":
        return f"drafted email to {args.get('to', '?')}"
    if action == "send_email":
        return f"sent email to {args.get('to', '?')}"
    if action == "open_url":
        return f"opened {args.get('url', '?')}"
    if action == "open_app":
        return f"opened {args.get('app_name', '?')}"
    if action == "play_music":
        return f"playing {args.get('query', '?')} in {args.get('app_name') or 'Spotify'}"
    if action == "remember":
        return f"remembered {args.get('key', '?')}"
    if action == "read_calendar":
        return "read calendar"
    if action == "get_weather":
        return f"got weather for {args.get('location') or 'stored location'}"
    if action == "open_in_claude_code":
        return f"opened {args.get('project', '?')} in Claude Code"
    return action.replace("_", " ")


def _banner(backend):
    return (
        "\n  mac-agent v0  ·  gated · logged\n"
        f"  backend: {backend.describe()}\n"
        "  try: say good morning   /   draft an email to me@x.com about lunch\n"
        "  type 'exit' to quit.\n"
    )


def repl(input_fn=None, backend_getter=None):
    read = input_fn or input
    getter = backend_getter or create_backend
    clock.start()
    episodic.init_session()
    banner_printed = False
    while True:
        try:
            user = _clean_input(read("you > ").strip())
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not user:
            continue
        if user in ("exit", "quit"):
            print("\nbye")
            break
        try:
            backend = getter()
        except BackendError as e:
            print(f"  [backend error]: {e.message}")
            if e.hint:
                print(f"        {e.hint}")
            continue
        if not banner_printed:
            print(_banner(backend))
            banner_printed = True
        try:
            handle(user, backend)
        except BackendError as e:
            print(f"  [{backend.name} error]: {e.message}")
            if e.hint:
                print(f"        {e.hint}")
        except Exception as e:
            print(f"  ! error: {e}")
            _episode(user, "failure", "error", {}, str(e))
