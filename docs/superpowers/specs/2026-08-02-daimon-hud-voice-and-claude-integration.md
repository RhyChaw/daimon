# Daimon: HUD, voice quality, and Claude Code integration

Status: design spec, not yet implemented
Date: 2026-08-02
Scope: one vacation-length push (~2 weeks)
Related: `docs/superpowers/specs/2026-06-28-ollama-action-reliability-design.md`

---

## 1. What this is

Daimon today is a working agent with a good spine — a code-enforced verb whitelist, a deterministic
Touch ID gate, an append-only audit log, and a three-arm memory ablation. What it is not yet is a
*presence*. It answers when spoken to; it does not sit alongside you while you work.

This spec covers the four changes that close that gap:

1. **A panel HUD** — a registry of pre-rendered UI panels the agent toggles by ID, never by
   generating markup.
2. **Voice that sounds like something** — replacing macOS `say` with a two-tier TTS router.
3. **Barge-in** — the ability to interrupt Daimon mid-sentence and have it stop instantly.
4. **Claude Code as a first-class subsystem** — driven over structured JSON events with hooks,
   instead of scraped out of a PTY.

Explicit non-goals for this push: more verbs, more model backends, mobile, multi-user.

---

## 2. Design principle: the model picks IDs, not content

Every feature below is designed around one constraint that already governs this codebase — an
8.1B local model with a tight token budget. The rule:

> The model's job is to choose from a small enumerated set. It never generates markup, never
> generates layout, and never generates anything whose length scales with the size of what's
> being displayed.

A `show_panel("calendar", {...})` call is ~30 tokens regardless of whether the calendar has two
events or twenty. A model that generates the calendar HTML costs hundreds of tokens and fails
half the time. This is the same instinct behind `_redirect_music_action` and `_coerce_action_args`:
constrain the surface until a weak model can't miss.

Everything in section 3 follows from this.

---

## 3. The panel HUD

### 3.1 Concept

Add two verbs to the whitelist in `mac_agent/actions.py`:

| Verb | Risk | Args | Behaviour |
|---|---|---|---|
| `show_panel` | auto | `panel_id`, `data` (optional dict) | Renders a registered panel into a HUD slot |
| `hide_panel` | auto | `panel_id` or `"all"` | Clears a slot |

`panel_id` is validated against a registry at the same layer that validates verbs. An unknown ID
is rejected by code, not by the model, and produces a corrective hint in step history — same
pattern as `_redirect_email_action`.

### 3.2 Panel registry

Panels live as static HTML fragments plus a tiny data-binding contract. Proposed initial set,
chosen because each one maps to an existing capability:

| `panel_id` | Backed by | Data shape |
|---|---|---|
| `calendar` | `read_calendar` | `{events: [{time, title, done}]}` |
| `weather` | `get_weather` | `{temp, unit, condition, location}` |
| `memory` | `memory.py` fact store | `{facts: [{key, value}]}` |
| `episodic` | `episodic.py` | `{events: [{ts, verb, outcome}]}` |
| `audit` | `audit.jsonl` tail | `{entries: [{ts, verb, decision, auth}]}` |
| `claude_session` | Claude Code stream (§5) | `{status, current_tool, files_touched, tokens}` |
| `now_playing` | `play_music` | `{track, artist, art_url}` |
| `transcript` | voice loop | `{turns: [{role, text}]}` |

Each panel is a `<template>` in `index.html` with `data-bind` attributes. The client clones the
template, fills the bindings from the payload, and mounts it. No `innerHTML` from model output —
this is also the injection boundary, see §7.

### 3.3 Layout

The globe stays as the centre of gravity; it is the one thing that reads as *alive* and it should
not become one widget among many. Panels dock around it rather than replacing it.

```
┌──────────────────────────────────────────────────────────┐
│  DAIMON                                    ● Connected   │
│                                                          │
│  ┌────────────┐                        ┌────────────┐    │
│  │  slot: L1  │        ╭────────╮      │  slot: R1  │    │
│  └────────────┘       │  GLOBE   │     └────────────┘    │
│  ┌────────────┐       │          │     ┌────────────┐    │
│  │  slot: L2  │        ╰────────╯      │  slot: R2  │    │
│  └────────────┘                        └────────────┘    │
│                                                          │
│              ┌──────────────────────────┐                │
│              │      slot: BOTTOM        │                │
│              └──────────────────────────┘                │
│                                                          │
│   [ Ask Daimon something...        ]   ( mic )  ( ↑ )    │
└──────────────────────────────────────────────────────────┘
```

