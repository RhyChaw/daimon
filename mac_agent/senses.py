"""
senses.py — data-gathering verbs (calendar, weather). Handlers return text for the tool loop.
"""

import json
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

    __slots__ = ("data", "say")

    def __init__(self, data, say=None):
        self.data = data
        self.say = say


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
    return events


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


def _calendar_speech(remaining, now):
    stamp = _fmt_time_12h(now)
    if not remaining:
        return f"It's {stamp}. Nothing left on your calendar today."
    details = [_event_detail(e) for e in remaining]
    if len(details) == 1:
        return f"It's {stamp}. You still have {details[0]}."
    joined = "; ".join(details)
    return f"It's {stamp}. Still on your calendar: {joined}."


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


def _fetch_calendar_raw():
    now = time.time()
    if _CALENDAR_CACHE["raw"] is not None and now - _CALENDAR_CACHE["at"] < _CALENDAR_TTL:
        return _CALENDAR_CACHE["raw"]
    proc = subprocess.run(
        ["osascript", script_path("read_calendar.applescript")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=120,
    )
    if proc.stderr.strip():
        err = proc.stderr.strip().splitlines()[-1]
        raise RuntimeError(err)
    raw = proc.stdout.strip()
    _CALENDAR_CACHE["raw"] = raw
    _CALENDAR_CACHE["at"] = now
    return raw


def read_calendar():
    try:
        raw = _fetch_calendar_raw()
    except subprocess.TimeoutExpired:
        return "(calendar read timed out — try again or check Calendar.app permissions)"
    except UnicodeDecodeError as e:
        return f"(calendar error: could not decode Calendar output — {e})"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e)).strip().splitlines()
        return f"(calendar error: {err[-1] if err else e})"
    except FileNotFoundError as e:
        return f"(calendar error: {e})"
    except RuntimeError as e:
        return f"(calendar error: {e})"
    now = clock_now()
    events = _parse_calendar_raw(raw) if raw else []
    remaining = _remaining_events(events, now)
    data = _format_calendar(events, now)
    return ToolResult(data, say=_calendar_speech(remaining, now))


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
