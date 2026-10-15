# The front model as two decisions, not one generation

> **Research record, not the shipped design (2026-09-24).** The front voice that shipped is
> described in `FRONT_COMPLEMENT_DESIGN.md`. The fine-tuned classifier here is not wired, and
> the task-2 (keep/drop) measurement was invalidated — see its CORRECTION section.

Supersedes the framing benchmarked in `small-model-front-benchmark.md`. That
benchmark asked the model to **compose** the borrower's words and scored a
copied line as a failure. The job is not composition. It is two decisions:

1. **On the caller's line** — pick which register of holding line is owed,
   if any. Code then selects the words.
2. **On the desk's answer** — decide whether the acknowledgement clause the
   desk wrote should still be spoken, given what was already said.

Re-reading the earlier result under this spec inverts it. "The models copy an
approved line 84–95% of the time instead of writing their own" was recorded as
the disqualifying finding. Under a select-don't-compose design **that is the
desired behaviour**, and the only real question is whether the line they reach
for is the right one for the moment.

## Why this is the safer shape

The model never emits text the borrower hears. It emits a label; a pre-linted
pool supplies the words.

- **A wrong answer is wrong, not unsafe.** The earlier run had Qwen3-1.7B
  produce *"Let me read that back exactly: $75.00 on your Visa ending in 2488,
  today, September 2, 2026. Is that all correct?"* — a payment readback from a
  component with no authority to confirm anything. Under this design that
  output is not reachable: the worst case is a HOLD line where SILENT was
  owed.
- **The guard runs once at load, not once per call.** Linting the pool caught
  `"No rush at all, I'm right here."` immediately — the guard's COMMITMENT
  lexicon fires on the bare word "no". That is a line that would otherwise
  have been generated live and silently suppressed.
- **It matches the codebase's own rule**: perceive with the LLM, decide with
  deterministic code (CLAUDE.md). The register is perception. The words,
  rotation and spacing are code.
- **It is trainable.** A 7-way label is a decision boundary a sub-2B model can
  be fine-tuned onto; "write a novel one-sentence line that avoids a closed
  class of words" is not.

## Task 1 — the register

Seven labels, `SILENT` the default and the fallback for anything unclear:

| register | what it is | pool |
|---|---|---|
| `SILENT` | identity data, plain answers, closings, unintelligible | — |
| `BEAT` | an authorizing confirmation | 4 lines |
| `HOLD` | a request, proposal or question | 4 lines |
| `PRESENCE` | a refusal, a stall, anger at the call | 3 lines |
| `SYMPATHY` | something hard in their life | 3 lines |
| `COURTESY` | the speaker is not the borrower | 1 line |
| `ROOM` | they asked for a moment for themselves | 2 lines |

Prompt: 695 tokens against the old 2,046. The label set maps 1:1 onto the
design taxonomy already hand-labelled on 20 ladders, so ground truth exists
for 46 turns with a 32.6% majority-class baseline.

## Task 2 — keep or drop the clause

Ground truth is the **22 real SPLIT lines** catalogued in
`FRONT_EMPATHY_SPLIT.md` §2.1, each an approved registry line whose
acknowledgement clause separates cleanly from its regulated remainder. Each
clause is tested against three situations: an acknowledging holding line
(`DROP`), an attention-only holding line (`KEEP`), and no holding line at all
(`KEEP`), plus the documented exception where the clause concerns something
only the desk can know (a declined card) and always stays.

Prompt: 411 tokens. Baseline 67.7% (always KEEP).

The decision is **semantic, not a flag**: "did the front speak" is a boolean
the gateway already has. What needs judgement is whether what it said did the
same work as the clause — a BEAT does not, a SYMPATHY line does.

## Where each call sits

Task 1 runs in the gap after the caller stops, racing the beat deadline.
Task 2 runs **serially in front of the desk's answer**, so its latency is dead
air on the reply the borrower is actually waiting for — it is the tighter of
the two budgets.

---

## Results, first round — Qwen3-0.6B and Qwen3-1.7B, RTX 4080

Latency is a non-issue at this shape. The output is one label, so the whole
chain is **21 ms** on the 0.6B and **44 ms** on the 1.7B, against a 900 ms
beat deadline.