Slot assignment is client-side policy, not model choice. The model says `show_panel("calendar")`;
the client decides that `calendar` prefers `L1`, evicts the least-recently-updated panel if the
slot is taken, and animates the swap. Keeping this out of the model saves tokens and avoids a
whole class of "the model put two things in the same slot" bugs.

`transcript` is pinned to `BOTTOM` and always present in voice mode.

### 3.4 Visual direction

The current UI is dark-navy-on-black with a cyan globe and a single blue accent. That reads as
*clean* but generic — it is one accent away from every other AI dashboard. Two suggestions,
neither expensive:

- **Give the accent a job.** Right now blue means "button". Make hue encode agent state, matching
  the globe's existing state machine: idle = the current cyan, thinking = a desaturated violet,
  acting = amber, gate = red, speaking = a brighter cyan. The panel borders and the globe share
  one variable. You get a peripherally-readable status indicator for free, and the interface stops
  looking static.
- **Type is doing nothing right now.** `DAIMON` in letterspaced caps is fine as a wordmark, but the
  panels should not be default system sans. A condensed technical face for panel labels and
  numerics against the body face gives the HUD an instrument-panel character that a rounded UI
  font can't. Keep it to two faces.

The signature element is already the globe. Spend the boldness there — the state-reactive shader
you have is the memorable thing — and keep every panel quiet, hairline-bordered, and dense.
Resist adding a second animated centrepiece.

### 3.5 Panels the agent shows *unprompted*

The Jarvis quality is anticipation, not obedience. Once the registry works, add a small
deterministic rule layer (no model call) in the WS server:

- Claude Code session starts → auto-show `claude_session`
- Any `confirm`-risk action proposed → auto-show `audit`
- First utterance of the day → auto-show `calendar` + `weather`
- Music starts → auto-show `now_playing`, auto-hide after 20s

These are `if` statements. They cost zero tokens and produce most of the perceived intelligence.

---

## 4. Voice: TTS and barge-in

### 4.1 Why `say` has to go

`say` is instant and robotic. Hosted neural TTS is expressive and adds a network round-trip to the
critical path. Neither is right alone, so route between them.

### 4.2 Two-tier TTS router

```
utterance ──> classify ──┬── short / system / ack  ──> local engine  (~0 ms first byte)
                         └── prose / explanation    ──> hosted engine (streaming)
```

**Local tier — Kokoro.** 82M parameters, runs on CPU at roughly 0.7× realtime on an M1 Air,
installs with `pip install kokoro`, Apache-2.0. Fourteen built-in voices, no cloning. It sounds
flat — which is exactly right for "opening Spotify" or "waiting for Touch ID". Piper is the
fallback if Kokoro's startup cost annoys you; it's smaller, flatter, and faster.

**Hosted tier — pick one, both are fine.** ElevenLabs if you want the best voice and don't mind
the price. Fish Audio S2 Pro is the closer analogue for this use case: sub-150ms latency,
emotion tags, self-hostable, ~$5.50/mo hosted. Either must be used in **streaming** mode; a
batch API call defeats the entire purpose.

Do **not** use Chatterbox, StyleTTS2, Tortoise, GPT-SoVITS, Dia, or Higgs. They're the highest-
quality open models and all of them want 8–24GB of VRAM you don't have on a laptop, and Tortoise
takes minutes per sentence. Those tools are for voiceover production, not conversation.

**Packaging — sidecar, not bundled.** The engine runs as a long-lived process *outside* the app
bundle, in its own venv, speaking over a local unix socket. It exposes `speak` / `stop` /
`status`, and it both synthesises and plays.

This is not a preference, it's forced. `pip install kokoro` means a `pyproject.toml` change, and
`launcher.py` triggers a full rebuild on that file's mtime. Full rebuild → new binary hash →
ad-hoc signature changes → macOS TCC silently invalidates the Accessibility grant, which is the
failure mode `app_main.py` already documents. Spotify keystrokes stop working while System
Settings still shows the checkbox ticked. Kokoro also pulls PyTorch — 2GB+ into a py2app bundle,
plus a playback library as a second native extension.

The sidecar sidesteps all of it: the bundle gains zero native dependencies, `pyproject.toml`
doesn't change, no rebuild, no re-grant. It follows the precedent already in the codebase —
`palace_memory.py` shells out to `palace mem recall` and degrades silently when it's absent.
Same shape here: **no sidecar → fall back to `say`.**

If Kokoro's startup cost is a problem, prefer an ONNX build over the PyTorch one — but that is
now a sidecar-internal detail, which is the point.

