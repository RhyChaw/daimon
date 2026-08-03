# Step 4 brief — the TTS sidecar

**Start here.** Supersedes `step-3-brief.md`. Written for a fresh session with no
prior context. Parent spec: `2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.2.

Steps 1–3 are committed and green (61 tests).

## Read this first: step 4 fixes a regression, it is not a quality upgrade

The sidecar was originally framed as packaging plus voice quality — avoid the rebuild,
get a better voice. That framing is wrong and it under-sells the work.

**Step 1 traded 4.9 s of dead air on a five-chunk utterance for interruptibility, and
nobody measured it.** `say` costs ~1.35 s from exec to first audio, and chunking spawns
one `say` per chunk, so a five-chunk utterance went from 12.6 s to 17.5 s — a **39%
regression on total utterance wall time**, shipped three commits ago and only found in
step 3.

Process spawn per chunk is the cost. A long-lived engine is the remedy. **Nothing else
in the plan addresses it.** That changes what step 4 has to prove: not "does Kokoro
sound better" but **"does per-chunk latency drop enough to justify chunking at all"**.

### Go/no-go, measure this first — not at the end

**What is the per-chunk cost floor for a warm sidecar?** Synthesis plus playback, per
chunk, with the engine already loaded. If it lands anywhere near 1.35 s, chunking is a
net loss and the 120-char chunk budget in `chunking.py` has to be reconsidered before
any of the rest of step 4 is built.

This is a go/no-go on the whole chunking design, so it comes first. The step-3 harness
measures it directly — see "Measuring audible latency" below.

Step 4 is also **not** just an engine swap for a second reason: two latent bugs go live
the moment playback moves server-side. Both are recorded below.

## What exists after step 3

`mac_agent/playback.py` — the out-of-process speaker behind a handle:

```
Player.play(text) -> handle          starts playback, returns once started
handle.wait(timeout=None) -> bool    True if finished, False on timeout
handle.stop()                        idempotent, safe from any thread
handle.done                          non-blocking predicate
```

`mac_agent/speech.py` — the queue, the accounting, and `Utterance`:

```
speak(text) -> Utterance             non-blocking; never returns None
Utterance.wait/stop/done             one speak() call, stoppable as a unit
wait_idle(timeout) -> bool           nothing queued and nothing playing
pending()                            chunks queued or in flight
DRAIN_TIMEOUT = 120.0
```

`agent.handle()`'s `finally` calls `_await_speech()` — a bounded `wait_idle` — before
emitting `state: idle`. That is the honest idle. No new WS message type was added; the
observable change is purely *when* `state: idle` fires.

**Step 4's job is to add a second `Player`.** `SayPlayer` stays as the fallback, per the
`palace_memory.py` precedent: no sidecar → fall back to `say`. Nothing above the Player
boundary should need to change.

## Measured on 2026-08-02, carry these numbers

Captured through a BlackHole loopback device, because process exit is not the quantity
of interest — `say` hands samples to CoreAudio and can exit while buffered audio plays.

| Quantity | Measured |
|---|---|
| signal → process exit | 2.5–3.9 ms (terminate and kill alike) |
| signal → **audible** silence | ≤ ~150 ms, two independent methods agreeing |
| SIGKILL wedging the audio device | does not happen; interleaved trials stayed clean |
| `say` exec → first audio | ~1.35 s default device, ~1.82 s targeting a named device |
| 5-chunk utterance vs one process | **+4.9 s of dead air** (12.6 s → 17.5 s) |

Two consequences for step 4:

- **≤150 ms already fits step 7's 200 ms budget**, so the sidecar's `{"cmd":"stop"}`
  does not have to beat a signal — it has to *not be much worse*. That is the bar the
  sidecar protocol must clear, and it is measured, not assumed.
- **The ~1.35 s startup is the headline**, per the reframing at the top of this brief.
  Do not try to fix it inside `SayPlayer`; it cannot be fixed there.

### Measuring audible latency — re-run this, do not re-derive it

Process exit is **not** the quantity to measure. `say` hands samples to CoreAudio and
can exit while buffered audio still plays; the same will be true of any sidecar that
owns its own output buffer. Measure the output stream.

Method that worked, and the one that did not:

- `say -a 96` plays into **BlackHole 16ch**, a virtual loopback device already installed
  on this machine; ffmpeg captures the same device (`avfoundation -i ":4"`) as raw s16le
  mono 48k. Loopback silence is bit-exact zero, so "last audible sample" is unambiguous,
  nothing is audible, and no microphone permission is involved.
- Align byte offsets to wall time by **timestamping ffmpeg's stdout in a reader thread**.
  An earlier harness mapped bytes to wall-clock by polling the capture *file's size*;
  that drifted **9.8 s over a 76 s run** because ffmpeg buffers its writes, and would
  have poisoned the budget had it been trusted.
- Cross-check with a method that needs no alignment at all: sweep the cut point and
  confirm audio *duration* tracks it 1:1. Measured slope 1.052 against an expected
  1.000. Two independent methods agreeing is what makes the number trustworthy.

The same harness measures the go/no-go above: a warm sidecar's per-chunk floor is just
first-audible-sample minus request time.

## The two preconditions — why this is not just an engine swap

Both are **live bugs that step 3 does not trigger**, because under mute nothing is
queued server-side and `wait_idle` returns immediately — step 3 is bit-identical in
browser voice mode. Both fire the moment playback moves server-side and mute stops
being the norm.

**(e) Convo-mode mid-utterance doubling.** `ws_server.py`'s `{"type":"voice"}` handler
calls `set_muted()`, and mute is checked once at *enqueue*. Entering convo mode while an
utterance is already queued leaves those chunks playing on the Mac while the browser
speaks the same text — doubled audio. The disconnect `finally` has the mirror problem:
it unmutes on last-client-drop with no regard for what is playing. Today mute is a
global boolean with no notion of what is in flight; once the server owns playback that
is no longer good enough.

**(f) The browser's 14-second `fallbackTimer`** (`index.html:905`). It clears
`awaitingReply`, and reschedules listening only `if(convoMode && !speaking)`. If it
fires while speech is still going, `awaitingReply` is already `false`, so the later
`_maybeResume()` — from both `done` and `state: idle` — no-ops, and **the conversation
silently stops listening**. Pre-existing, but honest idle plus server-side playback
lengthens turns and widens the window. The `serverBusy` latch (`index.html:786-790`) is
correct and needs no change; this timer is the broken part.

Also inherited, noted and left alone in step 3: `term`/`term_open` stomp the orb state to
`acting`/`listening` regardless of speech (`index.html:799, 808-812`); WS close resets to
idle with no knowledge of server-side audio (`index.html:779`); process exit kills the
daemon worker mid-utterance and orphans the `say` child.

**Temp files on abrupt exit — partially fixed, know the remainder.** Neither unlink path
runs if the interpreter dies while the worker is blocked in `handle.wait()`, because the
worker is a daemon thread and is killed outright. Reproduced every time, on both the old
`subprocess.run`/`finally` shape and the new handle. `playback.py` now tracks live temp
paths and sweeps them with `atexit`, which covers ordinary exit (`exit`, EOF, Ctrl-C):
verified 1 leak → 0. **SIGKILL still leaks one file per interrupted utterance and nothing
can cover that.** If the sidecar owns its own temp files, it inherits the same problem
and the same partial remedy.

## The accepted regression from step 3

`handle()` blocking on `wait_idle` means **the REPL cannot accept the next turn until
audio finishes**. You can no longer type a second question over a long answer. This was
accepted deliberately: queueing a question behind an unheard answer is the bug, not the
feature, and the correct escape hatch is barge-in in **step 7**. Until then, Ctrl-C is
the only out. Do not "fix" it by emitting idle from a waiter thread — that decouples
state emission from `handle()` and creates a second place where completion is observed,
which step 7's epoch counter would then have to reconcile.

## What step 4 must NOT do

- **No epoch counter, no cancellation policy, no VAD.** Still steps 7 and 8.
  `Utterance.stop()` exists and has no caller in `agent.py` by design. Adding one is
  step 7's job.
- **No per-pop `_MUTED` check.** Two independent reasons, below.
- **Do not change `pyproject.toml` or `setup_app.py`.** See the packaging constraint.

## Invariants — do not break

1. **Audit is one row per action, not per chunk.** A 5-chunk `say` produces exactly one
   `say` row. Step 3 deliberately added no completion row even though it now could.
2. **`_music_played_this_turn` is turn-scoped.** `handle()` clears it at turn start and
   in `finally`; `_execute_step` sets it once on `play_music` success.
3. **`_music_played_this_turn` is read at enqueue time only.** That is what makes the
   `finally` reset safe while chunks are still queued. Moving that read to dequeue time
   makes the reset a live bug. **This is the second independent reason not to add a
   per-pop `_MUTED` check** — the first is that a second cancellation path has to be
   reconciled with step 7's epoch counter. The pair is recorded together on purpose.
4. **One producer — enforced in code, not documented.** `wait_idle()` waits on *all*
   queued speech and `handle()` treats that as "this turn's speech". Those are the same
   set only because there is exactly one agent thread. **A sidecar that pushes its own
   audio, or any background narrator, breaks the equivalence silently** — `wait_idle`
   returns on someone else's speech and `handle()` emits exactly the premature idle
   this layer exists to prevent.

   Because that failure is silent, `speak()` records the thread that owns the pending
   set and **raises RuntimeError** if a second thread enqueues while speech is in
   flight. Ownership is per-pending-set, not per-process: a quiet queue can be claimed
   by whoever speaks next, so sequential use from different threads stays fine (the
   REPL thread differs between `cli.py` and the `.app`). Only genuine concurrency trips
   it, which is the harmful case.

   **This is the invariant step 4 is most likely to violate.** If the sidecar needs to
   push its own audio, do not delete the guard — `wait_idle` needs a scoped replacement,
   and that is a design decision worth surfacing rather than silently losing.
5. **Origin taxonomy is two values, by authorship not length.** `system` = deterministic
   and pre-rendered; `prose` = model-generated. If something fits neither, **ask** — do
   not grow the enum.
6. **The WS say payload is a contract.** `{type, text, chunks, origin}`. The browser must
   never re-implement the splitter and must never truncate — it used to cut at 600 chars.

## The fork — still uncrossed, carry it forward

- **(a) `.say` is origin-tagged system text the agent speaks. Plumbing. DONE in step 2.**
- **(b) `.say` short-circuits the model — calendar and weather answer directly from the
  pre-rendered text with no round-trip. OUT OF SCOPE, STILL.**

(b) kills a model call on the two highest-frequency paths and is most of a latency win.
It is also a real behaviour change on the exact tools Phase 3's panels are built around,
it needs eval coverage it does not have, and bundling it into other work makes a
regression there indistinguishable from a plumbing bug. `read_calendar` keeps its
pre-rendered `.say` **built and unspoken** — that unspoken text is the (b) hook.
Announcing today: `_open_app` only. It is a natural-looking next line of code. Do not
write it.

## The ② constraint — packaging

**Do not change `pyproject.toml` or `setup_app.py`.** `launcher.py` triggers a full
rebuild on either file's mtime; the rebuild changes the binary hash and macOS TCC then
silently drops the Accessibility grant while System Settings still shows it ticked.
Spotify keystrokes stop working with no visible cause.

This is exactly why the TTS engine is a **sidecar outside the bundle**, over a unix
socket, in its own venv. `pip install kokoro` would force that rebuild and drag PyTorch
into a py2app bundle. Precedent: `palace_memory.py` shells out to `palace` and degrades
silently when absent.

Adding a new module is fine and confirmed twice — step 1 and step 3's `playback.py` both
synced with `Synced N Python file(s) into bundle zip (no rebuild needed)`, signature
intact. If something appears to force a bundle change, **stop and flag it** first.

## Testing

Stdlib `unittest`. **No pytest** — adding it to `pyproject.toml` trips the rebuild above.

```
PYTHONPATH=. python3 tests/test_chunking.py        # 23 tests
PYTHONPATH=. python3 tests/test_speech_wiring.py   # 10 tests
PYTHONPATH=. python3 tests/test_playback.py        # 28 tests
```

Silent by construction. `test_playback.py` patches `speech._PLAYER` for the whole module
so no test can reach `/usr/bin/say` even by accident, and drives `speech._run_one` — the
worker's actual loop body — instead of starting the real worker thread. Do the same for
the sidecar: a fake Player, never a live socket. Note that `_WORKER_STARTED` is a module
global, so a single test that starts the real worker leaves every later test racing a
background consumer.

Tests that drain the queue by hand must call `item._release()`, the same accounting path
the worker uses. There is deliberately no reset hook — it would be a second way to reach
the same state and would hide the drift the accounting exists to catch.