### Task 1 — register

| model | prompt order | constrained | accuracy | prediction spread |
|---|---|---|---|---|
| Qwen3-0.6B | original | no | 6.5% | ROOM 43/46 |
| Qwen3-0.6B | original | yes | 6.5% | ROOM 43/46 |
| Qwen3-0.6B | reversed | no | 17.4% | SILENT 34/46 |
| Qwen3-0.6B | reversed | yes | 15.2% | SILENT 35/46 |
| Qwen3-1.7B | original | no | 41.3% | SILENT 14, ROOM 14, HOLD 10 |
| Qwen3-1.7B | original | yes | 39.1% | ROOM 16, HOLD 15, BEAT 6 |
| Qwen3-1.7B | reversed | no | 47.8% | SILENT 22, HOLD 12, COURTESY 6 |
| Qwen3-1.7B | reversed | yes | **50.0%** | HOLD 16, SILENT 14, BEAT 7 |

Majority-class baseline 32.6% (7 classes, 46 turns).

**Qwen3-0.6B does not read the utterance.** Its answer is whichever register
is defined LAST in the prompt: ROOM with the original ordering, SILENT when
the list is reversed. Constrained decoding does not move it, so this is not a
format problem. Four conditions, one mechanism. It is disqualified for this
task, and its apparent scores are artifacts of which label happened to sit
last.

**Qwen3-1.7B genuinely uses the input.** Its predictions spread across
registers and it clears the baseline in all four conditions. The best
configuration — reversed definitions, constrained decoding — reaches 50.0%,
95% CI [36.1, 63.9], whose lower bound clears the 32.6% baseline, but only
just. That is a real signal and a weak one: roughly half the registers are
still wrong.

Ordering matters even for the 1.7B (41.3% → 47.8%), which is worth keeping in
mind before reading any single prompt's score as the model's ceiling.

### Task 2 — keep or drop, and why it should not be a model

| model | option order | accuracy | DROP recall | KEEP recall |
|---|---|---|---|---|
| Qwen3-0.6B | DROP first | 32.3% | 10/10 | 0/21 |
| Qwen3-0.6B | KEEP first | 67.7% | 0/10 | 21/21 |
| Qwen3-1.7B | DROP first | 71.0% | 1/10 | 21/21 |
| Qwen3-1.7B | KEEP first | 74.2% | 2/10 | 21/21 |
| **deterministic rule, 0 ms** | — | **100.0%** | **10/10** | **21/21** |

Baseline (always KEEP) 67.7%.

Qwen3-0.6B answers whichever option is printed first, 31 of 31 in both
orderings — the same positional mechanism as task 1. Its 67.7% is exactly the
baseline and means nothing.

Qwen3-1.7B is stable across orderings, so it is not positional, but it catches
**1–2 of 10** duplicates. Catching the duplicate is the entire job, so it is a
constant-KEEP predictor with a rounding error on top.

**The rule that replaces it.** Task 1 already names the register the front
line came from, and the register is exactly what decides whether
acknowledgement happened: `SYMPATHY`, `COURTESY`, `ROOM` and `PRESENCE` do
that work, `BEAT` and `HOLD` are attention only, `SILENT` said nothing. Plus
the documented exception for clauses about events only the desk can know.

*Caveat, stated plainly:* the task 2 set was built by pairing acknowledging
lines against attention-only lines, which is the axis the rule keys on, so
100% is partly circular. What is not circular is that the register determines
the answer and task 1 supplies it free. The real consequence is that **task 2
has no independent error rate — it inherits task 1's.**

### Where this leaves the design

**One model call, not two.** Task 1 is the only decision that needs a model.
Task 2 is a lookup on its output.

**Qwen3-1.7B, not the 0.6B**, at 25–29 ms for the single call. The 0.6B is
disqualified on both tasks by the same positional failure.

**50% on a 7-way register is not deployable**, and it is the number to improve.
Two levers, in order: the label set may be too fine (BEAT/HOLD and
PRESENCE/SYMPATHY are the likely confusions and could merge), and this is now
a small closed-label classification, which is the shape fine-tuning handles
well and free-text generation never was.



---

## Second round — both tasks on Jev

