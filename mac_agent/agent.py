"""
agent.py — the core loop.

  you type  ->  local model proposes ONE action  ->  whitelist check
            ->  gate (if risky)  ->  execute  ->  audit log  ->  repeat

The model never touches your system directly. It can only *propose* a verb from
the whitelist; this code is what actually runs anything.
"""

import json
import os
import re
from urllib.error import HTTPError

from . import actions as actions_mod
from .actions import ACTION_SCHEMA
from .ollama_client import chat, parse_action, resolve_model
from .gate import allow
from .audit import log
from . import memory
from . import episodic
from . import palace_memory
from . import clock
from .senses import NeedsUserInput, ToolResult

DEFAULT_MODEL = "llama3.2"
FALLBACK_MODELS = ("llama3.1:latest", "llama3.1:8b", "llama3:latest")
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


def _pick_model():
    requested = os.environ.get("MACAGENT_MODEL", DEFAULT_MODEL)
    resolved, available = resolve_model(requested)
    if resolved:
        return resolved
    for candidate in FALLBACK_MODELS:
        resolved, _ = resolve_model(candidate)
        if resolved:
            print(f"  note: {requested!r} not installed — using {resolved!r}")
            print(f"        (run `ollama pull {requested}` to use the default)")
            return resolved
    print(f"Model {requested!r} not found.")
    if available:
        print(f"  available: {', '.join(available)}")
    else:
        print("  no models installed.")
    print(f"  run: ollama pull {requested}")
    return None


def _system_prompt():
    lines = [
        "You control a Mac through a FIXED set of actions.",
        'Reply with ONLY valid JSON. Example: {"action":"say","args":{"text":"I don\'t know"}}',
        "Allowed actions:",
    ]
    for name, spec in ACTION_SCHEMA.items():
        lines.append(f'  - {name}(args: {", ".join(spec["args"])}) — {spec["desc"]}')
    lines.append('When the user says "say <text>", use say with that exact text (keep apostrophes).')
    lines.append('For greetings and chat (hi, hello, how are you), use say with a friendly helpful reply.')
    lines.append('If the request does not fit an action, use "say" to ask a clarifying question.')
    lines.append('When the user says "remember …", use remember to store the fact (key + value).')
    lines.append("If Known facts show a contact with no email on file, use say to ask for their email — never draft_email or send_email until an address is known.")
    lines.append("If the message includes a Recently or Past similar line and the user asks what you did before, use say to answer from those events.")
    lines.append(
        "For schedule or calendar questions, always use read_calendar — never invent events or answer from Recently lines. "
        "read_calendar returns only today's remaining events (after the current time)."
    )
    lines.append("For weather questions, always use get_weather — never invent temperatures or answer from Recently lines.")
    lines.append("After a tool returns data, you will be asked to summarize it with say — use only the provided data.")
    lines.append("Output nothing except the JSON object.")
    return "\n".join(lines)


def _email_intent(text):
    return bool(re.search(r"\b(email|draft|send|mail)\b", text, re.I))


def _sense_action(text):
    if re.search(r"\b(weather|temperature|temp\b|rain|snow|forecast|humid|wind)\b", text, re.I):
        return "get_weather"
    if re.search(r"\b(calendar|schedule|my day|today'?s events|meetings|agenda|how'?s my day)\b", text, re.I):
        return "read_calendar"
    return None


def _say(text):
    ACTION_SCHEMA["say"]["handler"](text=text)
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
    log(action="remember", args={"key": key, "value": value}, decision="allowed", ok=True)
    _episode(task, "success", "remember", {"key": key, "value": value}, f"remembered {key}")


def _try_direct_say(user_text):
    m = re.match(r"^say\s+(.+)$", user_text, re.I | re.S)
    if not m:
        return False
    text = _normalize_text(m.group(1).strip())
    if not text:
        return False
    args = {"text": text}
    print(f"  -> proposed: say({json.dumps(args, ensure_ascii=False)})   [risk: auto]")
    try:
        ACTION_SCHEMA["say"]["handler"](**args)
        print("  ok")
        log(action="say", args=args, decision="allowed", ok=True)
        _episode(user_text, "success", "say", args, _success_note("say", args))
    except Exception as e:
        print(f"  ! error: {e}")
        log(action="say", args=args, decision="allowed", ok=False, error=str(e))
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


