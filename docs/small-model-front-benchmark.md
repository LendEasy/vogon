# Sub-2B models as the voice gateway's user-interface model

> **Research record, not the shipped design (2026-09-24).** The front voice that shipped is
> described in `FRONT_COMPLEMENT_DESIGN.md`. Kept for the measurements.

Measured 2026-09-22 on the local **RTX 4080** (sm_89, 16 GB), vLLM 0.24.0,
bfloat16, engine in-process, automatic prefix caching on.

The question: can an ultra-small Qwen/Gemma model (< 2B) carry **all input and
output text** for the voice gateway at ~100 ms per pass, doing three jobs —
(1) generate the filler, (2) strip the filler text the turn service repeats,
(3) be cheap to fine-tune?

**Answer: the latency is there with room to spare. The judgment is not.**
Qwen3-0.6B runs the filler pass in **49 ms** against a 500 ms budget, and
still holds 198 ms at sixteen concurrent calls. Latency was never the
constraint. But on 83% of turns it says a line copied verbatim out of its own
prompt, and **all three passes fail on capability**, each differently:

- **Pass 1 (filler)** — the models copy a stock phrase instead of reading the
  caller, and cannot stay silent on the turns that require it.
- **Pass 2 (strip)** — every model is beaten by doing nothing, and the best of
  them would delete a required compliance notice.
- **Pass 3 (turn-end)** — three of four are at or below chance; the one that
  is not can only see the half of the problem that is visible in text.

The useful result is not "no". It is that the task splits cleanly into a part
worth fine-tuning, a part that should be fixed structurally with no model at
all, and a part that is not a text problem.

---

## 0. Two hardware facts that bound every number here

**GPU1 is faulted.** `0000:06:00.0` enumerates on the PCI bus as `10de:2b85`
(RTX 5090) but NVML cannot open a handle for it:

```
Unable to determine the device handle for GPU1: 0000:06:00.0: Unknown Error
```

Everything below is therefore **RTX 4080**, and the recommendation assumes
that stays true: if the plan is to serve two serial passes from local GPU, it
is being sized on the slower of the two cards this box is supposed to have.
Try a reset before assuming software:

```bash
sudo nvidia-smi -r -i 1        # reset GPU1; needs root, no CUDA process may hold it
```

A card that has fallen off the PCI bus usually needs a reboot or a physical
reseat, so this may be a hardware trip rather than anything vLLM did.
 This also means the
`docs/classifier-benchmark-results.md` figures taken on the 5090 (Qwen3-4B at
86 ms, the split planner at 234 ms) are **not comparable** to anything in this
document. The 4080 column in that doc is.

**vLLM needs two workarounds on this box**, both caused by the faulted GPU1:

| symptom | cause | workaround |
|---|---|---|
| `NVMLError_Unknown` at import | vLLM enumerates devices by *physical* index, so `CUDA_VISIBLE_DEVICES=0` does not hide GPU1 | clamp `nvmlDeviceGetCount()` to 1 via a `sitecustomize.py` on `PYTHONPATH` (inherited by EngineCore subprocesses) |
| `Cannot re-initialize CUDA in forked subprocess` | broken NVML pushes torch onto the driver-API fallback, which initialises CUDA in the parent before the engine forks | `VLLM_ENABLE_V1_MULTIPROCESSING=0` (also removes IPC from the measurement) |

---

## 1. Method

No synthetic prompts. Every payload is the production artifact:

- **Pass A — filler.** The real `app/agents/collections/front.yaml` prompt
  (2,046 tokens) plus the real user message that
  `front_source._user_message()` builds. ~2,215 tokens in, ~15 out.
- **Pass B — strip.** A **decision**, not a rewrite: `{"drop": 0|1}` against
  the line already said and the desk's reply. ~160 in, 7 out.
- **Pass C — turn-end.** `DONE` / `MORE` on the borrower's utterance, 1 token
  out. This is the pass that addresses the Flux fragment defect.

Each warm repetition gets a **unique** user message. Re-issuing identical
prompts would let the prefix cache serve the whole prefill and flatter pass A;
only the static system half is legitimately shared.

