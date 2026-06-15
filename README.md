# Daimon

A local-first Mac agent you talk to in plain English. It controls your Mac through a fixed whitelist of actions — risky ones require Touch ID before they run, and everything is logged.

```
you type  →  model proposes one action  →  whitelist check
          →  Touch ID gate (if risky)   →  execute  →  audit log  →  repeat
```

## What it can do

| Request | What happens |
|---|---|
| "say good morning" | Speaks it aloud |
| "open Hacker News" | Opens in your browser |
| "open Spotify and play Tame Impala" | Searches and plays in Spotify |
| "what's the weather in London" | Fetches current conditions |
| "do I have anything today" | Reads your Calendar |
| "remember my prof's email is kevin@uni.edu" | Stores it for future use |
| "draft an email to my prof about the deadline" | Opens a visible Mail draft — no send |
| "send an email to my prof saying I'll be late" | Touch ID required, then sends |

Actions can chain. "Draft and send an email to me@x.com about lunch" drafts silently, then raises Touch ID for the send step only.

## The security model

`mac_agent/actions.py` is the whitelist — every verb the model can request is defined there. Unknown verbs are rejected by code, not by the model's judgement. Risky verbs (those that are outbound, irreversible, or touch private data) are marked `confirm` and require Touch ID before they execute. The model cannot override or bypass this.

```
risk: "auto"     →  runs without prompting     (say, open_url, open_app, play_music,
                                                 read_calendar, get_weather, remember,
                                                 draft_email)
risk: "confirm"  →  Touch ID required           (send_email)
```

Every action is appended to `audit.jsonl` with its decision, auth method (`biometric` / `terminal` / `denied`), and result.

## Run it

**Requirements:** macOS 12+, Python 3.10+, and [Ollama](https://ollama.com).

```bash
ollama pull llama3.2

git clone <repo-url> daimon && cd daimon
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

macagent start
```

This runs in your terminal. Touch ID is used if pyobjc is installed; otherwise it falls back to a `y/N` prompt.

## Daimon.app (recommended)

The bundled app runs with a proper macOS identity (`com.rhychaw.daimon`) so Calendar, Mail, and Spotify automation are attributed to Daimon rather than Terminal. Touch ID and Accessibility work reliably.

```bash
./scripts/build_app.sh
open dist/Daimon.app
```

Data is stored in `~/Library/Application Support/Daimon/`.

**After the first build**, grant Accessibility access once:
> System Settings → Privacy & Security → Accessibility → add Daimon.app

**After subsequent code changes**, `daimon start` syncs your Python edits directly into the bundle without rebuilding — the signature is preserved so the Accessibility grant carries over.

```bash
daimon start    # auto-syncs source → bundle, then launches
```

A full rebuild only runs when `pyproject.toml` or `setup_app.py` changes (i.e., a new dependency).

## Backends

Daimon supports two model backends, switchable in settings:

| Backend | How | When to use |
|---|---|---|
| **Ollama** (default) | Local, no API key | Fully offline, privacy-first |
| **Claude** | Anthropic API | Faster, stronger reasoning |

Set `ANTHROPIC_API_KEY` in `.env` and switch the backend to `claude` in settings to use Claude.

## Project layout

| Path | Role |
|---|---|
| `mac_agent/actions.py` | Verb whitelist + handlers — **the security boundary** |
| `mac_agent/gate.py` | Touch ID / passcode confirmation for risky actions |
| `mac_agent/audit.py` | Append-only log of every action and its outcome |
| `mac_agent/agent.py` | Core loop: propose → check → gate → execute → log |
| `mac_agent/keystrokes.py` | Spotify UI automation via in-process AppleScript |
| `mac_agent/memory.py` | Persistent fact store (contacts, preferences) |
| `mac_agent/senses.py` | Calendar and weather readers |
| `mac_agent/speech.py` | Text-to-speech output |
| `mac_agent/backend.py` | Ollama and Claude backend adapters |
| `mac_agent/app_main.py` | py2app entry point for `Daimon.app` |
| `mac_agent/console_ui.py` | Cocoa console window inside the bundled app |
| `mac_agent/launcher.py` | `daimon start`: sync-or-rebuild, then launch |
| `setup_app.py` | py2app config + Info.plist |
| `scripts/build_app.sh` | Full build + ad-hoc codesign |

## Notes

- `send_email` actually sends. Everything else is safe or reversible.
- The model proposes; this code decides. `gate.py` is deterministic — no AI involvement in whether an AI is allowed to act.
- Spotify automation uses in-process AppleScript (not subprocess) because `osascript` cannot send keystrokes without Accessibility access attributed to a real app bundle.
