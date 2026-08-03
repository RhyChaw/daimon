# Ollama Action Reliability — Design

**Date:** 2026-06-28
**Status:** Approved design, pending implementation plan
**Branch:** feat/globe-portal-voice

## Problem

The local backend (`llama3.1:8b` via Ollama) is unreliable at emitting correct
action JSON for tool calls. The motivating case: "open a claude code session"
either did nothing or sent the wrong argument.

Evidence from the real audit log (`~/Library/Application Support/Daimon/audit.jsonl`):

- `open_in_claude_code {"project": "Claude"}` → `needs-input` (not a registered alias)
- `open_in_claude_code {"project": ""}` → `rejected-args` (×2, including today)

Two root causes, one already fixed, one remaining:

1. **Silent rejection (FIXED).** `project` was a required arg; an empty value was
   stripped by `_coerce_args`, failed `_valid_args`, and the agent loop dropped it
   with no spoken feedback (`agent.py:823-832`). Fix applied: `project` is now
   optional and the handler raises `NeedsUserInput` (which is spoken) when no usable
   project is given, also tolerating the `"claude"`/`"claude code"` misfire.
   (`mac_agent/actions.py`)

2. **Model emits wrong/empty values (THIS SPEC).** `ollama_client.chat` uses
   `format:"json"` (`ollama_client.py:51`), which guarantees only *valid JSON*, not
   correct *values*. `args.project` is a free string field, and an 8B model given a
   free slot either under-fills it (`""`) or hallucinates a value echoed from the
   request (`"Claude"`).

## Goal

Make the Ollama backend reliably produce correct action JSON, using
`open_in_claude_code` as the worked example but with techniques that generalize to
every action. **Do not switch backends** — the explicit goal is to make the local
model good enough.

### Non-goals

- Switching to the Claude backend (rejected by the user).
- A two-call router architecture (doubles latency on an 8B; not worth it yet).
- Multi-session support (separate, later spec).

## Key insight

The hard part is not JSON syntax — it is *semantic slot-filling from a closed set*.
Small models are bad at "extract the exact token and place it here" but good at
"pick from this list" and "copy this example." The strategy is to stop asking the
model to do the thing it is bad at: constrain closed-set fields, and bypass the
model entirely for high-frequency, unambiguous intents.

The codebase already establishes the pattern: deterministic `_try_*` fast-paths
(`_try_spotify_play`, `_try_remember`, `_try_direct_say`) bypass the model for
reliable intents, and `_redirect_*` fixers (`_redirect_music_action`) correct the
model's plan when it picks the wrong action.

## Design — three levers

### Lever 1 — Deterministic fast-path (highest reliability, zero latency)

Add `_try_open_in_claude_code(user_text)` to `_handle_inner`, mirroring
`_try_spotify_play` (`agent.py:343`, called from the `_try_*` chain at
`agent.py:753+`).

Behavior:

- **Trigger:** text matches a "claude code" intent (e.g. `claude code`, or
  `open`/`start`/`session` near `claude`).
- **Alias scan:** scan `user_text` against the known alias set from
  `_projects.load()`. If **exactly one** registered alias appears as a word in the
  text, fire `open_in_claude_code(project=<alias>)` directly — **no model call**.
- **Trailing prompt:** if the request includes an instruction (e.g. "…and ask it
  to fix the login bug"), extract it and pass as `prompt`.
- **No alias found / ambiguous:** return `False` and fall through to the model path,
  which (via the already-fixed handler) asks "which project?".

Decision (confirmed): when **no** project is named, **always ask** — even if only
one project is registered. The fast-path therefore only fires on an explicit alias
match; it never auto-picks.

Result: "open foundry in claude code" becomes ~100% reliable and instant.
Generalizes: any intent over a closed vocabulary (apps, projects) deserves a
fast-path.

### Lever 2 — Structured outputs with enums (general fix for the model path)

Replace `format:"json"` with a real **JSON Schema** passed to Ollama's `format`
field (Ollama supports full JSON-Schema structured outputs via grammar-constrained
decoding). Build the schema **dynamically** from `ACTION_SCHEMA` + live data, the
same way `_system_prompt` is already built.

Schema shape (single schema per turn, since the model picks one action):

- `action` → `enum` of the action names in `ACTION_SCHEMA` (cannot invent verbs).
- `args` → object with permissive but value-constrained properties:
  - `project` → `enum: [<known aliases>, "unspecified"]`
  - `mode` → `enum: ["term", "terminal"]`
  - other fields (`text`, `query`, `url`, …) → `string`

Because decoding is grammar-constrained, the model **cannot** emit `project:""` or
`project:"Claude"` — it must pick a registered alias or the `"unspecified"`
sentinel, which the already-fixed handler turns into "which project?".

Implementation notes:

- `ollama_client.chat` gains an optional `fmt` parameter; when a schema dict is
  passed it is sent as `format`, else falls back to `"json"`.
- A schema-builder (location TBD in plan — likely `agent.py` near `_system_prompt`,
  or a small helper module) assembles the schema from `ACTION_SCHEMA` and
  `_projects.load()` so the `project` enum always reflects the current registry.
- `OllamaBackend.chat` passes the built schema through.
- One schema covers all actions; it constrains *values*, which is exactly the
  failure mode. Strict per-action arg shapes are explicitly out of scope here.

### Lever 3 — Few-shot examples + redirect safety net (cheap polish)

- Add 2-3 literal input→JSON examples to the system prompt for the tricky cases
  (models copy examples better than they obey prose). The current prompt
  (`agent.py:102-110`) describes the action but shows little literal JSON output.
- Add an `open_in_claude_code` fixer in the spirit of `_redirect_music_action`
  (`agent.py:362`): if the model emits an empty/`"claude"` project but the text
  contains a known alias, substitute it before execution.

## Build order

1. **Lever 1** (fast-path) — smallest change, biggest reliability gain, no latency.
2. **Lever 2** (schema + enums) — generalizes to every action.
3. **Lever 3** (few-shot + redirect) — polish.

## Testing

No pytest in the project; use standalone one-off scripts run via `.venv/bin/python`
(the pattern used to verify the bug fix). Per lever:

- **Lever 1:** unit-test `_try_open_in_claude_code` returns the right
  `(project, prompt)` for representative phrasings ("open foundry in claude code",
  "open a claude code session", "open foundry in claude and fix the login bug"),
  and returns `False`/falls through when no alias is present. Mock or guard the
  actual PTY spawn so tests don't launch real `claude` processes.
- **Lever 2:** unit-test the schema builder produces a schema whose `action` enum
  matches `ACTION_SCHEMA` keys and whose `project` enum matches `_projects.load()`
  plus `"unspecified"`. Integration check (manual, optional): a live Ollama call
  with the schema returns parseable JSON for a sample request.
- **Lever 3:** unit-test the redirect fixer rewrites empty/"claude" project →
  known alias when the alias is in the text.

## Deployment note

The running app is the built bundle (`dist/Daimon.app`). Source changes are picked
up via `daimon start`, which hot-syncs source into the bundle and relaunches (no
full rebuild needed unless `pyproject.toml`/`setup_app.py` change).

## Out of scope / follow-ups

- Multiple concurrent Claude Code sessions with a phone-side picker (separate spec;
  design already discussed: per-session ring buffer with replay, per-client
  independent focus, plus the PTY winsize-vs-mobile tension).
