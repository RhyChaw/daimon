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

## Phase A findings, 2026-08-03 — read before designing the sidecar

Established by reading the packages (`kokoro` **0.9.4**, `misaki` 0.9.4, `kokoro-onnx`
0.4.7) and `playback.py` / `speech.py`, not from memory. These change the design; they are
not background.

> **Version caveat, recorded because it nearly produced wrong findings.** The first pass
> used `pip download --no-binary :all:`, which restricts resolution to *source*
> distributions and silently resolves kokoro to **0.7.16** — `pip index versions kokoro`
> agrees, because it applies the same filter. The real latest is **0.9.4**, wheels only.
> Every conclusion below was re-verified against 0.9.4 as installed. If you need to
> inspect a package, install it and read site-packages; do not trust `--no-binary`
> resolution for "what is the latest version".

### CLOSED QUESTION: no, the engine cannot stream instead of us chunking

This is a natural thing for a later session to reopen. It is answered, with a reason.

`KModel.forward` (`kokoro/model.py:74-108`) is `@torch.no_grad()` and monolithic: it
materialises the whole alignment matrix over the utterance and runs the iSTFTNet decoder
once. **For one input segment the first and last sample become available at the same
instant.** `KPipeline.__call__` *is* a generator, but it yields one result per *segment*,
where a segment is split at **510 phoneme characters** (`kokoro/pipeline.py:188-214`,
`waterfall_last`). `chunking.MAX_CHARS = 120` — 120 chars of English is ~100-130
phonemes, so **one chunk is always exactly one segment**, i.e. one non-streamable forward
pass. The generator is real and useless at this granularity.

`kokoro-onnx` does not change it: `create_stream` (`kokoro_onnx/__init__.py:203-252`) is
an `AsyncGenerator` that yields whole `_create_audio` results per phoneme batch, batched
at the same `MAX_PHONEME_LENGTH` atom, wrapped in an `asyncio.Queue`.

**Chunking is therefore not redundant with engine-level streaming, and step 1's design
survives.** Sub-chunk streaming would mean reaching past both public APIs into the
decoder. That is a different project, not a step-4 option.

### Startup cost is not just the model, and there is a per-call cost that scales

`KModel.__init__` downloads `config.json` + `kokoro-v1_0.pth` from HF and loads 82M
params. But `KPipeline.__init__` for `lang_code='a'` also constructs `misaki.en.G2P`
(`kokoro/pipeline.py:108`), whose `__init__` (`misaki/en.py:492-500`) does
`spacy.load('en_core_web_sm')` — downloading it on first use if absent — plus `json.load`
of `us_gold.json` (2.96 MB) and `us_silver.json` (3.09 MB), each then grown. Cold start is
torch + transformers + spaCy + ~6 MB of JSON + the weights.

Per voice: `load_single_voice` (`pipeline.py:128-141`) downloads and `torch.load`s a
`.pt`, memoised. First use of a voice pays it; later uses are a dict hit.

Per call: every `__call__` runs a full spaCy pipeline pass (`misaki/en.py:537`) plus
lexicon lookup plus the forward pass, whose cost scales with predicted *audio duration*.
So per-chunk cost is fixed-component + token-scaling + duration-scaling. **`say`'s cost
was almost purely fixed. These are different curves** — which is why the go/no-go is
measured across the corpus length distribution and not at one test string.

Also: `pack[len(ps)-1]` (`pipeline.py:224`) indexes the voice pack by phoneme count, so
style conditioning is itself length-dependent. Short and long chunks are not the same
voice running faster or slower. That is a voice-consistency argument against very short
chunks, independent of latency.

### The espeak-ng silent-truncation bug — this is the 600-char bug wearing a new hat

`KPipeline.__init__` catches any `EspeakFallback` construction failure, logs a warning,
and proceeds with `fallback=None` and **`en.G2P(..., unk='')`** — unchanged in 0.9.4 at
`kokoro/pipeline.py:108-113`. With `unk=''`, an
out-of-dictionary word phonemises to the empty string and **vanishes from the audio**.
Calendar event titles, contact names, app names, project names. It fails silently, which
is worse than failing loudly, and it is the same class as the 600-char browser truncation
that invariant 6 exists to prevent.

Two rules, both MUST:

- **The sidecar must never run with `unk=''`.** If `EspeakFallback` fails at sidecar
  startup, **the sidecar refuses to start** and the player falls back to `say`. A sidecar
  that starts successfully and drops words is the worst outcome available — worse than no
  sidecar, because nothing surfaces.
