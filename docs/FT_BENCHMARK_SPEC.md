<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/FT_BENCHMARK_SPEC.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# FT seat v2 — the benchmark specification

Status: BUILT 2026-09-07, first run on r7 (`ft/bench/results/v2-r7.md`).
Companion to `FT_SEAT_V2_DESIGN.md` (why the seat exists, what it learned)
and `ft/README.md` (the scripts). This document fixes **what is measured,
how, and how it is reported**, so that every round, every artifact form and
every future model is scored the same way and the numbers can be compared.

Nothing here changes the model's input or output. §2 documents the contract
that was iterated to; the benchmark is built on top of it and would fail
loudly if it moved (`ft_serving.json` refusal, row-schema validation).

## 1. What is being measured

One model, one job: given a rendered call state, **word one turn** of a voice
collections call and emit a trailer saying what it asked for and whom it
handed to. It decides nothing — path, figures, asks and handoffs are chosen
by deterministic code before the model runs and enforced by the executor
after. So the question the benchmark answers is not "is the call right" but:

> Given the state production was in, does the model produce words that
> carry the same figures, the same ask and the same handoff, without ever
> writing a number — and how close is its wording to production's?

Three tiers, cheapest first; each is a gate for the next:

| tier | tool | cost | what it proves |
|---|---|---|---|
| **fidelity** (this spec) | `ft/bench/run.py` | free, ~1 min | the model learned its own contract; the round is worth gating |
| **words metric** | the gate log (`ft/pipeline.sh gate`) | paid | on live turns the executor accepted the model's words, per rejection class |
| **gate** | `evals.runner.run` on the committed suite, N=2 | paid, ~15 min | the call outcomes hold with the model in the seat |

A fidelity number is never a call-correctness claim (design doc §9a).

## 2. The contract (preserved)

### 2.1 Input — one user message, `ft_io.build_input`

```
HISTORY:
Agent: …            (both sides, every figure a {placeholder})
Borrower: …
FACTS: key=value  key=value            (the arbiter's non-default PathFacts)
DECISION: <action> <to> — <cause>      (decide()'s output)
STATE:
PATH: …                                (steering_block(memory, ready, dc), verbatim;
ORDER: …                                the deal on the table, ladder rung,
STILL TO COLLECT: …                     owed tells, next asks)
…
FIGURES (use only these): {key} {key} …   (the keys bound THIS turn, aliased)
IDENTITY: {borrower_full_name} …
BORROWER: <this turn's utterance, figures keyed>
```

System prompt: `ft_io.SYSTEM`; its sha256 is stamped into every adapter
(`ft_serving.json`) with the arm (`aliases`) and the closed-vocabulary flag.
The seat, the fidelity scorer and this benchmark all **refuse** to score an
adapter under a prompt it was not trained on.

### 2.2 Output — one assistant message, `ft_io.parse_output`

```
<speech with {placeholders}, no digit, no name>
---
{"awaiting": <field | "proposal" | "authorization" | null>,
 "handoff": "rails" | "referral" | "farewell" | null,
 "told": [<act>, …]}
```

A silent turn is `---` followed by the trailer only. A malformed trailer is a
finding, not an exception.

### 2.3 Row — `ft/bench/schema.py`, schema 1.0

`{id, case, category, seat, move, input, target}` plus optional `{source,
reason}` on mined rows. `id` is `<case>::t<n>` (prefixed `grid::`, `paraK::`,
`mined::` for the derived sources). Validation: every section present in
order, input ends at the BORROWER line, no money figure on the caller side,
target parses with no findings, no digit in the target speech, handoff in
vocabulary. **Warning**, not error: a target key the input never offered
(11 of 2,080 rows today, all in `para`/`mined`).

The label is production's own words in that state (design §6). That is the
honest definition and also the benchmark's main limitation (§11, gap 6).

## 3. Data and splits