`typesafe/jev-1.13` via OpenRouter's decisions endpoint. Task 1 is a `choice`
over the seven registers with `SILENT` as the bail-out; task 2 is a `noul`
where yes means drop. Both discriminators live in the option text, never in
the state, per §20.8 of the Jev design.

**77 requests, $0.0024 total, $0.0000317 each.** Jev is documented
deterministic (101 cases, 2 runs), so N=1.

### Task 1 — register

| | Qwen3-1.7B (best config) | **Jev** | baseline |
|---|---|---|---|
| accuracy | 50.0% | **84.8%** | 32.6% |
| gold in top 3 | — | **97.8%** | — |
| p50 latency | 25 ms | 159 ms | — |
| p95 latency | 29 ms | 606 ms | — |
| cost per call | local GPU | $0.0000317 | — |

**84.8% against the local model's 50.0%**, on the same 46 turns and the same
7-way label set. 95% CI [71.8, 92.4], comfortably clear of the baseline.

All six misses are legible, and none is a register the caller would hear as
wrong-tempered:

| category | wanted | got | confidence | utterance |
|---|---|---|---|---|
| D | HOLD | SILENT | 0.66 | "I can safely pay $75 today and the $112.36 remainder Sep…" |
| D | HOLD | SILENT | 0.77 | "I'll pay it myself September 16." |
| C | BEAT | SILENT | 0.47 | "Yes, that's correct." |
| D | HOLD | PRESENCE | 0.57 | "I mean $50 toward it, not the whole amount." |
| F | PRESENCE | HOLD | 0.44 | "I will not discuss payments, promises, links, or callbac…" |
| J | ROOM | PRESENCE | 0.88 | "I'm busy, but I don't know when I'll be free." |

Three of six fall **towards SILENT**, which is the safe direction: the
borrower hears nothing rather than the wrong thing. The PRESENCE/HOLD and
ROOM/PRESENCE confusions are the adjacency pairs flagged as merge candidates
in round one.

### Task 2 — keep or drop

A noul returns a **probability**, not a label, and that turned out to be the
whole story. At the arbitrary 0.5 threshold it looks like the local model:

| threshold | accuracy | DROP recall | KEEP recall |
|---|---|---|---|
| 0.50 | 74.2% | 2/10 | 21/21 |
| 0.20 | 93.5% | 9/10 | 20/21 |
| 0.16 | 96.8% | 10/10 | 20/21 |

The single overlapping KEEP case is `v3_payment_failed`: held *"I'm sorry,
that's a lot to be carrying."*, clause *"I'm sorry."*, substance *"That
payment didn't go through; the card was declined by the processor."* Jev
scores it 0.39, which is **correct on its face** — it is a duplicate
apology. It must be kept only because the clause concerns something only the
desk can know, and `FRONT_EMPATHY_SPLIT.md` §4.3 already makes that a code
rule rather than a judgement.

With that exception applied in code as the design specifies, the remaining 30
cases are **perfectly separable**:

| class | p_drop range | n |
|---|---|---|
| DROP | 0.16 – 0.75 | 10 |
| KEEP | 0.06 – 0.15 | 20 |

A gap from 0.15 to 0.16, and any threshold inside it scores 30/30. p50 137 ms,
p95 300 ms.

*Caveat:* the threshold is chosen on these same 31 cases, so the exact value
is fitted. What is not fitted is the separation — a clean gap with every DROP
above every KEEP is not something a thresholded constant predictor produces,
and the plateau from 0.16 to 0.20 holds 93.5–96.8% rather than turning on a
knife edge.

### What this changes

**Round one concluded task 2 should not be a model call.** That was measured
against models that could only emit a label. A noul emits a probability, and
on that signal task 2 is solvable — it was the 0.5 threshold that hid it.

The deterministic register rule still scores 100% on this set at 0 ms, so the
choice is now a real one:

| option | task 2 accuracy | added latency | independent of task 1? |
|---|---|---|---|
| register lookup in code | 100% (partly circular) | 0 ms | no — inherits task 1's errors |
| Jev noul at t≈0.16 | 100% of 30, + code rule | ~137 ms **serial** | yes |

