"""
senses.py — data-gathering verbs (calendar, weather). Handlers return text for the tool loop.
"""

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import memory
from .clock import now as clock_now
from .resources import script_path
_CALENDAR_CACHE = {"raw": None, "at": 0.0}
_CALENDAR_TTL = 300


_TS_FMT = "%Y-%m-%d %H:%M"


@dataclass
class _CalEvent:
    start: datetime
    end: datetime
    all_day: bool
    title: str
    location: str

_WMO = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    95: "thunderstorm",
}


class NeedsUserInput(Exception):
    def __init__(self, message):
        self.message = message


class ToolResult:
    """Handler return value: optional pre-built say text skips the LLM follow-up."""

    __slots__ = ("data", "say", "ok")

    def __init__(self, data, say=None, ok=True):
        self.data = data
        self.say = say
        self.ok = ok


def _fetch_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _parse_ts(text):
    return datetime.strptime(text.strip(), _TS_FMT)


def _parse_calendar_raw(raw):
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 4)
        if len(parts) < 4:
            continue
        try:
            start = _parse_ts(parts[0])
            end = _parse_ts(parts[1])
        except ValueError:
            continue
        all_day = str(parts[2]).strip().lower() in ("true", "yes", "1")
        title = parts[3].strip()
        location = parts[4].strip() if len(parts) > 4 else ""
        if not title:
            continue
        events.append(_CalEvent(start, end, all_day, title, location))
    events.sort(key=lambda e: e.start)
    return _dedupe_events(events)


def _dedupe_events(events):
    seen = set()
    out = []
    for e in events:
        key = (e.title, e.start, e.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _fmt_time(dt):
    return dt.strftime("%H:%M")


def _fmt_time_12h(dt):
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def _today_bounds(now):
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start, today_start + timedelta(days=1)


def _events_today(events, now):
    today_start, tomorrow_start = _today_bounds(now)
    out = []
    for e in events:
        if e.all_day:
            if today_start <= e.start < tomorrow_start:
                out.append(e)
        elif today_start <= e.start < tomorrow_start:
            out.append(e)
    return out


def _event_visible(e):
    if memory.is_ignored(e.title):
        return False
    if memory.is_marked_done(e.title):
        return False
    return True


def _remaining_events(events, now):
    return [
        e for e in _events_today(events, now)
        if _event_visible(e) and (e.all_day or e.end > now)
    ]


def _event_detail(e):
    loc = f" at {e.location}" if e.location else ""
    if e.all_day:
        return f"{e.title}{loc} (all day)"
    if e.start == e.end or (e.end - e.start).total_seconds() < 60:
        return f"{e.title}{loc} at {_fmt_time_12h(e.start)}"
    return f"{e.title}{loc} from {_fmt_time_12h(e.start)} to {_fmt_time_12h(e.end)}"


_QUERY_STOP = frozenset({
    "do", "i", "have", "is", "there", "am", "the", "a", "an", "my", "on", "at",
    "today", "tonight", "this", "evening", "morning", "afternoon", "calendar",
})


def _search_terms(query):
    return [
        w for w in re.findall(r"[a-z0-9]+", query.lower())
        if len(w) > 2 and w not in _QUERY_STOP
    ]


def _event_matches_query(e, query):
    terms = _search_terms(query)
    if not terms:
        return False
    return memory.key_matches_text(" ".join(terms), e.title)


def _answer_event_query(remaining, all_today, now, query):
    stamp = _fmt_time_12h(now)
    matches = [e for e in remaining if _event_matches_query(e, query)]
    if matches:
        return f"Yes — at {stamp} you still have {_event_detail(matches[0])}."
    matches = [e for e in all_today if _event_matches_query(e, query)]
    if matches:
        e = matches[0]
        if not e.all_day and e.end <= now:
            return f"You had {_event_detail(e)}, but it's already over."
        return f"I see {_event_detail(e)} today, but it's not on your remaining schedule."
    return f"No — at {stamp} I don't see that on your calendar today."


def _finished_schedule_notes():
    notes = []
    for line in memory.recall_for_schedule().splitlines():
        if line.endswith("= finished"):
            notes.append(line.split(" = ", 1)[0])
    return notes


def _calendar_speech(remaining, now, query="", all_today=None):
    if query and re.search(r"\b(do i have|is there|am i|got)\b", query, re.I):
        return _answer_event_query(remaining, all_today or remaining, now, query)
    stamp = _fmt_time_12h(now)
    finished = _finished_schedule_notes()
    done_clause = ""
    if finished:
        done_clause = f" {' and '.join(finished)} {'is' if len(finished) == 1 else 'are'} done."
    if not remaining:
        base = f"It's {stamp}. Nothing left on your calendar today."
        return base + done_clause if done_clause else base
    details = [_event_detail(e) for e in remaining]
    if len(details) == 1:
        return f"It's {stamp}. You still have {details[0]}." + done_clause
    joined = "; ".join(details)
    return f"It's {stamp}. Still on your calendar: {joined}." + done_clause


def _format_calendar(events, now=None):
    now = now or clock_now()
    remaining = _remaining_events(events, now)

    lines = [f"Now: {now.strftime('%H:%M')}", ""]
    if not remaining:
        lines.append("(nothing remaining today)")
        return "\n".join(lines)

    lines.append(f"Remaining ({len(remaining)}):")
    for e in remaining:
        loc = f" @ {e.location}" if e.location else ""
        if e.all_day:
            lines.append(f"  all day - {e.title}{loc}")
        elif e.start == e.end or (e.end - e.start).total_seconds() < 60:
            lines.append(f"  {_fmt_time(e.start)} - {e.title}{loc}")
        else:
            lines.append(f"  {_fmt_time(e.start)}-{_fmt_time(e.end)} - {e.title}{loc}")
    return "\n".join(lines)


def _calendar_not_running(err_text):
    text = str(err_text).lower()
    return "application isn't running" in text or "(-600)" in text


def _launch_calendar():
    subprocess.run(["open", "-a", "Calendar"], check=False)
    time.sleep(1.5)


def _run_calendar_script():
    proc = subprocess.run(
        ["osascript", script_path("read_calendar.applescript")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=180,
    )
    if proc.stderr.strip():
        err = proc.stderr.strip().splitlines()[-1]
        raise RuntimeError(err)
    return proc.stdout.strip()


def _fetch_calendar_raw():
    now = time.time()
    if _CALENDAR_CACHE["raw"] is not None and now - _CALENDAR_CACHE["at"] < _CALENDAR_TTL:
        return _CALENDAR_CACHE["raw"]
    last_err = None
    for attempt in range(2):
        try:
            raw = _run_calendar_script()
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e)).strip().splitlines()
            last_err = err[-1] if err else str(e)
            if attempt == 0 and _calendar_not_running(last_err):
                _launch_calendar()
                continue
            raise RuntimeError(last_err) from e
        except RuntimeError as e:
            last_err = str(e)
            if attempt == 0 and _calendar_not_running(last_err):
                _launch_calendar()
                continue
            raise
        _CALENDAR_CACHE["raw"] = raw
        _CALENDAR_CACHE["at"] = now
        return raw
    raise RuntimeError(last_err or "calendar read failed")


