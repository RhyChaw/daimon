# Step 3 brief — the playback layer

**Start here.** Supersedes `step-2-brief.md`. Written for a fresh session with no
prior context. Parent spec: `2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.

Steps 1 and 2 are committed and green. Step 3 is the heaviest piece of infrastructure
in the plan — steps 7 and 8 both stand on it.

## The problem statement

**There is currently no way to stop an in-flight utterance.**

`speech.py`'s worker thread calls `subprocess.run(["/usr/bin/say", "-f", path])` and
retains no handle. `speak()` checks `_MUTED` once, then enqueues every chunk in
microseconds — long before the worker finishes the first subprocess — so muting
mid-utterance drops 0 of 5 chunks. Measured, not assumed.

The server also has **no idea when speech ends**. `_execute_step` returns as soon as
the text is enqueued, so `handle()`'s `finally` emits `state: idle` while audio is
still playing. Chunking made that lie longer, not shorter.

## What step 3 builds

A playback layer with **handles and completion events**. Two things, both absent today:

1. A handle to whatever is currently speaking, which can be killed.
2. A real speech-ended signal, so `handle()`'s `finally` can emit an honest `idle`.

Fixing that `finally` is in scope and is the reason step 3 comes before anything else.

**The abstraction is "an out-of-process speaker I hold a handle to and can kill."**
That is true of `say` today and true of the Kokoro sidecar in step 4. If the interface
only makes sense for `subprocess.run`, it is wrong.

## What step 3 must NOT do

- **No epoch counter.** No `speech_epoch`, no monotonic generation number.
- **No cancellation policy.** Build the mechanism to stop audio; do not decide when to
  stop it. No barge-in, no stale-chunk dropping, no "drain vs. hard cancel".
- **No VAD, no mic work, no echo/output-device detection.** Step 8.
- **Epoch identity is assigned by the caller in step 7.** Do not bake it into the
  playback layer, and do not have `split_utterance` return anything but plain strings.

This is the bullet a fresh session is most likely to violate. Building a playback layer
with cancellation naturally invites building the cancellation policy at the same time —
which is how a refactor ends up bundled into the correctness-critical commit that step 7
is supposed to be. Resist it.

Also do not add a per-pop `_MUTED` check to get crude cancellation. A second
cancellation path is worse than none, because step 7's epoch counter then has to
reconcile with it.

## The fork — still uncrossed, carry it forward

Reviving `ToolResult.say` means two things. Only one is built.

- **(a) `.say` is origin-tagged system text the agent speaks. Plumbing. DONE in step 2.**
- **(b) `.say` short-circuits the model — calendar and weather answer directly from the
  pre-rendered text with no round-trip. OUT OF SCOPE, STILL.**

(b) kills a model call on the two highest-frequency paths and is most of a latency win.
It is also a real behaviour change on the exact tools Phase 3's panels are built around,
it needs eval coverage it does not have, and bundling it into other work makes a
regression there indistinguishable from a plumbing bug. It is a natural-looking next
line of code. Do not write it.

## `say` vs `announce` — why there are two fields

`ToolResult.say` = "text exists for this result". `ToolResult.announce` = "the agent
should voice it". Different questions; collapsing them crosses fork (b) by accident.

`announce` defaults to `False`, so nothing starts speaking unintentionally.
`read_calendar` keeps its pre-rendered `.say` **built and unspoken** — announcing it
would voice the answer twice, once from the tool and again when the model produces its
final `say` from the same `data`. That unspoken text is the (b) hook.

Announcing today: `_open_app` only.

## Invariants — confirmed by measurement, do not break

1. **Audit is one row per action, not per chunk.** A 5-chunk `say` produces exactly one
   `say` row. Per-chunk rows would also break `_success_note`'s 80-char episodic slice.
   `_open_app` now produces two rows — one `say`, one `open_app` — which is the action
   and the utterance being separately auditable, not per-chunk logging.
2. **`_music_played_this_turn` is turn-scoped.** `handle()` sets it `False` at turn start
   and in `finally`; `_execute_step` sets it `True` once on `play_music` success.
   Per-chunk suppression would let a chunk speak mid-turn and pause Spotify, which is the
   exact thing it exists to prevent.
3. **Origin taxonomy is two values, by authorship not length.** `system` = deterministic
   and pre-rendered; `prose` = model-generated. The step-6 router keys on origin *and*
   length, so origin need not encode "short". If something fits neither, **ask** — do not
   grow the enum.
4. **The WS say payload is a contract.** `{type, text, chunks, origin}`. `text` is the
   whole utterance for the transcript; `chunks` is what gets spoken. The browser must
   never re-implement the splitter and must never truncate — it used to cut at 600 chars.

## The ② constraint — packaging

**Do not change `pyproject.toml` or `setup_app.py`.** `launcher.py` triggers a full
rebuild on either file's mtime; the rebuild changes the binary hash and macOS TCC then
silently drops the Accessibility grant while System Settings still shows it ticked.
Spotify keystrokes stop working with no visible cause.

The TTS engine is therefore a **sidecar process outside the bundle**, over a unix
socket, in its own venv — `pip install kokoro` would otherwise force exactly that
rebuild, and drag PyTorch into a py2app bundle. Precedent: `palace_memory.py` shells out
to `palace` and degrades silently when absent. Same shape: no sidecar → fall back to
`say`.

Confirmed working during step 1, including the case I was least sure of: adding a new
module synced with `Synced 5 Python file(s) into bundle zip (no rebuild needed)`.
Signature survived, Accessibility intact.

If something appears to force a bundle change, **stop and flag it** before making it.

## Testing

Stdlib `unittest`. **No pytest** — adding it to `pyproject.toml` trips the rebuild above.

```
PYTHONPATH=. python3 tests/test_chunking.py        # 23 tests
PYTHONPATH=. python3 tests/test_speech_wiring.py   # 10 tests
```

Silent by construction: `speech._ensure_worker` is patched out so no worker thread
starts and `/usr/bin/say` is never invoked. Keep new tests silent the same way.

The abbreviation list in `chunking.py` is governed by a 100-utterance corpus pulled from
`~/.daimon/memory/attempts.jsonl` and `~/Library/Application Support/Daimon/audit.jsonl`,
not by prose convention. New entries need corpus evidence or an explicit asymmetry
argument.

## Noted for step 5, do not build now

The Calendar permission string — *"Daimon doesn't have Calendar access yet. Open System
Settings, Privacy and Security, Calendars, and turn on Daimon."* — is an instructional
string spoken identically every time access is missing. When the local-tier cache list is
drawn up, it likely wants shortening to the action (*"Daimon needs Calendar access — I've
opened the settings for you"*) with the full path shown in a panel rather than spoken.