| set | file | built by | role |
|---|---|---|---|
| suite harvest | `v2_train.jsonl` | `harvest_v2.py` (passing runs only) | train |
| corpora | `v2_corpora.jsonl` | `harvest_v2.py --corpora` | train |
| grid | `v2_grid.jsonl` | `gen_grid_v2.py` (injected facts, no LLM) | train |
| paraphrase | `v2_para.jsonl` | `gen_para_v2.py` (borrower line only, ×2) | train |
| mined | `v2_mined.jsonl` | `mine_rejections.py` + `fold_mined.py` | train |
| ladders | `v2_ladders.jsonl` | `harvest_v2.py --yaml evals/collections-ladders.eval.yaml` | train |
| **held-out** | `v2_heldout.jsonl` | `harvest_v2.py` — `HELD_OUT` whole cases | eval |
| **traces** | `v2_traces.jsonl` | `harvest_v2.py --corpus-file ft_conversations.json` | eval |
| **ladder held-out** | `v2_ladders_heldout.jsonl` | the ladder harvest's split | eval |

New held-out rows go in their **own file**, scored via `run.py --set
name=path`, never appended to `v2_heldout.jsonl`: that file is what every
previous round was measured on, and growing it makes the rounds
incomparable. The matrix's consistency check would catch it, but the right
place to not break comparability is here.

**Coverage, not volume, is what a harvest is judged on.** `ft/bench/coverage.py`
counts the rows the trainer actually uses behind each move, path, ladder rung,
told act and trailer shape, and `--baseline` diffs two audits: the number that
matters is states brought above the floor, not rows added. The model maps a
state to the line that belongs to it, so a state with one example is one it has
not seen, and neither more paraphrases of a neighbour nor a larger base model
supplies the missing label.

Split policy: **by whole case**, so no turn of a held-out call is ever in
training. `ft/bench/splits.py` checks three levels and writes
`ft/bench/results/splits_check.json`:

- **case leak** — a held-out case with a training row. Zero today.
- **input leak** — a held-out input byte-identical to a training input from
  another case. **5 of 26 held-out rows and 1 of 10 trace rows today**: the
  opening state of two scripted cases is often the same string. The report
  scores the unseen subset separately (§5).
- **near-duplicate** — a held-out row whose borrower-side text shares an
  8-gram with a training row. 22/26 today, because scripted scenarios share
  opening lines. Reported, not blocking; it says how lexically novel the
  evaluation is (not very).

Every data file is hashed into `ft/bench/data_manifest.json` (tracked) and
copied into the adapter's `train_manifest.json` at training time, with the
git sha and the dirty list of the FT sources.

## 4. Metrics

Per row, the model's speech `got` against the label's `want`, both parsed by
`ft_io.parse_output`, both alias-normalised to registry keys.

**Hard — the contract** (`ft/fidelity.py::_score`, reused verbatim):

| metric | definition | why |
|---|---|---|
| `digit` | a digit outside a placeholder in `got` | the invariant; a number spoken is a wrong amount waiting to happen |
| `unbound` | a key in `got` the FIGURES/IDENTITY tables never offered | an invented key is an unfillable sentence |
| `keys` | set of figure keys in `got` == set in `want` | the wrong-figure class: right words, wrong amount |
| `trailer` | `awaiting` and `handoff` equal, `told` equal as sets | did it ask for and hand off the same thing |
| `exact` | normalised `got` == normalised `want` | verbatim reproduction, the memorisation read |
| **`fit`** | no digit, no unbound, keys, trailer, and silent-vs-spoken agree | **the headline**: right figures, right ask, own words — the bar the state guard applies live |

**Soft — the wording**, on the same strings (`ft/bench/metrics.py`):

| metric | definition |
|---|---|
| `chrF` | character n-gram F2, n≤6, whitespace removed (Popović 2015, sacreBLEU defaults; local implementation, placeholders as one symbol) |
| `ROUGE-L` | LCS F1 over tokens (Lin 2004) |
| `length ratio` | tokens in `got` / tokens in `want` |

