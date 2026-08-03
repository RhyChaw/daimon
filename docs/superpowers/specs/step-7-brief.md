# Step 7 brief — barge-in and the epoch counter

**Start here.** This is the entry point. Supersedes `step-4-brief.md`. Written for a fresh
session with no prior context — everything step 7 needs is in this file. Parent spec:
`2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.4.

Steps 1–6 are committed and green (78 tests). Step 6 was **decided, not built** — see
[`step-6-decision.md`](step-6-decision.md): no hosted tier, one backend.

## What step 7 is

Let the user interrupt Daimon mid-sentence and have it stop, immediately and completely.

The mechanism is built. `Utterance.stop()` has existed since step 3 and gained a sidecar
implementation in step 4. **It has never had a caller.** Step 7 adds the first one, and
adds the policy that decides when to call it.

## Read this first: `stop()` is shipped, tested, and unproven

`speech.Utterance.stop()` drops queued chunks unspoken and stops the one playing. It is
covered by tests — stopping before playback starts, stopping the chunk in flight,
idempotency, stopping an already-finished utterance, bounded escalation, dead-sidecar
completion — and it was exercised by hand against both `say` and a live Kokoro sidecar.

**But no production code path has ever invoked it.** Step 7 is the first caller in anger.
Treat it as *tested but not proven in situ*, not as a path with mileage. The interesting
cases are the ones tests approximate rather than reproduce: `stop()` arriving from the WS
thread while the worker sits in `handle.wait()`, and `stop()` racing a chunk that is
mid-spawn. Both are handled by design; neither has met real timing under load.

## The measured numbers — grounded, not asserted

§4.4 sets the target at "shuts up within 200 ms". That budget is now **measured on both
backends**, on the BlackHole loopback harness.

| | measured |
|---|---|
| `say` — signal → process exit | 2.5–3.9 ms, terminate and kill indistinguishable |
| `say` — signal → **audible silence** | **≤ ~150 ms**, two independent methods agreeing |
| `say` — SIGKILL wedging the audio device | does not happen |
| **Kokoro — mid-playback `stop()`** | **2–7 ms** |
| **Kokoro — mid-synthesis `stop()`** | 139–146 ms, and **produces no audio at all**, `cancelled=True` |
| Kokoro — warm exec→audible | 729 ms median (527 ms fixed + 7.36 ms/char) |

**The 200 ms budget is met with room on both backends.** Barge-in latency is not the risk
in step 7; correctness of the cancellation path is.

Mid-synthesis is bounded by synthesis finishing rather than by the stop path, and no audio
is produced in that window — so the user hears silence, which is the desired outcome.

### How to re-measure, if you change the stop path

Process exit is **not** the quantity of interest — an engine hands samples to CoreAudio
and can exit or acknowledge while buffered audio still plays. Measure the output stream.

- Route playback to **BlackHole 16ch** (`say -a 96`; device id from `say -a '?'`; the
  sidecar takes `--device`). Capture the same device with
  `ffmpeg -f avfoundation -i ":4" -ac 1 -ar 48000 -f s16le -`. Loopback silence is
  bit-exact zero, so "last audible sample" is unambiguous, nothing is audible, and no
  microphone permission is involved.
- Align bytes to wall time by **timestamping ffmpeg's stdout in a reader thread**. Do
  **not** poll the capture file's size: it drifted 9.8 s over a 76 s run because ffmpeg
  buffers its writes, and produced numbers that looked plausible and were wrong.
- Cross-check with an alignment-free method: sweep the cut point, confirm audio *duration*
  tracks it 1:1. Step 3 measured slope 1.052, step 4 measured 1.007, against an expected
  1.000. Two independent methods agreeing is what makes a number worth quoting.

## The sidecar protocol — four MUSTs, all measured

`mac_agent/scripts/tts_sidecar.py`, newline-delimited JSON over a unix socket. These are
not style preferences; each was violated at some point and the violation was measured.

**MUST 1 — `stop(id)` marks `id` cancelled whether or not synthesis has started.** A
request cancelled at any point before its first sample produces **no audio**. `ack` means
*accepted*, not *started*, so `stop` legitimately races synthesis start. A stop that
no-ops on "not currently playing" would let the chunk speak anyway and defeat the
`late_stop` re-check in `speech.Utterance._play`. Verified: 0/3 trials produced audio.

**MUST 2 — the completion frame means the output device has DRAINED**, not that `write()`
returned. **Live hazard, measured, not theoretical.** `stream.write()` returns when data
is *queued*; the tail is still in the device buffer. A naive implementation that reports
done there leads drain by the entire audio duration, `_PENDING` hits zero, `wait_idle()`
returns, and `handle()` emits `state: idle` over live audio — reintroducing exactly the
premature idle step 3 existed to remove. Verified: 0/145 warm frames arrived early.

**MUST 3 — `stop` discards buffered audio via `stream.abort()`.** **Live hazard, measured,
not theoretical.** The 2–7 ms figure holds *only* because `abort()` throws away audio
already handed to the device. Setting a flag meaning "queue nothing further" leaves the
existing buffer draining and produces a number that looks fine and is fiction — the same
class of error as step 3's file-size polling.

**MUST 4 — a second concurrent `speak` is REJECTED, not queued.** Queuing inside the
sidecar creates a second place speech is pending, and `wait_idle()` would return with
audio still queued across the socket. The speech worker serialises today, so this cannot
happen — which is exactly why the protocol must state it, because "cannot happen" stops
being true silently. The player falls back to `say` for a rejected chunk rather than
dropping it.

## Bounded-stop is RECURSIVE

`stop()` is bounded so it cannot hang: ask, wait for the acknowledgement, then escalate —
close the socket, complete the handle, kill the sidecar.

**Then the transport it used to be bounded introduced a hang of its own.** Phase C bug 3:
`KokoroHandle._close()` called `self._rf.close()` from the stopping thread while the
reader thread was blocked in `readline()` on that same buffered reader.
`io.BufferedReader.close()` acquires the lock the blocked read holds — deadlock. That is
MUST 3's bounded-stop requirement one level down, in the transport. The fix is
`socket.shutdown(SHUT_RDWR)`, which needs no lock: the in-flight recv returns EOF and the
reader closes what it owns.

**Every layer that can block must be bounded, and the transport layer is a layer.** When
adding any blocking call beneath `stop()`, ask what bounds it. "The layer above has a
timeout" is not an answer — the layer above is waiting on the thing that deadlocked.

## Socketpair tests are necessary and NOT sufficient

**All three Phase C bugs passed the in-process socketpair tests and failed against a real
subprocess.** Every one lived in the gap between "both ends of a socket in one process"
and "a real subprocess with a real read loop":

1. `speak` ran inline on the connection's read loop, so the mid-playback stop frame was
   never read; the player burned its grace, escalated, and **killed a healthy sidecar**.
   614 ms against a 150 ms bar.
2. `stop()` waited for `done` rather than the `stopped` ack. `done` cannot arrive until
   synthesis finishes (~600 ms), so every mid-synthesis stop blew the grace and shut down
   a working process. `done` must also settle the stop-ack, or `stop()` blocks its full
   grace waiting for a frame that is already moot — 502 ms measured when `done` had landed
   at ~50 ms.
3. The `_close()` deadlock above.

An in-process fake has no separate read loop to block, answers instantly so no grace is
ever blown, and is not usually blocked in `readline()` when you close it.

**Keep the socketpair tests — they are fast, silent, and they pin the protocol. But
anything touching `stop()`, readiness, or the completion contract must get an end-to-end
run against a live sidecar before it is believed.** Step 7 adds the first real caller to
`stop()`; it will exercise these paths under real timing, and it must verify end-to-end.

## The epoch design — from §4.4, carry it exactly

Maintain a monotonically increasing `speech_epoch` in the WS server. **Every in-flight
synthesis request, every chunk of synthesised audio, and every playback handle is tagged
with the epoch it was created under.** On barge-in:

```
speech_epoch += 1
cancel all in-flight synthesis requests
stop playback immediately (not "after current chunk")
drop every queued chunk with a stale epoch
mark the interrupted turn in the episodic log as INTERRUPTED with the char offset reached
```

**Anything tagged with a stale epoch is discarded on arrival.** This is the only reliable
pattern — "drain the queue politely" always leaves a trailing sentence. Hard cancellation,
explicitly not a graceful drain.

Constraints on it:

1. **Epoch identity is assigned by the caller.** Do not push it down into the playback
   layer, and do not make `split_utterance` return anything but plain strings.
2. **There must be exactly one cancellation path.** This is why steps 3 and 4 both refused
   to add a per-pop `_MUTED` check for crude cancellation — a second path would have to be
   reconciled with the epoch counter. Keep it at one.
3. **Audit stays one row per action, not per chunk.** An interrupted utterance is marked on
   the existing row (§4.4 wants `INTERRUPTED` with the char offset reached), not logged as
   extra rows.

## The lock discipline you must not break

`speech.Utterance._play` — the shape and the reason:

1. under `_lock`: if `_stopped`, return; the chunk is dropped unspoken;
2. **release the lock**, call `_PLAYER.play(text)`. Deliberate: `play()` may be slow — a
   socket round trip — and holding the lock across it would leave `stop()` blocked behind
   exactly the call it is trying to cancel;
3. re-acquire, re-read `_stopped` into `late_stop`, publish `self._current = handle` only
   if not stopped. A `stop()` landing in the gap saw `_current is None`, so it could not
   have stopped this handle;
4. if `late_stop`: `handle.stop()` immediately, return;
5. otherwise `handle.wait(CHUNK_TIMEOUT)` **outside** the lock, then clear `_current`.

**The client-side re-check cannot cover the whole window.** `play()` returns on socket
*ack*, meaning the request was accepted — synthesis has not started. So `stop()` can reach
the sidecar before the request begins. That window is covered on the **sidecar side** by a
request-id-keyed cancelled-set (MUST 1), and only there. Both halves are required.

## The one-producer invariant, and what it does not protect

`speech.speak()` records the thread owning the pending set and **raises RuntimeError** if a
second thread enqueues while speech is in flight. Ownership is per-pending-set, not
per-process: a quiet queue can be claimed by whoever speaks next, so sequential use from
different threads is fine. Only genuine concurrency trips it.

Why it exists: `wait_idle()` waits on *all* queued speech and `handle()` treats that as
"this turn's speech". Those are the same set only because there is exactly one agent
thread. A second producer breaks the equivalence **silently** — `wait_idle` returns on
someone else's speech and `handle()` emits the premature idle this layer prevents.

> **The guard protects the queue; it does not protect the audio device.**

The sidecar owns a buffer the agent process cannot observe, which is the same epistemic
position a second producer creates. That is why MUST 2 exists and why it is measured
rather than asserted. If step 7 introduces a second producer, **replace the guard with
something scoped — do not delete it.**

Related trap, and the obvious way to write it: fallback must never re-enter
`speech.speak()` from the worker thread. That call is not the pending set's producer, so
the guard raises, `_run_one`'s `except Exception: pass` swallows it, the chunk is silently
dropped, and the accounting still balances so nothing looks wrong. Backend selection lives
inside `FallbackPlayer.play()`.

## The browser trap that will deadlock barge-in — write this test first

`index.html`'s `serverBusy` latch, lines 786–790:

```js
if(msg.state==="idle"){ serverBusy=false; _maybeResume(); }
else if(msg.state!=="listening"){ serverBusy=true; }
```

**Anything that is not `idle` or `listening` latches `serverBusy` true.** `_maybeResume()`
requires `convoMode && awaitingReply && !speaking && !serverBusy`. Without a *following*
`idle`, `_maybeResume` never fires and **the mic never reopens** — barge-in deadlocks the
conversation after exactly one use.

So an interrupted turn must still reach `state: idle`. An interrupt is not an excuse to
skip the idle emission; it is the case that most needs it.

**The first test to write: ten consecutive barge-ins, asserting the mic reopens every
time.** Not one — the failure is a latch, and a latch survives the first interaction
looking fine.

## Two preconditions that are now live

Both were narrow-window races before step 4 and are normal-use hazards now that
server-side playback is the norm and turns have lengthened.

**(e) Convo-mode mid-utterance doubling.** Two independent booleans gate the same text and
**neither is retroactive**: server-side `_MUTED`, read *once at enqueue* (`speech.py`), set
by the `{"type":"voice"}` handler (`ws_server.py:116-123`) and cleared on last-client-drop
(`:124-132`); and browser-side `convoMode`, checked in `speakDaimon` (`index.html:923`).
`_emit_say` (`agent.py:162-175`) broadcasts to every client **unconditionally**.

Tap the mic while the Mac is mid-utterance: chunks enqueued pre-mute stay queued and play,
while later `say` events are also spoken by the browser — two voices in one turn. The
disconnect path mirrors it: `set_muted(False)` with no regard for the browser's in-flight
`speechSynthesis`, and the WS `close` handler (`:775-779`) does **not** call
`_synth.cancel()` — only `stopConvo` does (`:957`).

**This is now a normal-use race, not an edge case.** The enqueue→playback window used to
be a couple of seconds at a turn's end; it now spans the whole turn. Note the fix is *not*
a per-pop `_MUTED` check — that is the second cancellation path constraint 2 forbids.

**(f) The browser's 14-second `fallbackTimer`** (`index.html:905`). `_onUtterance`
(`:900-906`) arms it. If it fires while `speaking === true` it clears `awaitingReply` and
its own `if(convoMode && !speaking)` guard blocks the reschedule, so **both** later callers
of `_maybeResume` no-op — the `done` callback (`:932`) and `state: idle` (`:787`). The
`r.onend` path (`:887`) already fired back at `_onUtterance`'s `_stopListen()`. The
conversation silently stops listening.

**The actual defect is that the 14-second constant was never derived from anything**, and
turn lengths have grown: `serverBusy` now stays true until server-side audio finishes, and
the sidecar adds per-chunk cost. A timer with no relationship to the turn length it bounds
is the bug; step 4 only moved the distribution across it.

## The echo problem — option 1 only

Once TTS is real audio through speakers with the mic open, Daimon hears itself and
interrupts itself in a loop.

**Ship option 1 and only option 1: detect the output device; if it is the built-in
speaker, fall back to push-to-talk.** It is a ~20-line check and it is honest.

Not in this push: half-duplex with a wake gesture, and acoustic echo cancellation. AEC is
correct and is a project of its own. Design the interface so it can slot in later.

VAD detection per §4.4, two thresholds to avoid false positives from background noise and
from Daimon's own voice: energy above threshold for ≥150 ms continuous, **and** a non-empty
transcript partial — speech, not a door slam.

## The regression step 7 is expected to remove

`handle()`'s `finally` blocks on `speech.wait_idle(DRAIN_TIMEOUT)` before emitting
`state: idle`. That is what makes idle honest, and it has an accepted cost: **the REPL
cannot accept the next turn until audio finishes.** You cannot type a second question over
a long answer, and Ctrl-C is the only escape.

That was accepted on the reasoning that queueing a question behind an unheard answer is the
bug, not the feature — and that **barge-in is the correct escape hatch, which is step 7.**
Removing this limitation is part of step 7's job, not a side effect to be discovered.

Do **not** remove it by emitting idle from a waiter thread. That decouples state emission
from `handle()` and creates a second place where completion is observed, which the epoch
counter would then have to reconcile with. Keep it synchronous in the `finally`.

## Open decision inherited from step 4

**Trailing-silence trimming.** Kokoro appends a fixed trailing near-silence: measured
**494 ms fixed + 0.050 ms/char** (and +1.5 ms per second of audio) over 145 warm chunks,
stdev 30 ms, flat across every size bucket. It does not scale, so trimming it in the
sidecar would remove N × ~450 ms of the agent sitting in `wait_idle` after audio is
functionally over — ~10 s on a 22-chunk utterance. Real value; it should get done.

**It requires a fresh loopback measurement of the completion margin before it is applied —
not a code review.** The margin being in the safe direction is the only reason MUST 2
holds; trimming shrinks it to roughly `stream.latency`.

## Constraints that still bind

- **Do not change `pyproject.toml` or `setup_app.py`.** Full rebuild → new binary hash →
  macOS TCC silently drops the Accessibility grant while System Settings still shows it
  ticked, and Spotify keystrokes stop working with no visible cause. Adding a module is
  fine and confirmed three times.
- **Sidecar venv is Python 3.12 at `~/.daimon/tts-venv`.** Do not "fix" it to 3.13: `blis`
  has no cp313 wheel and its source build dies in Cython. The constraint that matters is
  "outside the bundle, not the system interpreter"; 3.12 satisfies it.
- **Locate the sidecar with `resources.script_path()`, never a `__file__` join** — py2app
  puts `mac_agent` inside `Resources/lib/pythonX.Y/`, and the manual join makes the `.app`
  silently use `say` while dev runs look perfect.
- **The WS say payload is a contract:** `{type, text, chunks, origin}`. The browser must
  never re-implement the splitter and must never truncate — it used to cut at 600 chars.
- **`_music_played_this_turn` is read at enqueue time only.** That is what makes the
  `finally` reset safe while chunks are still queued.
- **Origin taxonomy is two values by authorship:** `system` (deterministic, pre-rendered)
  and `prose` (model-generated). If something fits neither, **ask** — do not grow the enum.
- **The fork, still uncrossed:** `.say` short-circuiting the model for calendar and weather
  is **out of scope**. `read_calendar` keeps its pre-rendered `.say` built and unspoken;
  that unspoken text is the hook. Do not write it.

## Testing

Stdlib `unittest`. **No pytest** — adding it to `pyproject.toml` trips the rebuild above.

```
PYTHONPATH=. python3 tests/test_chunking.py        # 23 tests
PYTHONPATH=. python3 tests/test_speech_wiring.py   # 10 tests
PYTHONPATH=. python3 tests/test_playback.py        # 28 tests
PYTHONPATH=. python3 tests/test_sidecar.py         # 17 tests
```

Silent by construction. `test_playback.py` patches `speech._PLAYER` for the whole module
so no test can reach `/usr/bin/say`; `test_sidecar.py` uses `socket.socketpair()` so no
sidecar is spawned and nothing is audible. The two Kokoro tests synthesise without opening
an output device and skip unless `~/.daimon/tts-venv` exists (override with
`DAIMON_TTS_PYTHON`). `_WORKER_STARTED` is a module global, so a single test that starts
the real worker leaves every later test racing a background consumer. Tests that drain the
queue by hand must call `item._release()` — the same accounting path the worker uses.

## Two working rules, learned the expensive way

- **Never pipe a long-running command's output to a file.** `grep` and `tail` both
  block-buffer when stdout is not a terminal, so progress sits unflushed and the file looks
  empty. This cost a completed 155-chunk measurement run, killed on the false belief it had
  wedged, and then cost a second run the same way. Use `stdbuf -oL`, or don't pipe, or
  redirect and `tail` separately. **An empty output file is not evidence of a hang — check
  the process, not the file.**
- **Commit before probes.** A throwaway experiment ran `git checkout mac_agent/playback.py`
  and reverted the file to a HEAD that predated the entire step's work. It was recoverable
  only because the context was still live; an hour later it would not have been.
