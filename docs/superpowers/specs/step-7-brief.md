# Step 7 carry-forward — barge-in and the epoch counter

**This is not the entry point.** `step-4-brief.md` is. This file exists because a few
facts have to survive two steps to reach the session that sets the barge-in budget, and
the brief chain only carries things one hop reliably. Read the current entry-point brief
first; come here when step 7 actually starts.

Parent spec: `2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.4.

## The stop budget is already met — measured, not assumed

§4.4 sets the target at "shuts up within 200 ms". With `/usr/bin/say` that is **already
true today**, measured during step 3 on 2026-08-02:

| | measured |
|---|---|
| signal → process exit | **2.5–3.9 ms**, terminate and kill indistinguishable |
| signal → **audible** silence | **≤ ~150 ms** |
| SIGKILL wedging the audio device | does not happen |

This disconfirmed a structural prediction that `say` would keep playing buffered audio
after the process died. It does not, materially. `SayHandle.stop()` therefore sends
SIGTERM first and escalates to SIGKILL only if the process lingers — the gentler signal
was not slower.

**Do not treat ≤150 ms as permanent.** It is a property of `say`, not of the playback
layer. If step 4 landed a sidecar, the number belongs to the sidecar's `{"cmd":"stop"}`
and must be re-measured before the 200 ms budget can be claimed.

### How to re-measure it

Process exit is **not** the quantity of interest — an engine hands samples to CoreAudio
and can exit or acknowledge while buffered audio still plays. Measure the output stream.

- Route playback to **BlackHole 16ch**, a virtual loopback device already installed here
  (`say -a 96`; device id from `say -a '?'`). Capture the same device with
  `ffmpeg -f avfoundation -i ":4" -ac 1 -ar 48000 -f s16le -`. Loopback silence is
  bit-exact zero, so "last audible sample" is unambiguous, nothing is audible, and no
  microphone permission is involved.
- Align bytes to wall time by **timestamping ffmpeg's stdout in a reader thread**. Do
  **not** poll the capture file's size: that drifted 9.8 s over a 76 s run because
  ffmpeg buffers its writes, and produced numbers that looked plausible and were wrong.
- Cross-check with an alignment-free method: sweep the cut point, confirm audio
  *duration* tracks it 1:1. Step 3 measured slope 1.052 against an expected 1.000. Two
  independent methods agreeing is what makes the number worth quoting.

## What step 7 inherits

`speak()` returns an `Utterance` with `wait/stop/done`. `Utterance.stop()` drops queued
chunks unspoken and stops the one playing. **It is fully built and has no caller in
`agent.py` — that is deliberate.** Step 3 built the mechanism and explicitly refused the
policy, so that a refactor would not be bundled into the correctness-critical commit
step 7 is supposed to be. Step 7 is where the callers arrive.

**Know what that means: `stop()` has never had a caller.** It is covered by tests —
stopping before playback starts, stopping the chunk in flight, idempotency, stopping an
already-finished utterance, and the temp-file unlink on the stopped path — and it was
exercised by hand against real `say` during step 3. But no production code path has ever
invoked it, and step 7 will be the first to do so in anger. Treat it as *tested but not
proven in situ*, not as a path with mileage on it. In particular the interesting cases
are the ones tests approximate rather than reproduce: `stop()` arriving from the WS
thread while the worker sits in `handle.wait()`, and `stop()` racing a chunk that is
mid-spawn. Both are handled by design — the re-check after `play()` returns, and the
idempotent unlink — but neither has met real timing.

## The regression step 7 is expected to remove

`handle()`'s `finally` blocks on `speech.wait_idle(DRAIN_TIMEOUT)` before emitting
`state: idle`. That is what makes idle honest, and it has an accepted cost: **the REPL
cannot accept the next turn until audio finishes.** You cannot type a second question
over a long answer, and Ctrl-C is the only escape.

That was accepted on the reasoning that queueing a question behind an unheard answer is
the bug, not the feature — and that **barge-in is the correct escape hatch, which is
step 7**. Removing this limitation is part of step 7's job, not a side effect to be
discovered.

Do **not** remove it by emitting idle from a waiter thread. That decouples state
emission from `handle()` and creates a second place where completion is observed, which
the epoch counter would then have to reconcile with. Keep it synchronous in the
`finally`.

## Constraints that still bind

1. **Epoch identity is assigned by the caller.** Do not push it down into the playback
   layer, and do not make `split_utterance` return anything but plain strings.
2. **There must be exactly one cancellation path.** This is why step 3 refused to add a
   per-pop `_MUTED` check for crude cancellation — a second path would have to be
   reconciled with the epoch counter. Keep it at one.
3. **`_music_played_this_turn` is read at enqueue time only**, which is what makes
   `handle()`'s `finally` safe to reset it while chunks are still queued. Moving that
   read to dequeue time makes the reset a live bug. This is the second independent
   reason not to add a per-pop check.
4. **One producer, enforced in `speech.speak()`.** It records the thread owning the
   pending set and raises if a second one enqueues concurrently, because `wait_idle`
   returning on foreign speech reproduces the premature idle step 3 removed. If step 7
   introduces a second producer, replace the guard with something scoped — do not
   delete it.
5. **Audit stays one row per action, not per chunk.** An interrupted utterance should be
   marked on the existing row (§4.4 wants `INTERRUPTED` with the char offset reached),
   not logged as extra rows.
6. **Do not change `pyproject.toml` or `setup_app.py`.** Full rebuild → new binary hash
   → macOS silently drops the Accessibility grant while System Settings still shows it
   ticked, and Spotify keystrokes stop working with no visible cause.

## Still out of scope at step 7

VAD, mic work, and echo/output-device detection are **step 8**. §4.4 ships "assume
headphones, detect output device, fall back to push-to-talk" first; acoustic echo
cancellation is a project of its own and is not this push.