def _try_status_update(user_text):
    """Deterministic finished/ignore updates (no model round-trip)."""
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
        log(action="remember", args={}, decision="ambiguous", ok=False)
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


def _valid_args(spec, args):
    allowed = set(spec["args"])
    optional = set(spec.get("optional_args", []))
    required = allowed - optional
    keys = set(args.keys())
    return keys <= allowed and required <= keys


def _build_tool_followup_prompt(user_text, action, result):
    extra = ""
    if action == "read_calendar":
        extra = (
            "The tool data lists only remaining events (after Now). "
            "Mention EVERY remaining event — do not skip, merge, or invent any.\n"
        )
    return (
        f"Request: {user_text}\n\n"
        f"Tool {action} returned:\n{result}\n\n"
        f"{extra}"
        "Summarize ONLY using the tool data above. Reply with a say action."
    )


def _run_action(user_text, model, action, args, *, tool_followup=True):
    """Execute one whitelisted action; follow up with say when handler returns data."""
    spec = ACTION_SCHEMA[action]
    risk = spec["risk"]
    print(f"  -> proposed: {action}({json.dumps(args, ensure_ascii=False)})   [risk: {risk}]")

    if action == "remember":
        validated = memory.validate_remember(args.get("key"), args.get("value"), user_text)
        if validated == "ambiguous":
            _say("I heard more than one email address. Which email belongs to whom?")
            log(action=action, args=args, decision="ambiguous", ok=False)
            _episode(user_text, "failure", action, args, "ambiguous multi-email remember request")
            return
        if validated == "invalid":
            print("  [rejected] remember requires a non-empty key and value")
            log(action=action, args=args, decision="rejected-args", ok=False)
            _episode(user_text, "failure", action, args, "remember missing key or value")
            return
        key, value = validated
        _save_remember(user_text, key, value)
        return

    if risk == "confirm" and not allow(action, args):
        print("  x denied")
        log(action=action, args=args, decision="denied", ok=False)
        _episode(user_text, "failure", action, args, f"denied {action}")
        return

    if "to" in args:
        email = _resolve_recipient(args["to"])
        if not email:
            label = memory.contact_label(args["to"])
            _ask_for_email(label)
            log(action=action, args=args, decision="needs-email", ok=False)
            _episode(user_text, "partial", action, args, f"asked for {label}'s email")
            return
        args = {**args, "to": email}
        print(f"  -> to: {email}")

    if action == "say":
        print(f"  -> {args.get('text', '')}")

    try:
        handler = getattr(actions_mod, spec["handler"].__name__)
        result = handler(**args)
    except NeedsUserInput as e:
        _say(e.message)
        log(action=action, args=args, decision="needs-input", ok=False)
        _episode(user_text, "partial", action, args, e.message)
        return
    except Exception as e:
        print(f"  ! error: {e}")
        log(action=action, args=args, decision="allowed", ok=False, error=str(e))
        _episode(user_text, "failure", action, args, str(e))
        return

    tool_data = result.data if isinstance(result, ToolResult) else result
    preset_say = result.say if isinstance(result, ToolResult) else None

    if preset_say and tool_followup:
        print(f"  -> data received")
        log(action=action, args=args, decision="allowed", ok=True)
        _episode(user_text, "success", action, args, _success_note(action, args, tool_data))
        _run_action(user_text, model, "say", {"text": preset_say}, tool_followup=False)
        return

    if isinstance(tool_data, str) and tool_followup:
        print(f"  -> data received")
        log(action=action, args=args, decision="allowed", ok=True)
        _episode(user_text, "success", action, args, _success_note(action, args, tool_data))
        try:
            raw = chat(model, _system_prompt(), _build_tool_followup_prompt(user_text, action, tool_data))
            msg = parse_action(raw)
            follow_action = msg["action"]
            follow_args = _normalize_args(msg.get("args", {}))
        except (json.JSONDecodeError, TypeError, KeyError, HTTPError, Exception) as e:
            print(f"  [could not summarize tool data]: {e}")
            _say("Sorry, I couldn't summarize that.")
            _episode(user_text, "failure", "parse", {}, f"tool follow-up failed: {e}")
            return
        if follow_action != "say":
            print(f"  [expected say after tool data, got {follow_action!r}]")
            _say("Sorry, I couldn't summarize that.")
            return
        if not _valid_args(ACTION_SCHEMA["say"], follow_args):
            print(f"  [rejected] wrong args for say: {list(follow_args)}")
            return
        _run_action(user_text, model, follow_action, follow_args, tool_followup=False)
        return

    print("  ok")
    log(action=action, args=args, decision="allowed", ok=True)
    _episode(user_text, "success", action, args, _success_note(action, args))