- **Regression guard:** a test speaking a deliberately out-of-dictionary token — a
  made-up proper noun — asserting audio is produced. Same family as step 1's preservation
  test.

**And the default path is broken here, which makes this worse than a missing dependency.**
kokoro 0.9.4 pulls `espeakng-loader`, which ships its own `libespeak-ng.dylib` plus data,
and `misaki/espeak.py` wires it up at import. On this machine that dylib has the **build
machine's data path baked in** and ignores both `EspeakWrapper.set_data_path()` and
`ESPEAK_DATA_PATH`:

```
Error processing file '/Users/runner/work/espeakng-loader/espeakng-loader/
espeak-ng/_dynamic/share/espeak-ng-data/phontab': No such file or directory.
```

The bundled `espeak-ng-data/phontab` *is* present — it is the dylib that will not look at
it. **espeak's C code then exits the process rather than raising**, so a `try/except`
around `EspeakFallback(...)` does not catch this.

Verified fix — point the wrapper at the brew install. **Order matters and is
counter-intuitive:** `misaki/espeak.py` calls
`EspeakWrapper.set_library(espeakng_loader.get_library_path())` at *module import time*,
so doing this before the import is silently overwritten and still crashes. It must happen
**after** importing `misaki.espeak` and **before** `KPipeline` constructs its
`EspeakFallback`:

```python
from misaki import espeak                      # this import resets the library path
espeak.EspeakWrapper.set_library('/opt/homebrew/lib/libespeak-ng.dylib')
espeak.EspeakWrapper.set_data_path('/opt/homebrew/share/espeak-ng-data')
from kokoro import KPipeline                   # only now is it safe to build the pipeline
```

Measured working: `Zorbulon` → `zˈɔɹbjʊlən`. Without it, that word becomes `''`.

So the startup check cannot be "did the constructor raise". **It must be a positive
assertion: phonemise a known out-of-dictionary word and require non-empty output**, in a
context where a hard process exit is also treated as failure. That is the same shape as
the regression test, which is not a coincidence — the test and the startup guard should
share the OOD token.

`brew install espeak-ng` is a required system/sidecar dependency (installed here
2026-08-03, eSpeak NG 1.52.0). It touches neither `pyproject.toml` nor `setup_app.py`.

### The sidecar protocol — MUSTs, not implementation notes

**1. `stop(id)` marks `id` cancelled whether or not it has started.** A request that finds
itself cancelled at any point before its first sample produces **no audio**.

This is forced, not stylistic. `play()` returns on socket *ack*, which means the request
was accepted — synthesis has not run. So `stop()` can arrive before synthesis starts, and
a stop that no-ops on "not currently playing" lets the chunk speak anyway. That defeats
the `late_stop` path at `speech.py:154-155`, which step 3 built specifically to prevent
this. Step 3's single re-check splits into two obligations:

- *client side, unchanged*: the `late_stop` re-check after `play()` returns still closes
  the window where `stop()` saw `_current is None`;
- *sidecar side, new*: a request-id-keyed cancelled-set. **The agent-side re-check cannot
  cover this**, because acceptance and synthesis-start are different moments on the far
  side of the socket.

Step 7 is the first caller of `stop()` in anger and will hit this on turn one.

**2. The completion frame must mean the output device has drained** — not "I wrote the
last sample to a buffer". `playback.py:14-17` already records this hazard for `say`; it
applies identically to completion, and it is worse across a process boundary. If it is
wrong, `handle.wait()` returns early → `_PENDING` hits zero → `wait_idle` returns →
`handle()` emits `state: idle` over live audio. That is the exact bug step 3 removed.
**This is a claim, so it gets measured on the loopback** — see the go/no-go harness.

**3. `stop()` must be bounded.** `SayHandle.stop()` cannot hang: worst case it kills the
process (`playback.py:105-118`, `_STOP_GRACE = 0.25`). A socket `stop()` that waits on a
wedged sidecar hangs forever, on the WS thread, in step 7. Same escalation shape: ack
timeout → close the socket → kill the sidecar process.

**4. `stop()` is called from a thread that is not the one in `wait()`.** `Utterance.stop`
(`speech.py:124-134`) calls `current.stop()` from the caller's thread while the worker is
blocked in `handle.wait()` (`speech.py:158`). So a single half-duplex socket shared by
`wait` and `stop` is wrong — the writer contends with a blocked reader. Either one
connection per chunk plus a separate control connection, or one multiplexed connection
with a demultiplexing reader thread keyed on request id. The per-chunk-connection shape is
the direct analogue of "`SayHandle` owns its process".