Wording metrics are **descriptive**: a model that says the right thing more
briefly scores lower on them and that is not a defect. They exist so a
wording drift is visible between rounds, not as a gate.

**Operational**: latency p50/p95 per generation (concurrency noted; the
voice budget for this turn is under a second).

**Failure class** per non-fit row, strictest first: `digit` > `unbound` >
`wrong-key` > `trailer` > `silent/spoken`. Same order as the fidelity
printout so the two never disagree.

## 5. Statistics

Following Miller (2024), *Adding Error Bars to Evals*
([arXiv 2411.00640](https://arxiv.org/abs/2411.00640)):

- **Every rate carries a 95% interval**, computed by **cluster bootstrap by
  case** (2,000 resamples, seeded): the rows of one call are not independent
  draws, and the naive binomial interval understates the uncertainty. The
  Wilson row-level interval is stored alongside for reference.
- **Two models on the same rows are compared as paired differences**
  (`ft/bench/compare.py`, and the base-model control inside `run.py`): the
  per-row difference, its cluster-bootstrap interval, and a two-sided
  **cluster sign-flip permutation p-value** (exact enumeration under 15
  clusters, so p can never fall below the data's resolution). Output is the
  evalci-style claim ([arXiv 2607.04429](https://arxiv.org/abs/2607.04429)):
  `Δ=+3.1 pts, 95% CI [1.2, 5.0], p=0.002, n=26 rows / 18 cases`.
- **Base-model control on the same contract** (`--control Qwen/Qwen3-8B`,
  served alongside the adapter for free): the difference is what the
  fine-tune bought; the adapter's absolute number alone is not evidence.
- **Memorisation read**: fit on a seeded sample of the rows the trainer
  actually used (dedup and cap applied, `fidelity._trained_rows`) minus
  held-out fit. Over +20 points = memorising; under +10 = generalising.
- **Unseen subset**: held-out fit recomputed on the rows whose input is not
  byte-identical to a training input.
- **Planning figure** `stats.rows_for_delta(p, δ)`: rows needed to detect a
  δ-point change at 80% power. At p=0.77, δ=10 points that is ~300 rows.
  The held-out set has 26 — read §11, gap 1, before believing any single
  round-over-round movement.

What a single run of this benchmark can and cannot say (r7): held-out fit
76.9% [60.7, 92.6]. It rejects "the model does not work" (base: 3.9%) and
"the model memorised" (gap +8.1). It cannot distinguish 77% from 70% or 85%.

## 5a. Comparing several fine-tunes against a baseline

`compare.py` answers "is B different from A". `ft/bench/matrix.py` answers the
question a round actually poses: **of these k candidates, which beat the
baseline?** Three things it adds over running the pairwise tool k times.

**One baseline, k paired comparisons.** Every candidate is differenced against
the same baseline row by row and clustered by case. The baseline is any results
file: the base model (the floor), last round's adapter (did r8 move?), the
merged artifact (did the merge cost anything?), or `<run>.json#control` — the
base-model rows a run already scored alongside itself, so no extra GPU pass is
needed to have something to difference against.

**A family correction.** k comparisons at alpha 0.05 give a 1 − 0.95^k chance
that one looks significant by luck: 14% at k=3, 23% at k=5. Holm-Bonferroni
corrects the p-values and `alpha/k` widens the intervals to match, so the table
and its verdict answer the same question. This changes conclusions, which is
why it is not optional: re-analysing nine models on one benchmark, 3 of 8
adjacent gaps stopped being significant once the family was corrected for
([evalci](https://arxiv.org/abs/2607.04429)). A pinned test holds the case
exactly at the boundary — six of ten cases improving gives p = 0.031, which
clears alpha alone and does not clear it as one of four.

**A consistency check, before any statistic.** Comparing runs scored on
different data is the easiest way to publish a wrong number, so the data
manifest hash, the prompt sha and the row ids are compared first; a mismatch is
reported and the comparison falls back to the intersection of rows. A metric
absent from any run's rows is dropped and named rather than silently compared.

The verdict is written so it cannot be over-read. With 26 held-out rows the
honest answer is usually "no candidate is separable from the baseline", and it
says exactly that, naming the largest point estimate and calling it not
evidence. Separating candidates *from each other* is a different family and the
tool says so, pointing at `compare.py`.

Serving is the other half. `pipeline.sh serve-many v2-r6,v2-r7,v2-r8` puts the
base and every adapter behind one vLLM, so every model answers the **same rows
in the same session** and there is no drift between runs to explain away. LoRA
adapters are 182 MB each, so this costs one base model's memory however many
rounds are loaded. `pipeline.sh sweep` does that, benchmarks the baseline and
each candidate, and writes `ft/bench/results/MATRIX.md`.

## 6. The report

`ft/bench/report.py` renders every results file to Markdown with eight fixed
sections: **1 provenance** (served name, form, base, training stamp, prompt
sha, code sha and dirty list, data manifest per file, decoding), **2
headline** (every rate with its CI; gap; unseen subset), **3 stop rule**, **4
control and paired difference**, **5 slices** (by category, by move), **6
wording**, **7 failures** (class counts and examples with want/got/trailer),
**8 the reproduce command**. Results JSON is schema-versioned
(`result_schema: 1.0`); a reader refuses a version it does not know.

`ft/bench/results/RESULTS.md` is the leaderboard, regenerated from the JSON
files, one line per run. `RUNS.md` in the evals skill stays the narrative
ledger; the leaderboard is the numbers.

## 7. Stop rules

- **Before training**: `ft/lint_data.py` passes (the invariant) and every
  row validates (`ft/bench/schema.py`). Split leaks are reported; a case
  leak should be fixed in the harvest before training.
- **Before the paid gate** (`run.py` exit 4 otherwise): `digit = 0` and
  `unbound = 0` on held-out, held-out fit ≥ 50% (`FT_STOP_FIT`). The 50% is
  the design doc's floor, not a target — it exists so an adapter that
  learned nothing never spends a gate.
- **Merged form**: `verify_merge.py` correct (0 not-base+delta, 0
  unchanged), `compare_merged.py` argmax agreement ≥ 99.5% on held-out
  inputs, and the merged model **re-benchmarked as its own artifact**
  (`--form merged`); a merge inherits nothing.

## 8. The pipeline — `ft/pipeline.sh`

```
ft/pipeline.sh data           harvest + corpora + grid + paraphrase + fold + lint + schema + splits + manifest   (PAID)
ft/pipeline.sh train  v2-r8   LoRA round -> ~/ft-runs/v2-r8 (+ ft_serving.json + train_manifest.json)
ft/pipeline.sh serve  v2-r8   vLLM :8011, adapter as qwen3-8b-ft, base as Qwen/Qwen3-8B (the control)
ft/pipeline.sh bench  v2-r8   -> ft/bench/results/v2-r8.{json,md}, RESULTS.md      (free)
ft/pipeline.sh gate   v2-r8   the paid 201-case gate + words metric + rejection miner (needs FT_GATE_YAML)
ft/pipeline.sh merge  v2-r8   merge fp16 -> verify weights -> argmax compare vs the adapter
ft/pipeline.sh publish v2-r8  dataset + model card bundle -> ~/ft-publish/v2-r8 (nothing uploaded)
ft/pipeline.sh round  v2-r8   train -> serve -> bench (-> gate)
PYTHONPATH=. python3 ft/bench/compare.py ft/bench/results/v2-r7.json ft/bench/results/v2-r8.json
```

The prompt flags are fixed inside the script (aliases arm, closed
vocabulary, caller keys on). Every stage logs to `$FT_SCRATCH` and refuses to
continue on a failed predecessor. `ft/round.sh` is the earlier, shorter form
of `serve → bench → gate` and stays until a round has run through
`pipeline.sh` end to end; then it goes.

## 9. Two forms: the adapter we test, the merged model we ship

The **LoRA adapter** (182 MB) is the local artifact: fast to train, swappable
at runtime, but it only serves behind `--enable-lora`. The **merged model**
(15.3 GiB fp16) is what ships: one set of weights any runtime loads with no
flags, which is what Bedrock's custom import and most export toolchains want.

A merge is a **new artifact and inherits no numbers.** W' = W + (α/r)·B·A is
exact in real arithmetic and rounded once in storage, so the merged model has
to be served, benchmarked and carded in its own right. Three checks, in order,
all run by `ft/pipeline.sh merge`:

| check | tool | floor |
|---|---|---|
| the weights are base + (α/r)·B·A | `verify_merge.py --json` | 0 not-base+delta, 0 unchanged |
| the same next token on held-out inputs | `compare_merged.py --diff` (teacher-forced, `CMP_ROWS=26`) | ≥ 99.5% argmax agreement |
| the same benchmark numbers | `run.py --form merged` then `compare.py` | paired CI on fit includes 0 |

fp16 storage is the default and not an accident: bf16 has 8 mantissa bits and
the delta is 0.1–0.6% of the weight it lands on, so bf16 keeps only cos≈0.90
of the delta's direction where fp16 keeps cos≈1.000 (measured on r6; the
behavioural cost was 99.67% vs 100.00% argmax agreement).

Both proofs travel **with the weights**, the same principle as the serving
stamp: `merge_lora.py` writes `ft_serving.json` with `form`, `merged_from`,
`merged_at` and `merged_dtype`, and the merge stage writes `merge_check.json`
with the two verification results. A merged model that leaves this machine
carries the evidence that it is the adapter it claims to be.

The release path is one command:

```
ft/pipeline.sh ship v2-r8      # merge -> serve merged -> bench merged -> compare vs adapter -> publish
```

## 10. Publishing

`ft/bench/publish.py` writes a review bundle for **either form**, never
uploads. The form is read from `ft_serving.json` and cross-checked against the
directory's shape (`--form` overrides); the card, the metadata and the serve
instructions branch on it.

| | adapter | merged |
|---|---|---|
| `library_name` | `peft` | `transformers` |
| `base_model_relation` | `adapter` | `finetune` |
| serve | `--enable-lora --lora-modules … --max-lora-rank` | no flags, `--dtype auto` |
| LoRA recipe in the card | its own `adapter_config.json` | read from `merged_from` |
| extra sections | — | Merge (provenance + equivalence) |

What it writes:

- `dataset/README.md` — a dataset card in the Hugging Face layout with the
  Gebru et al. datasheet sections (motivation, composition, collection,
  preprocessing, split hygiene, personal data, uses, distribution) filled from
  the manifest and the split check; `MANIFEST.json`; `row.schema.json`;
  the splits as JSONL.
- `model/README.md` — the model card: YAML metadata, the I/O contract, the
  training table, the merge section for a merged model, the benchmark table
  with intervals and the base control, limitations, the serve command; plus
  `ft_serving.json`, `merge_check.json` and the results JSON.
- `model/<weights>` — hard-linked by default so a 15 GiB merged model costs
  nothing per round and the folder is still pushable in one call. Falls back
  to a symlink across filesystems, which the tool says out loud because such a
  bundle cannot be moved or tarred without `-h`. `--weights copy` makes it
  standalone, `--weights none` writes cards only.
- `pii_scan.json` — e-mails, phone numbers, SSN shapes, capitalised two-word
  names, minus the fixture allow-list. **Refuses** the bundle on any other
  hit unless `--allow-pii-review`.

**Two refusals, both about publishing something nobody measured.** The PII
scan above, and the form guard: publishing merged weights against an adapter's
benchmark is refused, because a merge inherits no numbers (§9).
`--allow-form-mismatch` publishes anyway and stamps the warning into the card
next to the table.

Before upload: set both licenses (the base is Apache-2.0; the adapter's and
the data's are the team's call), name the real dataset repo in the model
card's `datasets` field, and decide whether the `CONV-*` trace scenarios,
re-scripted from read calls, are published or kept internal.

## 11. Gaps against standard practice, and the fix for each

| # | gap | standard | status today | proposed fix |
|---|---|---|---|---|
| 1 | **Evaluation sets are small**: 26 held-out rows / 18 cases, 10 trace rows / 4 cases. The interval on r7 is 32 points wide. | ≥100 rows; ~300 to see a 10-point move (§5); Miller: 1,000 questions for a new eval | measured, reported | Raise `HELD_OUT` to ~60 cases (one per family, the suite has 251) and harvest at `--runs 3` for the held-out cases only (different arbiter samples give different turns). Add the campaign and pivot corpora's held-out calls. Target ≥150 held-out rows. **Named 2026-09-10:** `evals/collections-ladders.eval.yaml` (64 cases, 50 passing) is in the repo and has never been harvested; its cases land on the states the coverage audit found thinnest — the ladder rungs and returns, the FAQ re-surface, `no_resolution`, `callback_only`, low-confidence. `evals/adversarial.eval.yaml` (9), `side-effects` (6) and `adversarial_g2c` (2) are also unharvested. |
| 2 | **Input leaks across the split**: 5/26 held-out inputs are byte-identical to a training input from another case. | de-duplicate eval against train at the example level (GPT-3 §4, and every leaderboard's contamination check) | detected, unseen subset reported | Drop leaked rows from the held-out file at harvest time (`harvest_v2` gets the split check), or move the twin case into `HELD_OUT`. Until then read the "unseen" number. |
| 3 | **Low lexical novelty**: 22/26 held-out rows share an 8-gram with training on the borrower side. | held-out data drawn from a different distribution than training (chronological or source split) | measured | Keep a **source-disjoint** eval set: the trace set is that already (real-call re-scripts). Grow it to ≥50 rows from read calls, never harvested into training. Report it as the primary generalisation number once it is large enough. |
| 4 | **No base-model control was run before** (the design doc's ablations were seat ablations). | score the base on the same contract, same week, same judge | fixed: `--control`, paired | Keep it in every run. Add a second control when a competing base is considered (Qwen3-4B, gemma-4-4b): same rows, same prompt. |
| 5 | **No uncertainty on any number** in the ledger; round-over-round claims were made on point estimates. | intervals + paired tests (Miller 2024; evalci) | fixed: cluster-bootstrap CI, paired permutation | Adopt the rule: a round is "better" only when the paired CI on fit excludes 0. With today's n that needs a ~20-point move; gap 1 is what lowers the bar. |
| 6 | **The label is production's wording**; a better-than-production sentence scores as a miss, and exact-match rewards copying. | pairwise preference judging with position swap (MT-Bench, Zheng et al. 2023), human spot-audit, calibrated judge | not built (design doc §12 open question) | Add a `judge` set: for held-out rows where `fit` holds and `exact` does not, ask an LLM judge (Cerebras, temperature 0, both orders) "which wording is better for a live voice turn: A or B", report win-rate with CI, and audit 30 pairs by hand once to calibrate the judge. Keep it out of the stop rule until calibrated. |
| 7 | **One seed, one run per round**; training variance is unmeasured. | ≥3 seeds for any claimed hyper-parameter effect | `seed=20260904` fixed in the trainer | When a recipe change (lr, epochs, cap) is evaluated, train 3 seeds via `FT_SEED` and report mean ± sd of held-out fit. Not per round — per recipe decision. |
| 8 | **Wording metrics are local implementations** (chrF, ROUGE-L). | sacreBLEU / rouge-score with version strings in the report | implemented, tested on identities and disjoints | `pip install sacrebleu rouge-score` in `ft-venv`, switch `metrics.py` to them behind the same function names, print their versions in provenance. |
| 9 | **Training data is gitignored** and has no version of record; the trainer's effective set is reconstructed by re-reading `train_v2.py`. | dataset versioning (DVC, HF datasets with revisions), manifest committed | manifest with sha256 committed (`data_manifest.json`), copied into each adapter | Write the trainer's **effective** row list (after dedup/cap) to `~/ft-runs/<tag>/train_rows.jsonl` so the memorisation sample and any audit read the real set, not a reconstruction. Then publish each version to a private HF dataset repo with the manifest hash as the revision tag. |
| 10 | **Trailer labels are inconsistent** in `told`: the same line is labelled `told: []` in one case and `told: ['waiver_conditions']` in another (5 of r7's 6 held-out misses are `told` disagreements on byte-identical speech). | label-quality audit, inter-annotator agreement | visible in §7 of the report | Audit `told` derivation in `harvest_v2._told_this_turn` against `TELL_SPECS`: if the act is discharged by the line, the label must say so every time. A `told`-only mismatch should also be reported as its own class so it is not counted with real trailer errors. |
| 11 | **Merged-model equivalence was measured on 8 rows, and the publisher could not bundle a merged model at all** (it read `adapter_config.json` unconditionally and raised on every merged directory). | full eval of every shipped artifact | FIXED 2026-09-10: `pipeline.sh merge` compares all 26 held-out inputs, enforces the 99.5% floor and writes `merge_check.json` beside the weights; `publish.py` reads the form, cards both, and refuses a form/benchmark mismatch; `pipeline.sh ship` is the release path | Remaining: no merged model has been through it yet. Run `ship` on r8 and put its `-merged` line in the leaderboard next to the adapter's. |
| 12 | **Latency is measured at concurrency 4** and never gated. | p95 under the product's budget at production concurrency | recorded | Add a `--workers 1` latency pass on 20 rows to the bench, and a stop rule on p95 (the voice budget is under 1 s to first audio; Bedrock's r6 profile was 0.4–1.8 s and would fail it). |
| 13 | **No robustness set**: every input is a clean scripted state. | perturbation / stress evals | none | Generate a perturbed copy of the held-out inputs (STT-style noise in the BORROWER line, reordered FACTS) and report fit on it as a separate set. The contract must hold when the transcript is dirty. |
| 14 | **Model and dataset cards did not exist**; license unset. | HF release checklist; Model Cards (Mitchell et al. 2019); Datasheets (Gebru et al. 2018) | cards generated by `publish.py`, license `TODO` | Fill the licenses, decide the trace set's publishability, and add a `.eval_results` block to the model card once the numbers are final. |
| 15 | **Judge/perception conflation in the paid gate**: the gate's scorer uses the same LLM family as the arbiter, and judge flips have been mis-read as model regressions (r7's THIRDPARTY case). | independent judge, or agreement measured | known from the ledger | Out of this spec's scope (it is the gate's), but the fidelity tier now gives a judge-free number to read first. |

## 12. References

- Miller, E. (2024). *Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations.* arXiv:2411.00640.
- *evalci: A Python Library for Statistically Rigorous Comparison of Language Model Evaluations.* arXiv:2607.04429.
- Zheng, L. et al. (2023). *Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena.* arXiv:2306.05685 — position bias, pairwise judging with order swap.
- Brown, T. et al. (2020). *Language Models are Few-Shot Learners*, §4 — n-gram overlap decontamination of evaluation sets.
- Popović, M. (2015). *chrF: character n-gram F-score for automatic MT evaluation.* Lin, C.-Y. (2004). *ROUGE.*
- Mitchell, M. et al. (2019). *Model Cards for Model Reporting.* Gebru, T. et al. (2018). *Datasheets for Datasets.*
- Hugging Face: [model release checklist](https://huggingface.co/docs/hub/en/model-release-checklist), [model cards](https://huggingface.co/docs/hub/model-cards), [dataset cards](https://huggingface.co/docs/hub/datasets-cards).
