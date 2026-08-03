# Step 6 decision — no hosted TTS tier

**Decided 2026-08-03.** Supersedes the hosted-tier half of
[`step-6-brief.md`](step-6-brief.md). Parent spec:
`2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.2.

## The decision

**There is no hosted tier. The router collapses to a single backend.**

§4.2 planned two tiers — Kokoro locally, ElevenLabs or Fish Audio hosted — with a router
choosing between them. Step 4's measurement removed the reason for the second tier, so the
router has nothing left to route between and is not built.

## The evidence

Measured 2026-08-03 on the step-3 BlackHole loopback harness, across **155 real chunks**
from the 97-utterance corpus, warm = calls 11+ (n=145). Cross-checked alignment-free at
slope 1.007 against an expected 1.000.

| | |
|---|---|
| warm exec→audible, median | **729 ms** |
| warm floor | **592 ms** |
| cost model | **527 ms fixed + 7.36 ms/char** |

A hosted tier would have to beat 729 ms *including a network round trip*, and then justify
a subscription on top. Fish Audio's advertised sub-150 ms is synthesis latency on someone
else's hardware without the round trip; comparing it to 729 ms would be exactly the
category error the step-4 brief warns about when it insists on exec-to-audible rather than
synthesis-only numbers.

Local synthesis also has no per-utterance cost, no network dependency, no API key to
manage, and no failure mode where speech stops because a service is down.

## What would reopen this

**Voice quality, from real use.** Sustained complaints that Kokoro sounds wrong, or a
concrete use case needing a voice it cannot produce — another language, a specific
persona, cloning.

**Not latency.** That argument is settled, and re-litigating it needs new measurements
rather than new opinions. If someone proposes a hosted tier on speed grounds, the answer
is the table above.

## What survives from step 6's original scope

- **Origin-based routing already exists and stays.** `{system | prose}`, by authorship not
  length, tagged at `_emit_say` and carried in the WS payload. It was never only about
  picking an engine — what remains of step 6's value is whatever else keys off `origin`.
  The enum does not grow; if something fits neither value, ask.
- **The path-spelling content problem, reassigned.** Kokoro pronounces filesystem paths
  character by character — 17.4 s of audio for a 133-char path that leaked into an error
  message. That is a **splitter** concern, not a router one: paths and URLs want different
  treatment from prose, and a smaller chunk budget would only spell the path across more
  chunks. Detail in [`step-6-brief.md`](step-6-brief.md).
- **The chunk-budget analysis is unaffected** and still lives in `step-6-brief.md`:
  `MAX_CHARS = 120` is too permissive at the tail, the right budget is variable rather
  than a smaller constant, and `pack[len(ps)-1]` means chunk length shifts voice
  conditioning — so it cannot be tuned on latency alone.