**5. `done` must stay a non-blocking predicate.** `proc.poll()` is non-blocking; a socket
handle cannot do a blocking `recv` there. Natural mapping: a `threading.Event` set by
whatever reads the completion frame; `done = event.is_set()`, `wait = event.wait(timeout)`.

**6. `stop` discards buffered audio — `stream.abort()`, not "stop writing".** Measured:
45–51 ms stop→silence *only because* `abort()` throws away audio already handed to the
device. Setting a flag that means "queue nothing further" leaves the existing buffer
draining and would have produced a number that looked fine and was fiction.

**This is the same class of error as step 3's file-size polling** — a measurement of a
proxy that tracks the real quantity right up until it doesn't, producing plausible wrong
numbers. Step 3's version drifted 9.8 s over 76 s. Both are recorded together because the
failure mode is the same one, not because the mechanisms are related.

**7. Concurrency: reject, do not queue.** One shared `OutputStream` means two in-flight
`speak` requests contend for one device. The speech worker serialises today so it cannot
happen — but the protocol must still say what the sidecar does, because "cannot happen"
is exactly the kind of assumption that stops being true silently.

**The sidecar rejects a second concurrent `speak` with a busy error.** Not queuing:
queuing inside the sidecar creates **a second place where speech is pending**, and the
one-producer reasoning in `speech.py` stops holding — `wait_idle()` would return with
audio still queued on the far side of the socket, which is protocol MUST 2's failure
arriving by another route. A busy error is loud and surfaces the broken assumption; a
queue hides it.

### Fallback shape — one rule, and the failure it prevents

**Backend selection happens INSIDE `Player.play()`, which returns a `SayHandle` on
fallback. `speech.speak()` is never re-entered from the worker thread.**

The obvious way to write §4.2's "no sidecar → fall back to `say`" is
`except SidecarUnavailable: speech.speak(text)`. Trace what that does: the call happens on
the `daimon-say` worker, which is **not** `_PRODUCER`, with `_PENDING > 0` — so the
one-producer guard raises `RuntimeError` (`speech.py:244-261`), and `_run_one`'s
`except Exception: pass` (`speech.py:203-204`) **swallows it**. The chunk is silently
dropped, the utterance is silently truncated, the accounting stays correct so nothing
looks wrong, and the guard's carefully-written diagnostic is never seen by anyone.

**Second rule: the sidecar is mute until asked, including for its own errors.** No "model
loaded", no "voice ready", no spoken error. Errors go over the socket as data. Nothing the
sidecar decides to say on its own initiative can ever reach the output device — such audio
never touches `_PENDING`, so the one-producer guard cannot see it, and `wait_idle` returns
while it is audible.

### Where the one-producer invariant actually breaks

Swapping `SayPlayer` for a sidecar `Player` adds **no** second producer through the queue.
The guard keys on `threading.get_ident()` at `speak()`; `_PLAYER.play()` runs on the
worker, which never calls `speak()`. Invariant 4 holds by construction.

It breaks through the **handle**, at the completion contract — protocol MUST 2 above. If
`wait()` returns on ack rather than on drain, `_PENDING` reaches zero while the sidecar's
buffer is still audible, `wait_idle()` returns, and `handle()` emits idle over live audio.
Same failure as a second producer, arriving where no guard is watching:

> **The guard protects the queue; it does not protect the audio device.**

The sidecar owns a buffer the agent process cannot observe, which is the same epistemic
position a second producer creates. **The guard needs no scoped replacement — it needs a
peer.** And because a protocol saying "done" is a claim, the peer is a measurement, not an
assertion.

### Environment pins

