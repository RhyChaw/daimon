# Step 2 brief — origin tagging, ToolResult.say, the _open_app audit hole

> **Superseded by [`step-3-brief.md`](step-3-brief.md).** Step 2 is committed. This
> file is kept as the record of what step 2 was scoped to; the current brief carries
> the fork, the invariants and the packaging constraint forward.

Recovery point. If the working session was summarized, start here, not from memory.

Parent spec: `2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.

## The fork — do not cross it

Reviving `ToolResult.say` means two different things. Only one is in scope.

- **(a) `.say` becomes the origin-tagged system text spoken when the agent decides to
  speak a tool result. Plumbing only. IN SCOPE.**
- **(b) `.say` short-circuits the model — calendar and weather answer directly from the
  pre-rendered text with no round-trip. OUT OF SCOPE.**

(b) is tempting: it kills a model call on the two highest-frequency paths and is most
of a latency win. It is also a real behaviour change on the exact tools Phase 3's
panels are built around, it needs eval coverage, and bundling it into a plumbing
commit makes a regression there indistinguishable from a plumbing bug.

Build (a) so that (b) is a small change later. **Do not make it.** (b) is a
natural-looking next line of code, which is why it is written down here.

## Origin taxonomy — two values, by authorship

| value | meaning |
|---|---|
| `system` | deterministic, pre-rendered: `ToolResult.say`, fast-path utterances, denial and gate messages, step-cap messages, `NeedsUserInput` prompts |
| `prose` | anything the model generated |

Defined by **who authored the text, not by length** — the step-6 router keys on origin
*and* length, so origin need not encode "short". If something does not fit these two,
**ask**; do not grow the enum quietly.

## Step-1 invariants — confirmed, now protected

1. **Audit is one row per action, not per chunk.** A 5-chunk `say` produces exactly 1
   row. Per-chunk rows would also break `_success_note`'s 80-char episodic slice.
2. **`_music_played_this_turn` is turn-scoped.** `handle()` sets it `False` at turn
   start and in `finally`; `_execute_step` sets it `True` once on `play_music` success.
   Per-chunk suppression would let a chunk speak mid-turn and pause Spotify, which is
   the exact thing it exists to prevent.
3. **There is no way to stop an in-flight utterance.** `speak()` checks `_MUTED` once,
   then enqueues every chunk before the worker finishes the first `say`. This is the
   honest current state and it is **step 3's** problem to fix, with a real playback
   layer. Do not add a per-pop `_MUTED` check to get a crude version — a second
   cancellation path is worse than none, because step 7's epoch counter then has to
   reconcile with it.

## The ② constraint — packaging

**Do not change `pyproject.toml` or `setup_app.py`.** `launcher.py` triggers a full
rebuild on either file's mtime; the rebuild changes the binary hash, and macOS TCC
then silently drops the Accessibility grant while System Settings still shows it
ticked. The TTS engine is a **sidecar process outside the bundle** over a unix socket
for exactly this reason. If something appears to force a bundle change, **stop and
flag it** before making it.

Verified live during step 1: adding a new module (`chunking.py`) synced into the
bundle with `Synced 5 Python file(s) into bundle zip (no rebuild needed)`. Signature
survived, Accessibility intact. The new-file case works.

## Step 2 scope

Three pieces, one piece of work:

1. `_open_app` stops speaking via `_speak_async`; it returns `ToolResult(..., say=...)`
   and `_execute_step` emits with `origin="system"`, audits, and speaks it. One
   mechanism closes both the dead-field problem and the audit hole.
2. Fast-path utterances (`agent.py` `_say`) reach the event bus so they exist on both
   surfaces, tagged `system`.
3. `.say` text is written as something a person would say aloud — not a status line.
   These are the utterances the local tier speaks most often and caches in step 5; a
   system message is the same words every time, so a stiff one is stiff a hundred
   times. Leave `.say` unset where the right behaviour is silence (`remember` already
   confirms itself on the fast path); do not invent text to fill the field.

Tests: stdlib `unittest`, no pytest.
`PYTHONPATH=. python3 tests/test_chunking.py` and `tests/test_speech_wiring.py`.