**Playback is server-side.** Python owns synthesis *and* playback; the browser stops being an
audio device. Three interfaces exist (REPL, Daimon.app, web UI) and browser-side playback gives
voice to only one of them — the `.app` is the surface holding the bundle identity and the TCC
grants, so it cannot be the mute one. Cancellation becomes a local process kill instead of a WS
round-trip, and we avoid streaming synthesised PCM outbound over the WebSocket. The interrupt
*signal* still arrives inbound over the WS, since the mic is browser-side, but that's one small
message on localhost.

Keep the browser `speechSynthesis` path behind a flag as a degraded fallback for remote/Tailscale
access where there is no local audio device. Do not delete it.

Accepted cost: we lose `getUserMedia`'s built-in `echoCancellation` as a freebie. That was
already out of scope — echo handling this phase is §4.4 option 1.

**Implementation notes**

- The router is deterministic: utterance under N characters, or originating from a fast-path/
  system message, goes local. Everything else goes hosted. No model call to decide.
- Warm the hosted connection at session start. Cold TLS on the first utterance is the thing users
  perceive as "it's slow".
- Cache local-tier audio for the fixed system phrases. There are maybe 30 of them.
- Fall back to local on any hosted error, and log it. Never fail silent into no audio.

### 4.3 Sentence chunking

Today `say` receives whole utterances and queues them. That is the direct cause of the
non-interruptible feel. Replace with:

1. A splitter emits complete sentences, or clauses at 120+ chars.
2. Each chunk is synthesised and enqueued for playback independently.

**Correction — chunking buys interruptibility, not latency.** An earlier draft of this section
claimed time-to-first-audio stops depending on total response length. That is not achievable
here, because **neither backend streams**: `ollama_client.py` sets `"stream": False` with
`"format": "json"`, and `anthropic_client.py` is a blocking `urlopen`. More fundamentally the
model does not emit prose — it emits `{"action":"say","args":{"text":"…"}}`, and the text only
exists once the whole JSON object has parsed. There is nothing to stream into.

So what chunking delivers in Phase 1 is a **clean place to stop between sentences**, which is
what barge-in stands on. Measure Phase 1 against "can we stop cleanly mid-utterance", not
against TTFA.

The latency win arrives in Phase 4, where the Claude Code `stream-json` control path genuinely
streams. **Do not design anything now to accommodate future streaming.** When we get there, the
cheap version is to emit the say text as the first key in the action object and parse partial
JSON. Noted as a future option; build nothing for it.

### 4.4 Barge-in

The hard part, and the reason it doesn't work today.

**Epoch counter.** Maintain a monotonically increasing `speech_epoch` in the WS server. Every
chunk of synthesised audio, every in-flight synthesis request, and every playback handle is
tagged with the epoch it was created under. On barge-in:

```
speech_epoch += 1
cancel all in-flight synthesis requests
stop playback immediately (not "after current chunk")
drop every queued chunk with a stale epoch
mark the interrupted turn in episodic log as INTERRUPTED with the char offset reached
```

Anything tagged with a stale epoch is discarded on arrival. This is the only reliable pattern —
attempting to "drain the queue politely" always leaves a trailing sentence.

**Detection.** Run VAD on the mic *while* Daimon is speaking. Two thresholds to avoid false
positives from background noise and from Daimon's own voice:

- energy above threshold for ≥ 150ms continuous, AND
- the transcript partial is non-empty (i.e. it's speech, not a door slam)

**The echo problem.** Right now voice mode mutes `say` to avoid doubling. Once TTS is real audio
through speakers with the mic open, Daimon will hear itself and interrupt itself in a loop. Three
options in increasing order of effort:

1. **Assume headphones.** Detect output device; if it's the built-in speaker, fall back to
   push-to-talk. Ship this first — it's a 20-line check and it's honest.
2. **Half-duplex with a wake gesture.** Mic closed while speaking; spacebar or a wake word
   reopens it. Loses true barge-in but never false-triggers.
3. **Acoustic echo cancellation.** Correct, and a project of its own. Not this push.

Ship (1), design the interface so (3) can slot in later.

### 4.5 Latency budget

**These are Phase 4 targets, not Phase 1 exit criteria.** Per the §4.3 correction, no backend
streams today, so time-to-first-audio cannot be moved by chunking alone. Do not gate Phase 1 on
the table below.

Target: **< 800ms** from end-of-speech to first audible syllable, p50.

| Stage | Budget | Lever |
|---|---|---|
| Endpointing | 200–400ms | VAD tuning; this is the biggest and cheapest win |
| Final STT | 100–300ms | already acceptable, leave alone |
| Model TTFT | 200–600ms | streaming; warm model; see §4.6 |
| First audio | 50–150ms | local tier for the opening chunk |

### 4.6 The multi-step problem

`MAX_AGENT_STEPS = 5` means a tool-chaining turn can be five silent model round-trips. This is
the single largest source of "it feels slow", and no TTS change fixes it.

Mitigation, in order of value:

- **Speak between steps.** After step 1 resolves, emit a short local-tier utterance describing
  what's happening ("checking your calendar"). The user hears progress instead of silence. This
  is what makes multi-step agents tolerable and it costs nothing.
- **Instant acknowledgement.** On end-of-speech, before the model is even called, play a cached
  local-tier token. Cancellable by barge-in.
- **Consider dropping to 3 steps** for voice-initiated turns and keeping 5 for typed ones. Voice
  users have far less patience than typed users.

---

## 5. Claude Code integration

### 5.1 Replace the PTY as the control path

`open_in_claude_code` currently spawns a PTY rendered through xterm.js. Keep that — it's a great
*display*. But stop using it as the control path: reconstructing agent state from ANSI escape
codes is fragile and gives Daimon nothing to talk about.

Drive Claude Code through structured output instead:

```
claude -p "<task>" --output-format stream-json
```

Each line is a self-contained JSON event describing a message, tool call, tool result, or status
update. Streaming JSON on stdin lets Daimon feed follow-up turns into the same session. For turns
where you want a typed result back, `--output-format json --json-schema <schema>` returns
schema-conforming output in a `structured_output` field.

Note: `--bare` is recommended for scripted calls, but it requires `ANTHROPIC_API_KEY` or an
`apiKeyHelper` and **skips hooks, MCP config, and CLAUDE.md discovery**. Since §5.2 depends on
hooks, Daimon must run non-bare.

### 5.2 Hooks are how Daimon gets a voice in the loop

Claude Code hooks fire at lifecycle boundaries — `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
`Stop`, `SubagentStop`, `SessionStart`, `PreCompact` — and communicate via exit codes and a JSON
protocol supporting allow / deny / ask decisions.

Wire them to Daimon's WebSocket:

| Hook | Daimon behaviour |
|---|---|
| `SessionStart` | `show_panel("claude_session")`, speak a one-line ack |
| `PreToolUse` (write/bash) | Speak the intent; return `ask`; surface the existing gate overlay |
| `PostToolUse` | Update `claude_session` panel; stay silent unless it failed |
| `Stop` | Speak the summary; offer the obvious next step |
| `PreCompact` | Silent; log token position to the panel |

The `PreToolUse` → `ask` path is the important one. It is architecturally identical to `gate.py`:
deterministic, AI-free, code-enforced. You already believe in this pattern; this extends it across
the boundary into Claude Code.

The mental model worth keeping: **CLAUDE.md persuades, permissions filter, hooks enforce.**

### 5.3 The conversational loop you actually want

The goal — "we work through everything together" — decomposes into three concrete behaviours:

1. **Narration.** Daimon speaks tool intent before execution, not after. Requires §5.2.
2. **Elicitation.** Claude Code stops and asks; Daimon speaks the question and routes your spoken
   answer back in over stdin. Requires §5.1 streaming input.
3. **Interruption.** You say "no, stop" mid-narration; Daimon kills the turn. Requires §4.4 —
   and note the barge-in epoch must also cancel the pending hook decision, defaulting to *deny*.

Default-deny on interruption is a safety property, not a convenience. Write the test for it.

### 5.4 Subscription accounting

Agent SDK usage, `claude -p`, and third-party app usage all draw from the same subscription usage
limits as interactive use. A voice loop firing an agent turn per utterance burns limits far faster
than typing. Add per-turn token spend to the `claude_session` panel from day one — you already
have this instinct from the palace-ai token-reduction work; point it at yourself.

**Before using a work-provided Claude Max seat for this**, ask your manager. It will almost
certainly be fine. Discovering it later is a worse conversation than having it in week one.

---

## 6. Evaluation

`run_eval.py` measures task success. Extend it to measure *interaction*, because that's what this
push is actually changing. Hold the model fixed and weak, as before.

New metrics:

| Metric | Definition | Target |
|---|---|---|
| `ttfa_p50` / `ttfa_p95` | end-of-speech → first audible syllable | < 800ms / < 1500ms |
| `false_endpoint_rate` | turns cut off mid-sentence by VAD | < 2% |
| `barge_in_latency` | interrupt detected → audio silent | < 200ms |
| `stale_chunk_leak` | chunks played after epoch bump | 0, hard fail |
| `panel_accuracy` | correct panel shown for a seeded intent | > 90% |
| `narration_precision` | narrated tool matches executed tool | 100%, hard fail |

`stale_chunk_leak` and `narration_precision` are correctness bugs, not quality metrics. Fail the
build on them.

Seed a fixture set of recorded audio turns so this runs without a human in the loop. This is the
piece that makes the whole project legible as engineering rather than as a demo — very few people
have a voice agent with a latency regression suite.

---

## 7. Security and isolation

Two threat surfaces, both new with this push.

### 7.1 Panel injection

Panel payloads flow from tool results (calendar titles, email subjects, file paths) into the DOM.
A calendar event named `<img onerror=...>` is an injection vector, and tool output is exactly the
channel the current threat literature flags for indirect prompt injection.

Rules:
- Panels bind via `textContent`, never `innerHTML`.
- `panel_id` validated against the registry in code before the payload is touched.
- Payload shape validated per-panel against a schema; reject and log on mismatch.
- Nothing from a tool result is ever eval'd, rendered as markup, or used as a URL without
  scheme validation (you already do this for `open_url` — reuse it).

### 7.2 Work/personal partition

This is the part to get right before the co-op starts, not after.

CVRD's core data is PHI. Daimon listens continuously, transcribes through a browser API that
sends audio to a third party, appends to `attempts.jsonl` and `audit.jsonl` in plaintext,
injects the last-3 episodic events into future prompts, and recalls arbitrary past context via
`palace mem recall`. Each of those is a good design decision for a personal assistant and a
reportable incident on a machine touching benefits data.

Required before the laptop touches work data:

- **Directory whitelist**, not blacklist. Daimon sees an explicit list of personal paths. Work
  repos, work Mail, work Calendar are not on it.
- **Local STT on work days.** The browser SpeechRecognition path sends audio off-device. That's
  fine for personal use and not fine with work audio in the room. Whisper.cpp with a small model
  is the drop-in.
- **Push-to-talk, not open-mic, during work hours.** Also solves the echo problem in §4.4.
- **Redact on write, not on read.** Anything reaching `attempts.jsonl` is permanent.
- **A work-mode switch** that enforces all of the above as one toggle, defaulting to on during
  business hours.

Raise this proactively with your manager. "I have a personal voice agent, here's how it's
isolated from company data" is a strong first-week conversation.

---

## 8. Sequencing

Two weeks, in dependency order. Each phase ends shippable.

**Phase 1 — voice quality (days 1–4)**
Sentence chunking → origin tagging → playback layer → TTS sidecar → local Kokoro tier →
hosted tier + router → warm connections.
*Done when:* an utterance can be stopped cleanly between sentences, and the server emits a real
speech-ended signal instead of assuming idle. **Not** "first audio starts before the model
finishes generating" — see the §4.3 correction; that exit criterion was unachievable without
streaming backends and moves to Phase 4.

**Phase 2 — barge-in (days 4–6)**
Epoch counter → VAD-while-speaking → output-device detection with push-to-talk fallback →
`stale_chunk_leak` test.
*Done when:* you can talk over Daimon and it shuts up within 200ms, ten times in a row.

**Phase 3 — panel HUD (days 6–10)**
Registry + two verbs → templates + binding → slot policy → state-reactive accent →
unprompted rules.
*Done when:* the 8B model reliably picks the right panel ID across the seeded intent set.

**Phase 4 — Claude Code (days 10–13)**
`stream-json` control path → `claude_session` panel → hooks → narration → spoken elicitation →
interrupt-cancels-hook with default deny.
*Done when:* you can say "run the tests", hear what it's about to do, and say "no" to stop it.

**Phase 5 — evals (day 13–14)**
Recorded fixtures, the metrics table in §6, hard failures wired into the build.

Cut Phase 3 before Phase 4 if time runs short. The Claude Code loop is the thing you'll use
every day; the panels are the thing that looks good in a screenshot.

---

## 9. Open questions

- Does the hosted TTS tier justify its network hop, or is Kokoro-everywhere good enough once
  chunking is in? Measure before committing to a subscription.
- ~~Should `speech_epoch` be global or per-surface?~~ **Resolved: global, owned by the server.**
  Per-surface was only ever forced by browser-side playback, and §4.2 moves playback server-side.
- Where does spoken input to Claude Code get gated? An utterance routed into a live coding session
  is a different risk class from `say`, and the current whitelist doesn't model it.
- Is `MAX_AGENT_STEPS` the right knob, or should voice turns get a wall-clock budget instead of a
  step budget?