Register accuracy replays the **same 20 ladders / 46 borrower turns** the
Cerebras prototype used (`voice-gateway/prototypes/front_model/RESULTS.md`),
through the production message builder and the **real**
`front_guard.guard()`. No paid judge — every number is objective.

One measurement detail: `accuracy.py` passes `forbidden_names=["Alex"]` on
every turn, including the verified ones where the production `front_source`
would permit the name. The guard is therefore marginally stricter here than in
the gateway, so trip counts are an upper bound. No conclusion turns on it —
the failures were copying and silence, not name leaks.

### What these numbers can and cannot support

Decoding is greedy (`temperature=0.0`), so every accuracy figure is **one
deterministic draw**. Re-running reproduces it exactly; that removes sampling
noise but does nothing for the small denominators, which are the real limit.
95% Wilson intervals on the headline figures:

| claim | k/n | point | 95% CI |
|---|---|---|---|
| Qwen3-1.7B pass C accuracy | 20/26 | 76.9% | [57.9, 89.0] |
| Qwen3-0.6B pass C accuracy | 16/26 | 61.5% | [42.5, 77.6] |
| Qwen2.5-1.5B pass C accuracy | 14/26 | 53.8% | [35.5, 71.2] |
| Gemma-3-1b pass C accuracy | 13/26 | 50.0% | [32.1, 67.9] |
| Qwen3-1.7B pass C MORE recall | 4/10 | 40.0% | [16.8, 68.7] |
| Qwen3-0.6B pass A silent-correct | 4/10 | 40.0% | [16.8, 68.7] |
| Gemma-3-1b pass A silent-correct | 7/10 | 70.0% | [39.7, 89.2] |
| Qwen3-0.6B pass B accuracy | 7/14 | 50.0% | [26.8, 73.2] |
| Qwen3-1.7B pass B accuracy | 3/14 | 21.4% | [7.6, 47.6] |

**What does NOT survive this.** Every pass C interval overlaps the 61.5%
baseline, Qwen3-1.7B's included — so "only the 1.7B beats guessing" is not
established. Every pass C interval overlaps every other, so the model ranking
on that pass is noise. The same holds for silent-correct: all four intervals
overlap, so no model is shown to be better at silence than another.

**What does survive.** Both pass B intervals sit entirely below the 78.6%
do-nothing baseline, so "worse than not running the pass" holds. Latency is
48 samples per cell with p95 reported, and Qwen2.5-1.5B measured twice on
separate runs agreed to within 6 ms. The copy rate is a mechanical string
match against the prompt's example list, not a judgement.

**Two further limits.** The pass A prompt is the production artifact and has
been through nine tuning rounds; the pass B and C prompts were written for
this benchmark and are **untuned**, so their numbers are a floor. And pass C's
MORE class is capped at the 14 fragments Flux actually produced in two logged
runs — that class cannot be enlarged without more real fragmented calls, so
its interval will stay wide.

---

## 2. Latency — comfortably inside budget

p50 / p95 ms, single stream, RTX 4080.

| model | pass A filler (2.2k in / ~15 out) | pass B strip (160 / 7) | pass C turn-end (150 / 2) | A cold |
|---|---|---|---|---|
| qwen25-0.5b | 42 / 50 | 17 / 17 | 6 / 7 | 261 |
| qwen3-0.6b | 49 / 55 | 20 / 20 | 8 / 8 | 92 |
| gemma3-1b | 81 / 81 | 48 / 48 | 16 / 16 | 297 |
| qwen25-1.5b | 103 / 114 | 44 / 44 | 17 / 17 | 196 |
| qwen3-1.7b | 118 / 137 | 48 / 48 | 19 / 19 | 209 |

(p50 / p95 ms, single stream)

Prefix caching matters on pass A and nowhere else — the static system
prompt is 2,046 of the 2,215 tokens:

| | p50 ms | p95 ms |
|---|---|---|
| prefix caching on | 49 | 55 |
| prefix caching off | 77 | 83 |

### Concurrency — one 4080 has real headroom

Pass A, the heaviest of the three. Batch latency for N calls landing in the
same engine step:

#### Qwen3-0.6B

| concurrent calls | batch p50 ms | batch max ms |
|---|---|---|
| 1 | 54 | 88 |
| 2 | 66 | 66 |
| 4 | 82 | 83 |
| 8 | 123 | 124 |
| 16 | 198 | 199 |
| 32 | 340 | 343 |