def _build_prompt(user_text):
    parts = []
    recent = episodic.recent_summary(3)
    if recent:
        parts.append(recent)
    past = palace_memory.recall_summary(user_text, n=3)
    if past:
        parts.append(past)
    required = _sense_action(user_text)
    if required == "read_calendar":
        schedule_facts = memory.recall_for_schedule()
        if schedule_facts:
            parts.append(f"Schedule notes:\n{schedule_facts}")
    facts = memory.recall(user_text)
    if facts:
        parts.append(f"Known facts:\n{facts}")
    parts.append(f"Request: {user_text}")
    if required:
        parts.append(f"Required action: {required} (fetch live data; do not guess with say).")
    return "\n\n".join(parts)


def handle(user_text, model):
    user_text = _clean_input(user_text)
    if not user_text:
        return

    if _try_remember(user_text):
        return

    if _try_status_update(user_text):
        return

    if _try_direct_say(user_text):
        return

    required = _sense_action(user_text)
    if required == "read_calendar":
        _run_action(user_text, model, "read_calendar", {})
        return

    if _email_intent(user_text):
        for key in memory.recalled_keys_missing_email(user_text):
            label = memory.contact_label(key)
            _ask_for_email(label)
            log(action="draft_email", args={"to": key}, decision="needs-email", ok=False)
            _episode(
                user_text,
                "partial",
                "draft_email",
                {"to": key},
                f"asked for {label}'s email",
            )
            return

    prompt = _build_prompt(user_text)
    required = required or _sense_action(user_text)
    try:
        raw = chat(model, _system_prompt(), prompt)
    except HTTPError as e:
        print(f"  [ollama {e.code}]: model {model!r} not available")
        print(f"        run: ollama pull {model}")
        return
    except Exception as e:
        print(f"  [ollama error]: {e}")
        return
    try:
        msg = parse_action(raw)
        action = msg["action"]
        args = _normalize_args(msg.get("args", {}))
        if required and action == "say":
            raw = chat(
                model,
                _system_prompt(),
                f"{prompt}\n\nYou used say but must use {required} to fetch live data. "
                f'Reply with only JSON. For get_weather use {{"action":"get_weather","args":{{}}}} '
                f'when location is not specified; for read_calendar use {{"action":"read_calendar","args":{{}}}}.',
            )
            msg = parse_action(raw)
            action = msg["action"]
            args = _normalize_args(msg.get("args", {}))
        if required and action == "say":
            _run_action(user_text, model, required, {})
            return
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as e:
        print("  [could not parse model output]:", raw)
        print(f"  ({e})")
        _say("Sorry, I didn't understand that request.")
        _episode(user_text, "failure", "parse", {}, f"unparseable model output: {raw!r}")
        return

    spec = ACTION_SCHEMA.get(action)
    if spec is None:
        print(f"  [rejected] unknown action: {action!r}")
        log(action=action, args=args, decision="rejected-unknown", ok=False)
        _episode(user_text, "failure", action, args, f"rejected unknown action {action!r}")
        return
    if not _valid_args(spec, args):
        print(f"  [rejected] wrong args for {action}: {list(args)}")
        log(action=action, args=args, decision="rejected-args", ok=False)
        _episode(user_text, "failure", action, args, f"rejected wrong args for {action}")
        return

    _run_action(user_text, model, action, args)


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
    if action == "remember":
        return f"remembered {args.get('key', '?')}"
    if action == "read_calendar":
        return "read calendar"
    if action == "get_weather":
        return f"got weather for {args.get('location') or 'stored location'}"
    return action.replace("_", " ")


def _banner(model):
    return (
        "\n  mac-agent v0  ·  local · gated · logged\n"
        f"  model: {model}   (set MACAGENT_MODEL to change)\n"
        "  try: say good morning   /   draft an email to me@x.com about lunch\n"
        "  type 'exit' to quit.\n"
    )


def repl(input_fn=None):
    read = input_fn or input
    clock.start()
    model = _pick_model()
    if not model:
        return
    episodic.init_session()
    print(_banner(model))
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
            handle(user, model)
        except Exception as e:
            print(f"  ! error: {e}")
            _episode(user, "failure", "error", {}, str(e))
