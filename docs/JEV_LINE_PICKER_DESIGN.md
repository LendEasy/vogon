# Jev line picker for v3 resolve — design and measurements

Status 2026-09-19: stages 1–3 measured on recorded turns, no seat, nothing
wired. Terms are defined where first used. Every state Jev sees is JSON.

## 1. Terms

- **resolve** — the v3 agent that turns a borrower's turn into the agent's
  next words on the money conversation. Two production seats exist: the
  deterministic one (`v3_resolve`, with the move catalog `resolve_ont`) and
  the LLM executor (`v3_resolve_llm`).
- **decision** — `decide()`'s ruling for the turn: stay or switch, the path,
  the cause. Made by code from the classifier's facts. Never Jev's.
- **path** — where the money conversation is: negotiating, full_today,
  partial_today, scheduled, split, pay_ahead, payoff, partial_plus_review,
  promise, deferral, review, callback_only, self_serve, no_resolution.
- **family** — a path's kind: payment (an order is staged and rails takes
  the money), promise, deferral, booking (a case is opened and referral
  takes over), self, terminal.
- **move** — the catalog entry the deterministic seat's move catalog claimed
  for the turn (`stage_partial_today`, `refer_silent`, ...), or `legacy` when
  the per-path executor spoke instead.
- **tell** — a piece of content the path owes the borrower (the read-back,
  the fee-waiver condition, the promise recap). 30 exist (`TELL_SPECS`), each
  discharged by one registry line.
- **line** — an approved spoken line in the registry (`v3_lines.yaml`, 204
  lines): a template with `{placeholders}`, a must-say list and a
  must-not-say list.
- **steering** — the block the deterministic executor renders for itself
  before a turn: path, order on the rails, still to collect, still to tell
  (can say now / blocked), the next-ask and next-tell choices.
- **trailer** — the three labels the fine-tune harvest records with every
  turn: `awaiting` (what the borrower is asked next), `handoff` (whose the
  next step is), `told` (the tells this turn delivered).
- **picker** — the thing this document designs: Jev choosing how a decided
  turn is shaped and which lines it recites, with code reciting them.

## 2. Why a picker

`v3_resolve_llm` makes one free-text LLM call per turn: code assembles a
plan (path, owed tells with their lines, allowed lines, figures, verdicts)
and the LLM writes the words. Jev cannot write words, so the picker splits
that call: **Jev selects** among named options, **code recites** the chosen
registry lines with the figures filled, as the deterministic seat does
today. The free text disappears, and with it the execution-claim gate the
LLM seat's doc names as its pre-borrower blocker: a recited line cannot
claim a capture. The picker is the mirror image of the fine-tune seat
(`resolve_ft2`: code picks, the model writes).

## 3. The recorded data

The fine-tune harvest (`ft/harvest_v2.py`, files `ft/data/v2_*.jsonl`)
records, per resolve turn of a passing case, the state as production saw it
and production's own turn as gold, with the trailer. 2,344 rows; 702 are
real turns (train 158, ladders 64, corpora 404, coverage 40, traces 10,
held-out 26), the rest paraphrases and grids over the same states. Only
passing cases are harvested, so the gold is "what production said on a
call that passed", the same limit the fine-tune spec names.

Two fields a picker needs are not in the rows, and both are one line in the
harvest: the **line ids recited** (the harvest spies on every
`_recite(lid, params)` and drops the ids; an offline template match recovers
them on 1,434 of 2,344 rows, the rest being silent staging turns and
composites) and the **candidate set** (the LLM seat's `allowed_lines`,
computable in the harvest from the pre-stage objects it already holds).
Re-harvesting costs one pass of the suite through the deterministic seat,
the price of one N=1 eval run.

## 4. Owed is not said

The rule the state has to make unmistakable, and the reason stage 3 is a
question at all. A tell that is owed, even one the steering marks "can say
now", may not be delivered this turn: another agent may take the turn (a
question answered by assist, a hold, a dispute, an identity check), the
turn may be a silent staging that hands the read-back to rails, or an ask
may come first. Only what this turn's own words deliver counts. A blocked
tell cannot be delivered until its needs are collected. The state carries
this as its second definition, `owed_is_not_said`, and every discharge
question repeats it.