Task 2 sits **serially in front of the desk's answer**, so 137 ms is dead air
the borrower waits through. The lookup is free and its errors are already
counted in task 1's 84.8%. The noul earns its 137 ms only where task 1 was
wrong about the register — which is the case worth measuring next.

### Latency, honestly

Jev is **6x slower than the local 1.7B** on task 1 (159 vs 25 ms p50) and its
p95 is 606 ms. Against the 900 ms beat deadline that fits, but with far less
headroom than local inference, and the tail is what decides whether a beat
fires. The trade is 35 accuracy points for ~130 ms and $0.00003.


---

## Third round — merged registers

Two merges, chosen because swapping the pair costs tone nothing:

| merge | why it is safe |
|---|---|
| `BEAT` + `HOLD` -> **ATTEND** | both say "I am looking at it"; they differ only in line length, which rotation can pick |
| `PRESENCE` + `ROOM` -> **PATIENCE** | both say "no pressure, I am here" |

`SYMPATHY` stays separate (a death must not get the refusal line), `COURTESY`
stays (a stranger must not get sympathy), `SILENT` stays (it is the bail-out).
Five registers, majority baseline **54.3%** — up from 32.6%, because ATTEND is
now 25 of 46 cases.

### The merge alone is mostly cosmetic

Remapping the existing 7-way predictions onto 5 labels, with no new calls:

| | accuracy | baseline | lift |
|---|---|---|---|
| 7-way, as run | 87.0% | 32.6% | +54.3 |
| 5-way, remapped only | 89.1% | 54.3% | +34.8 |

It fixes exactly **one of six** misses (`ROOM`→`PRESENCE`), and the lift over
baseline *falls*. Merging makes the number look better by making the task
easier.

### Rewriting the criteria is what moved it — and made the errors worse

| run | accuracy | misses | toward silence (safe) | spoke wrongly |
|---|---|---|---|---|
| 5-way, remapped only | 89.1% | 5 | 3 | 2 |
| 5-way, criteria rewritten | 93.5% | 3 | **0** | **3** |

Accuracy rose 4.4 points beyond the remap, and every surviving miss flipped to
the unsafe direction. Two plain answers (*"Tomorrow afternoon Pacific."*,
*"Yes, I can make the next regular payment."*) were pulled into ATTEND, and so
was a hardship line — *"My hours were cut for two weeks. I cannot safely pay"*
— which would have answered distress with *"let me look at that for you"*.

The cause was a criteria sentence widening ATTEND to cover any statement
naming an amount or a date, written after seeing exactly the three misses it
was meant to fix, **without the exclusion in the same change**. That is the
over-pull CLAUDE.md warns about, reproduced exactly.

### With the exclusions added

Three exclusions, one per over-pull: an ANSWER containing a time or amount is
still an answer; hardship outranks ATTEND even when an amount is named; and
SILENT explicitly covers answers that mention a time.

| | accuracy | ATTEND | COURTESY | PATIENCE | SILENT | SYMPATHY |
|---|---|---|---|---|---|---|
| final merged | **100.0%** | 25/25 | 3/3 | 4/4 | 10/10 | 4/4 |

p50 **142 ms**, p95 **350 ms** — a materially better tail than the 7-way's
606 ms. Stable: **three repeat runs, 100% each, 0 of 46 answers unstable.**

**This 100% is fitted and is not a generalization estimate.** Every criteria
sentence that moved it was written after reading the misses on these same 46
turns. What it does establish is that the register signal is *reachable* at
this label granularity and that the failures were prompt defects rather than
model limits. A held-out number needs turns that were never used to write the
criteria, and the corpus has category labels for only these 46.

### Jev is not fully deterministic on a boundary case

The design doc records "101 cases, 2 runs, identical answers". The 7-register
question, run four times on identical input, gave 84.8%, 87.0%, 87.0%, 84.8%.
Forty-five of 46 answers never moved. One did, always the same one:

```
LADDER-HARDSHIP-SPLIT-CURE-01 turn 2   "Yes, that's correct."
    BEAT <-> SILENT
```

That is the genuine boundary between an authorizing confirmation and a plain
answer, and it is the case the 5-way merge dissolves. Two runs cannot detect a
2% flip rate, which is why the doc's determinism claim held and this one did
not. The merged question showed no instability across three runs.