- **Sidecar venv is Python 3.12.** System `python3` here is 3.14.5. 3.13 fails: `spacy` →
  `thinc` → `blis` has no cp313 wheel and its source build dies in Cython
  (`CompileError: blis/py.pyx`). 3.12 installs clean. Verified working set: kokoro 0.9.4,
  misaki 0.9.4, torch 2.13.0, numpy 2.5.1, spacy 3.8.14. (The `numpy==1.26.4` hard pin
  that constrained 0.7.16 is **gone** in 0.9.4 — the interpreter constraint is spaCy's,
  not numpy's.)
- **`espeak-ng` must be installed *and explicitly wired up*** — see the silent-truncation
  section. Installed here 2026-08-03: eSpeak NG 1.52.0, `/opt/homebrew/bin/espeak-ng`.
- Kokoro's own deps carry **no audio output library** — it does not play audio at all.
  §4.2's "it both synthesises and plays" is a requirement on the sidecar you write.
  Sample rate is **24000 Hz**.
- **The sidecar must play in-process** (sounddevice/CoreAudio), not by writing a WAV and
  shelling out to `afplay`. A socket sidecar needs no temp files at all — all of
  `_track`/`_untrack`/`_LIVE_PATHS`/the `atexit` sweep (`playback.py:48-71`) is
  `SayPlayer`-specific. Shelling out to `afplay` inherits the whole temp-file problem
  **and loses the remedy**, because the `atexit` sweep lives in the agent process and
  cannot reach files the sidecar created.

### GO/NO-GO RESULT, measured 2026-08-03 — verdict (a), with a budget caveat

Harness: step 3's, re-implemented from `step-7-brief.md:30-45`. BlackHole 16ch loopback,
ffmpeg `avfoundation :4` s16le mono 48k, stdout timestamped in a reader thread. Corpus:
97 real utterances from `attempts.jsonl` + `audit.jsonl` → **155 chunks** via
`split_utterance`. Warm := calls 11+ (n=145). **Alignment-free cross-check: duration vs
cut-point slope 1.007 against expected 1.000** (step 3 got 1.052), so the two methods
agree and the numbers are quotable.

| Quantity | Measured | `say` baseline |
|---|---|---|
| cold, process launch → ready | **6.56 s** (≈3.5 s imports, ≈3.0 s model+G2P+voice) | n/a |
| first 10 calls after ready, exec→audible | median **751 ms**, max 1126 ms | 1350 ms |
| **warm exec→audible** | min 592, p25 681, **median 729**, p75 800, p90 989, max 2334 ms | 1350 ms |
| stop → audible silence, mid-playback | **45–51 ms** | ≤150 ms |
| stop mid-*synthesis* → audio produced | **0 / 3 trials** | n/a |
| completion frame vs last audible sample | **0 / 145 early**; +406/+500/+566 ms | n/a |

**Per-chunk cost decomposes as `527 ms fixed + 7.36 ms/char`** (linear fit over 145 warm
chunks). `say` was ~1350 ms almost entirely fixed. That difference is the whole result:

| chunk size | warm exec→audible (median) |
|---|---|
| 0–19 ch (n=37) | 677 ms |
| 20–39 ch (n=68) | 727 ms |
| 40–59 ch (n=22) | 812 ms |
| 60–79 ch (n=7) | 989 ms |
| 80–99 ch (n=4) | 1125 ms |
| 100–119 ch (n=5) | 1288 ms |
| 120+ ch (n=2) | 2218 ms |

**Verdict (a): the warm floor is well under 1.35 s at real chunk lengths, so chunking was
right and the engine swap is justified.** The corpus median chunk is **35 characters**,
costing ~727 ms — 1.9× faster than `say`.

**But the 120-char budget is the one regime where the sidecar buys nothing.** At 120 chars
a chunk costs 527 + 883 = **1410 ms**, i.e. *worse* than `say`'s 1350 ms. The sidecar's
advantage is a function of chunk length and it disappears exactly at the ceiling we set.
Measured speech rate is 54.8 ms of audio per char, which gives the regression arithmetic:

| case | `say` unchunked | `say` chunked | sidecar chunked |
|---|---|---|---|
| 5 × 35 ch (the realistic case) | 10.9 s | 16.3 s (+49%) | **13.5 s (+24%)** |
| 3 × 60 ch | 11.2 s | 13.9 s (+24%) | **12.8 s (+14%)** |
| 5 × 120 ch (at the ceiling) | 34.2 s | 39.6 s (+16%) | 39.9 s (+17%) — **no gain** |

So the shipped regression roughly halves, and interruptibility is retained. It does **not**
vanish: every chunk boundary still costs 527 ms of dead air instead of 1350 ms. Chunking
remains a net loss versus not chunking; it is now an affordable one.

### MAX_CHARS = 120 was a guess, and the data says it is too permissive

Recorded as a wrong asserted number, not as "guidance". **120 was chosen in step 1 from
prose-splitting convention, with no data behind it.** The measurement says the sidecar's
advantage evaporates exactly at that ceiling: 1.9× faster than `say` at the corpus median
of 35 chars, and *nothing* at 120. A number asserted from convention failed against
measurement.

**This is the second time in this project that an asserted number failed against data.**
The first was the prediction that `say` would keep playing buffered audio after the
process died — disconfirmed in step 3, which is why `SayHandle.stop()` sends SIGTERM
first. Both were reasonable-sounding and both were wrong. The pattern is the point: assert
numbers as hypotheses and measure them.

### DECISION: MAX_CHARS stays at 120 through step 4

Not a deferral — a decision, with reasons:

- **Step 4 is one variable.** Swapping the engine and re-tuning the splitter in the same
  step destroys the attribution two phases of measurement just earned.
- **The right budget is not a single number.** The `fixed + per-char` curve says first
  chunk small (time-to-first-audio dominates) and later chunks large (amortisation
  dominates). That is a *variable-budget splitter* — a step 1 revision, not a constant
  change.
- **The tail is a splitter gap, not a budget gap.** 4/145 warm chunks exceeded 1350 ms,
  and two of those (133 ch) *already exceed* `MAX_CHARS` because `_split_long` found no
  clause boundary to cut at and returned the chunk whole.

Everything needed to tune it later is carried in `step-6-brief.md`.

**Budget data from the curve.** Only 4/145 warm chunks exceeded the 1350 ms baseline
(87, 115, 133, 133 chars) — the 133s are chunks `_split_long` could not break because no
clause boundary existed, so they exceed `MAX_CHARS` already. The budget's real job is tail
control, and lowering it is cheap because it is almost never binding:

- first-audio ceiling → budget: ≤900 ms → 51 ch, ≤1000 ms → 64 ch, ≤1100 ms → 78 ch
- chunks re-split at a lower budget: 120→2, 100→6, **80→11**, 60→20 (of 155)

A budget near **80** caps worst-case first audio around 1.1 s while re-splitting only 11
of 155 chunks. Note the opposite pressure, which is why this is guidance and not a
decision: because cost is `fixed + per-char`, *larger* chunks amortise better (22.4 ms/char
at 35 ch vs 11.8 ms/char at 120 ch), so shrinking the budget adds 527 ms boundaries. The
principled shape is a **small first chunk and larger subsequent ones** — fast time-to-first-
audio, fewer boundaries after. That is a step-4-completion design decision, not a
measurement.

### Two implementation constraints the spike proved the hard way

- **One long-lived `sd.OutputStream`, never `sd.play()` per request.** `sd.play()` opens
  and tears down a PortAudio stream each call; doing that across a few hundred requests
  from a few hundred threads **killed the process with no Python traceback** — reproduced
  at 155 and 12 chunks, not at 3, so it is a race, not a threshold. A persistent stream
  also makes stop honest.
- **`stop` must `abort()` the stream, not just stop writing.** The 45–51 ms figure holds
  only because `abort()` discards already-buffered audio. Flagging "write no more" would
  leave the buffer draining and measure a fiction.
- **`write()` returning is not drain.** `stream.write()` returns once data is queued; the
  tail is still in the device buffer. The spike sleeps `stream.latency` before sending
  `done`, which is why 0/145 frames arrived early. Protocol MUST 2 is therefore confirmed
  as a live hazard, not a theoretical one — the naive implementation leads drain by the
  entire audio duration.

### The 6.56 s cold start is a design problem, not a footnote

It is the largest number in the run and longer than most turns. The first utterance after
app launch either waits on it or falls back to `say` — and falling back means **the first
thing Daimon says after every launch sounds different from everything after it.** That is
worse than being slow, because it is a recurring, noticeable inconsistency rather than a
one-time delay.

**DECISION: eager start with `say` fallback, and readiness is a queryable state.**

- The sidecar is launched at agent startup, not on first `speak()`.
- It warms itself with a synthesis it discards, so "ready" means ready to hit the warm
  numbers, not merely "process alive".
- Until it signals ready, `Player.play()` returns a `SayHandle`. The voice changes once,
  mid-session, in the first seconds after launch.
- **Readiness must be a real state the player can query — not a race that resolves by
  timeout.** The transition has to be observable, because a voice change that happens for
  unobservable reasons is indistinguishable from a bug.

Rejected: blocking the first `speak()` for up to N seconds. A blocking first utterance is
the failure users notice most, and it converts a cosmetic inconsistency into dead air.

### Measure the trailing silence once, in Phase C

The completion margin was +406/+500/+566 ms, **inflated by Kokoro's trailing near-silence**
— "last non-zero sample" precedes "buffer drained" by however much silence the model
appends.

One question, one measurement: **is that trailing silence fixed-length or proportional to
utterance length?**

- *Fixed* → trimming it in the sidecar before playback is free, and removes N × ~450 ms of
  the agent sitting in `wait_idle` after the audio is functionally over.
- *Proportional* → leave it alone.

**Do not trim on a guess.** The margin being in the safe direction is the only reason
protocol MUST 2 holds today; trimming blindly is the fastest way to turn a satisfied
invariant into a violated one.

### Step 4 must-do list

- **`handle.wait()` at `speech.py:158` has no timeout.** Safe for `say` — a local binary
  that always terminates, per `speech.py:64-68`. Unsafe for a socket: an unresponsive
  sidecar hangs the worker thread forever and `DRAIN_TIMEOUT = 120.0` is the only backstop.
  That is exactly what the comment at `speech.py:66-67` anticipated lowering **with
  evidence**, and the go/no-go measurement produces that evidence.

## The two preconditions — why this is not just an engine swap

Both are **live bugs that step 3 neither introduced nor changed**. Step 3 is bit-identical
in browser voice mode, because under mute nothing is queued server-side and `wait_idle`
returns immediately. Both go from narrow-window to normal-use the moment playback moves
server-side and mute stops being the norm.

**Corrected 2026-08-03: an earlier draft of this brief said both were unreachable today.
That was wrong for both.** Each is reachable now through a narrow timing window; what
step 4 changes is the width of the window, not the existence of the bug. The corrected
framing matters because "unreachable today" invites deferring them, and "reachable but
rare, about to become common" does not.

**(e) Convo-mode mid-utterance doubling.** There are two independent booleans gating the
same text, and **neither is retroactive**: server-side `_MUTED`, read *once at enqueue*
(`speech.py:239`), set by the `{"type":"voice"}` handler (`ws_server.py:116-123`) and
cleared on last-client-drop (`ws_server.py:124-132`); and browser-side `convoMode`,
checked in `speakDaimon` (`index.html:923`), set in `startConvo` (`:944`). `_emit_say`
(`agent.py:162-175`) broadcasts to every client **unconditionally**, independent of mute.

*Reachable today:* tap the mic while the Mac is mid-utterance. Chunks enqueued pre-mute
stay in `_QUEUE` and play; `say` events after that point are also spoken by the browser;
two voices overlap within one turn. The disconnect path is the mirror — `set_muted(False)`
(`ws_server.py:127-130`) with no regard for the browser's in-flight `speechSynthesis`
queue, and the WS `close` handler (`index.html:775-779`) does **not** call
`_synth.cancel()`; only `stopConvo` does (`:957`). So the browser keeps speaking while the
Mac resumes.

*What step 4 changes:* today the enqueue→playback window is a couple of seconds at the end
of a turn, so the collision needs a mic tap at exactly the wrong moment. Once the sidecar
makes server-side playback the norm, honest idle plus per-chunk sidecar latency stretch
that window across **the entire turn**. A gate read once at enqueue, against a queue that
drains over the turn's full length, is a race that fires in normal use.

Note the fix is *not* a per-pop `_MUTED` check — invariant 3 and step 7's
single-cancellation-path constraint both forbid it. This needs a design decision.

**(f) The browser's 14-second `fallbackTimer`** (`index.html:905`). `_onUtterance`
(`:900-906`) sets `awaitingReply=true; serverBusy=true` and arms it. If it fires while
`speaking===true`: it clears `awaitingReply`, and its own `if(convoMode && !speaking)`
guard fails so it does **not** schedule listening. `_maybeResume` (`:910-916`) requires
`awaitingReply`, so **both** later callers no-op — the `done` callback at `:932` and
`state: idle` at `:787`. `r.onend` (`:887`) would also restart listening, but it already
fired back at `_onUtterance`'s `_stopListen()` while `awaitingReply` was still true, and
will not fire again. **The conversation silently stops listening.** The `serverBusy` latch
(`index.html:786-790`) is correct and needs no change.

*Reachable today:* browser TTS on a long answer can exceed 14 s from the end of the user's
utterance. *What step 4 changes:* `serverBusy` now stays true until server-side audio
actually finishes rather than clearing when the model stops, and sidecar per-chunk cost
lengthens every turn — both push the turn-time distribution across the threshold.

**The actual defect in (f) is that the 14-second constant was never derived from
anything.** A timer with no relationship to the turn length it is supposed to bound is the
bug; step 4 only moves the distribution across it. This is a step-4-completion problem,
not a step-4-measurement problem.

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