## 5. The state (version 3)

Built by `benchmarks/jev_picker_state.build_state_v3` from a row's text plus
the domain's own artifacts (the line registry, the move catalog, the
memory's requirement, field and obligation specs, the first-version facts
questions, the LLM seat's path-to-lines map and hard rules). Everything is
JSON. Every name that appears anywhere in the state is defined under
`names`. Every line is given as its id AND its complete text, nested as
`{"line_id": ..., "text": ..., "must_say": [...], "must_not_say": [...]}`.
What a row cannot supply is stated as `"not_recorded_in_this_corpus"`, not
omitted, so the shape is the same every turn. An abbreviated real example
(a split staging turn) is `docs/jev_picker_state_example.json`; the full
one is written by `--dump-state`.

| block | contents | form |
|---|---|---|
| `definitions` | 15 entries: purpose ("the decision is already made"), owed-is-not-said, one per block | prose |
| `this_turn.facts` | the arbiter's non-default facts, figures as `{placeholder}` keys | JSON; every key defined in `names.facts` |
| `this_turn.decision` | action, path, cause, **family** | JSON; paths and families defined |
| `this_turn.move` | the catalog move that claimed the turn (`legacy` when none): kind, class, the line ids it speaks, its documentation (the catalog's own comment, or a description built from its perception, effects and speak) | JSON |
| `this_turn.verdicts` | the rulings the cause and move make visible (floor, beyond-authority kind, over-cure, waiver, the split inside the window), and the rulings NOT visible from the row, listed under `unknown` | JSON |
| `steering` | path, **order as an object** (staged / executed / slots), still to collect, tells can-say-now and blocked with needs, still to do, next-ask and next-tell choices, ladder, awaited answer, path family, plus `raw` verbatim | JSON lists + one raw string |
| `record` | tells already given, outcomes so far, deal on the table: all `not_recorded_in_this_corpus`; the conditions this turn's facts disclose | JSON |
| `owed_under_decided_path` | `requirements_for(path, conditions)`: fields, tells, obligations | JSON; all three name sets defined |
| `tells` | all 30: name, meaning, its line (id + full text + must/must-not), owed by the decided path, can say now, blocked needs, already given (`not_recorded_in_this_corpus`) | JSON |
| `candidates` | the lines this turn may recite, each with id, full text, must-say, must-not-say and a **role**: the decided path's ask line, the line discharging an owed tell, the explainer for the extracted `info_topic` (the variant production would pick), the review opening named by the extracted kind, the close affordance, the claimed move's own line | JSON, 5 per row at the median, 24 at most |
| `hard_rules` | the LLM seat's 11 fixed rules | JSON list |
| `figures` | the placeholder keys bound this turn and the identity keys; the harvest's one-line meaning | keys only, values never |
| `names` | definitions: the facts keys used this turn (from the first-version facts questions, values by hand), the fields offered, the 30 tells, the obligations owed, all 14 paths, 6 families, 12 conditions, the figure keys bound this turn (57 in the corpus, all defined), the claimed move | JSON |
| `vocabulary` | the facts seat's 16 domain terms | JSON |
| `conversation`, `borrowers_turn` | the last eight turns, the turn | JSON |

### 5a. The audit of opaque names

`undefined_names(state)` lists every name in the state that has no
definition. Over the 702 rows: **none**. Before the audit, the facts keys
(61 distinct), the field names (18), the obligation names, 52 of the 57
figure keys, the path names and the move names all travelled bare.

Two things the rows cannot give and the state now says so instead of
guessing: tells already given (the steering lists only tells with a
pending status, so a tell absent there is NOT known to be given — reading
absence as "given" marked `fee_waiver_condition` given on the very turn
that delivers it), and the deal on the table / outcomes so far, which the
harvest does not render. Both come back with the re-harvest.

### 5b. Size

Measured over all 702 rows (`--state v3 --measure`), the request being
state plus the 32 questions:

| | chars | est. tokens (4 chars each) | API-reported input tokens |
|---|---|---|---|
| median row | 112k | 28.0k | — |
| p90 | 116k | 29.0k | — |
| largest five | 123–124k | 31.1k | 31,824–32,044, all answered, 300–500 ms, $0.0013 each |

So the v3 request is about 3.5× the v2 one (19.5k chars) and the API took
the largest without error. Where the size sits:

- the **questions** are 72k of the 112k chars, and 47k of that is one
  paragraph — the delivery rules of section 8's v2 — repeated verbatim in
  each of the 30 discharge questions (about 11.7k tokens). Once stage 3
  moves to code (section 9) those 30 questions and their paragraph go with
  it; a stage-4 request carries one yes/no per candidate line, five at the
  median;
- the **state** is 40k chars, of which `tells` (all 30 with their full
  lines) is 17k and `names` 8k. A stage-4 state can list only the tells
  the decided path owes, since the candidates already carry every line
  text that matters.

No published context limit exists for the model; 32k input tokens is the
largest request sent so far and it was answered at the usual latency.
## 6. The questions, in the order we ask for them

Each stage is a bench on the harvested rows before it is a question in a
seat, gated by the one before. Stages 1–3 need no re-harvest: their gold is
the trailer every row carries.

| stage | question | shape | gold |
|---|---|---|---|
| 1 | `handoff` — whose is the next step | choice: none / rails / referral / farewell | trailer `handoff` |
| 2 | `awaiting` — what the borrower is asked to answer next | choice over the decided path's fields, the steering's still-to-collect and next-ask lists, `proposal`, `authorization`, `none` | trailer `awaiting` |
| 3 | `discharge:<tell>` — does this turn's speech deliver it | one yes/no per tell, 30 | trailer `told` |
| 4 | `say:<line>` — is this candidate recited | one yes/no per candidate line | the recited ids (needs the re-harvest) |
| 5 | instrument, resume capacity, done | the LLM seat's typed output fields | memory fields; possibly the facts seat's job already |

The three questions as sent (v2 wording):

```json
{
 "handoff": {
  "type": "choice",
  "instructions": "After THIS turn, whose is the next step? Read this_turn.decision: a switch to a money path that stages an order hands to rails (the read-back and the authorization are rails' words, often with this turn SILENT); a switch to review hands to referral; a close hands to farewell; when the agent asks the borrower something, answers a question, or the path needs more from the borrower before anything is staged, the agent speaks again next: none.",
  "criteria": {
   "none": "the agent itself speaks next: the decided path is negotiating (no order staged), or a promise/deferral path whose amount, date or capacity the agent still asks for, or a payment path where the AGENT still asks the amount or the second leg's date because this turn's facts do not carry them, or the turn answers a question and keeps the floor",
   "rails": "the decided path's family is payment (full_today, partial_today, scheduled, split, pay_ahead, payoff, partial_plus_review) and the amount and timing are known — this turn's facts carry the amount or the date, or the path already holds them — so an order is staged. 'instrument' and 'authorization' under STILL TO COLLECT are RAILS' asks, never the agent's: they mean rails, not none. This turn may be silent or may speak only the path's pre-read-back tells",
   "referral": "the decided path's family is booking (review, callback_only), or the composite partial_plus_review opens its case now: referral takes over",
   "farewell": "the call closes after this turn: no_resolution, self_serve routes given, a promise RECORDED this turn (amount and date both known), a deferral APPROVED this turn"
  }
 },
 "awaiting": {
  "type": "choice",
  "instructions": "What does THIS turn's last sentence ask the borrower to answer? One thing at most. A named FIELD only when the agent asks for that specific value and waits for it; `proposal` only when a specific deal is put to the borrower for a yes or no; `authorization` only when the agent itself reads back and asks for the yes. When the decided path hands to rails or referral, the agent asks nothing itself: none. An open-ended question is none. Offering or moving to a hardship ladder option (catch-up, split, deferral: 'would paying it in two parts work?') asks for `hardship_ladder` when that field is offered; an over-cure choice asks for `over_cure_choice`.",
  "criteria": {
   "none": "nothing specific is asked: a statement, an answer, a close, a silent staging, an OPEN-ENDED question ('is there any amount you could safely pay today?', 'what would work for you?') that names no field and no deal, or a turn that hands to rails or referral, which do their own asking",
   "proposal": "a specific deal is put to the borrower for a yes or no: a counter-offer with a figure ('could you do {minimum_card_charge} today?'), a courtesy-waiver offer ('that brings what's due down to {...}. Can you...?'), or — when a deal already stands on the table (steering.order is staged or a proposal is open) — any question the agent asks about it, including 'which day works best for you?'",
   "authorization": "a read-back asks the borrower to authorize the payment (only when the agent itself reads back, not rails)"
  }
 },
 "discharge:remainder_ack": {
  "type": "noul",
  "instructions": "Does THIS turn's speech deliver the tell 'remainder_ack' — saying its line or its full content? Status: not owed by the decided path. Its line says: \"Okay, {amt_str} today. That leaves {rem_str} still past due after it posts, so the account won't be current yet.\". OWED IS NOT SAID: a tell is delivered only when this turn's own words carry it. A staging turn (family payment, order staged) still speaks the path's pre-read-back tells (remainder_ack when a partial leaves a remainder, fee_waiver_condition when a split cures, payahead_total_cap, exact_payoff_quote) but leaves readback and confirmation to rails. A tell whose line is an ASK (account_position's pressing summary) is delivered only when the agent makes THAT ask this turn, not when it asks or says something else — and account_position itself was given by the opener before these turns: on these turns it is not delivered again. debt_disclosure IS delivered by the first summary of what is owed ('are you able to take care of the {past_due_total} today?') and by the hardship opening that names the missed payment. A switch to promise with the amount and the date both known RECORDS the promise now and delivers promise_recap and stays_past_due together; a promise turn that still asks the amount or the date delivers neither. A split staging turn's confirmation line carries fee_waiver_condition. A courtesy-waiver offer ('I can waive the {late_fee} late fee as a courtesy') delivers waiver_conditions. An over-cure choice turn delivers over_cure_offer. readback is delivered only when THIS turn's words are the read-back itself ('To confirm: ... Do you authorize that payment?'); on a fresh staging rails reads back. When the turn hands to referral, the review-side tells (no_approval_promise, holds, specialist_timeline) are referral's to give, not this turn's.",
  "criteria": {
   "true": "this turn's words carry 'remainder_ack': the content of that line is said now",
   "false": "'remainder_ack' is not said this turn: it is not due, it is blocked, another agent takes the turn, the turn is silent, the agent asks or says something else, or rails/referral will say it"
  }
 }
}
```

## 7. Scoring

Exact agreement with the recorded label per question; for discharge, the
set of tells predicted equals the set told (set-exact), and per-tell
precision and recall. Every row at N=1, since the rows are many. Two
readings stand beside Jev's score on every stage: a **majority-label code
rule**, leave-one-out on the same rows (the most common label for the row's
decided path, or for its move and path), and the seat split (legacy versus
ont). A miss is read three ways before it is called Jev's: the offered
option set may be wrong, the recorded label may rest on a fact the state
does not show, or the option text may not say what the label means. Only
the third is a criteria fix.

## 8. Results, stages 1–3 (702 real rows, N=1)

Reports: `benchmarks/reports/jev_picker_s123_v0`, `_v1`, `_v2`. One Jev
request per row, 32 questions, p50 265 ms, $0.0009 per row.

| version | change | handoff | awaiting | told set-exact | told precision / recall |
|---|---|---|---|---|---|
| v0 | the state as in section 5 minus `family`; plain criteria | 487/702 (69%) | 375/702 (53%) | 261/702 (37%) | 0.11 / 0.15 |
| v1 | `decision.family` in the state; family rules in the three questions' option text; owed-is-not-said in every discharge question | 528 (75%) | 578 (82%) | 421 (60%) | 0.35 / 0.45 |
| v2 | rails' own asks (instrument, authorization) are never the agent's; counters and standing deals are `proposal`; the ladder asks `hardship_ladder`; the corpus's delivery facts per tell (which turn delivers debt_disclosure, waiver_conditions, the promise pair, the split's fee condition; review-side tells are referral's) | 538 (77%) | 596 (85%) | 556 (79%) | 0.73 / 0.46 |
| v3 state | section 5's state: candidates with full lines, the move, verdicts, the structured order, every name defined; the v2 questions unchanged (`jev_picker_s123_v3`, 31 s, $0.86) | 603 (86%) | 621 (89%) | 545 (78%) | 0.76 / 0.40 |
| **code rule, by decided path only** | majority label, leave-one-out | **617 (88%)** | 567 (81%) | 543 (77%) | — |
| **code rule, by move + path** | majority label, leave-one-out | **644 (92%)** | **656 (93%)** | **599 (85%)** | — |

The v3 state moved hand-off by 65 rows (193 of 242 rails turns now read
as rails; 34 of 46 farewell turns still read as none) and awaiting by 25,
and lost 11 on discharge: precision rose to 0.76 but recall fell to 0.40,
with two tells collapsing — `hardship_menu` 17 → 0 of 24 and
`deferral_terms` 19 → 1 of 21 — while `stays_past_due` and
`waiver_conditions` rose. Every v3 row stays below the code rule on all
three stages.

What moved and what did not (v0 → v2):

- Stage 1 never got past the same miss: 119 of the 242 rails hand-offs
  are still read as `none`, on payment paths whose steering lists
  `instrument, authorization` under STILL TO COLLECT. Telling Jev in the
  option text that those are rails' asks (v2) moved 12 rows. The label is a
  function of the decided path: the path alone reproduces it on 88% of
  rows, the move and path on 92%.
- Stage 2 improved most, and beats the path-only rule (596 vs 567): Jev
  reads open-ended questions as `none` and the ladder offers as
  `hardship_ladder` once told. The residual is `proposal` (15/45 — the
  harvest labels a counter-offer or any question over a standing deal as
  proposal, a convention the words alone do not show) and 31 referral turns
  where Jev says the agent asks the callback window (referral asks it).
- Stage 3 went from guessing (precision 0.11) to conservative (0.73 / 0.46).
  The over-fire was exactly the caveat of section 4: owed read as said,
  `account_position` alone 227 false positives in v0. What remains missed
  are deliveries that are deterministic consequences of the move: the
  split's confirmation line carries `fee_waiver_condition` (26 missed), the
  courtesy-waiver offer delivers `waiver_conditions` (12), the recording
  turn of a promise delivers `promise_recap` and `stays_past_due` (11 each),
  the composite's staging delivers `remainder_to_review` (11). Saying so in
  the option text (v2) did not move them: Jev keeps answering "not said"
  where the seat's own rule says "always said on this move".
- Two label facts worth knowing regardless of Jev: in the whole corpus
  `account_position` is owed on 227 negotiating turns and marked told on
  none (the pressing summary line that discharges it is never spoken; the
  labels credit `debt_disclosure` to that summary instead), and `readback`
  is told on 13 resolve turns, the amend-mid-authorization cases where
  resolve reads back itself.

## 9. The reading

**Stages 1–3 belong to code.** The turn's shape — who speaks next, what is
asked, which owed tells are delivered — is a function of the decision and
the claiming move, both of which code already holds, and a majority rule
over them beats Jev on every stage. Jev's answers add nothing a lookup does
not, and on hand-off they subtract. This is the facts-seat lesson again
(perceive with Jev, decide in code), arrived at from the other side: these
three are decisions, not perceptions.

That leaves stage 4 as the only place a picker could earn its seat: the
choice of line within a move where the catalog offers siblings and code has
no verdict — the soft versus firm options line, the wording of an
acknowledgement, which explainer variant. The seat's doc measured the LLM
seat's sibling choices as the probabilistic part; whether Jev does better
is the open question, and it needs the re-harvest of section 3 (line ids
and the allowed set per turn) before it can be asked. Nothing is wired
until that number exists.

## 10. Files

- `benchmarks/jev_picker_bench.py` — the v2 state builder, the three question
  builders, the run and the report (`--limit`, `--files`, `--dump-state`,
  `--state v2|v3`, `--measure`).
- `benchmarks/jev_picker_state.py` — the v3 state: candidates with full
  lines, the move, the verdicts, the record, and the definitions of every
  name (`undefined_names` is the audit).
- `docs/jev_picker_state_example.json` — an abbreviated real v3 state.
- `benchmarks/reports/jev_picker_s123_v0|v1|v2/` — `report.md`,
  `results.jsonl`, `state_example.json`, `questions_example.json` (reports
  are not tracked).
- `ft/data/v2_*.jsonl` — the harvested rows (section 3).
- The facts seat and the classification seats this builds on:
  `JEV_CLASSIFICATION_DESIGN.md`.