### Cost so far

| round | requests | cost |
|---|---|---|
| 7-way + noul | 77 | $0.0024 |
| merged + determinism repeat | 92 | $0.0037 |
| merged v2 + repeat | 92 | $0.0037 |
| stability, 3 runs | 138 | $0.0068 |
| **total** | **399** | **$0.0166** |


---

## Fourth round — fine-tuning Qwen3-0.6B

Subject: **Qwen3-0.6B**, whose zero-shot failure was positional rather than
capability, and which is the cheapest to train and fastest to serve.

Method: **distillation**. Jev labels 602 unique borrower utterances harvested
from the eval corpora; the 46 human-labelled turns are excluded from training
**by case id and by utterance text**, so the evaluation is a genuine held-out
number rather than teacher agreement. Full fine-tune in bf16 (peft is not
installed and installing it into the vLLM venv risks that environment; a 0.6B
trains comfortably without it), loss on the label tokens only.

Inference is **one forward pass**: the five labels have distinct first tokens,
so the classifier is an argmax over five logits. No generation, no parsing, no
format failure is reachable.

### Results on the 46 held-out turns

| run | accuracy | macro recall | SILENT | ATTEND | PATIENCE | SYMPATHY | COURTESY |
|---|---|---|---|---|---|---|---|
| base, untuned | 6.5% | 20.0% | 0% | 0% | 0% | 0% | 100% |
| v1, 602 rows | 73.9% | 57.2% | 40% | 96% | 75% | 75% | 0% |
| v2, +augmentation | 78.3% | 58.2% | 70% | 96% | 25% | 100% | 0% |
| **v3, +varied context, capped majority** | 76.1% | **69.9%** | 90% | 76% | 50% | 100% | 33% |
| *majority class* | *54.3%* | *20.0%* | | | | | |
| *Jev, merged criteria (fitted)* | *100.0%* | *100.0%* | | | | | |

**Read macro recall, not accuracy.** ATTEND is 25 of 46 test turns, so raw
accuracy rewards a model that over-predicts it — which is why v2 scores higher
than v3 while being the worse classifier. By macro recall v3 is best by
11.7 points.

**Fine-tuning works, and the gain is enormous.** The untuned 0.6B is
degenerate at 20.0% macro recall, predicting COURTESY for all 46 turns. Tuned,
it reaches 69.9% at **14.2 ms p50**, a tenth of Jev's 142 ms.

### Two augmentation bugs, both found by testing rather than assumed

**Round 2 — a spurious context feature.** COURTESY stayed at 0/3 despite 27
training examples including near-identical phrasings. The cause: all 25
third-party augmentation rows shared one agent line, so the student learned
*"COURTESY when the agent line is the short greeting"* rather than learning it
from the caller's words. Proven directly by swapping only the context:

| gold utterance | under the real context | under the augmentation's context |
|---|---|---|
| "There's no one here by that name." | PATIENCE | **COURTESY** |
| "My wife handles this account for me." | SILENT | **COURTESY** |
| "Her name is Dana, she's my spouse." | SILENT | **COURTESY** |

All three correct under the augmentation context and wrong under the real one.
Varying the context across augmentation rows moved COURTESY from 0/3 to 1/3.

**Round 2 — an uncapped majority class.** Oversampling raised the minorities
to 140 each but left ATTEND at 421 of 981 rows, and PATIENCE collapsed to 1/4.
Capping the majority too moved macro recall 58.2% → 69.9%.

### Where the remaining errors are

Four of v3's eight misses are one boundary: an authorizing confirmation read
as a plain answer.

```
"Yes, $75 today is correct."                 ATTEND -> SILENT
"Yes, that amount, date and card are correct."  ATTEND -> SILENT
"Yeah, go ahead and take care of it on the card." ATTEND -> SILENT
"Yes, that's correct."                       ATTEND -> SILENT
```

That is the **same boundary Jev flips on non-deterministically** across runs,
so it is a genuinely ambiguous distinction rather than a student defect. It is
also the safe direction: the borrower hears nothing instead of the wrong
thing.

### Cost and honesty

