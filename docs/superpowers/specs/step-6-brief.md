# Step 6 carry-forward — the chunk budget and the TTS router

**This is not the entry point.** `step-4-brief.md` is. This file exists because the step-4
measurement produced everything needed to tune the chunk budget, and that tuning is
deliberately *not* step 4's job. Read the current entry-point brief first; come here when
the budget or the router is actually being worked on.

Parent spec: `2026-08-02-daimon-hud-voice-and-claude-integration.md` §4.2, §4.3.

## Why the budget was not changed in step 4

`chunking.MAX_CHARS = 120` was chosen in step 1 from prose-splitting convention, **with no
data behind it**. The step-4 measurement shows it is too permissive at the tail. It was
still left alone, deliberately:

- Step 4 is one variable. Re-tuning the splitter in the same step as the engine swap
  destroys the attribution the measurement earned.
- The right budget is **not a single number** — see the counter-pressure below.
- The worst offenders are a *splitter* gap, not a budget gap: two 133-char chunks already
  exceed `MAX_CHARS` because `_split_long` found no clause boundary and returned the chunk
  whole (`chunking.py:94-105`).

## The measured constants — 2026-08-03, Kokoro 0.9.4 sidecar

BlackHole loopback harness, 155 chunks from the 97-utterance corpus, warm = calls 11+
(n=145). Cross-checked alignment-free, slope 1.007 vs expected 1.000.

**Per-chunk exec→audible = `527 ms fixed + 7.36 ms/char`.**

For contrast, `/usr/bin/say` was ~**1350 ms, almost entirely fixed**. That difference is
what turns "which engine is faster" into a chunk-size design equation.

Measured speech rate: **54.8 ms of audio per character.** Synthesis is far faster than
realtime, so the per-char term is overhead, not a floor.

### First-audio ceiling → implied budget

| ceiling | max chars |
|---|---|
| ≤ 900 ms | 51 |
| ≤ 1000 ms | 64 |
| ≤ 1100 ms | 78 |
| ≤ 1350 ms (the `say` baseline) | 112 |

### Cost by chunk size (warm medians)

| size | median | | size | median |
|---|---|---|---|---|
| 0–19 ch (n=37) | 677 ms | | 80–99 ch (n=4) | 1125 ms |
| 20–39 ch (n=68) | 727 ms | | 100–119 ch (n=5) | 1288 ms |
| 40–59 ch (n=22) | 812 ms | | 120+ ch (n=2) | 2218 ms |
| 60–79 ch (n=7) | 989 ms | | | |

### How much a lower budget would actually re-split

| budget | chunks affected (of 155) |
|---|---|
| 120 (today) | 2 |
| 100 | 6 |
| 80 | 11 |
| 60 | 20 |

Corpus median chunk is **35 characters**. The budget is almost never binding — its real
job is tail control, which is why lowering it is cheap.

## The counter-pressure — why smaller is not simply better

Because cost is `fixed + per-char`, **larger chunks amortise the fixed cost better**:

- at 35 chars: (527 + 258) / 35 = **22.4 ms per character**
- at 120 chars: (527 + 883) / 120 = **11.8 ms per character**

Every chunk boundary costs 527 ms of dead air. Shrinking the budget adds boundaries.

So there are two opposed forces inside one utterance:

- **Time-to-first-audio** is set by the *first* chunk alone → argues for a small first chunk.
- **Total wall time** is set by the *number* of boundaries → argues for large later chunks.

The principled shape is therefore a **variable budget: small first chunk, larger
subsequent ones.** That is a step 1 splitter revision, not a change to a constant, which
is the other reason it was not done in step 4.

## The constraint that stops this being a pure latency optimisation

**`pack[len(ps)-1]` — the voice pack is indexed by phoneme count** (`kokoro/pipeline.py`,
`KPipeline.infer`). The style vector conditioning the synthesis is selected by the length
of the phoneme string.

Consequence: **short and long chunks are not the same voice running faster or slower — they
carry different style conditioning.** A variable budget shifts voice conditioning *between
chunks within a single utterance*, which is exactly where a listener would notice it.

- Latency argues for small first chunks.
- Voice consistency argues against variance in chunk length.

Whoever tunes this needs both numbers in front of them. Do not optimise the budget purely
against the latency curve — validate by listening to a multi-chunk utterance under any
proposed variable scheme, and treat audible voice drift across chunk boundaries as
disqualifying however good the latency looks.

## Also relevant to the router

§4.2 puts a hosted tier alongside the local one, keyed on `origin` and length. The
`origin` taxonomy is two values by authorship, not length — `system` (deterministic,
pre-rendered) and `prose` (model-generated). **If something fits neither, ask; do not grow
the enum** (step-4 invariant 5).

The WS say payload `{type, text, chunks, origin}` is a contract. The browser must never
re-implement the splitter and must never truncate — it used to cut at 600 chars.
