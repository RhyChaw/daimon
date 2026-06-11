# daimon memory ablation

The experiment that decides whether memory earns its place: hold the model fixed
and weak, vary only the memory, measure what changes.

## Run

```bash
# Ollama must be running. Arm C needs palace-ai v2 (mem recall), e.g.:
export DAIMON_PALACE_BIN=/path/to/palace-ai/.venv/bin/palace
python run_eval.py
```

If `palace` on PATH is an older build without `mem recall`, arm C is skipped and
A/B still run. Point `DAIMON_PALACE_BIN` at palace-ai v2 (same as `macagent`).

## The three arms

Same model, same low temperature (0.2 — this is an ablation, not a vibe check),
same whitelist prompt. Only the memory differs:

| Arm | Memory |
|-----|--------|
| **A — none** | nothing |
| **B — simple** | fact store (keyword) + `recent()` = fixed last 3 events |
| **C — palace** | same fact store + `palace mem recall` (query-relevant episodic) |

B and C differ **only** in the episodic layer — fixed last-3 vs query-relevant
recall. That isolates exactly what palace adds today. The fact store is shared,
because palace doesn't do facts; comparing them on facts would be unfair.

## What each task is for

- **control-routing** — needs no memory ("say good morning", "what's the weather").
  Should PASS in all three arms. Confirms the harness is fair and that memory
  doesn't *hurt* the easy cases.
- **fact-resolution** — needs the fact store ("email my prof" → kevin@edu).
  A fails (no memory); B and C pass.
- **buried-episodic** — needs an event that is NOT in the last 3. The advisor's
  email lives only in seed event #2 of 9. A fails, **B fails** (last-3 can't see
  it), **C passes** (query recall finds it). This is the task palace exists to win.
- **paraphrase-ceiling** — the fact key is `temp_unit` but the question says
  "unit / temperature". Keyword recall can't bridge that, so **B and C both fail**.
  Only embeddings would pass it.

## How to read the result

The honest, expected pattern:

- **B and C both beat A** → memory rescues a weak model. (Your headline result.)
- **C beats B on the buried-episodic rows** → palace's query-relevant recall does
  real work a fixed window can't. (The architecture validation.)
- **B and C tie-and-fail on the paraphrase row** → keyword recall has a ceiling;
  the embeddings/`activate()` upgrade is the only thing that breaks it. This tells
  you *whether and when* to build it.

If C does **not** beat B anywhere, palace isn't earning its keep yet for daimon —
and you've saved yourself from building the graph integration on faith.

## Notes

- Scoring `say` content against a 3B model is noisy; routing tasks are the firmest
  signal. Run it 2–3 times and watch the pattern, not a single number.
- Add tasks by editing `tasks.json`. Put new memory only the right arm should have
  into `seed_events` (for episodic) or `facts` (for the shared store).
- Seed memory is written to `./eval-mem/attempts.jsonl` in palace's format, so
  both B (reads the file) and C (`palace mem recall --root ./eval-mem`) see the
  same history.