| item | value |
|---|---|
| Jev teacher labels, 602 + 2×50 augmented | **$0.034** |
| training, per round on one RTX 4080 | ~2 minutes |
| rounds run | 3 |
| inference | 14.2 ms p50, local, no per-call cost |

The held-out number is honest — those 46 turns never entered training. The
**augmentation is not blind**: it was written after reading v1's misses, so
some of the gain from v1 to v3 reflects knowledge of the test set. The two
augmentation bugs above were found and fixed on the same 46 turns.

### Where this leaves the choice

| | untuned 0.6B | tuned 0.6B (v3) | Jev |
|---|---|---|---|
| macro recall | 20.0% | 69.9% | 100% (fitted) |
| p50 latency | 15 ms | 14 ms | 142 ms |
| p95 latency | 16 ms | 15 ms | 350 ms |
| per-call cost | none | none | $0.0000317 |
| deterministic | yes | yes | no — one boundary case flips |

Jev is still materially more accurate. The tuned student is **10x faster and
free per call**, and its remaining errors sit on the one boundary the teacher
cannot hold either. The obvious next step is more teacher labels: 602 rows
with one usable COURTESY example is the binding constraint, not the method.


---

## Fifth round — does a bigger model help?

Same distilled data, same held-out 46, LoRA r=32 on every size so the method
is matched. `peft` was installed with `--no-deps` (torch and transformers
untouched; vLLM re-verified after).

| run | accuracy | macro recall | p50 | SILENT | ATTEND | PATIENCE | SYMPATHY | COURTESY |
|---|---|---|---|---|---|---|---|---|
| 0.6B base, untuned | 6.5% | 20.0% | 14.9 ms | 0% | 0% | 0% | 0% | 100% |
| 0.6B full FT, lr 1e-5 | 76.1% | 69.9% | 14.2 ms | 90% | 76% | 50% | 100% | 33% |
| 0.6B LoRA, lr 1e-4 | 93.5% | 85.9% | 14.1 ms | 100% | 96% | 100% | 100% | 33% |
| 1.7B LoRA, lr 1e-4 | 93.5% | 85.9% | 14.7 ms | 100% | 96% | 100% | 100% | 33% |
| 4B LoRA, lr 1e-4 | 95.7% | 92.5% | 21.5 ms | 100% | 96% | 100% | 100% | 67% |
| **0.6B full FT, lr 1e-4** | **95.7%** | **94.2%** | **14.1 ms** | 100% | 96% | 75% | 100% | **100%** |
| *Jev (fitted)* | *100.0%* | *100.0%* | *142 ms* | | | | | |
| *majority class* | *54.3%* | *20.0%* | | | | | | |

### Size buys nothing here

**0.6B → 1.7B: zero.** Both 93.5%, both 85.9% macro, and they differ on
exactly **1 of 46** predictions — a COURTESY case both get wrong anyway. The
checkpoints are genuinely distinct (80 MB vs 140 MB adapters, different
bases); the models simply converge to the same solution.

**0.6B → 4B: +2.2 points**, at 1.5x the latency — and a properly tuned 0.6B
**matches it on accuracy and beats it on macro recall** (94.2% vs 92.5%) while
being 7x smaller and 1.5x faster. The 4B's advantage over the 0.6B LoRA is
entirely one extra COURTESY case, the class with one usable training example.

This is the third time the same thing has shown up: the 1.7B was worse than
the 0.6B zero-shot on pass A, worse on the register choice, and now identical
after training. **Parameter count is not the lever on this task.**

### The learning rate was the lever, and a control caught it

The LoRA runs used lr 1e-4 and the full-FT runs 1e-5, so method and learning
rate moved together. A control — full FT at the LoRA learning rate — was run
before drawing any conclusion:

| | lr 1e-5 | lr 1e-4 |
|---|---|---|
| 0.6B full fine-tune | 76.1% | **95.7%** |

The entire 76.1% → 93.5% gap attributed to LoRA was the learning rate. Full
fine-tuning is not worse than LoRA here; it was undertrained. Without the
control this document would have reported "LoRA beats full fine-tuning",
which is false.

### What is left

The best model misses two of 46:

```
"Yes, that's correct."                                  ATTEND -> SILENT
"I will not discuss payments, promises, links, or ..."  PATIENCE -> ATTEND
```