def _calendar_permission_denied(message):
    text = str(message).lower()
    markers = (
        "not authorized",
        "not allowed",
        "operation not permitted",
        "(-1743)",
        "-1743",
        "assistive",
        "automation",
        "privacy",
        "calendar access",
    )
    return any(m in text for m in markers)


def _calendar_error(message):
    if _calendar_not_running(message):
        _launch_calendar()
        say = "I opened Calendar. Ask me about your schedule again."
    elif "timed out" in str(message).lower():
        say = "Calendar is taking too long. Try again in a moment."
    elif _calendar_permission_denied(message):
        say = (
            "Daimon doesn't have Calendar access yet. "
            "Open System Settings, Privacy and Security, Calendars, and turn on Daimon."
        )
    else:
        say = "I couldn't read your calendar. Check Calendar permission for Daimon in System Settings."
    return ToolResult(message, say=say, ok=False)


def read_calendar(query=None):
    try:
        raw = _fetch_calendar_raw()
    except subprocess.TimeoutExpired:
        return _calendar_error("(calendar read timed out — try again or check Calendar.app permissions)")
    except UnicodeDecodeError as e:
        return _calendar_error(f"(calendar error: could not decode Calendar output — {e})")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e)).strip().splitlines()
        return _calendar_error(f"(calendar error: {err[-1] if err else e})")
    except FileNotFoundError as e:
        return _calendar_error(f"(calendar error: {e})")
    except RuntimeError as e:
        return _calendar_error(f"(calendar error: {e})")
    now = clock_now()
    events = _parse_calendar_raw(raw) if raw else []
    all_today = [e for e in _events_today(events, now) if _event_visible(e)]
    remaining = _remaining_events(events, now)
    data = _format_calendar(events, now)
    q = (query or "").strip()
    return ToolResult(data, say=_calendar_speech(remaining, now, q, all_today))


def _temp_unit():
    stored = memory.lookup("temp_unit") or "Celsius"
    return "fahrenheit" if stored.lower().startswith("f") else "celsius"


def _unit_label(unit_param):
    return "°F" if unit_param == "fahrenheit" else "°C"


_PLACEHOLDER_LOCATIONS = {
    "default", "stored", "unknown", "here", "current", "my location", "local", "user",
}


def get_weather(location=None):
    if location is not None and not str(location).strip():
        location = None
    if location and str(location).strip().lower() in _PLACEHOLDER_LOCATIONS:
        location = None
    if not location:
        location = memory.lookup("location")
    if not location:
        raise NeedsUserInput("What city are you in?")

    geo = _fetch_json(
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": location, "count": 1, "language": "en", "format": "json"})
    )
    results = geo.get("results") or []
    if not results:
        return f"(no location found for {location!r})"

    place = results[0]
    lat = place["latitude"]
    lon = place["longitude"]
    name = place.get("name", location)
    admin = place.get("admin1") or place.get("country") or ""

    unit_param = _temp_unit()
    weather = _fetch_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "temperature_unit": unit_param,
            "wind_speed_unit": "kmh",
        })
    )
    current = weather.get("current") or {}
    temp = current.get("temperature_2m")
    code = current.get("weather_code", -1)
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    desc = _WMO.get(code, "unknown conditions")
    label = _unit_label(unit_param)

    parts = [f"{name}" + (f", {admin}" if admin else "")]
    if temp is not None:
        parts.append(f"{temp}{label}")
    parts.append(desc)
    if humidity is not None:
        parts.append(f"humidity {humidity}%")
    if wind is not None:
        parts.append(f"wind {wind} km/h")
    return " · ".join(parts)