#### Qwen3-1.7B

| concurrent calls | batch p50 ms | batch max ms |
|---|---|---|
| 1 | 137 | 209 |
| 2 | 149 | 149 |
| 4 | 177 | 177 |
| 8 | 234 | 235 |
| 16 | 341 | 341 |
| 32 | 552 | 553 |

Qwen3-0.6B serves **sixteen simultaneous calls in 198 ms**, still inside both
the 500 ms budget and the 900 ms beat deadline. Concurrency is not the
constraint either.

## 3. Register accuracy — the part that fails

| model | lines attempted /46 | playable /46 | distinct lines | silent correct | spoke correct | JSON ok | p50 ms |
|---|---|---|---|---|---|---|---|
| qwen25-0.5b | 36 | 5 | 5 | 100.0% (10) | 12.1% (33) | 78.3% | 42 |
| qwen3-0.6b | 45 | 37 | 9 | 40.0% (10) | 87.9% (33) | 97.8% | 48 |
| gemma3-1b | 14 | 13 | 3 | 70.0% (10) | 27.3% (33) | 30.4% | 62 |
| qwen25-1.5b | 46 | 31 | 10 | 50.0% (10) | 72.7% (33) | 100.0% | 102 |
| qwen3-1.7b | 41 | 33 | 13 | 50.0% (10) | 78.8% (33) | 91.3% | 114 |
| **cerebras qwen-3.8-27b (production today)** | **36** | **35** | ~20 | keeps identity silent | - | - | **227** |

## 3b. The same measurement on the full corpus

The 20 ladders above were **hand-picked during prompt design**, not sampled.
Seventeen corpus families never appear in them (WAIVER, SPLIT, PARTIAL,
PROMISE, OVERCURE, PAUSE, SESSION and ten more), so that run is a convenience
subsample and cannot support a ranking.

This run covers **all 83 cases with a desk trace, 213 borrower turns** — 4.6x
the turns, including every family. It drops the label-dependent metrics
(silent/spoken labels exist only for the hand-picked 20, and hand-labelling
167 more turns would mean grading the models against my own labels) and keeps
only what needs no label. `speak rate` is the label-free stand-in for silence
discipline: the teacher speaks on 78.3% of its turns, so a model at 99% is not
choosing. `truncated` closes a confound the 46-turn run never recorded.

| model | speak rate (teacher 78.3%) | playable | distinct lines | copy rate [95% CI] | JSON ok | guard trips | truncated |
|---|---|---|---|---|---|---|---|
| qwen25-0.5b | 77.5% | 28/213 | **11** | 28.6% [15, 47] | 77.5% | 64.3% | 4 |
| qwen3-0.6b | 99.5% | 152/213 | **10** | 84.2% [78, 89] | 99.5% | 28.2% | 1 |
| gemma3-1b | 30.0% | 57/213 | **5** | 94.7% [86, 98] | 30.0% | 3.3% | 0 |
| qwen25-1.5b | 99.5% | 134/213 | **13** | 95.5% [91, 98] | 99.5% | 36.6% | 0 |
| qwen3-1.7b | 96.7% | 176/213 | **18** | 89.8% [84, 93] | 97.2% | 14.1% | 0 |

### Two harness defects found while checking this, both against my own numbers

**1. The token cap depressed format scores.** The 46-turn run capped output at
48 tokens and did not record `finish_reason`. At 96 tokens with truncation
recorded, Qwen3-1.7B's JSON validity went 91.3% → 97.2% with **zero**
truncations, and Qwen3-0.6B's 97.8% → 99.5%.

**2. The strict parser scored Gemma's content as absent.** Gemma-3-1b returns
a bare string in TYPOGRAPHIC quotes — `“Let me pull that up for you, one
moment.”` — not a JSON object. That is a wrapper failure a three-line parser
fixes, but it was scored as no output at all. Re-parsing leniently (accepting
a bare straight- or curly-quoted string), with the guard's repeat history
reset per call as the gateway does:

| model | strict parse | lenient parse | playable | distinct lines | copy rate |
|---|---|---|---|---|---|
| Qwen2.5-0.5B | 36/46 | 41/46 | 6 | 6 | 33% |
| Qwen3-0.6B | 45/46 | 45/46 | 37 | 9 | 83% |
| Gemma-3-1b | **14/46** | **46/46** | **35** | 8 | 77% |
| Qwen2.5-1.5B | 46/46 | 46/46 | 31 | 10 | 93% |
| Qwen3-1.7B | 42/46 | 44/46 | 37 | 13 | 94% |

Gemma goes from 13 playable lines to 35. **It is not the broken outlier
reported in §3** — it copies like the others. Every Gemma figure in §2 and §3
is depressed by the strict parser and should be read with that correction.
`rescore_lenient.py` in the harness regenerates this table.

The correction does not touch the central result: under lenient parsing all
five models still copy between 77% and 94% of their lines from the prompt's
example list, with 6 to 13 distinct strings across 46 turns.


## 4. The finding: they copy, they do not read

The prompt lists 14 example output lines and tells the model, in those words,
*"Never copy an example line; say it your own way, specific to what this
caller said."*

Measured on the **full 213-turn corpus** (§3b), with 95% Wilson intervals:

| model | playable /213 | distinct strings | copy rate [95% CI] |
|---|---|---|---|
| Qwen2.5-0.5B | 28 | 11 | 28.6% [15, 47] |
| Qwen3-0.6B | 152 | **10** | **84.2% [78, 89]** |
| Gemma-3-1b | 57 | **5** | **94.7% [86, 98]** |
| Qwen2.5-1.5B | 134 | **13** | **95.5% [91, 98]** |
| Qwen3-1.7B | 176 | **18** | **89.8% [84, 93]** |

**This is the one result that replicated out of sample.** Qwen3-0.6B's copy
rate was 83.8% on the hand-picked 46 turns and 84.2% on 213 turns spanning 17
families the first sample never touched — a shift of four tenths of a point.
Qwen2.5-0.5B's low rate is a small-denominator artifact: it produced 28
playable lines of 213 and tripped the guard on 64% of turns.

The "novel" lines are mostly truncations of example lines
(`"Let me look at that for you."` is an example minus three words). Across
**213** turns Qwen3-0.6B produced **10 distinct strings**; the 27B teacher
produced roughly twenty across 46 and chose *when to use none*.

Three consequences follow, and they are the whole result:

1. **It cannot stay silent.** Label-free and on the full corpus: Qwen3-0.6B
   speaks on **99.5%** of 213 turns [97.4, 99.9] and Qwen2.5-1.5B on 99.5%,
   against the teacher's 78.3%. It answered `"ZIP? It's 90210."` with
   *"Thanks for letting me know."* and `"That answers everything. Goodbye."*
   with *"I hear you, and I'm not rushing you."* — both example lines, both
   attached to the wrong moment. (The per-category silent/spoken split stays
   on the 46-turn sample and its intervals are too wide to rank models; see
   §1.)

2. **It repeats.** 8 `repeat` and 8 `opener` guard trips, purely from
   re-emitting the same two or three phrases. The guard suppresses them, so
   the caller hears silence rather than a defect — but the turn is wasted.

3. **The LLM stops earning its place.** A model doing nearest-neighbour
   retrieval over a fixed phrase list is doing what the tier-0 beat pool
   already does at 0 ms. The stated reason for using an LLM here was *"to
   avoid saying a polarity or commitment word at the wrong moment"* — that
   requires reading the utterance, which is exactly what is missing.

**Scale within the family does not fix it.** Qwen3-1.7B is *worse* than
Qwen3-0.6B on JSON validity and speak-correctness, copies more (96% vs 83%),
and costs 2.4× the latency. This is not a parameter-count wall; it is a
task-training wall.

---

## 5. Passes B and C, measured on real data

Latency alone cannot decide these, and the first version of this document
recommended both on latency alone. Both now have ground truth taken from the
2026-09-20 smoke runs, not from cases written to suit the model.

### Pass C — turn-end. Only the 1.7B beats guessing, and the split is legible.

26 cases: the 12 borrower lines Flux delivered **whole** in the normal-persona
run (DONE), and the 14 **fragments** Flux actually produced in the two
slow-speaker runs (MORE where the scripted line continued past it). Always
answering DONE scores 61.5%.

| model | accuracy | DONE recall | MORE recall (the fragments) |
|---|---|---|---|
| qwen3-0.6b | 61.5% | 100.0% | **0.0%** |
| gemma3-1b | 50.0% | 37.5% | **70.0%** |
| qwen25-1.5b | 53.8% | 56.2% | **50.0%** |
| qwen3-1.7b | 76.9% | 100.0% | **40.0%** |
| *majority-class baseline* | *61.5%* | *100%* | *0%* |

Three of four models land at or below the majority baseline, and Qwen3-1.7B
above it — but **the intervals all overlap the baseline and each other**
(§1), so this table ranks nothing. Read it as: no model is *shown* to detect
fragments, and one is worth a larger trial.

The one durable observation is the shape of Qwen3-0.6B's failure: it answers
DONE to all 26 cases, so it is not a weak detector, it is not a detector.
Qwen3-1.7B's errors at least point the safe way — **100% DONE recall** means
it never held a complete utterance in this sample, so it would not add latency
to a normal turn.

**What it catches and what it misses is the finding.** It flags
`"of my social."`, `"The vast four."`, `"above my social"` — fragments that
are *syntactically* incomplete. It misses `"yes"`, `"Yeah."`,
`"That's fine."` — fragments that are **complete sentences which happened to
continue**. After *"Is now an okay time to talk?"*, `"Yeah."` is a correct and
complete answer in text; only the audio knows the speaker kept going.

So pass C is two tasks wearing one name. The syntactic half is real, cheap
(19 ms) and safe. The prosodic half is **not recoverable from text** and
belongs at endpointing or in a turn-service continuation merge — which is
where the fragment root cause was already traced. Note Flux itself, which
*has* the audio, also got these wrong.

### Pass B — strip. Every model is beaten by doing nothing.

14 cases: real front lines paired with the real desk reply that followed, from
three smoke runs. **Always keeping** — that is, never running the pass at all
— scores 78.6%.

| model | accuracy | drop recall (3 cases) | keep recall (11 cases) |
|---|---|---|---|
| qwen3-0.6b | 50.0% | 100.0% | 36.4% |
| gemma3-1b | 0.0% | 0.0% | 0.0% |
| qwen25-1.5b | 7.1% | 0.0% | 9.1% |
| qwen3-1.7b | 21.4% | 100.0% | 0.0% |
| *majority-class baseline (always keep)* | *78.6%* | *0%* | *100%* |

**Not one model beats the do-nothing baseline; the best reaches 50%.** The
failures are not noise, they are the specific way each model is broken:
Qwen3-1.7B drops *every* sentence, Qwen2.5-1.5B is anti-correlated (it keeps
all three real duplicates and drops clean replies), and Gemma-3-1b emits
nothing parseable, consistent with its 30.4% JSON rate on pass A.

The concrete harm is worth naming, because a "decision-shaped" output was
supposed to make this pass safe. Qwen3-0.6B returned `{"drop": 1}` after the
front said *"Checking on that."* — which would have cut the desk's opening
sentence *"Thank you, you're verified. Quick required notice: this is an
attempt to collect a debt…"* — and again after *"Bear with me."*, cutting
*"That's done — I've processed the $75.00 payment on your Visa ending in 2488.
Your confirmation number is PAY-291-T7-1."* Emitting an index rather than text
keeps the model from *rewriting* compliance-critical lines, but it does not
stop it from **deleting** them.

The more useful number is the composition of the set: **3 of 14** real pairs
carried a duplicate at all, and **all three were empathy lines**
(*"I'm sorry, that's a lot to be carrying right now."* against the desk's
*"I'm sorry — that's a lot to deal with…"*, and *"Take your time, I'm right
here."* against *"Of course — take your time…"*).

Those are recited registry lines. The `empathy:` / regulated split already
designed in `FRONT_EMPATHY_SPLIT.md` removes every one of them structurally,
for free, deterministically, with no model on the serial path. **Pass B is a
problem that should not be solved with an LLM at all** — what would remain is
only the generated `v3_assist` answer path.

---

## 6. Recommendation

**Pass A and B are out on the evidence here. Pass C is undecided.** The
100 ms target was never the constraint — Qwen3-0.6B runs the filler pass in
49 ms and holds 198 ms at sixteen concurrent calls. Capability is, and it
fails differently on each pass.

| pass | verdict | why |
|---|---|---|
| **A — filler** | **stay on the cloud model** | **For the register, not the latency.** Local would genuinely be faster where it counts: pass A races the 900 ms beat deadline, and Cerebras at 227 ms p50 / **560 ms max** has come within 340 ms of it (and has already exceeded the 500 ms budget), where Qwen3-0.6B's worst observed case is **56 ms**. The disqualifier is that the small model copies a stock phrase instead of reading the caller — which is the entire reason an LLM sits on this path. |
| **B — strip** | **do the registry split instead** | The duplicates are rare (3 of 14 real pairs) and all are recited empathy lines. `FRONT_EMPATHY_SPLIT.md` removes them deterministically and keeps the serial path model-free. |
| **C — turn-end** | **unresolved — needs more fragments** | Qwen3-1.7B scored above baseline at 19 ms and caught 4 of 10 fragments with no false holds, but its interval [57.9, 89.0] includes the baseline. This is a lead, not a result. Deciding it needs more real fragmented calls, which only production or more slow-speaker runs will produce. The prosodic half is not a text problem either way and belongs at endpointing. |

**If pass A must go local, fine-tune — do not prompt-engineer.** That is point 3 of the
original brief, and it is the right lever. The prompt already contains the
correct silent examples and the explicit instruction *"Never copy an example
line"*; the models copied anyway on 83–96% of turns. Nothing about a 2,000-token
prompt is going to teach a 0.6B model **when to say nothing**, because that is
a decision boundary, not a style. The data for a distillation run exists:

- 414 labelled rows across nine prototype runs, each carrying the line, the
  guard trips, whether it played and a judge verdict — but only **46 unique
  turns**.
- The ladder corpus holds **84 cases / 226 borrower turns**, and the teacher
  (`qwen-3.8-27b` on Cerebras) labels them at 227 ms.
- the application's `ft/` tooling and `FT_BENCHMARK_SPEC.md` (copied here) already tool LoRA training
  and merged-form publishing.

**Pick Qwen3-0.6B as the student — because scale is not the lever here.**
Qwen3-1.7B was *worse* than Qwen3-0.6B on pass A (91.3% vs 97.8% valid JSON,
78.8% vs 87.9% speak-correct, 96% vs 83% copied) despite 2.4× the parameters
and 2.4× the latency. A ceiling that does not move with parameter count is a
training problem, which is what justifies taking the cheapest student and
spending the budget on data instead. The 0.6B is also the fastest (49 ms) and
holds sixteen concurrent calls at 198 ms. Its problem — copying, and never
staying silent — is exactly what supervised fine-tuning on teacher labels
addresses.[^json]

[^json]: Its 97.8% JSON validity is base-model behaviour and should not be
    weighted heavily: after SFT, format compliance comes from the training
    data, not from the base model.

---

## 7. Reproducing

Harness: `voice-gateway/prototypes/small_model/` (bench, accuracy, concurrency,
workloads, the NVML `sitecustomize`). Raw JSON under `results/`.

```bash
export PYTHONPATH=$PWD CUDA_VISIBLE_DEVICES=0 CUDA_DEVICE_ORDER=PCI_BUS_ID
export VLLM_LOGGING_LEVEL=WARNING VLLM_ENABLE_V1_MULTIPROCESSING=0
python bench.py    --model Qwen/Qwen3-0.6B --out results/v2-qwen3-0.6b.json   # latency
python accuracy.py --model Qwen/Qwen3-0.6B --out results/acc-qwen3-0.6b.json  # register, 20 ladders
python score_c.py  --model Qwen/Qwen3-0.6B --out results/passc-qwen3-0.6b.json # turn-end
python score_b.py  --model Qwen/Qwen3-0.6B --out results/passb-qwen3-0.6b.json # strip
python conc.py     --model Qwen/Qwen3-0.6B --out results/conc-qwen3-0.6b.json  # concurrency
python inject.py   # regenerate every table in this document from results/
```