The first is the boundary **Jev itself flips on across runs** — an authorizing
confirmation against a plain answer — and it fails toward silence, the safe
direction. Three repeat evaluations give 95.7% every time (greedy argmax over
five logits is exactly deterministic, unlike Jev).

### Standing comparison

| | tuned 0.6B | Jev |
|---|---|---|
| accuracy, held-out 46 | **95.7%** | 100% (fitted, not held out) |
| macro recall | 94.2% | 100% (fitted) |
| p50 / p95 | 14.1 / 14.6 ms | 142 / 350 ms |
| per-call cost | none | $0.0000317 |
| deterministic | yes | no |
| teacher spend to build | $0.034 one-off | — |

The two numbers are not like for like: Jev's 100% was reached by rewriting
criteria against these same 46 turns, while the student never saw them. On
the honest comparison the student is within a few points of its own teacher,
**10x faster, free per call, and deterministic**.

COURTESY remains the one real data gap, and 602 rows with a single usable
example is the binding constraint on every model tried.


---

## Sixth round — CPU

The tuned classifier is **one forward pass with no generation** (argmax over
five logits), so it is a prefill-only workload of roughly 2 x params x tokens
FLOPs with nothing to amortise. Prompts average 109 tokens after fine-tuning,
the long instructions having moved into the weights.

Host: AMD Ryzen 7 7800X3D, 8 cores / 16 threads, AVX-512 with `avx512_bf16`.
Model: `fullft-0.6b-lr1e4`, the 95.7% winner.

### It runs, and a single core makes the deadline

bfloat16, varying threads:

| threads | p50 | p95 | accuracy | agrees with GPU |
|---|---|---|---|---|
| 1 | 592 ms | 696 ms | 95.7% | 100% |
| 2 | 310 ms | 363 ms | 95.7% | 100% |
| 4 | 171 ms | 196 ms | 95.7% | 100% |
| **6** | **134 ms** | **153 ms** | 95.7% | 100% |
| 8 | 161 ms | 194 ms | 95.7% | 100% |

**Accuracy is identical to the GPU at every setting, and every answer matches
the GPU's on all 46 turns.** fp32 vs bf16 can move an argmax; here it does
not.

Two practical notes:

- **bf16 is 2x faster than fp32** on this host (165 ms vs 351 ms at 8
  threads) — the `avx512_bf16` path is real.
- **Do not use the SMT threads.** 16 threads collapses to 2,530 ms (fp32) and
  1,679 ms (bf16), an order of magnitude worse than 8. Six physical cores is
  the optimum; eight already shows contention with the rest of the system.

### Throughput: what one CPU actually serves

Independent single-threaded workers, each doing the full classification:

| workers | aggregate | per worker | per-call latency |
|---|---|---|---|
| 1 | 1.6 /s | 1.6 /s | 628 ms |
| 4 | 6.5 /s | 1.6 /s | 618 ms |
| 8 | 11.5 /s | 1.4 /s | 697 ms |

Scaling is near-linear to 4 workers and still 72% efficient at 8, with every
call inside the 900 ms beat deadline.

**11.5 classifications/second.** One classification is owed per borrower turn,
not per second, so calls-per-box depends on turn rate: at roughly 6 borrower
turns per minute per call, this host would carry on the order of a hundred
concurrent calls. That is an extrapolation from turn rate, not a measurement.

### The standing comparison, complete

| | CPU (6 threads) | GPU (RTX 4080) | Jev |
|---|---|---|---|
| accuracy, held-out 46 | 95.7% | 95.7% | 100% (fitted) |
| p50 | 134 ms | 14 ms | 142 ms |
| p95 | 153 ms | 15 ms | 350 ms |
| per-call cost | none | none | $0.0000317 |
| deterministic | yes | yes | no |
| hardware | any 8-core box | a GPU per host | none, network call |

**The CPU is as fast as Jev at p50 and twice as fast at p95**, with no
network, no API key, no per-call cost, deterministic answers, and the same
accuracy as the GPU. For a voice gateway that already runs on CPU hosts, this
removes the GPU from the deployment entirely.

The GPU remains 10x faster and is what training needs, but nothing about
serving this classifier requires one.


---

## CORRECTION — the task 2 ground truth was wrong

Reading the end-to-end transcripts exposed a defect in the task 2 dataset that
three prior rounds of scoring did not. It reverses the task 2 conclusion.

### The defect

`e2e_data.task2_cases()` paired acknowledging front lines with empathy clauses
**round-robin**, without checking that the two carried the same move. **7 of
the 10 DROP labels pair registers that do not match**:

| front line | its register | desk clause | clause move | same move? |
|---|---|---|---|---|
| "I'm sorry, that's a lot to be carrying." | SYMPATHY | "I'm sorry — that's a lot to deal with." | SYMPATHY | yes |
| "I'm sorry you're dealing with that." | SYMPATHY | "I'm sorry — that sounds genuinely difficult." | SYMPATHY | yes |
| "Thanks for letting me know." | COURTESY | "I'm sorry — that sounds frightening." | SYMPATHY | **no** |
| "Take your time, I'm right here." | PATIENCE | "Thank you for telling me." | COURTESY | **no** |
| "I hear you, and I'm not rushing you." | PATIENCE | "Thank you for being straight with me." | COURTESY | **no** |
| "I'm sorry, that's a lot to be carrying." | SYMPATHY | "Thanks for telling me." | COURTESY | **no** |
| "I'm sorry you're dealing with that." | SYMPATHY | "That's alright — nothing has to be settled…" | PATIENCE | **no** |
| "Thanks for letting me know." | COURTESY | "I hear you on the late fee." | PATIENCE | **no** |
| "Take your time, I'm right here." | PATIENCE | "I appreciate the offer." | PATIENCE | yes |
| "I hear you, and I'm not rushing you." | PATIENCE | "Thanks for talking this through with me." | COURTESY | **no** |

The transcript that exposed it: the gateway says *"Thanks for letting me
know."* to a wrong party, the desk's clause is *"I'm sorry — that sounds
frightening."* about flooding, and the label says DROP. Dropping it deletes
the only sympathy in the turn.

### Jev was right and the labels were wrong

Sorting Jev's probabilities against the CORRECTED reading:

| group | p_drop |
|---|---|
| genuine duplicates (same move) | 0.26, **0.67**, **0.75** |
| mislabelled as DROP (moves differ) | 0.16, 0.20, 0.25, 0.28, 0.29, 0.31, 0.34 |
| true KEEP | 0.06 – 0.15, one outlier at 0.39 |

**Jev put the two real sympathy-on-sympathy duplicates at the top, far above
everything else**, and correctly hesitated in the middle band on the
mismatched pairs. Its probability tracks the actual semantics.

That means:

- **The "2/10 DROP recall at threshold 0.5" was not a failure.** It caught
  exactly the two genuine duplicates and correctly declined the seven that
  were not duplicates at all. The default threshold was right.
- **The "perfect separation at t≈0.16" was fitting a threshold to bad
  labels.** It is withdrawn.
- **The deterministic register rule's 100% is withdrawn.** It scored 100%
  because it keys on the same crude axis used to write the labels — front
  register is acknowledging — so it agreed with the errors by construction.
  The transcript shows it dropping sympathy after a courtesy line.

### What actually stands for task 2

A keep/drop decision needs the **moves compared**, not the presence of an
acknowledgement: COURTESY does not do SYMPATHY's work, and PATIENCE does not
do COURTESY's. The register from task 1 is an input to that comparison, not
the answer.

Jev's noul already does this comparison and appears to do it well, but "appears"
is as far as three genuine duplicates can carry. **Task 2 has no trustworthy
measurement yet.** What it needs is a dataset built by pairing each clause
with a front line of a *named* move — matching and mismatching deliberately,
not round-robin — which is a rebuild, not a re-score.

### Why this took four rounds to find

Every earlier round scored the dataset against itself. The rule agreed with
the labels, Jev's threshold was tuned to the labels, and the local models were
graded against the labels. Only rendering the end-to-end transcript put the
front line and the clause side by side as the borrower would hear them, and
the error was obvious in one line. **Aggregate accuracy on a small
hand-built set cannot detect a systematically mislabelled set; reading the
output can.**
