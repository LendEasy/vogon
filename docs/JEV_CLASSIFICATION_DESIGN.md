<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/JEV_CLASSIFICATION_DESIGN.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# Jev for classification: design, measurements, and open decisions

Status: prototype, measured, not wired into any live path, not committed
(2026-09-19). Every number below comes from a run whose report is on disk;
the path is given next to the number.

## 1. Terms

| term | meaning in this document |
|---|---|
| Jev | TypeSafe's `typesafe/jev-1.13`, reached through OpenRouter. It does not generate text. It takes a *state* and a set of typed *questions* and returns one typed answer per question with a probability. |
| decisions endpoint | `POST https://openrouter.ai/api/alpha/decisions`. The only endpoint that serves Jev; chat completions rejects the model. |
| state | The input Jev judges. A string, or a JSON object of related context. We send a JSON object. |
| question | One item Jev answers. Three shapes: `noul` (yes/no, returns a probability of yes), `choice` (one key out of a `criteria` map, returns the key, a probability per key, and a confidence), `score` (a point on an ordered scale). |
| criteria | For a choice: `{key: description}`. Jev matches the state against the descriptions and picks a key. For a noul: `{"true": ..., "false": ...}`. |
| bail-out | A criteria key whose meaning is "nothing in this family is present". Every choice question we build has one (`none`, `stay`, `unspecified`, `not_mentioned`). Without it Jev must put every turn somewhere. |
| turn | One borrower utterance and the agent's response to it. |
| arbiter | `app/agents/collections_v3/arbiter.py`. Its `extract_path_facts` asks an LLM for a `PathFacts` object; its `decide()` turns `PathFacts` plus call state into a path decision in code. |
| PathFacts | The arbiter's schema: ~41 booleans, ~11 enums (Python `Literal`), and value fields (amount, dates, counts, free text). |
| categorical fields | The booleans and enums of PathFacts. Jev can answer these. It cannot write an amount or a date, so the value fields stay with the LLM. |
| path | What the call is trying to do. One of 19 names in `arbiter.PATHS` (`negotiating`, `full_today`, `split`, `promise`, `review`, ...). `decide()` returns `stay` or `switch <path>`. |
| planner intent | The routing label the planner assigns to a turn, from the workflow's `planner.intents` table (28 in `collections_v3`). Up to three per turn. |
| dual intent | A turn that carries two planner intents, for example a payment answer plus an account question. |
| exclusive group | A set of labels of which at most one can be true of a turn. Asked as one choice question with a bail-out. |
| rider | A label or fact that can be true alongside anything else (for example `asks_credit_promise`). Asked as a yes/no. |
| steering | The text block production renders from the working memory for the arbiter: path, awaiting answer, ladder state, staged order, the deal on the table. |
| like-for-like replay | Re-running Jev on every turn of a recorded eval run with the same context production had on that turn, and grading against what production recorded. |
| control | In the replay, running the recorded facts through `decide()` and checking that the recorded decision comes out. If it does not, the state reconstruction for that turn is wrong and the turn is not evidence of anything. |

## 2. What Jev is, measured

| property | value | source |
|---|---|---|
| request shape | `{model, state, questions}`; `state` may be a JSON object | probe 2026-09-18 |
| answer shape, choice | `{choice, probabilities{key: p}, confidence}` | probe |
| answer shape, noul | `{noul: p}` where p is the probability of yes | probe |
| 1 question, string state | 341 ms | probe |
| 35 questions, object state | 276 ms, 2,126 input tokens, $0.00009 | probe |
| 28 fact questions, structured state | p50 189 ms, p95 563 ms, $0.00036 per turn | `benchmarks/reports/jev_ladder_replay_v2_structured/` |
| 30 questions (facts + 2 decisions) | p50 220 ms, $0.00060 per turn | `benchmarks/reports/jev_ladder_replay_v2_decisions/` |
| determinism | 101 cases, 2 runs, identical answers | `benchmarks/reports/jev_facts_paired/` |
| pricing | $0.042 per million input tokens, output free | OpenRouter listing |

Questions are answered independently of one another. An answer to one
question does not condition another. Any rule of the form "field B only
means something when field A is set" has to be applied in code after the
answer (section 5.3).

Client: `app/llm/jev.py`. Key: `OPEN_ROUTER_API_KEY` from the process
environment or `data-plane/.env`. Python SDK `openrouter` 1.2.0,
`client.alpha.decisions.create` / `create_async`. Timeout 4,000 ms.
Offline tests: `tests/test_jev_client.py` (10, fake transport).

## 3. Where Jev sits

```
                borrower turn
                     |
                     v
   +-----------------------------------------------+
   | state builder  (jev_state.build_state)         |
   |   working memory + node outputs -> JSON        |
   |   + definitions + history turns + utterance    |
   +-----------------------------------------------+
                     |
                     v  one request, ~30-36 questions
   +-----------------------------------------------+
   | Jev                                            |
   |  fact groups (11 choice) + passthrough (5)     |
   |  + riders (10 noul)                            |
   |  + path_decision (choice)     [instrument]     |
   |  + intent (choice)            [instrument]     |
   |  + 6 intent groups (choice)   [instrument]     |
   +-----------------------------------------------+
        |                    |                 |
        v                    v                 v
  categorical facts     path answer      intent answers
        |                (graded only)     (graded only)
        v
  merge() onto the LLM's PathFacts (values stay the LLM's)
        |
        v
  decide()  ->  stay / switch <path>
```

Today nothing past the Jev box is live. The merge, decide() and grading
run only inside the replay harness.

## 4. The state block

### 4.1 First version and what was wrong with it

The first prototype pasted the arbiter's three text blocks into the state:
the history block, the decisions block, and the steering block. Reviewed
on a real ladder turn (`benchmarks/reports/jev_ladder_replay_v2/blocks.json`):

- The decisions block is a JSON object rendered to a string and then
  placed inside a JSON string. It arrived double-encoded with escaped
  quotes.
- Its `rejection_reasons` field carried the previous turn's LLM reasoning
  verbatim. Jev was reading another model's prose as call state.
- The ladder appeared twice in two vocabularies: `catchup` in the
  decisions block, `catch_up` in the steering line.
- The FIGURES glossary took about a third of the state. No question asks
  about a figure.

### 4.2 Current version

`app/agents/collections_v3/jev_state.py` renders the state from the
objects the text was made from: `WorkingMemory` and the node outputs.

```
{
  "definitions": { key: one sentence saying what the key means for the classifier },
  "today": "2026-09-02",
  "call": {
    "path": "negotiating",
    "awaiting_answer": ["hardship_ladder"],      # what the agent's last question asks for
    "still_to_collect": [...],
    "already_answered": [...],
    "conditions_on_record": ["distress", ...],  # earlier turns' facts, not this turn's
    "identity_verified": true|false,
    "sms_blocked": false,
    "already_recorded": null | "a promise to pay is already recorded" | ...
  },
  "hardship_ladder": null | {
    "active": true, "on_table": "split", "offered": [...], "declined": [...],
    "next": [...], "pending_question": "Can you safely pay something today ...?"
  },
  "order_on_rails": null | {amount, type, date, channel, status, second_leg?},
  "rails_result": null | "captured" | "scheduled" | "declined" | ...,
  "deal_on_table": null | {kind, slots},
  "last_deal": null | {kind, slots, status},
  "specialist_review_topic": null | "...",
  "conversation": [ {"borrower": "..."}, {"agent": "..."}, ... ],   # last 6 turns, oldest first
  "borrowers_turn": "..."                                            # the turn to classify, last
}
```

Rules that the definitions state and that the questions rely on:

- A yes/no in the borrower's turn answers `call.awaiting_answer` and
  nothing else. When it is empty, no question is pending.
- `conditions_on_record` are earlier turns' facts. Only the words of
  `borrowers_turn` can disclose something this turn.
- `hardship_ladder.on_table` is the only rung a ladder response can be
  about. When `hardship_ladder` is null, an ordinary payment ask, a
  read-back or an authorization question is not a rung.

Sizes on a ladder turn: about 1,400 characters of state plus 2,500 of
definitions. Latency and cost did not change against the text version.

### 4.3 Gotchas

- The gate result and the opener's memory snapshot are written before turn
  0 and are not in the per-turn node-output deltas the replay reads. On
  replays `identity_verified` reads false and turn 0 has no path. Production
  has both. This affects the replay harness, not the builder.
- There is no global instruction slot in the request format. The one
  sentence every fact question needs ("judge only the words of the
  borrower's turn") is repeated in each question's instructions.

## 5. Fact questions (the arbiter's categorical fields)

### 5.1 Version 1: one question per field

`app/agents/collections_v3/jev_facts.py`. Every boolean as a noul, every
enum as a choice: 52 questions. Instructions and criteria copied from the
arbiter's system prompt. An import-time assertion fails if the schema gains
a boolean or enum this module does not ask.

Measured on the arbiter prompt's own contrast pairs (101 cases, 237
asserted field values, `benchmarks/jev_facts_bench.py`,
`benchmarks/reports/jev_facts_paired/`):

| extractor | cases passing every run | flaky | asserted pairs correct | p50 |
|---|---|---|---|---|
| Jev v1, 2 runs | 91/101 | 0 | 452/474 (95.4%) | 192 ms |
| production `extract_path_facts` (gpt-oss-120b), 2 runs | 73/101 | 7 | 416/474 (87.8%) | 1,188 ms |

The gold here is the prompt's own examples, so this measures agreement with
the written rules, not call outcomes.

### 5.2 Version 2: grouped questions

The like-for-like ladder replay of v1 showed that every systematic
over-fire was two members of one group asked as separate yes/no questions:
`remainder_unpayable` beside a plain partial, `unsure_which_option` beside
"what is the next option", a ladder `accept` beside an authorization yes,
`commits_full_amount` on a yes to a capacity question.

The groups come from a co-occurrence count over 317 recorded FACTS events
of the ladder run: fields that never fire together on one turn are one
choice question; fields that ride on others stay yes/no.
`app/agents/collections_v3/jev_facts_v2.py`, 18 choices and 10 nouls.

| group (question key) | options (bail-out first) | PathFacts fields it sets |
|---|---|---|
| `scope` | unspecified, payment_change, account_question, capacity_constraint | `payment_update.purpose` |
| `money_shape` | none, full_amount, catch_up, partial, split, payoff, settlement, pay_ahead, deferral | `commits_full_amount`, `commits_catch_up`, `proposes_split`, `wants_loan_closed`, `offers_settlement`, `wants_pay_ahead` |
| `timing` | none, today, calendar_date, weekday, relative | `explicit_today`, `amount_is_today`, `names_calendar_date` |
| `channel` | unspecified, our_card, our_link, asks_us_to_schedule, self_serve_app, self_managed | `names_our_rails`, `wants_link`, `asks_scheduled_payment`, `pays_self_serve`, `declines_mechanism`, `payment_update.mechanism` |
| `open_answer` | none, yes, no, unclear | `open_answer` |
| `ladder_response` | none, accept, reject, question, reconsider, unclear | `ladder_response` |
| `ladder_target` | none, current, catchup, split, deferral, all_payment_discussion, specialist | `ladder_target` |
| `ladder_second_reject` | none, catchup, split, deferral, review | `ladder_second_reject` |
| `asks` | nothing, next_option, options_in_general, which_option_fits, explainer, payoff_quote, account_figure, human_or_callback | `ladder_next_requested`, `asks_payment_options`, `unsure_which_option`, `asks_payoff_quote` |
| `refusal_scope` | none, one_option, no_reason_given, all_payment_talk, all_contact, finished | `rejection_reason_unknown`, `refuses_payment_discussion`, `refuses_all_contact`, `declines_further_help` |
| `income_event` | none, temporary_disruption, permanent_reduction, medical_leave, disaster, hardship_other | `distress`, `temporary_income_disruption`, `beyond_permanent`, `medical_leave` |
| `next_installment` | not_mentioned, affordable, at_risk, hedged | `can_resume_next_payment`, `next_installment_at_risk` |
| `today_capacity` | unspecified, nothing_today, amount_is_everything, rest_cannot_be_promised | `cannot_pay_anything_today`, `financial_exhaustion`, `remainder_unpayable` |
| passthrough choices | `beyond_kind`, `legal_hold_kind`, `amends_slot`, `info_topic`, `stated_weekday` | same field, v1 criteria |
| riders (noul) | `ready_to_commit`, `asks_credit_promise`, `refuses_sms_only`, `objects_to_late_fee`, `claims_first_time_fact`, `offers_dispute_withdrawal`, `asks_non_positive_amount`, `reports_existing_payment`, `social_only`, `wants_callback_only` | same field |

An import-time assertion checks that the union of everything the groups,
passthroughs and riders set is exactly the set of categorical fields of
PathFacts.

### 5.3 Structural rules applied in code

Because questions are independent, these are applied to the answers:

- `ladder_target` and `ladder_second_reject` are forced to `none` when
  `ladder_response` is `none`. Measured before the rule: 58 of 66
  `ladder_target` over-fires carried `ladder_response` `none`.
- `ladder_second_reject` is forced to `none` unless `ladder_response` is
  `reject`.

### 5.4 Prompt design for a fact question

What held up under measurement:

- The instructions ask one question. The contrast pairs go in the criteria,
  not the instructions. For a noul, the `true` text is the positive
  examples and the `false` text is the explicit negatives.
- The negative must name the neighbour. "What else can we consider?" fired
  `unsure_which_option` at 0.6 to 0.7 on seven turns until the `asks`
  group's `next_option` was an option beside `which_option_fits`. A choice
  with the neighbour present is more reliable than a yes/no with the
  neighbour described.
- Examples copied from the arbiter prompt carry its loose phrases. "That's
  all I can manage" is in the prompt's `financial_exhaustion` examples and
  made Jev read "a hundred today ... is what I can manage" as exhaustion on
  three turns.
- The "this turn" rule has to be in every instruction and in the state's
  definitions. Without it Jev reads a hardship disclosed on turn 0 as a
  fact on turn 3 (35 over-fires in v1, 1 in v2).

## 6. Intent classification

### 6.1 One question over all labels

`app/agents/jev_intent.py`: a single choice over the workflow's intent
labels, each label's criteria text being its definition paragraph.
Structurally impossible labels (a CONFIRMATION with nothing pending, an
AVAILABILITY_RESPONSE outside the availability step, and so on) are removed
from the criteria before the call rather than filtered from the answer.

Measured on the hand-labelled fixture `tests/fixtures/intent_labels.json`
(348 turns, 329 single-intent; `benchmarks/jev_intent_bench.py`,
`benchmarks/reports/jev_intent_full/`):

| classifier | top-1 on single-intent turns | top-3 recall | p50 |
|---|---|---|---|
| Jev, full definitions as criteria | 97.9% | 100% | 154 ms |
| Jev, first sentence only | 96.0% | 100% | 148 ms |
| gpt-oss-120b, production prompt (recorded baseline) | 77.5% | - | ~530 ms |
| Qwen2.5-1.5B NLI, production prompt (recorded baseline) | 71.7% | - | - |

### 6.2 Why one question cannot see a second intent

The choice probabilities are a normalised competition. On the 329
single-intent turns the runner-up label had probability 0 on 237 and under
0.10 on 316. On the 19 dual turns the second gold label had 0.03 or less on
12. Taking the runner-up above a threshold recovers at most 7 of 19 second
intents at 0.05 (with 16 false seconds) and 2 of 19 at 0.4 (with 0 false).

Independent yes/no "rider" questions per label see the second intent
(recall 14 of 21 at 0.4) but fire on neighbours (40 of 329 single-intent
turns got a false second at 0.4), because nothing tells a yes/no that
CONFIRMATION and PAYMENT_RESPONSE cannot both be true of one turn. Reports:
`benchmarks/reports/jev_intent_full_riders/`, `jev_intent_riders_all/`.

### 6.3 Six exclusive groups

Survey of 3,051 turns with a recorded planner intent set across 56 eval
report directories (`evals/reports/*/traces/*.json`): 765 turns (25%) carry
more than one intent. The frequent pairs:

| pair | turns |
|---|---|
| HARDSHIP_SIGNAL + PAYMENT_RESPONSE | 294 |
| CONTINUATION + PAYMENT_RESPONSE | 80 |
| ACCOUNT_INQUIRY + PAYMENT_RESPONSE | 76 |
| ACCOUNT_INQUIRY + HARDSHIP_SIGNAL | 37 |
| CONFIRMATION + CONTINUATION | 31 |
| CONFIRMATION + PAYMENT_RESPONSE | 29 |
| CONTINUATION + FAREWELL | 19 |
| DNC_REQUEST + PAYMENT_CANCEL | 19 |

Genuine duals are a move plus a rider from a different family. Pairs inside
the response family (CONFIRMATION, PAYMENT_RESPONSE, CONTINUATION) are
planner hedges on one act and route to the same agent in `collections_v3`.

The groups (`jev_decisions.INTENT_GROUPS`), each one choice question with a
`none` bail-out; members in the priority order the criteria state:

| group | members | bail-out means |
|---|---|---|
| response | CONFIRMATION, PAYMENT_RESPONSE, CONTINUATION, AVAILABILITY_RESPONSE, IDENTITY_FIELD | the turn does not answer the agent's pending question |
| question | ACCOUNT_INQUIRY, BALANCE_REPEAT, FAQ_QUESTION | no question asked |
| pivot | DECEASED_SIGNAL, LEGAL_HOLD_SIGNAL, DISPUTE, PAYMENT_CANCEL, CO_BORROWER_SIGNAL, HARDSHIP_SIGNAL | no signal that changes what the call is doing |
| contact | DNC_REQUEST, RECORDING_REVOKE, SMS_DECLINE, PRIVACY_CONCERN, TRUST_CONCERN | no contact, consent or trust content |
| flow | LANGUAGE_REQUEST, HUMAN_REQUEST, AGENT_CALLBACK, HOLD_REQUEST, FAREWELL | no request to change how the call continues |
| safety | SAFEGUARDING_SIGNAL, ABUSIVE_LANGUAGE, EMPATHY_TRIGGER | no safety or conduct signal |

PARTY_TYPE is excluded; the planner prompt forbids emitting it.

Ceiling, computed before running: 594 of the 765 recorded dual turns (78%)
are exactly one intent per group. Of the 171 collisions, 152 are pairs that
route to the same agent. The 19 that do not are HARDSHIP + PAYMENT_CANCEL
(9), HARDSHIP + DISPUTE (8), where the router's priority order already picks
one, and three double questions ("what's my balance, and will this hurt my
credit?").

The intent definitions are parsed from the workflow's own
`planner.intent_system_prompt` (INTENT TYPES section) and cut at 900
characters, so Jev and the planner read one text.

### 6.4 Measurements of the six groups

Hand-labelled fixture, 312 collections turns, 19 dual
(`benchmarks/jev_intent_groups_bench.py`, `benchmarks/reports/jev_intent_groups/`):

| measure | six groups | riders | runner-up rule |
|---|---|---|---|
| exact set match, all turns | 267/312 | - | - |
| gold label present, single-intent turns | 284/293 | - | - |
| single-intent turns given an extra intent | 33/293 | 25 to 40 | 0 to 16 |
| dual turns, exact set | 8/19 | 4/19 | 2/19 |

| group | gold present: picked the gold member | gold absent: picked non-none |
|---|---|---|
| response | 215/219 | 3/93 |
| question | 18/22 | 3/290 |
| pivot | 24/25 | 8/287 |
| contact | 28/33 | 15/279 |
| flow | 14/14 | 1/298 |
| safety | 13/13 | 3/299 |

The contact leak is TRUST_CONCERN picked beside a dispute, abuse, legal hold
or human request. The pivot and safety leaks are HARDSHIP or EMPATHY picked
beside a safeguarding disclosure, and DNC beside a cease-and-desist. Four of
the 11 dual misses are the fixture labelling near-identical turns
differently.

Recorded ladder run, 153 turns with a planner answer, 48 dual
(`benchmarks/reports/jev_ladder_replay_v2_groups/`):

| measure | value |
|---|---|
| exact set match | 78/153 |
| same set of target agents | 112/153 |
| dual turns exact / same targets | 6/48 / 22/48 |

The losses here are planner conventions, not perception: 25 options
questions ("what are my options?") the planner records as PAYMENT_RESPONSE
and Jev puts in the question group; 14 PAYMENT_RESPONSE turns where Jev
bailed out of the response group; 10 turns where HARDSHIP_SIGNAL was recorded
as a routing label on a money offer with no hardship in the words.

## 7. Decision questions

`app/agents/collections_v3/jev_decisions.py` adds two choices to the same
request: `path_decision` (stay plus the 19 paths, criteria from the ontology's
path families) and `intent` (the 28 `collections_v3` intents). Both are
graded only. Report: `benchmarks/reports/jev_ladder_replay_v2_decisions/`.

On the 96 control-valid ladder turns:

| route to a path | matches the recorded decision |
|---|---|
| `decide()` in code on Jev's grouped facts | 87/96 |
| Jev's direct `path_decision`, exact | 70/96 |
| Jev's direct `path_decision`, gold in top-3 | 90/96 |

Both right on 65; code route right and direct wrong on 22; the reverse on 5.
The direct misses are things the model has no way to know: the card floor
(30 dollars is not a split), over-cure arithmetic, whether a date is inside
the scheduling window, and sequencing policy (a global refusal with a
question stays open so the question gets answered; a deferral is not agreed
until the borrower says so).

Intent question against the planner's recorded turn_intents (153 turns):
first intent matched 97, in the recorded set 125, recorded label in Jev's
top-3 145, same target agent 148.

## 8. Like-for-like replay results, all versions

Same recorded run (`evals/reports/ladders_full_0914_n2`, 84 cases, 226
turns, 155 with production FACTS), same 96 control-valid turns. Harness:
`benchmarks/jev_ladder_replay.py`. Control: recorded facts through
`decide()` reproduce the recorded decision on 96 of 109; the 13 misses are
the turn-0 gap in section 4.3 and a few decisions taken by the ont seat's
own logic.

| measure | v1, text state | v2, text state | v2, structured state |
|---|---|---|---|
| `decide()` reaches the recorded action and path | 79/96 | 87/96 | 87/96 |
| ladder_target set where production had none | 66 | 7 | 2 |
| ladder_response set where production had none | 56 | 7 | 2 |
| remainder_unpayable set where production had none | 31 | 0 | 0 |
| temporary_income_disruption set where production had none | 35 | 1 | 1 |
| unsure_which_option set where production had none | 11 | 1 | 1 |
| ready_to_commit set where production had none | 14 | 12 | 6 |
| dual-fact turns fully reproduced (of 109) | 72 | 54 | 49 |

The last row falls because v2 enforces "disclosed this turn" for the income
event while production carries `distress` and `temporary_income_disruption`
onto later turns (24 turns). `decide()` did not move on any of them.

The nine remaining decision differences in the structured run:

| turn | recorded | with Jev | cause |
|---|---|---|---|
| "Go ahead with the payment." at an authorization ask | switch full_today | stay | no money shape chosen, only ready_to_commit |
| "I cannot pay anything until September 15 ... What option fits?" | stay | switch review | `which_option_fits` chosen over `options_in_general` |
| "Can we discuss a smaller payment?" | stay | switch review | Jev follows the prompt's "smaller payment, no count = affordability"; production did not |
| "Fine, I promise to pay 30 dollars this Friday." after the floor line | stay | switch promise | Jev follows the prompt's own example; production did not |
| "a hundred today and the rest ... on the ninth" | stay | switch split | Jev follows the prompt; production did not |
| "twenty-five hundred on this today and be done" | stay | switch partial_plus_review | `amount_is_everything` on "be done" |
| "forty dollars, and I'd pay it myself on the twelfth" | switch promise | switch review | `amount_is_everything` on "only manage" |
| "A hundred today and eighty-five on the ninth is what I can manage" | switch split | switch partial_plus_review | `amount_is_everything` on "is what I can manage" |
| "Put all of it on. Whatever's left over can go against the loan." | stay | switch full_today | over-cure read as full amount |

## 9. Findings

1. Jev's single choice question is the right instrument for a primary
   label: 97.9% top-1 on hand-labelled intents against a 77.5% production
   baseline, at about a third of the latency.
2. The choice distribution cannot report a second label. A normalised
   competition puts the loser near zero whenever the winner is clear.
3. Independent yes/no questions report second labels but fire on
   neighbours. The fix is not a threshold; it is putting the neighbours in
   one choice question with a bail-out.
4. Groups are found from data, not designed: count which labels never fire
   together in recorded output, and make each such set one question. This
   worked for the arbiter's facts (79 to 87 of 96 decisions) and for
   intents (dual recall 4 to 8 of 19 on the labelled fixture).
5. Perceive with Jev, decide in code. Asking Jev for the path directly lost
   17 of 96 turns to floor, window and sequencing rules. The existing split
   in `CLAUDE.md` stands.
6. The state must be structured and defined. Text blocks carried
   double-encoded JSON, another model's reasoning, and a glossary nothing
   used. The structured state removed the ladder over-fires that survived
   the question redesign, at no latency cost.
7. Production's labels are conventions as much as perceptions. HARDSHIP_SIGNAL
   on a money offer, PAYMENT_RESPONSE on "what are my options?", and
   carrying `distress` onto later turns are choices the planner and arbiter
   prompts make. Jev cannot infer them from definitions and should not be
   graded as wrong for missing them until each is decided.
8. Gold sets disagree with themselves. The intent fixture labels
   near-identical abusive turns differently and is inconsistent on
   EMPATHY_TRIGGER beside SAFEGUARDING_SIGNAL. With 19 dual turns, one label
   moves the headline number by 5 points.

## 10. Gotchas

- The request format has no shared instruction slot. Rules that apply to
  every question are repeated per question.
- Answers are independent. Cross-field rules (section 5.3) live in code.
- Every choice needs a bail-out. A forced choice puts a bare "Okay"
  somewhere substantive.
- Noul probabilities around 0.5 are coin flips (`remainder_unpayable` at
  0.51 to 0.76 on turns that said nothing about a remainder). A weak
  criterion produces them; a threshold hides them.
- Definitions parsed from the planner prompt run to 5K characters each;
  they are cut at 900 for the intent questions. The cut is at a word
  boundary and was not tuned.
- The replay's turn-0 gap (section 4.3) means turn-0 rows have no path and
  no gate result. Read turn-0 differences with that in mind.
- The `intent_labels.json` fixture is the v2 collections workflow (23
  labels); the ladder run is v3 (28). The six groups are defined on v3 and
  filtered to the labels each workflow has.

## 11. Open decisions

These need a decision before any of this is wired, because each changes
what "correct" means:

1. Is `distress` a fact of the turn that discloses it, or of every later
   turn? The prompt says the former; production does the latter.
2. Does HARDSHIP_SIGNAL stay a routing label the planner attaches to money
   offers inside a hardship case?
3. Is CONFIRMATION versus PAYMENT_RESPONSE defined by what the agent asked
   or by the borrower's words? The recorded labels swap both ways.
4. The three turns where Jev applied a written arbiter rule that production
   ignored (smaller payment, sub-floor promise, split proposal): which side
   is right?
5. Does EMPATHY_TRIGGER co-fire with SAFEGUARDING_SIGNAL, or does the
   priority order make it moot?

## 12. Files and how to rerun

| file | role |
|---|---|
| `app/llm/jev.py` | client, question builders, answer types |
| `app/agents/jev_intent.py` | single-choice intent classifier, structural pruning |
| `app/agents/collections_v3/jev_facts.py` | v1 facts, 52 questions, `merge()` |
| `app/agents/collections_v3/jev_facts_v2.py` | v2 grouped facts, 28 questions, structural rules |
| `app/agents/collections_v3/jev_state.py` | structured state with definitions |
| `app/agents/collections_v3/jev_decisions.py` | `path_decision`, `intent`, the six intent groups |
| `benchmarks/jev_intent_bench.py` | intent fixture, single choice (+ riders) |
| `benchmarks/jev_intent_groups_bench.py` | intent fixture, six groups |
| `benchmarks/jev_facts_bench.py` | arbiter contrast pairs, Jev vs production |
| `benchmarks/jev_ladder_replay.py` | like-for-like replay of a recorded run |
| `tests/test_jev_client.py` | offline tests |

```
cd data-plane
PYTHONPATH=. .venv/bin/python benchmarks/jev_intent_bench.py --out benchmarks/reports/jev_intent_full --criteria full
PYTHONPATH=. .venv/bin/python benchmarks/jev_intent_groups_bench.py --out benchmarks/reports/jev_intent_groups
PYTHONPATH=. .venv/bin/python benchmarks/jev_facts_bench.py --out benchmarks/reports/jev_facts_paired --runs 2 --paired
PYTHONPATH=. .venv/bin/python benchmarks/jev_ladder_replay.py \
    --report evals/reports/ladders_full_0914_n2 --out benchmarks/reports/jev_ladder_replay_v2_structured \
    --extractor v2 --state structured [--decisions] [--intent-groups]
```

Every run writes its report under `--out`; the console gets a summary. The
`--paired` flag is the only one that calls the production LLM.

## 13. Not done

- Nothing is wired into `Planner.plan()` or `extract_path_facts`.
- No commit has been made.
- The three criteria edits named in section 6.4 (TRUST_CONCERN as a rider,
  EMPATHY versus HARDSHIP beside safeguarding, "what are my options" in the
  response family) have not been made.
- The v2 fact criteria edits named in section 8 (`amount_is_everything`,
  over-cure, `which_option_fits`) have not been made.
- No measurement against the pivot corpus or the eval suite. Every number
  here is a replay of one recorded run or a fixture.

## 14. Labelled examples in the state

Added 2026-09-19 after section 13 was written.

The request format has no examples slot, so reference turns go into the
state as `labelled_examples`: a list of `{agent_said, borrower_said,
response, pivot, question}` with a definition entry, and the six group
questions are told to match the closest example before using the
definitions. Module: `app/agents/collections_v3/jev_examples.py`. Flags:
`--examples` on `benchmarks/jev_intent_groups_bench.py` and on
`benchmarks/jev_ladder_replay.py`.

Two sources: 18 canonical examples written from the planner prompt's rules,
weighted to the response and pivot families; and the failures of the
six-group runs, harvested into `benchmarks/data/jev_group_failures.json`
with the expected labels of their source. A run on the fixture uses only
ladder-derived failures and the ladder run uses only fixture-derived ones,
so no example is a test turn of the run it appears in. 36 examples per run,
about 8.7K characters.

Result (full tables and every flipped turn in `docs/JEV_EXAMPLES_REVIEW.md`):

| measure | fixture, no examples | fixture, with | ladder, no examples | ladder, with |
|---|---|---|---|---|
| exact set match | 267/312 | 264/312 | 78/153 | 83/153 |
| dual turns exact | 8/19 | 7/19 | 6/48 | 8/48 |
| pivot false fires | 8/287 | 15/287 | 4/97 | 5/97 |
| p50 latency | 160 ms | 172 ms | 189 ms | 200 ms |

Finding: examples transmit the conventions of the run they came from.
Ladder-derived examples carry HARDSHIP_SIGNAL on money offers, and on the
human-labelled fixture Jev then added HARDSHIP to "I can only do fifty
dollars at the moment" and to three stress-without-circumstance turns.
Fixture-derived examples improved the ladder run by 5 turns and broke
none. Examples are a lever on conventions; the convention (open decisions
1 and 2 in section 11) has to be decided before the example set is chosen.
The review list for that decision is `docs/JEV_EXAMPLES_REVIEW.md`.

## 15. Vocabulary, conventions and account facts in the state

Added 2026-09-19 after section 14.

Three blocks were added to the structured state (`jev_state.py`), each with
its own definition entry:

- `vocabulary`: 16 domain terms, one sentence each (past due, regular
  payment, floor, payoff, late fee, payment answer, confirmation,
  acknowledgement, hardship, ladder, rung, our rails, self-managed payment,
  read-back, specialist review, dispute). Shared by every question.
- `conventions`: 12 statements of how this workflow labels neighbouring
  cases. C1 to C10 restate the planner prompt's CLASSIFICATION RULES. C11
  (a payment offer inside a hardship conversation also carries
  HARDSHIP_SIGNAL) and C12 (an options question while negotiating is a
  PAYMENT_RESPONSE, not an FAQ) are conventions observed in recorded output
  that no prompt sentence states; they are open decisions 1 and 2 in
  section 11 and are included because the recorded labels are the current
  grading standard.
- `account`: past due, regular payment, floor, payoff, late fee, next due
  date, days past due, from the same `Figs` object production uses. Only on
  the ladder run; the intent fixture's scenarios run on a mock account with
  a $0.00 balance.

Results, six-group intents, no examples:

| measure | fixture, baseline | fixture, + vocab & conventions | ladder, baseline | ladder, + vocab, conventions & account |
|---|---|---|---|---|
| exact set match | 267/312 | 277/312 | 78/153 | 100/153 |
| single-intent turns given an extra intent | 33/293 | 24/293 | - | - |
| dual turns exact | 8/19 | 8/19 | 6/48 | 23/48 |
| same set of target agents | - | - | 112/153 | 134/153 |
| question family false fires | 3/290 | 4/290 | 26/135 | 1/135 |
| pivot family false fires | 8/287 | 5/287 | 4/97 | 7/97 |
| flow + safety false fires | 1 + 3 | 0 + 0 | 0 | 0 |
| `decide()` on Jev's facts (96 control-valid turns) | - | - | 87/96 | 89/96 |

Reports: `benchmarks/reports/jev_intent_groups_vocab2/`,
`benchmarks/reports/jev_ladder_replay_v2_groups_vocab3/`.

Three things were learned on the way to those numbers:

1. A convention placed in the state does not bite. With C12 in the state
   only, FAQ_QUESTION still fired on "what is the next option?" on 23
   ladder turns at 0.77 to 0.91 confidence. The same sentence placed in the
   question family's instructions and in its `none` criterion took the
   false fires from 26 to 1 and the ladder exact match from 82 to 100. The
   criteria text of a question outranks anything in the state; a rule that
   decides between two options belongs in the option text.
2. A vocabulary entry is read literally. "Hardship" without an explicit
   exclusion for stress-only turns made three EMPATHY_TRIGGER turns gain
   HARDSHIP_SIGNAL; adding the exclusion removed them.
3. The account definition's first wording ("an amount equal to past_due is
   the whole past due") made the money-shape question read every dated
   $202.36 offer as `commits_full_amount`, against the arbiter's rule that
   a named figure is an amount. Rewording it as "a named figure is still a
   named amount" brought the over-fire from 13 back to 10 (baseline 7). The
   account block is a lever on the fact questions as well as the intent
   questions and its wording needs the same care as a criterion.

Compared with examples (section 14): vocabulary and conventions moved the
labelled fixture by +10 exact and the recorded ladder run by +22, with no
per-call cost beyond about 4.6K characters of state and no measurable
latency change. Examples moved the fixture by -3 and the ladder by +5.

## 16. C11 in the pivot criteria, and inline examples

Added 2026-09-19 after section 15.

Changes: C11 (a payment offer, a rung decline, or a next-option request
inside a hardship conversation also carries HARDSHIP_SIGNAL) moved from the
state into the pivot family's instructions and its `none` criterion, keyed
on the state's `hardship_ladder` and `conditions_on_record`. Every
vocabulary term, every convention, and every group bail-out gained an
inline example. The comparative account statements ("the named amount is
below the floor", "above the payoff") are parked for the next experiment.

| measure | fixture, §15 | fixture, §16 | ladder, §15 | ladder, §16 |
|---|---|---|---|---|
| exact set match | 277/312 | 282/312 | 100/153 | 96/153 |
| single-intent turns given an extra intent | 24/293 | 18/293 | - | - |
| dual turns exact | 8/19 | 7/19 | 23/48 | 24/48 |
| same set of target agents | - | - | 134/153 | 133/153 |
| pivot false fires | 5/287 | 4/287 | 7/97 | 12/97 |
| contact false fires | 11/279 | 7/279 | 0 | 0 |
| `decide()` on Jev's facts | - | - | 89/96 | 87/96 |

Reports: `benchmarks/reports/jev_intent_groups_vocab3/`,
`benchmarks/reports/jev_ladder_replay_v2_groups_vocab4/`.

The inline examples helped the labelled fixture (+5 exact, extras 24 to
18, contact false fires 11 to 7). C11 in the criteria did not help the
ladder, and the reason is in the turns, not in Jev:

- Jev applied C11 as written and fired HARDSHIP_SIGNAL on 8 turns that sit
  inside an active ladder with distress on record ("I cannot catch up within
  a week. What is the next option?", "I do not want to move a payment to the
  end. What else is available?", "Yes, I authorize the hundred today"). The
  planner recorded PAYMENT_RESPONSE alone on those.
- The planner recorded HARDSHIP_SIGNAL on 6 turns with NO hardship context
  at all, no ladder and no distress on record ("Can I split it into two
  payments?", "I can do 30 today and the rest on Friday", "No, change the
  amount from $202.36 to $75").

So the recorded HARDSHIP_SIGNAL is not "hardship conversation plus a
payment move". It fires on split, partial and amount-reduction offers
regardless of context, and skips some in-context moves. That is a routing
habit of the production planner (those offers land on the hardship
executor), not a convention a definition can state. Open decision 2 in
section 11 is now sharper: either write the rule the planner actually
follows and grade Jev on it, or drop HARDSHIP_SIGNAL as a routing label on
money offers and let the arbiter's facts route them.

`decide()` on the facts moved 89 to 87 because the vocabulary and account
blocks are shared with the fact questions; the two turns that moved are in
the money-shape family and are within the run-to-run noise seen across
sections 8 and 15 (87 to 89).

## 17. Decision: HARDSHIP_SIGNAL is not a routing label

Decided 2026-09-19 (open decision 2 in section 11, closed).

HARDSHIP_SIGNAL means the turn itself discloses a circumstance or asks to
reduce or restructure what is owed. A payment offer, a split, a partial, a
decline of a rung, or a request for the next option is a PAYMENT_RESPONSE
only, even inside a hardship conversation. Applied in three places: the
pivot family's instructions and `none` criterion, convention C11 in the
state (now states the decision), and the ladder grading.

Grading rule: a recorded HARDSHIP_SIGNAL stays in the expected set only
when the arbiter's recorded facts for that turn show a disclosure
(`distress`, `beyond_kind`, or `medical_leave`); otherwise it is removed.
`--drop-hardship-routing` on the replay harness. On this run it removed
the label from 16 turns and kept it on 32. Eight turns whose only recorded
label was HARDSHIP_SIGNAL with no disclosure are left with an empty
expected set and are excluded from the intent grading (the planner
recorded nothing else for them). Jev's answers on those eight: PAYMENT_RESPONSE
on the five split, partial and amount-change offers, CONFIRMATION on the
deferral agreement, HARDSHIP_SIGNAL on "next month's payment is going to be
a stretch too" and on "my hours got cut, I could do a hundred today".

| measure | §16 (routing label expected) | §17 (decision applied) |
|---|---|---|
| turns graded | 153 | 145 |
| exact set match | 96/153 (63%) | 98/145 (68%) |
| same set of target agents | 133/153 (87%) | 125/145 (86%) |
| dual turns graded / exact / same targets | 48 / 24 / 36 | 33 / 6 / 22 |
| pivot false fires | 12/97 | 1/113 |
| pivot recall | 48/56 | 24/32 |
| response false fires | 13/33 | 7/25 |
| `decide()` on Jev's facts | 87/96 | 87/96 |

Report: `benchmarks/reports/jev_ladder_replay_v2_groups_nohr/`.

Reading the residue: of the 8 pivot misses that remain, 7 are turns 1 and
2 of hardship cases where the arbiter's recorded facts carry `distress`
forward from the turn-0 disclosure, so the grading still expects
HARDSHIP_SIGNAL there and Jev, applying the "this turn" rule, does not.
That is open decision 1 (is `distress` a fact of the turn that discloses
it, or of every later turn), now the only thing between Jev and the
recorded labels in this family. The one genuine miss is "Things are tight.
I can do a hundred today and the rest in two weeks", where "things are
tight" is a borderline disclosure.

The dual-turn count fell from 48 to 33 because 15 of the recorded duals
were a payment answer plus the routing label. The dual exact number (6 of
33) is now bounded by open decision 1 and by CONFIRMATION versus
PAYMENT_RESPONSE (open decision 3), not by the pivot family.

## 18. Convention keys

The conventions in the state are a dict keyed by the rule's name (the key
is visible to Jev). The numbers used in sections 15 to 17 map as follows:

| was | key |
|---|---|
| C1 | confirmation_needs_pending |
| C2 | preface_before_question_is_one_intent |
| C3 | action_plus_contact_are_two_intents |
| C4 | first_time_miss_is_not_dispute |
| C5 | already_paid_is_dispute |
| C6 | declines_are_payment_response_not_empathy |
| C7 | repeat_vs_account_vs_faq |
| C8 | goodbye_word_is_farewell |
| C9 | availability_only_during_check |
| C10 | pivot_priority_order |
| C11 | hardship_is_disclosure_not_routing (the decision in section 17) |
| C12 | options_question_is_payment_response |

## 19. The hardship condition question (enter / continue / exit / none)

Added 2026-09-19. Motivation: the pivot family answers "did this turn
disclose a hardship". The workflow also needs to know whether the CALL is in
a hardship condition after each turn, and whether the turn moved it in or
out. Those are different facts (section 17), and the second one had no
explicit signal once HARDSHIP_SIGNAL stopped being a routing label.

`jev_decisions.hardship_condition_question()`: one choice with four options,
each carrying a definition, examples and counterfactuals:

| option | meaning |
|---|---|
| none | no condition before the turn and none created (an offer, split, partial, date, question or "I can't pay right now" with no circumstance) |
| enter | no condition before; the turn discloses a circumstance, asks to reduce what is owed, or says the amount is everything they have |
| continue | condition before; the turn stays inside it (a ladder move, an offer, an authorization, a question, a return to a hardship option) |
| exit | condition before; the borrower's own words end it (retracts the circumstance, commits to the full standard payment with no relief, withdraws from every hardship option) |

Gold for none/enter/continue comes from the recorded working memory: the
condition before a turn is `distress` in `conditions_on_record` or an active
ladder, the condition after is the same read on the next turn. The recorded
code never clears the condition (`arbiter.superseded_conditions` closes only
the two capacity facts), so there is no recorded gold for exit; every exit
Jev calls is listed for review instead.

Result on the ladder run (`benchmarks/reports/jev_ladder_replay_hardship_cond/`):

| gold \ Jev | none | enter | continue | exit |
|---|---|---|---|---|
| none | 79 | 1 | 0 | 0 |
| enter | 0 | 26 | 0 | 0 |
| continue | 0 | 0 | 49 | 0 |

Agreement 154 of 155. The one disagreement is "Honestly, 30 is all I can
manage right now", which Jev calls enter (exhaustion) and the recorded memory
did not mark as distress, although the recorded decision sent the turn to
review on the exhaustion fact; the question's definition is the more
consistent of the two.

Jev never chose exit. Checked against the turns most likely to be exits: "I
found funds, let us go back to catching up" (continue 0.93, exit 0.07, and
the counterfactual says continue because the catch-up is a hardship option),
"I do not want to discuss any arrangements now" (continue 0.93), "Put all of
it on, whatever's left over can go against the loan" (continue 0.74, exit
0.25, the only turn where exit drew any weight). The other candidates
("I changed my mind, I found enough to pay the full $202.36", "I do not want a
review, I'll pay $40 myself") occur in cases with no hardship on record, so
they are none, correctly.

What this shows: the ladder suite contains no turn that leaves a hardship
condition, which is consistent with the code never modelling one. The exit
option is defined and its counterfactuals hold on the near misses, but it is
untested on positives. The next experiment is a small scripted set of exit
turns (retraction, full payment after a disclosure, refusal of all relief)
played against a real hardship state from the recorded run, so the option is
measured on positives before anything depends on it.

Cost: one more choice question in the same request; latency unchanged.

## 20. Every improvement, in order, and what it moved

A single ledger of the steps in sections 5 to 19. Each delta is against the
row above it in the same table. Report paths are in the sections named.

### 20.1 Fact extraction (the arbiter's categorical PathFacts)

Measure: `decide()` on Jev's categorical facts (overlaid on the recorded
values) reaches the recorded action and path, on the same 96 control-valid
ladder turns.

| step | change | decide() same path | what else moved | section |
|---|---|---|---|---|
| 0 | production gpt-oss-120b on the arbiter's contrast pairs (facts bench, not the replay) | 73/101 cases, 87.8% of asserted values, p50 1,188 ms | baseline | 5.1 |
| 1 | Jev v1: 52 independent questions, production's text blocks as state | 79/96 | 91/101 on the contrast bench (95.4%), p50 192 ms, identical across 2 runs | 5.1, 8 |
| 2 | Jev v2: 28 grouped questions from the co-occurrence analysis | 87/96 | ladder_target over-fire 66 to 7, remainder_unpayable 31 to 0, temporary_income_disruption 35 to 1, unsure_which_option 11 to 1 | 5.2, 8 |
| 3 | rung fields forced to none when there is no ladder response (code rule) | 87/96 | 58 of the remaining 66 rung over-fires removed | 5.3 |
| 4 | structured state from the working memory, definitions, history as turns, glossary dropped | 87/96 | ladder_target 7 to 2, ready_to_commit 12 to 6, latency unchanged | 4.2, 8 |
| 5 | vocabulary, conventions and account facts in the state | 89/96 | the first account wording cost 2 back later (section 15) | 15 |
| 6 | C11 in the pivot criteria, inline examples everywhere | 87/96 | inside the 87 to 89 noise band since step 2 | 16 |

Net: 79 to 87 of 96, every systematic over-fire family removed. The nine
remaining differences are floor, over-cure and window rules the model cannot
know, plus three turns where Jev follows the written prompt and production
did not (section 8).

### 20.2 Primary intent, one choice question, hand-labelled fixture

| classifier | top-1 on single-intent turns | section |
|---|---|---|
| production gpt-oss-120b, production prompt | 77.5% | 6.1 |
| Jev, one choice over the definitions | 97.9% | 6.1 |

Unchanged since; every later step concerned the second intent.

### 20.3 Dual intents, six exclusive groups, hand-labelled fixture

312 collections turns, 19 of them dual.

| step | change | exact set | extras on single-intent turns | dual exact | section |
|---|---|---|---|---|---|
| a | riders: one yes/no per label | not measured as sets | 25 to 40 | 4/19 | 6.2 |
| b | runner-up of the single choice above a threshold | - | 0 to 16 | 2/19 | 6.2 |
| c | six exclusive groups with bail-outs | 267/312 | 33/293 | 8/19 | 6.4 |
| d | labelled examples in the state (cross-harvested) | 264/312 | 36/293 | 7/19 | 14 |
| e | vocabulary and conventions in the state | 277/312 | 24/293 | 8/19 | 15 |
| f | inline examples on every term, convention and bail-out; C11 in criteria | 282/312 | 18/293 | 7/19 | 16 |
| g | descriptive convention keys (rerun, no text change) | 280/312 | 18/293 | 6/19 | 18 |

Net: exact 267 to 280, extras 33 to 18, dual recall doubled against the
riders. Flow and safety false fires went to zero; contact false fires
(TRUST_CONCERN beside a dispute) 15 to 7. Step d is the one step that went
backwards. The fixture is the v2 collections workflow and is inconsistent on
four of its 19 dual turns, so dual exact is near its ceiling for this design.

### 20.4 Dual intents, six groups, recorded ladder run

153 turns with a planner answer; the expected set is the production
planner's output, so this measures agreement with production, including its
habits.

| step | change | exact set | same target agents | question false fires | pivot false fires | section |
|---|---|---|---|---|---|---|
| c | six groups | 78/153 | 112/153 | 26/135 | 4/97 | 6.4 |
| d | examples in the state | 83/153 | 114/153 | 23/135 | 5/97 | 14 |
| e | vocabulary and conventions in the state | 83/153 | 113/153 | 26/135 | 5/97 | 15 |
| e' | C12 moved from the state into the question family's criteria | 100/153 | 134/153 | 1/135 | 7/97 | 15 |
| f | C11 in the pivot criteria, inline examples | 96/153 | 133/153 | 1/135 | 12/97 | 16 |
| h | decision: HARDSHIP_SIGNAL is not a routing label (criteria, convention, grading) | 98/145 | 125/145 | 1/127 | 1/113 | 17 |
| g | descriptive convention keys (rerun) | 98/145 | 124/145 | 1/127 | 1/113 | 18 |

Net: exact 78 to 98 on a smaller, cleaner denominator (8 turns whose only
recorded label was a routing HARDSHIP_SIGNAL are excluded); same-agent
routing 112 to 124; question false fires 26 to 1; pivot false fires 4 to 1
after the decision. Step e' is the largest single move of the whole
exercise.

### 20.5 Decision questions, ladder run

| question | result | conclusion | section |
|---|---|---|---|
| direct path_decision | 70/96 exact, 90/96 top-3, against 87/96 for decide() on Jev's facts | perceive with Jev, decide in code; the 22 turns the code wins are floor, window and sequencing rules | 7 |
| single intent against the planner's recorded set | 148/153 same target agent | routes like the planner but cannot say "and also" | 7 |
| hardship condition: enter / continue / exit / none | 154/155 on the three classes with recorded gold; exit never chosen | pivot-in is decisive; pivot-out is defined with counterfactuals, the suite has no exit turns, and the code never clears the condition | 19 |

### 20.6 What did not move

Latency stayed between 150 and 220 ms p50 at every step; cost between
$0.0004 and $0.0006 per turn. Dual exact on the fixture stayed at 6 to 8 of
19 throughout.

### 20.7 Tried and set aside

| approach | why | section |
|---|---|---|
| riders as independent yes/no questions | recall without precision; fired on neighbours the format could not tell apart | 6.2 |
| thresholding the single choice's runner-up | the distribution is normalised; nothing recoverable at any threshold | 6.2 |
| labelled examples in the state | transmits the conventions of the run they were harvested from; hurt the human-labelled set | 14 |
| C11 as first written ("in a hardship conversation, a payment move also carries HARDSHIP_SIGNAL") | the recorded labels did not follow it in either direction | 16 |
| conventions in the state as the place to referee between options | no effect; the same sentence in the option text was the largest gain measured | 15 |

### 20.8 Lessons that generalise

1. Groups come from data: labels that never co-occur in recorded output are
   one choice question; labels that ride on others are yes/no.
2. Every choice needs a bail-out, and the bail-out needs its own examples.
3. A rule that decides between two options lives in the option text. The
   state can define terms and supply facts; it cannot referee.
4. Cross-field rules (a rung without a response) live in code, because
   answers are independent.
5. Perceive with the model, decide in code. Floor, window, arithmetic and
   sequencing are not the model's to know.
6. Grade against a source whose conventions are known. A recorded planner
   label can be a routing habit, and grading against it penalises the
   reading of the words.
7. Wording in the state reaches every question that shares it. A definition
   written for one family can move another.

## 21. Full configuration run and the scripted exit probe

Run 2026-09-19 with everything in one request: 28 fact questions, 2
decision questions, 6 intent groups, the hardship condition question (37
questions), structured state with vocabulary, keyed conventions and account
facts, HARDSHIP_SIGNAL graded as a disclosure label.

| measure | value | earlier best |
|---|---|---|
| `decide()` on Jev's facts, 96 control-valid turns | 88/96 | 87 to 89 |
| direct path_decision exact / top-3 | 72/96 / 89/96 | 70 / 90 |
| single intent: same target agent as the planner | 134/145 in set, 120 first | 148/153 on the old grading |
| six groups: exact set / same targets | 98/145 / 125/145 | 98 / 125 |
| hardship condition: none / enter / continue | 154/155 | 154/155 |
| fixture six groups: exact / extras / dual exact | 282/312 / 17/293 / 7/19 | 282 / 18 / 7 |
| request p50 / p95 / cost | 298 ms / 637 ms / $0.00098 per turn | 189 ms with 28 questions |

Reports: `benchmarks/reports/jev_full_ladder/`, `benchmarks/reports/jev_full_fixture/`.
The 37-question request costs about 110 ms more at p50 than the 28-question
one; the instruments (path_decision, single intent) can be dropped from a
production request, which brings it back to the 35-question shape.

### Exit probe

`benchmarks/jev_exit_probe.py`: 13 scripted turns played against 4 real
hardship states from the recorded run (ladder on split, ladder on deferral,
distress with a $60 partial staged, distress with an over-cure offered), plus
4 controls on a no-hardship state. 56 plays. Labels are the author's.
Report: `benchmarks/reports/jev_exit_probe/`.

| expected | agreement |
|---|---|
| exit (6 turns × 4 states) | 22/24 |
| continue (7 near-miss turns × 4 states) | 27/28 |
| none / enter controls | 4/4 |

Exit positives at 0.89 to 1.00 across all four states: a retraction with a
full payment, "the money came through, take the whole $202.36", "forget the
options, I'll pay the whole thing myself", "I don't want a review or a
deferral, I'm not arranging anything", "things aren't as bad as I made out".
The counterfactuals held: "I found funds, let us go back to catching up" is
continue on all four states (0.68 to 0.99), the rung decline with a
next-option request is continue at 0.98 to 1.00, the credit question and the
acknowledgement are continue at 0.98 or above, and the exit phrase played on
the no-hardship state is none at 0.97.

The two exit misses are one utterance, "My sister is covering it. Charge the
full amount today", which came back continue on two of the four states at
0.44 and 0.77: a third party covering the payment reads as help within the
hardship rather than its end. The one continue miss is "Yes, I can make the
September 22 payment" on the split state, where exit drew 0.34 against
continue; on the deferral state, where that sentence answers the rung's
question, it is continue at 0.99. Both are borderline by the author's own
reading and are listed for review rather than treated as defects.

What this establishes: the exit option works on positives that the recorded
suite never contained, and it does not fire on the near misses the
counterfactuals name. Adopting it means giving the working memory a way to
close the `distress` condition, which `arbiter.superseded_conditions` does
not do today.

## 22. The seats: Jev in the live turn path, measured on the suites

Built 2026-09-19. Two flags, both off by default; `resolve_ont` untouched.

| flag | what it replaces | module |
|---|---|---|
| `V3_ARBITER_SEAT=jev` | the arbiter's PathFacts call in the turn block and its re-surface callers | `jev_seat.py` (dispatch in `arbiter.extract_path_facts`, structured inputs handed over by the turn block in `resolve.py`) |
| `PLANNER_SEAT=jev` | the planner's intent call | `jev_planner_seat.py` (branch in `Planner.plan`) |

The arbiter seat sends one Jev request (28 fact questions, the hardship
condition, and per-turn selection questions for `addresses`, `next_ask`,
`next_tell`) and one slim LLM call for the values that Jev cannot answer
(`jev_values.PathValues`: amount, second amount, fraction, the calendar date
and ISO form, weekday offset, horizons, deferral count, months, legs,
disaster event and place, changed fields; about 15 fields, no booleans),
and assembles one PathFacts. The planner seat sends the six intent groups
and returns the same `MultiIntentLLMResponse`; speech order, the repeat
reference and entity co-extraction are not carried.

What is left for an LLM after the seats: the values call, the free-text
FAQ answer in assist, and the value remainders of the gate, rails and
referral classifiers (an identity factor, an amount, a callback time).

### 22.1 First run, N=1, seats on, against the recorded runs

| suite | past run | seat run | new fails | fixed | fail in both |
|---|---|---|---|---|---|
| ladders, 84 cases | 77 pass (`ladders_full_0914_n2`, N=2) | 70 pass | 11 | 4 | 3 |
| SCEN-HARDSHIP, 59 cases | 46 pass (`main_surf_0914_n1`, N=1) | 50 pass | 5 | 9 | 3 |

Reports: `evals/reports/jev_seat_ladders_n1/`, `evals/reports/jev_seat_hardship_n1/`.
Cases the seat fixed on the hardship slice: FINANCIAL-EXHAUSTION-ANY-AMOUNT,
GENERAL-INSTALLMENT, LONGTERM-MOD, MORETIME-REVIEW, NOPAY, OPTION-REVIEW,
PARTIAL-01, SEVEN-DAY-SEPARATE, SPLIT-CURE-PENNY-SHORT.

### 22.2 Root causes of the new failures, from the traces

1. **Values call truncated.** `max_tokens` was 1024 and one draw of three
   returned exactly 1024 output tokens for the 15-field object, so the split
   legs ("$75 today and $112.36 on September 9") came back empty with a stray
   backslash as evidence. The floor table's diagnosis; raised to 2048.
2. **The values call is not faster than the old PathFacts call.** Measured
   636 to 1,013 ms for 15 fields against 1,188 ms p50 for 60. Strict-JSON
   output overhead, not field count, sets the floor: 734 to 1,024 output
   tokens for an object that is 15 numbers and strings.
3. **Sequential seat missed the turn block's late publish.** The executor
   awaits the block only when a planner-routed agent will consume it; on
   turns routed to assist the result is published late only if the pass is
   still current. Jev then values took long enough that four turns lost
   their block facts (a global refusal, a declined mechanism). Changed to
   parallel by default; `JEV_SEAT_SEQUENTIAL=1` keeps the fed form.
4. **Chained callers re-extracted without the structured state.** When the
   seat's intents send a turn to assist first and resolve runs chained, the
   re-surface path calls the seat with only the text blocks, and the two
   readings disagree (on the settlement turn the inline pass returned no
   facts at all). Fixed with a per-turn cache in the seat: a chained caller
   gets the block's own answer.
5. **`amount_is_everything` on "is what I can manage" / "and be done".**
   The criterion inherited the prompt's loose phrase; three ladder and one
   hardship case routed to review or partial_plus_review. Criterion tightened
   to the borrower's own words about their pocket.
6. **PRIVACY_CONCERN definition cut short.** The intent definitions were cut
   at 900 characters; the prompt's own verification-refusal sentence sits
   inside the FAQ paragraph in the workflow JSON, and privacy scenarios 2 and
   3 fell past the cut. Two verification cases lost. Definitions now whole,
   and the refusal rule restated in the privacy criteria.
7. **`which_option_fits` on "What options fit?"**, and **`beyond_kind` none
   on a named wildfire**: criteria and a structural rule (income event
   disaster implies a disaster review).
8. **Values rule contradicted production**: "a yes to a read-back names no
   figure" dropped the $112.36 the borrower repeated while authorizing the
   second leg. Removed.

### 22.3 Targeted rerun after fixes 1, 3, 5, 6, 7 (29 cases, N=1)

| suite | new fails from 22.1 now passing | seat-fixed controls still passing |
|---|---|---|
| ladders | 4 of 11 (PROMISE-BELOW-FLOOR, SPLIT-DE-MINIMIS, both VERIFY cases) | 2 of 4 (SPLIT-BELOW-FLOOR and THIRDPARTY-MANAGER went back to failing; both failed in the past run too, so they are bouncers) |
| SCEN-HARDSHIP | 3 of 5 (DISASTER-RELIEF, OVER-CURE-CAP, SPLIT-CURE-MISS-REMAINDER) | 9 of 9 |

Reports: `evals/reports/jev_seat_ladders_fix1/`, `evals/reports/jev_seat_hardship_fix1/`.
The remaining ladder failures were all the chained re-extraction in 22.2
item 4 (AUTH-AMOUNT-CHANGE, REJECT-ALL-REOPEN, UNSUPPORTED-REVIEW-DECLINED,
CALLBACK-RETURN-DECLINED, RETURN-SPLIT-FAQ-CONSENT) plus OVERCURE-ABOVE-PAYOFF
(item 5, "and be done") and CATCHUP-OUTSIDE-WINDOW (a `reject` reading where
production read `question`). Fixes 4, 5 and 8 were applied after this run;
their rerun is in 22.4.

### 22.4 Second and third targeted reruns (fixes 4, 5, 8, and three more)

Fix 4 (seat cache for chained callers), 5 ("and be done"), 8 (a repeated
figure on an authorization is named), then: the response group's bail-out
told that a refusal of the pending offer or of payment discussion followed
by a question is still a response; the values prompt told that a
conditional settlement figure is the amount; and a code rule that
`financial_exhaustion` cannot ride an amount at or above the past due (the
seat has the account figures). Reports: `jev_seat_ladders_fix2/3`,
`jev_seat_hardship_fix2/3`.

Latest result per case with the seats, against the past runs:

| suite | seats, latest per case | past run | fixed by the seats | still failing that passed before |
|---|---|---|---|---|
| ladders, 84 | 75 | 77 | SPLIT-CLEARS-CURE, WAIVER-DECLINE-TODAY (+ THIRDPARTY-MANAGER and SPLIT-BELOW-FLOOR, both bouncers that failed in the past run and flip run to run) | AUTH-AMOUNT-CHANGE-FRESH, CALLBACK-RETURN-DECLINED, HARDSHIP-CATCHUP-OUTSIDE-WINDOW, HARDSHIP-REJECT-ALL-REOPEN, UNSUPPORTED-REVIEW-DECLINED-CONTINUE |
| SCEN-HARDSHIP, 59 | 54 | 46 | nine cases (22.1) plus DISASTER-RELIEF, OVER-CURE-CAP, SPLIT-CURE-MISS-REMAINDER, SETTLEMENT-NO-COUNTER recovered | REDUCED-01 (the assist agent quotes $6.51 on the interest question; a wording judge, same intents and decisions as the past run) |

The five ladder cases that remain are one mechanism, and the third rerun
shows the fix I tried for it did not take. On a turn whose first clause
refuses the pending offer or all payment discussion and whose second clause
asks a question ("I do not want to discuss any arrangements now. Just tell
me my APR.", "No specialist callback. Just tell me what September 22 adds.",
"I haven't authorized the new amount yet. Tell me what you are asking me to
approve."), the six-group planner seat returns only the question family's
label. The production planner returns two labels, and the second one routes
the turn to resolve, which is where the refusal gets recorded in the working
memory. With only the question label, the turn goes to assist, resolve never
runs, the block's facts are published late or not at all, and the refusal is
never recorded. Restating the rule in the response group's bail-out text did
not change Jev's answer on any of the three turns. Two options for the next
pass: move the rule into the response group's instructions rather than its
bail-out, or add a yes/no to the planner seat ("does the turn refuse the
pending offer or payment discussion?") whose yes maps to PAYMENT_RESPONSE.
The second is the pattern that has worked elsewhere in this document.

CATCHUP-OUTSIDE-WINDOW and CALLBACK-RETURN-DECLINED are readings the arbiter
prompt would defend (a `reject` of the catch-up with a date; an affordability
review asked for by name), graded by judges written for production's
readings. They are listed, not chased.

### 22.5 Where the seats stand

- Hardship slice: 54 of 59 against 46, the seat's largest effect, driven by
  the arbiter facts on hardship turns (section 22.1's nine plus four
  recovered).
- Ladders: 75 of 84 against 77 at this point; 77 of 84 after the refusal
  rider in 22.6, which closed the open mechanism.
- Latency: not improved. The values call costs 640 to 1,010 ms on its own,
  which is the old arbiter's cost; Jev's 0.3 to 0.4 s runs beside it. The
  turn's critical path is the values call's strict-JSON overhead, and the
  next latency lever is that call (a non-strict JSON mode, or a smaller
  model for fifteen numbers), not Jev.
- Cost: about $0.001 of Jev per turn on top of the values call.
- Nothing is committed. Both seats are off unless the flags are set.

### 22.6 The refusal rider on the planner seat

Added 2026-09-19 after 22.5. One yes/no in the planner seat's request,
`refuses_pending`: does the turn refuse the agent's pending offer or payment
discussion altogether, even if it goes on to ask a question. A yes with no
response-family label present inserts one ahead of the question label:
CONFIRMATION when an order is staged awaiting authorization (a "no" to the
read-back, which follows the rails agent, as production labels it), else
PAYMENT_RESPONSE (which reopens resolve). Reports: `jev_seat_ladders_fix4`,
`jev_seat_ladders_fix5`.

| case | before the rider | after |
|---|---|---|
| HARDSHIP-REJECT-ALL-REOPEN | fail (assist only; refusal never recorded) | pass (PAYMENT_RESPONSE + ACCOUNT_INQUIRY) |
| UNSUPPORTED-REVIEW-DECLINED-CONTINUE | fail (same) | pass (PAYMENT_RESPONSE + ACCOUNT_INQUIRY) |
| AUTH-AMOUNT-CHANGE-FRESH | fail | routes as CONFIRMATION + BALANCE_REPEAT; judge still fails because assist answers the balance question before rails re-asks, and the fresh-authorization line that invalidates the earlier $202.36 is not spoken. Production labelled the turn CONFIRMATION + CONTINUATION, so only rails spoke. The residual is the question family reading "tell me what you are asking me to approve" as a balance repeat. |
| CALLBACK-RETURN-DECLINED | fail | fail; judge on the callback slot wording after a review the arbiter prompt would open |
| HARDSHIP-CATCHUP-OUTSIDE-WINDOW | fail | fail; judge on a conditional-deferral explanation after a `reject` reading the arbiter prompt would defend |

The rider confirmed the mechanism in 22.4: the refusal now reaches resolve
on all five turns. Restating a rule in a group's bail-out text did nothing;
a yes/no of its own fixed it in one pass, as in every earlier case in this
document.

Ladders, latest per case with the seats: 77 of 84, equal to the LLM-only
run's both-of-two-runs figure and about two below its per-run rate. Three
remain that passed before, all judge wording downstream of a defensible
reading. Hardship slice unchanged at 54 of 59.

### 20.9 The seats (appended after sections 22 and 23)

| step | change | ladders (84) | SCEN-HARDSHIP (59) | section |
|---|---|---|---|---|
| s0 | LLM-only stack, recorded runs | 77 (both of two runs; ~79 per run) | 46 to 50 | 22.1, 23 |
| s1 | both seats on, first clean run | 70 | 50 | 22.1 |
| s2 | values cap, parallel seat, whole definitions, exhaustion / options / privacy criteria, disaster rule | +4 of the 11 new fails | +3 of the 5 new fails | 22.3 |
| s3 | seat cache for chained callers, repeated-figure rule, "and be done" | +1 | - | 22.4 |
| s4 | refusal-then-question in the bail-out text, settlement figure, exhaustion vetoed by the account figures | +1 | +1 | 22.4 |
| s5 | refusal rider on the planner seat (a yes/no of its own) | +2 → 77 | - → 54 | 22.6 |

## 23. Totals against the LLM-only stack, from past reports

Every LLM-only figure below is a recorded run; nothing was rerun for this
section. The suites grew over time, so only same-size runs are compared.

### 23.1 SCEN-HARDSHIP slice, 59 cases (current size since 2026-09-08)

| run | date | stack | pass |
|---|---|---|---|
| `full_n1_0908` | 2026-09-08 | LLM only, N=1 | 50/59 |
| `main_surf_0914_n1` | 2026-09-14 | LLM only, N=1 | 46/59 |
| Jev seats, first clean run | 2026-09-19 | seats, N=1 | 50/59 |
| Jev seats, latest per case after the fixes | 2026-09-19 | seats, N=1 | 54/59 |

On the earlier 56-case version the LLM-only stack scored 48 (08-21), 50
(08-26), 51 (08-28), 50 (08-31), 53 (09-01), 47 (09-02), 51 (09-06, ont
seat), 53 (09-06, FT seat). The LLM-only band on this slice over three
months is 46 to 53; 54 of 59 is at the top of it, and the seats' first clean
run is level with the best recent LLM-only run.

### 23.2 Ladders, 84 cases

| run | date | stack | pass |
|---|---|---|---|
| `ladders_full_0912_n2` | 2026-09-12 | LLM only, N=2, both runs | 64/83 |
| `ladders_full_0914_n2` | 2026-09-14 | LLM only, N=2, both runs | 77/84, 4 more flaky |
| `ladders_surf_0914_n2` | 2026-09-14 | LLM only, N=2, both runs | 76/84 |
| Jev seats, first clean run | 2026-09-19 | seats, N=1 | 70/84 |
| Jev seats, latest per case after the fixes and the refusal rider | 2026-09-19 | seats, N=1 | 77/84 |

The LLM-only "pass" is pass in both of two runs; per single run it is about
79 of 84 with the four flaky cases counted at half. The seats at 77 are
level with the both-runs figure and about two below the per-run rate. The
three cases still open that passed before (AUTH-AMOUNT-CHANGE-FRESH,
CALLBACK-RETURN-DECLINED, HARDSHIP-CATCHUP-OUTSIDE-WINDOW) are judge
wording downstream of a defensible reading, not routing or facts (22.6).

### 23.3 Caveats

- The seat figures marked "latest per case" come from five runs with the
  seat code changing between them; the single clean runs are 50 of 59 and
  70 of 84. A clean rerun of both suites with the final code has not been
  done.
- The eval suite beyond the hardship slice, 278 of 337 cases, has not been
  run with the seats. The LLM-only full-suite figure is 280 of 337
  (`main_surf_0914_n1`); there is no seat figure to set against it.
- Latency is unchanged: the values call (640 to 1,010 ms) costs what the old
  arbiter call cost, and Jev runs beside it (22.5).
- Nothing is committed. Both seats are off unless `V3_ARBITER_SEAT=jev` and
  `PLANNER_SEAT=jev` are set.

## 24. Full clean runs with the final seat code, N=1

Run 2026-09-19 after every fix in sections 22.2 to 22.6. Both seats on,
`resolve_ont` untouched, `REDIS_ENABLED=false`, default seat
`v3_resolve_ont`. Reports: `evals/reports/jev_seat_ladders_final_n1/`,
`evals/reports/jev_seat_main_final_n1/`. Zero crashed runs in either.

| suite | seats, one clean run | LLM only (recorded) | new fails | fixed | fail in both |
|---|---|---|---|---|---|
| eval suite, 337 | 284 pass, 45 fail, 8 unverifiable | 280 pass (`main_surf_0914_n1`, N=1) | 25 | 29 | 20 |
| ladders, 84 | 75 pass, 9 fail | 77 pass (`ladders_full_0914_n2`, both of two runs) | 5 | 3 | 4 |

### 24.1 How much of the movement is noise

Between the LLM-only stack's own two most recent full runs (`full_n1_0908`
and `main_surf_0914_n1`, with ledger fixes in between) 104 of 337 cases
changed status. Against that set: 20 of the 29 cases the seats "fixed" are
cases that flip on the LLM-only stack too, and 5 of the 25 new failures
are. So the fixed side is mostly bouncers, and the new-failure side is
mostly seat behaviour. The +4 headline is inside the noise; the 25 new
failures are the signal.

### 24.2 The 25 + 5 new failures, by mechanism (from the traces)

| mechanism | cases | what happens |
|---|---|---|
| A. planner seat returns NO intent | HUMAN-CALLBACK, MILITARY, REGULATORY-COMPLAINT, DISPUTE-PENDING (+ turns inside others) | 41 turns in the run came back with an empty set: callback times ("Tomorrow afternoon Pacific"), "No, that's all. Thank you.", "Yes, please", "Send me the link by text", a dispute-detail turn. The six groups were built from negotiation turns; a short answer to a non-payment question has no family, so the planner falls back and the wrong agent, or none, takes the turn. |
| B. extra rider from a group | COLLIDE-PAUSE-DISPUTE, PAUSE-RESUME (eval and ladder), PROMPT-INJECTION-AUTHORITY, TRUST-CALLBACK, HUMAN-CALLBACK, REJECT-SMS-AND-CALLBACK, PTP-ZERO-CONTINUE | PRIVACY_CONCERN on "Hold on, someone's at the door" (the contact group's overheard scenario) sends the turn to the privacy handler, which ends the call; ABUSIVE_LANGUAGE on the injection turn; DISPUTE on "this sounds like a scam"; RECORDING_REVOKE on "don't record it" (a payment promise, not audio consent); PAYMENT_RESPONSE added beside HUMAN_REQUEST / TRUST_CONCERN, which puts resolve ahead of the handler. |
| C. the hardship decision plus the tightened exhaustion criterion | HARDSHIP-SPLIT-BELOW-FLOOR (eval), HARDSHIP-TOUR-DEFERRAL and CATCHUP-OUTSIDE-WINDOW (ladders) | Production reached the review via the HARDSHIP routing label and via "30 is all I can manage" read as exhaustion; both were removed by design in 22.2 and 17, and the arbiter now reads a rung decline with a date as `promise`. The three cases followed those readings off the ladder. |
| D. facts or values differences | AMEND-DATE-ON-SCHEDULED, COURTESY-THREE-DAY-SEPARATE, DATE-PAST, PARTIAL-PROMISE-REMAINDER, DEFERRAL-CAPACITY-REASK, SETTLEMENT-NO-COUNTER, PAYAHEAD-ABOVE-PAYOFF, ACCOUNTINFO-ALLOCATION-PASTDUE, CALLBACK-RETURN-DECLINED | An amendment not applied (full_today instead of the amended scheduled day), a three-day courtesy read as today, "schedule it for yesterday" accepted, a remainder promise read as a split, the settlement figure dropped again (it passed in the targeted rerun; the values call is sampled), a pay-ahead figure not compared with the payoff, a question read as a payment answer. |
| E. judge wording with the same decisions and intents | HARDSHIP-REDUCED, OPEN-PURPOSE-QUESTION, DATE-WEEKEND, PROMISE-01, REJECT-METHOD, AUTH-AMOUNT-CHANGE-FRESH | Same route, same path; the judge objected to a line. |

Families A and B are the planner seat and account for roughly half of the
new failures. Both have the same shape as the refusal rider in 22.6: a
class of turn the six groups do not cover (A), and a rider that fires on a
neighbour it was never told to exclude (B). The fixes are of the same kind
as before: a bail-out that covers answers to non-payment asks, and negative
examples on the contact and safety groups (a hold is not being overheard;
an instruction to ignore instructions is not abuse; "don't record it" about
a promise is not recording consent).

Family C is a design consequence, not a defect: three cases that
production passed through the routing label now need the ladder executor to
hold a rung decline with a date on the ladder, which is open decision 4 in
section 11 made concrete.

### 24.3 Where the seats stand after the clean runs

- Eval suite: level with the LLM-only stack overall (284 against 280, inside
  the noise), better on the hardship slice (52 of 59 against 46) and on
  payoff (16 of 17 against 11), worse on short answers to non-payment
  questions and on the hold / injection / trust riders.
- Ladders: 75 against 77 both-of-two-runs (about 79 per run), with three of
  the five new failures being the hardship-decision consequence and two
  being judge wording.
- The planner seat is the weaker of the two. The arbiter seat's facts moved
  the hardship and payoff families up; the planner seat's empty sets and
  riders are what moved the rest down.
- Latency unchanged (22.5). Nothing committed; seats off by default.

## 25. Fixing the planner seat's empty intent sets (mechanism A of section 24)

Count from the clean eval run: 41 turns in 38 cases returned no intent;
6 of those cases failed (HUMAN-CALLBACK, MILITARY, REGULATORY-COMPLAINT,
DISPUTE-PENDING, NOTHING-ARRANGED-CLOSE, AUDIO-BARGE-STOP). The production
planner returns an empty set on the same turn shapes about half the time
(eleven of its own on "waive the late fee" alone); its fallback plan
carries those, and the seat only lost where the fallback routed differently.

What Jev returned on those turns was not six confident "none"s. Five groups
were none at 0.9 to 1.0, correctly. The response group was none at 0.30 to
0.66 with the right label as runner-up at 0.2 to 0.4 (CONTINUATION on "No,
that's all. Thank you.", CONFIRMATION on "Yes, please" to a callback offer,
PAYMENT_RESPONSE on "I've never missed before, I just forgot"). Callback
times ("Tomorrow at 10am Pacific") had the right label, AGENT_CALLBACK, in
the flow group at 0.07 to 0.32. Every one of these turns answers a pending
question that is not about paying, and the response family's options were
written around the payment ask.

Fix, criteria only: the response group's instructions now say the pending
question can be about anything and name the label a direct answer takes
(yes/no to any pending offer → CONFIRMATION; a payment-related answer or
request, including "send me the link" and "can you waive the fee", or a
first-time-miss admission → PAYMENT_RESPONSE; a closing "no, that's all" or a
supplied detail for a non-payment question → CONTINUATION); the flow group
says a day or time given for a callback is AGENT_CALLBACK, "don't interrupt
me" is a HOLD_REQUEST, and asking by name for a review is HUMAN_REQUEST; the
question group says a status question about something just done is
ACCOUNT_INQUIRY; the pivot group carries the workflow's two conventions for
the registered-email change and the predatory remark.

Re-probed on the same 41 turns with the same state: empty sets 41 → 12 after
the first pass, → 4 after the second ("Sorry, I'm back. What were you
saying?", "So you can't waive the late fee?", "Yes, I need an extension
review.", "Send me the link by text.", the last three being opening turns
with no agent line in the probe's reconstructed history).

Rerun of the six failing cases at N=1 (`evals/reports/jev_seat_empty_fix/`):
DISPUTE-PENDING, HUMAN-CALLBACK, MILITARY and REGULATORY-COMPLAINT pass;
AUDIO-BARGE-STOP is unverifiable on a text replay (phone-audio metrics);
NOTHING-ARRANGED-CLOSE now has non-empty intents on every turn and fails on
a judge line about the farewell restating the past-due status.

## 26. Fixing the rider over-fires (mechanism B of section 24)

Negatives added to four groups, criteria only: a hold ("Hold on, someone's at
the door") is a HOLD_REQUEST, not the overheard case of PRIVACY_CONCERN,
which needs the borrower to say they cannot speak freely; "don't record it"
said about a payment or a promise is not RECORDING_REVOKE; "this sounds like
a scam" is TRUST_CONCERN, not DISPUTE; an instruction to ignore instructions
is not ABUSIVE_LANGUAGE; a request for a human, a trust refusal, or a channel
refusal with a self-serve remark takes no PAYMENT_RESPONSE beside it.

Rerun of the eight rider cases with seven controls (the real privacy,
hostile, trust and recording-revoke cases; `evals/reports/jev_seat_rider_fix/`):
six targets pass (COLLIDE-PAUSE-DISPUTE, PAUSE-RESUME in both suites,
PROMPT-INJECTION-AUTHORITY, TRUST-CALLBACK, PTP-ZERO-CONTINUE, HUMAN-CALLBACK),
all seven controls still pass. REJECT-SMS-AND-CALLBACK is now a wording
judge on the executor's extra sentence with the rider gone.

COLLIDE-CANCEL-HARDSHIP needed two more things, both found in the trace.
The cancellation handler yields when a turn carries PAYMENT_CANCEL and
PAYMENT_RESPONSE together over a live order ("a replacement, not a cancel")
and hands the words to rails, which re-asks for authorization; the seat had
labelled "cancel that, I can't afford it, I lost my job" with both because
the response group read the reason as a decline. Production labelled it
CANCEL + HARDSHIP and the void ran. Fixes: the response group now says a
cancellation with a reason takes no PAYMENT_RESPONSE unless new terms are
offered; the planner seat gained a second rider, `discloses_hardship`, so a
hardship disclosed beside a cancellation or a dispute (where the exclusive
pivot family drops it) still reaches the planner's record; and PAYMENT_CANCEL
is ordered first, as planner rule P1.5 says.

Rerun of every cancellation case (`evals/reports/jev_seat_cancel_fix2/`):
COLLIDE-CANCEL-HARDSHIP passes (CANCEL + CONFIRMATION + HARDSHIP), the eight
other cancellation cases hold, and CANCEL-PRE-01, which fails on the
LLM-only stack and failed on the seats' clean run, now passes.

Mechanism B is closed. The planner seat now carries two riders (refusal,
hardship disclosure) beside the six groups; both are the same pattern: a
fact that must survive beside an exclusive family's winner is asked as its
own yes/no. Not closed from this batch: HARDSHIP-SPLIT-BELOW-FLOOR, which is
mechanism C (the hardship-label decision and the tightened exhaustion
criterion), not a rider.

## 27. Fixing mechanism C (the hardship decision's consequences)

Three cases, three causes, each read from decide()'s recorded cause:

- TOUR-DEFERRAL: on "Yes, I can afford the regular $187.36 on September 22.
  Please explain the deferral terms", the values call extracted amount
  187.36 and the date, and decide() read "a partial beyond the scheduling
  window" and switched to promise. Fix: the values prompt says the regular
  installment they can afford is not money offered; and a seat rule clears
  an amount equal to the regular payment when the turn says the next
  payment is affordable and proposes nothing else.
- SPLIT-BELOW-FLOOR: "Honestly, 30 is all I can manage right now" must read
  as exhaustion to reach the review; the tightened criterion (section 22.2,
  item 5) had removed it with the two over-fires. Fix: exhaustion restored
  for "is all I can manage" with no plan for the rest, and a seat rule that a
  split or a self-managed promise in the same turn clears it, which keeps
  SPLIT-DE-MINIMIS and PROMISE-BELOW-FLOOR on their paths.
- CATCHUP-OUTSIDE-WINDOW: the stay rule production hit ("explain fitting
  temporary relief") requires `temporary_income_disruption` on the turn.
  Production carried it from turn 1; the seat's "this turn" rule did not, and
  on top of that the seat's income-event group had read "My hours got cut at
  work last month" as a hardship with no stated recovery rather than a
  temporary disruption, so no condition was ever recorded to carry. Two
  fixes: the seat re-asserts `temporary_income_disruption` when it is already
  in the memory's recorded conditions (the words disclose it once, the state
  carries it after — open decision 1, settled for this field), and the
  income-event criteria read hours cut, shifts lost or a gap between jobs as
  temporary by nature, as production and the ladder suite do.

Reruns (`evals/reports/jev_seat_c_fix_ladder`, `jev_seat_c_fix_eval`,
`jev_seat_c_fix2_ladder`): all three targets pass. Controls: DEFERRAL-01,
KNOWN-FACTS (both suites), TOUR-REVIEW, PARTIAL-EXHAUSTION-REFER,
PROMISE-BELOW-FLOOR, SPLIT-DE-MINIMIS, FAQ-CATCHUP, RETURN-SPLIT-FROM-DEFERRAL,
REJECT-ALL-REOPEN, MEDICAL-NO-RESUME, EXHAUSTION-PARTIAL-OVER-FLOOR,
FINANCIAL-EXHAUSTION-ANY-AMOUNT, PARTIAL-01 all pass; DEFERRAL-CAPACITY-REASK
(a family-D case) now passes as well. One control failed a judge line about
the split ask, RETURN-CATCHUP-FROM-SPLIT; it is in the set that flips between
the two LLM-only runs of 2026-09-14 and its decisions match the past run.

Mechanisms A, B and C from section 24 are closed. Not rerun as full suites
since; the totals in section 24 predate sections 25 to 27.

## 28. Fixing family 1 (the seat skipped the arbiter's post-extraction steps) and what it uncovered

Family 1 was six cases whose values reached decide() in a different shape
than production's: the seat returned `assemble(facts, values)` as-is, while
`extract_path_facts` runs five steps on its output first. Reruns are
`evals/reports/jev_seat_d_fix2` (16 cases), `_d_fix4` (16) and `_d_fix5` (5).

**The seat now runs the same five steps in the same order**: `_sanitize_dates`
(a non-ISO date is dropped), `_resolve_weekday` (a day name becomes a date, code
does the calendar), `_reconcile_mechanism` (a named rail of ours withdraws
`declines_mechanism`), `_name_the_disaster` and `_name_the_settlement` (set the
review kind). Then the seat's own code rules run. Fixed by this alone:
COURTESY-THREE-DAY, DATE-PAST, SETTLEMENT-NO-COUNTER, PAYAHEAD-ABOVE-PAYOFF,
PROMISE-01 (with three values-prompt rules: a past day such as "yesterday"
is a calendar date with an ISO form so code can reject it; "a full year
ahead" is 12 months; the regular installment they can make is not an offer,
but "schedule $187.36 on the 16th" is), and a seat rule for "the rest": after
a captured payment, a self-managed date with no amount is a promise for
past due minus captured, not a split (PARTIAL-PROMISE-REMAINDER's path).

**Three more defects came out of the reruns**, each read from the trace:

1. *The projection erased Jev's date.* `bind_payment_update` drops every date
   field of a `payment_change` whose `changed_fields` lacks "date". Jev read
   scope=payment_change and amends_slot=date for "can you make it Thursday
   instead?"; the values call listed no change (a question form); the date
   was erased and the anchored ask ("which day works best?") fired instead of
   the amend. The same erasure took "Not today — I get paid Friday, can I do
   it then?" (no deal yet, so no amends_slot) and nothing was staged. Seat
   rule: under a payment_change, a named day is in `changed_fields`; an
   amended amount or instrument likewise. Which slot moved is Jev's fact; the
   values call only names the value, and a cross-field rule belongs in code.
2. *A weekday carried from history.* "Yes, I authorize that." two turns after
   "make it Thursday" came back `stated_weekday=thursday` from Jev AND
   evidence "Thursday" from the values call; the weekday became a date and
   re-fired the amend on an authorization. The `stated_weekday` passthrough
   now carries the this-turn prefix the riders already had, and the values
   prompt says a value comes from the borrower's turn only.
3. *"No, that's all. Thank you." to a read-back.* The response group had it as
   CONTINUATION (its example was the answer to "anything else?"); answering
   "Is that all correct?" it is a CONFIRMATION (nothing to change). Criteria
   now say so. Production labelled the turn CONFIRMATION+CONTINUATION.

**And one that is not the seat's.** PARTIAL-PROMISE-REMAINDER kept ending at
turn 4 with `finalize_reason=caller_farewell` while production's identical
turns did not. The farewell agent's own LLM classifier (`v3_farewell`, three
buckets: nothing_else / has_question / unclear) was captured in-run with its
exact input: "I'll pay the rest myself on Friday." after "anything else?",
with "a promise to pay $127.36 is already recorded" in the decisions block.
Direct probe on that exact input, N=5: `nothing_else` 3/5. A statement of what
the borrower will do had no bucket, and the recorded promise pushed it toward
"done". Production's pass was the other 2 of 5. Fix in `farewell.py`'s prompt
(shared by both stacks, not a seat change): a statement of what they will do
is a new topic, not a goodbye, even when the decisions already record it.
After: target 5/5 has_question; controls "No, that's all. Thanks." 5/5
nothing_else, "Correct." 5/5 unclear, "Will I get a receipt?" 5/5
has_question. A perception flake, so the rate is recorded here rather than
pinned in a unit test; the 51 existing farewell and Jev tests pass.

Two in-run values flakes were measured and not chased: PAYAHEAD-2MO's
`months_named` came back 0 once in-run and 2 on 5/5 direct probes in the
parallel form; it passed on both reruns.

**Result** (`jev_seat_d_fix4`, 16 cases, then `_d_fix5`, 5): every family-1
target passes — COURTESY-THREE-DAY, DATE-PAST, SETTLEMENT-NO-COUNTER,
PAYAHEAD-ABOVE-PAYOFF, PROMISE-01, PARTIAL-PROMISE-REMAINDER. Controls hold:
AMEND-DATE-ON-SCHEDULED, PAYAHEAD-2MO, DATE-WEEKEND, DATE-CHANGE-ON-SPLIT,
SETTLEMENT-REVIEW, PARTIAL-REMAINDER-PROMISE-BOUNDARY,
PARTIAL-REMAINDER-SCHEDULE-AMOUNT, PROMISE-DATE-TOO-FAR, WAIVER-NONQUALIFY.
AMEND-VAGUE-ASKS-THE-DAY, which fails on the LLM-only stack, passes with the
seats. Gotcha from this section: an eval rerun without `--workflow
collections_v3` replays the v2 demo and reports a plausible 6/16 — check the
agent ids in the trace before reading a table.

Families A, B, C and 1 from section 24 are closed. The section 24 totals
predate sections 25 to 28; a clean full rerun of both suites would refresh
them.

## 29. The last seat-only failures, and the seats made production code

### 29.1 The four seat-only failures (reruns `jev_seat_e_fix_eval`, `jev_seat_e_fix_ladder`)

"Seat-only" means the case passes on the LLM-only stack and failed at its
latest seat run. Each was read from the trace, not the table.

| case | cause in the trace | change | after |
|---|---|---|---|
| ACCOUNTINFO-ALLOCATION-PASTDUE | "how would a payment made now be split?" → planner seat PAYMENT_RESPONSE, Jev `info_topic=splitcure_noncure`; the word "split" pulled an allocation question into a split offer | question group and `info_topic` criteria: how a payment is applied across fees, interest and principal is ACCOUNT_INQUIRY with no topic; "split" there means allocation | pass; the two allocation neighbours (CURRENT, NEXT-PAYMENT-PASTDUE) hold |
| CALLBACK-RETURN-DECLINED | "I want a human to review my hardship" → pivot group HARDSHIP_SIGNAL (a mention, not a disclosure) sent the call to review; "I am not sure about that slot. Do not book it yet." → refuses_pending mapped to PAYMENT_RESPONSE because no order was staged, though the pending offer was a callback slot | pivot criteria and the hardship rider: the word hardship inside a request, with no circumstance, is not a disclosure. The refusal rider now maps by what is pending: an order awaiting authorization → CONFIRMATION; an open resolve ask (`call.awaiting_answer`) → PAYMENT_RESPONSE; otherwise the refused offer is another agent's → CONFIRMATION | pass; REJECT-ALL-REOPEN, UNSUPPORTED-REVIEW-DECLINED, HARDSHIP-DEFERRAL, FAREWELL-MIDAUTH, HUMAN-CALLBACK, COLLIDE-CANCEL-HARDSHIP hold |
| AMEND-VAGUE-ASKS-THE-DAY | passed by luck: Jev read "could we push it back a little?" with no `amends_slot` | the `amends_slot` question now also sees a live order awaiting authorization as the deal on the table (its instruction said "only under THE DEAL ON THE TABLE", and a staged order is rendered under `order_on_rails`) | pass, and now on the anchored move |
| AUTH-AMOUNT-CHANGE-FRESH | Jev returned no `amends_slot` for "change the amount from $202.36 to $75" for the same reason | same change: `amends_slot=amount` now returned | still fails, for a reason that is not the seat's (below) |

SPLIT-BELOW-FLOOR, claimed fixed under mechanism C but never rerun after the
exhaustion rule, was included and passes. HARDSHIP-REDUCED fails on identical
speech to the production pass (a $6.51 accrual figure the judge rejects) and
is a judge line, not a seat difference.

**AUTH-AMOUNT-CHANGE-FRESH is a wording gap that the correct label exposes.**
With PAYMENT_RESPONSE the turn reaches rails first (the active agent at the
read-back); its pivot pre-check consumes the turn block, voids the $202.36
order silently, and hands the turn to resolve, where the ont seat's
`stage_partial_today` move now finds no live order and stages the $75 with
no "that replaces the $202.36" line. Production passed this case because its
planner labelled the turn HARDSHIP_SIGNAL, which skipped rails, left the old
order live, and made the ont delegate to the legacy stage code that speaks
the replacement line. The invalidation belongs to whoever voids the order;
that is a `resolve_ont`/rails change and is not made here.

### 29.2 What "production code" means for the seats

Nothing about the questions or the state changed. What changed is how the
seats are switched on and where they live:

- **Flags are settings, not `os.environ` reads.** `settings.planner_seat` and
  `settings.v3_arbiter_seat` (`llm` | `jev`, default `llm`), plus
  `open_router_api_key`, `jev_model`, `jev_timeout_ms`, in `app/config.py`.
  The env names are unchanged (`PLANNER_SEAT`, `V3_ARBITER_SEAT`,
  `OPEN_ROUTER_API_KEY`), so the run commands in this document still work.
  The Jev client reads the key, model and timeout from settings when the app
  is importable and from the env otherwise (the benchmarks run without it).
- **The planner never imports a domain package.** The planner seat moved to
  `app/agents/collections_v3/jev_planner_seat.py`, and the workflow JSON
  declares it: `planner.seat_module` in `collections_v3.json`. The platform
  knows the verb (a seat classifies) and the domain names the module. With
  `planner_seat=jev` and no declared module the planner logs a warning and
  uses the LLM call. The arbiter seat was already inside the domain package.
- **The single-choice intent prototype left the app.** `app/agents/jev_intent.py`
  (section 2's first experiment, used only by `benchmarks/jev_intent_bench.py`
  and two tests) is now `benchmarks/jev_intent_proto.py`.
- **The sequential experiment branch is gone.** The seat runs Jev and the
  values call in parallel, the form the measurements settled on (section
  22.3); `JEV_SEAT_SEQUENTIAL` no longer exists.
- **Tests.** `tests/test_jev_seats.py` (offline, a fake `jev.decide` that
  answers from the questions' own criteria): the changed_fields rules, the
  remainder-promise rule, the no-signal floor on a failed decisions call, the
  arbiter dispatch, the refusal rider's three mappings, the hardship rider
  beside a cancellation with PAYMENT_CANCEL first, the planner's seat
  selection, and the workflow's declaration. `tests/test_jev_client.py`
  covers the client. The platform/domain boundary tests pass.

To enable on a host: set `OPEN_ROUTER_API_KEY`, then `PLANNER_SEAT=jev` and/or
`V3_ARBITER_SEAT=jev`. Either seat can be taken alone. Both are off by
default.

**One facts module (follow-up commit, same day).** The app carried two:
`jev_facts.py` (the first, 52-question version) and `jev_facts_v2.py` (the
grouped questions the seat uses), with v2 importing the passthrough choices,
the rider wording, the ladder rungs, the field list, the text-state builder
and `merge` from v1. The pieces v2 borrowed are now written into it, and it
is the app's only facts module, `app/agents/collections_v3/jev_facts.py`;
its coverage assertion derives the categorical field list from the
`PathFacts` schema itself instead of from v1's list, and checks every choice
key against the schema's Literal values. The first version moved to
`benchmarks/jev_facts_v1.py`, still the baseline the facts bench and the
`--extractor v1` replay measure. The question set was checked byte-for-byte
against the committed v2 before the move (28 questions, identical), so no
suite was rerun; a three-case smoke run with the seats on passed. Earlier
sections that say `jev_facts_v2` describe this module under its old name.

### 29.2b The first full run with the seats as production code, and what it exposed

The first full N=1 run on the productionized code (`jev_prod_*_n1`, first
pass) scored eval 304/337 against 280 LLM-only and ladders 76/84 against 77.
Seven eval and four ladder cases failed that had passed on the LLM-only
stack; three (HARDSHIP-REDUCED, OPEN-PURPOSE-QUESTION, REJECT-METHOD) are the
judge lines of section 29.1, AUTH-AMOUNT-CHANGE-FRESH is the wording gap
above, RETURN-CATCHUP-FROM-SPLIT is the known bouncer. The other six were
read from their traces; four were caused by this section's own edits:

| case | cause in the trace | change |
|---|---|---|
| SELFHARM | "with all this debt I don't want to be here anymore" → safety group EMPATHY_TRIGGER at 0.36; SAFEGUARDING had no examples in its criteria | safety criteria: any expression of not wanting to live or be here, however oblique and whatever it is attributed to, is SAFEGUARDING; in doubt, safeguarding — the cost of the miss is not symmetric |
| HARDSHIP-CALLBACK-ONLY | "I have a hardship but I don't want to go through details" lost HARDSHIP_SIGNAL to the "naming hardship in a request is not a disclosure" rule of 29.1 | the rule now distinguishes: a first-person claim of having one IS a disclosure, details or not; only the word inside a review/human request ("review my hardship") is not |
| CALLBACK-UNSURE-NOT-BOOKED (both suites) | "I'm busy, but I don't know when I'll be free" → response group PAYMENT_RESPONSE (an availability answer with no time had no home); "No, don't book tomorrow at nine. I can ask one question" → the 29.1 mapping put CONFIRMATION in front, which sent the turn to resolve instead of the handler holding the callback offer, and the decline was never recorded | flow criteria: an "I can't name one yet" answer to the callback-window question is AGENT_CALLBACK. The refusal rider's mapping: order staged → CONFIRMATION; resolve holds an open ask → PAYMENT_RESPONSE; a bare refusal → CONFIRMATION; refusal-then-question of RESOLVE's own offer → CONFIRMATION; when an interrupt handler holds the floor with an offer of its own, nothing — the label routes the turn and the handler reads the refusal from the log |
| SCHEDULE-WEEKEND-POST | "Saturday the fifth" on 2026-09-02 → values call date_iso 2026-11-07 (a Saturday, not a fifth); the arrangement was refused as too far out | seat rule: the spoken day number outranks the model's ISO date (`_spoken_date_iso` puts the named day on the calendar) |
| CALLBACK-BOOKING-PENDING (ladder) | "Is that definitely booked?" → ACCOUNT_INQUIRY (section 25 had made status questions ACCOUNT_INQUIRY); production labels it FAQ_QUESTION and the callback-status answer differs | question criteria: the status of a payment just made is ACCOUNT_INQUIRY; whether a callback just requested is confirmed is FAQ_QUESTION |
| COLLIDE-CANCEL-HARDSHIP (moved during the rerun) | response group PAYMENT_RESPONSE at 0.39 beside PAYMENT_CANCEL at 0.99; the handler reads the pair as a replacement and yields | planner seat: beside a cancellation, a PAYMENT_RESPONSE below 0.6 is dropped (a replacement offer is confident; a hedge is not) |

Reruns (`jev_seat_f_fix_*`, `jev_seat_g_fix_*`): all six pass; controls
CALLBACK-RETURN-DECLINED, REJECT-ALL-REOPEN, UNSUPPORTED-REVIEW-DECLINED,
HUMAN-CALLBACK, TRUST-CALLBACK, CALLBACK-ONLY-NO-ARRANGEMENT,
COLLIDE-SELFHARM-OVERRIDE, CANCEL-PRE-01, REJECT-SMS-AND-CALLBACK,
HUMAN-HOLIDAY, PAUSE-IDLE-NO-CALLBACK, DATE-WEEKEND, DATE-PAST,
AMEND-DATE-ON-SCHEDULED, ACCOUNTINFO-ALLOCATION-PASTDUE hold. The refusal
rider's mapping went through three forms in one day; the lesson is the one
from section 27: a rider that inserts a label must know WHO made the offer
it answers, and the state did not say. `_interrupt_holds_the_floor` reads
the graph's node statuses for that.

### 29.2c Per call, from the collections demo page

The settings flags are the host's default. A single call can choose for
itself: the initiate bodies (`POST /api/v1/twilio/initiate` and
`/api/v1/calls/initiate`) take `classification_seat: "jev"`, the API edge
validates it and preflights the OpenRouter key (503 when missing, so the
call is refused rather than quietly classified by the LLM), stores it as
`config["_classification_seat"]`, and stamps `jev_classification` onto the
record's `agent_variant` so the call log can filter on it. One resolver,
`app.config.classification_seat(config, which)`, answers "which seat does
THIS call use" everywhere: the planner is built with the call's config
(`Planner(runtime, call_config=config)`), and every `extract_path_facts`
call site hands the call's config over (resolve's turn block and inline
re-extraction, rails' pre-check, the ont seat's inline extraction, the LLM
executor's two sites). The collections demo page has a **Classification**
toggle (LLM / Jev) beside the Resolve Seat toggle; it sends the field only
when Jev is chosen. No new flag: the per-call value outranks the settings,
in both directions.

### 29.2d What a seat call leaves in the call log

Reviewed against the LLM-only path after the per-call wiring. Unchanged in
shape: `intent_classified` (the planner seat returns the same response
model; the reasons read `jev <group> <confidence>` instead of a sentence),
`facts_extracted` (the FACTS event lists the non-default fields; the
reasoning is prefixed with the hardship-condition answer), `path_decided`
and everything after it. Already traced: the values call, tag `v3_values`,
because it goes through `llm_structured_call`.

Missing until now: the decisions call itself. `jev.decide` only wrote a
process-level log line, so a seat turn's call log showed one `llm_called`
row where production shows one, the sidecar hid the parallel ~300 ms, the
metrics sink never saw the cost, and Jev's answers with their probabilities
— the perception surface that section 1 of CLAUDE.md's trace rule asks for
— were recorded nowhere. Now every decisions call emits the same trace
record an LLM call does: an `llm_called` row (tags `jev_planner`,
`jev_seat`), a sidecar row whose user message is the state, whose system
prompt is the question set and whose response is every answer with its
probabilities, and a metrics emission priced from the cost the API
reported (`LLMTraceRecord.cost_usd`; the tracer falls back to the Cerebras
rates when a record carries none, which is every LLM call). The metrics rows
carry `pricing=api` or `pricing=rate`; the dashboard re-prices only the
latter from tokens, because a decisions call's ~8k input tokens at the
Cerebras rate would have overstated it several times over. The call-log
page renders the rows with its existing LLM badge. The eval harness does
not install a tracer, so its traces are unchanged.

### 29.3 Final results against the last LLM-only runs

Filled from `evals/reports/jev_prod_main_n1` and `jev_prod_ladders_n1` (N=1,
both seats on, every case), against `main_surf_0914_n1` and
`ladders_surf_0914_n2`:

| suite | seats on (final run) | LLM-only | fixed | new fails | fail in both |
|---|---|---|---|---|---|
| eval (337 cases) | **301** pass, 29 fail, 7 unverifiable | 280 pass, 6 unverifiable | 30 | 9 | 27 |
| ladders (84) | **76** pass | 77 pass (any-of-two 80, both 76) | 3 | 4 | 4 |

By category on the eval suite (seats / LLM-only / n): HARDSHIP 51/46/59,
PAYOFF 16/11/17, COLLIDE 11/10/11, DATE 5/4/5, PARTIAL 8/8/8, CALLBACK 3/4/4.

Two full runs were made on the productionized code; the first (section 29.2b)
scored 304 and 76 before the six fixes there, the second 301 and 76 after. 30
of the first run's 31 fixed cases are fixed again in the second; the new-fail
sets differ almost entirely between the two (7 then 9, 3 in common), which is
what the 104-case flip between the two LLM-only runs of 2026-09-14 predicts
for N=1. The nine new eval failures of the final run, read from their traces:

- **Same labels as production, downstream or judge**: FAREWELL-MIDAUTH,
  REFUSAL-GOODBYE, OPS-AUTOMATION-HOLD (identical intents; rails, farewell
  and assist wording), HARDSHIP-REDUCED, OPEN-PURPOSE-QUESTION, REJECT-METHOD
  (identical speech to the production pass), CALLBACK-BOOKING-PENDING (now
  FAQ_QUESTION as production labels it; the judge objects to the callback
  handler's "within two business days" line on the re-surface).
- **Values-call flakes**: SCHEDULE-14-DAYS ("the full $202.36 scheduled
  September 16" read as a pay-ahead on this draw; passed on the first run),
  SETTLEMENT-NO-COUNTER (the conditional settlement figure; passed on the
  four targeted reruns of section 28).

The four new ladder failures: AUTH-AMOUNT-CHANGE-FRESH (section 29.1, not
the seat's), RETURN-CATCHUP-FROM-SPLIT (the known judge bouncer),
NEGOTIATION-PARTIAL-FROM-DATE (values: a $75 partial kept the earlier
September 9 instead of today), NEXT-PAYMENT-AT-RISK-LATE ("Things are
tight. I can do a hundred today" carries no HARDSHIP_SIGNAL under section
17's disclosure rule, where production's planner labelled it hardship — the
convention difference that decision accepted).

**Verdict.** The seats improve the eval suite by 21 cases at N=1 and hold
the ladders within one case, on the same code that ships the seats off by
default. Latency is unchanged (the values call is the floor, section 22.3).
Committed with the seats off; enable per host with the two env flags.

## 30. The line picker for resolve

A separate design: `voice-gateway/docs/JEV_LINE_PICKER_DESIGN.md`.

## 31. The seat request, key by key and question by question (record of 2026-09-22)

Both seats send the same state object (`jev_state.build_state`). Definitions, vocabulary and conventions are sent with it; they are summarised here and printed in full by `build_state`.

### 31.1 The state

| key | definition sent with it |
|---|---|
| today | The call's date. Dates in the borrower's turn are relative to it. |
| call.path | The arrangement the call is currently working towards. 'negotiating' means no arrangement has been chosen yet. |
| call.awaiting_answer | The items the agent's last question asks for. A direct yes or no in the borrower's turn answers THESE and nothing else; when empty, no question is pending. |
| call.still_to_collect | Fields the arrangement still needs from the borrower. |
| call.already_answered | Fields already answered earlier in the call; a follow-up narrows them, it does not re-answer them. |
| call.conditions_on_record | Circumstances already recorded on earlier turns (for example 'distress'). They are NOT this turn's facts; only the borrower's turn can disclose something this turn. |
| call.identity_verified | Whether identity verification has passed. |
| call.sms_blocked | True when the borrower has already asked not to be texted; a payment link by text is then not an option. |
| call.already_recorded | An outcome already recorded on this call, if any: a specialist review opened, a promise to pay recorded, or the call closed without an arrangement. |
| hardship_ladder | Present only while the hardship options are being offered in order. 'on_table' is the rung the agent just offered and is the ONLY rung a ladder response can be about; 'declined' lists rungs already r… |
| hardship_ladder.pending_question | The question the offered rung asks. |
| order_on_rails | A payment order staged for our rails, if any: amount, type, date, channel, and whether it is still awaiting authorization or already executed. Null when nothing is staged. |
| rails_result | What the rails last did on this call: captured, scheduled, declined, payment_failed, self_serve, or null. |
| deal_on_table | The standing deal under read-back, with its slots. An amendment moves ONE slot of THIS deal; null when no deal stands. |
| last_deal | A previous deal and what became of it (accepted, superseded, withdrawn). |
| specialist_review_topic | The topic of a specialist review already opened, if any. |
| conversation | The last turns, oldest first. 'agent' lines are the lender's agent; 'borrower' lines are the caller. The borrower's newest turn is NOT here — it is 'borrowers_turn'. |
| borrowers_turn | The turn to classify. Every question is about these words. |
| account | The borrower's figures on this call, for reading what a figure in the turn refers to. A dollar figure the borrower names is still a NAMED AMOUNT even when it equals past_due; only 'the whole thing', … |

Shape:
```
state
├── definitions, vocabulary (16 terms), conventions
├── today
├── account: past_due, regular_payment, floor, payoff, late_fee, next_due_date, days_past_due
├── call: path, awaiting_answer, still_to_collect, already_answered, conditions_on_record, identity_verified, sms_blocked, already_recorded
├── hardship_ladder: active, resolved_choice, on_table, offered, declined, next, pending_question   (only when a ladder is active)
├── order_on_rails: amount, type, date, channel, status, second_leg
├── rails_result
├── deal_on_table: kind, slots        last_deal: kind, slots, status
├── specialist_review_topic
├── conversation: last 6 turns, {agent}/{borrower}, oldest first
└── borrowers_turn                    always last
```

Vocabulary terms: acknowledgement, confirmation, dispute, floor, hardship, ladder, late fee, our rails, past due, payment answer, payoff, read-back, regular payment, rung, self-managed payment, specialist review.

Conventions (keys): action_plus_contact_are_two_intents, already_paid_is_dispute, availability_only_during_check, confirmation_needs_pending, declines_are_payment_response_not_empathy, first_time_miss_is_not_dispute, goodbye_word_is_farewell, hardship_is_disclosure_not_routing, options_question_is_payment_response, pivot_priority_order, preface_before_question_is_one_intent, repeat_vs_account_vs_faq.

### 31.2 The arbiter seat: 28 fact questions

Grouped choice questions (the option writes the PathFacts fields shown):

- **scope**: unspecified → payment_update.purpose=unspecified | payment_change → payment_update.purpose=payment_change | account_question → payment_update.purpose=account_question | capacity_constraint → payment_update.purpose=capacity_constraint
- **money_shape**: none | full_amount → commits_full_amount=True | catch_up → commits_catch_up=True | partial | split → proposes_split=True | payoff → wants_loan_closed=True | settlement → offers_settlement=True | pay_ahead → wants_pay_ahead=True | deferral
- **timing**: none | today → explicit_today=True, amount_is_today=True | calendar_date → names_calendar_date=True, amount_is_today=False | weekday → amount_is_today=False | relative → amount_is_today=False
- **channel**: unspecified → payment_update.mechanism=retain | our_card → names_our_rails=True, payment_update.mechanism=agent_payment | our_link → names_our_rails=True, wants_link=True, payment_update.mechanism=link | asks_us_to_schedule → asks_scheduled_payment=True, names_our_rails=True, payment_update.mechanism=agent_payment | self_serve_app → pays_self_serve=True, payment_update.mechanism=self_pay | self_managed → declines_mechanism=True, payment_update.mechanism=self_pay
- **open_answer**: none → open_answer=none | yes → open_answer=yes | no → open_answer=no | unclear → open_answer=unclear
- **ladder_response**: none → ladder_response=none | accept → ladder_response=accept | reject → ladder_response=reject | question → ladder_response=question | reconsider → ladder_response=reconsider | unclear → ladder_response=unclear
- **ladder_target**: none → ladder_target=none | current → ladder_target=current | catchup → ladder_target=catchup | split → ladder_target=split | deferral → ladder_target=deferral | all_payment_discussion → ladder_target=all_payment_discussion | specialist → ladder_target=specialist
- **ladder_second_reject**: none → ladder_second_reject=none | catchup → ladder_second_reject=catchup | split → ladder_second_reject=split | deferral → ladder_second_reject=deferral | review → ladder_second_reject=review
- **asks**: nothing | next_option → ladder_next_requested=True, asks_payment_options=True | options_in_general → asks_payment_options=True | which_option_fits → unsure_which_option=True | explainer | payoff_quote → asks_payoff_quote=True | account_figure | human_or_callback
- **refusal_scope**: none | one_option | no_reason_given → rejection_reason_unknown=True | all_payment_talk → refuses_payment_discussion=True | all_contact → refuses_all_contact=True | finished → declines_further_help=True
- **income_event**: none | temporary_disruption → distress=True, temporary_income_disruption=True | permanent_reduction → distress=True, beyond_permanent=True | medical_leave → distress=True, medical_leave=True | disaster → distress=True | hardship_other → distress=True
- **next_installment**: not_mentioned | affordable → can_resume_next_payment=True | at_risk → next_installment_at_risk=True | hedged
- **today_capacity**: unspecified | nothing_today → cannot_pay_anything_today=True | amount_is_everything → financial_exhaustion=True | rest_cannot_be_promised → remainder_unpayable=True

Pass-through choice questions (the option is the field value):

- **ladder_second_reject**: none | catchup | split | deferral | review
- **info_topic**: none | fullcure_cost | partial_noncure | partial_installment | splitcure_noncure | splitcure_miss_remainder | deferral_fee | deferral_terms | autopay_coverage | hold_status | ops_hold | interest_waiver | installment_after_options | registered_email_issue | predatory_editorial | fee_waiver_request
- **beyond_kind**: none | affordability | settlement | disaster | modification | plan
- **legal_hold_kind**: none | bankruptcy | attorney | cease_and_desist
- **amends_slot**: none | date | amount | second_date | second_amount | instrument
- **stated_weekday**: none | monday | tuesday | wednesday | thursday | friday | saturday | sunday

Yes/no questions (each sets the boolean of the same name): ready_to_commit, asks_credit_promise, refuses_sms_only, objects_to_late_fee, claims_first_time_fact, offers_dispute_withdrawal, asks_non_positive_amount, reports_existing_payment, social_only, wants_callback_only.

Plus `hardship_condition` (none | enter | continue | exit → `PathFacts.hardship_condition`) and, when the executor has a ready set, `addresses:<field>` per open field and `next_ask` / `next_tell` choices.

Code rules after the answers: rung fields forced to none without a ladder response; a spoken day number outranks the model's ISO date; the mechanism reconciled with the channel; a payment change with a named day gets `date` in changed_fields; disaster and settlement names filled from the state.

### 31.3 The values call (LLM, strict JSON): evidence, amount, second_amount, fraction_named, date_spoken, date_iso, stated_weekday_offset, date_horizon_days, remainder_horizon_days, defer_payments, months_named, split_legs_named, disaster_event, disaster_location, changed_fields.

### 31.4 The planner seat: six intent groups plus two riders

- **response**: CONFIRMATION | PAYMENT_RESPONSE | CONTINUATION | AVAILABILITY_RESPONSE | IDENTITY_FIELD | none — "Does the borrower's turn ANSWER what the agent just asked, and how? The agent's pending question can be about ANYTHING — a payment, a read-back, a callback win…"
- **question**: ACCOUNT_INQUIRY | BALANCE_REPEAT | FAQ_QUESTION | none — "Does the borrower ASK something this turn, and what kind? Asking what payment options exist, what the next option is, or what else can be considered while nego…"
- **pivot**: DECEASED_SIGNAL | LEGAL_HOLD_SIGNAL | DISPUTE | PAYMENT_CANCEL | CO_BORROWER_SIGNAL | HARDSHIP_SIGNAL | none — "Does the turn carry a signal that changes what the call is doing? When more than one applies, pick the first in this order: deceased, legal hold, dispute, canc…"
- **contact**: DNC_REQUEST | RECORDING_REVOKE | SMS_DECLINE | PRIVACY_CONCERN | TRUST_CONCERN | none — "Does the turn restrict CONTACT or CONSENT, or question the call's legitimacy? Pick the widest that applies: a do-not-call outranks a text opt-out. NOT this fam…"
- **flow**: LANGUAGE_REQUEST | HUMAN_REQUEST | AGENT_CALLBACK | HOLD_REQUEST | FAREWELL | none — "Does the borrower ask to change HOW the call continues? A day or time the borrower gives in answer to the agent's callback-window question — or an answer that …"
- **safety**: SAFEGUARDING_SIGNAL | ABUSIVE_LANGUAGE | EMPATHY_TRIGGER | none — "Is there a safety or conduct signal? Self-harm risk outranks abuse, which outranks distress needing empathy. SAFEGUARDING_SIGNAL is any expression of not wanti…"
- riders: `refuses_pending` (adds PAYMENT_RESPONSE when no response label was chosen), `discloses_hardship` (adds HARDSHIP_SIGNAL).

Code rules after the answers: CONFIRMATION only when something is staged or an ask is pending; a PAYMENT_RESPONSE below 0.6 beside PAYMENT_CANCEL is dropped; PAYMENT_CANCEL is ordered first.

## 32. Retrospective, 2026-09-22 — what moved the seats, and the cost measurement

The full ledger is §20 (facts and intents, step by step), §22–§29 (the seats in the
live turn path) and §30 (the line picker). This section is the short record.

**What worked, with the number behind it.** Grouping questions from co-occurrence
data (facts 79→87/96; dual-intent recall 4→8/19). Rules that referee between options
in the option text, not the state (recorded-run exact agreement 83→100/153 from one
moved sentence). A structured, defined state instead of text blocks (ladder over-fires
removed at no latency cost). A bail-out option on every choice. Cross-field rules in
code.

**What did not, despite expectation.** Riders as independent yes/no questions
(25–40 false extras). Thresholding a runner-up (nothing recoverable). Labelled
examples harvested from a recorded run (the one step that went backwards, 267→264).
Jev choosing the path or the line directly (lost to code both times). Live-turn
latency (the LLM values call at 640–1,010 ms is the floor).

**Final suite numbers, N=1, both seats on** (`main_surf_0914_n1`, `jev_prod_*_n1`,
`txmem_*_n1`):

| suite | LLM-only | seats, 2026-09-19 | seats after the memory port, 2026-09-21 |
|---|---|---|---|
| main, 337 | 280 | 301 | 306 |
| ladders, 84 | 77 | 76 | 79 |

**Per-turn cost, measured 2026-09-21** (11 payment-flow cases, 34 turns, both
stacks, tokens from the client log lines, Cerebras at config's $0.35/$0.75 per 1M
with cached input at the full rate; Jev provider-priced at $0.042/1M):

| | LLM-only | Jev seats |
|---|---|---|
| classification calls per 1,000 turns | $15.0 (planner + arbiter) | $2.2 (values call $1.1 + Jev $1.1) |
| whole turn, every agent LLM call | $16.0 | $3.2 |
| per replaced call | arbiter $0.0085, planner $0.0055 | $0.0005, $0.0006 |

That is 6.8x on the calls replaced and 5.0x on the turn; the "40x" and "400x"
figures are per-token prices, not per-turn measurements. A cache discount on the
LLM side would lower the ratio, not raise it.

## 33. The gate's recording question, and the voicemail on a full-duplex line (2026-09-23)

**Terms.** *Recording*: a voicemail greeting, answering machine or carrier
announcement heard as the call's first words. *Party field*: the gate's
four-way LLM answer (borrower / wrong_party / voicemail / unclear). *Opening*:
the agent's first line ("...on a recorded line. May I speak with Alex
Morgan?"), played as a PROTECTED, ABSORB-ALL playout. *Absorbed*: speech the
gateway heard during a playout and logged but never queued as a turn.

### 33.1 Why a second question

Direct probes at N=5 (`voicemail_bench.py`, `voicemail_jev.py`,
`voicemail_jev_counter.py`, 2026-09-23):

| instrument | 14 common greetings | 8 live neighbours | 16 hard live | 14 hard recordings |
|---|---|---|---|---|
| gate party field == voicemail | 9/14 stable on all 5 draws | 8/8 never | — | — |
| Jev noul "is this a recording?" at 0.40 | 14/14 | 8/8 | 14/16 | 12/14 |

The party field's five flips were first-person personal greetings ("Hi, this
is Alex. Sorry I missed you. Leave a message.") and the carrier announcements
that never say "message". The question's four misses sat in a 0.35–0.55 band:
a live business answer ("You've reached Morgan Plumbing, this is Alex..."
0.34–0.42), "not around the office" (0.47–0.52), and two recordings with no
absence cue ("This is Alex's phone. He'll get back to you." 0.13–0.16; "Yo,
you know what to do." 0.21–0.25).

### 33.2 The seat

`collections_v3/jev_gate.py`, selected by `V3_GATE_VOICEMAIL_SEAT=jev` (or the
call's `_classification_seat`). On the PARTY stage only, the gate runs the
question in parallel with its LLM classifier and reconciles in code:

    p >= 0.60   voicemail, whatever the LLM's party
    p <= 0.30   never voicemail — an LLM `voicemail` becomes `unclear`, and
                the party re-ask is the presence probe
    between     the LLM's party stands
    JevError    the LLM's party stands (the seat never guesses)

State: `{account_holder, first_turn}` with definitions. Cost $0.000028 per
question, 347 ms measured in-call (parallel with the LLM, so the turn costs
max of the two). Tests: `tests/test_v3_gate_voicemail_seat.py` (16).

### 33.3 The end-to-end call, and what it showed

`data-plane/tests/gateway_e2e/voicemail_duplex_call.py` runs the turn service and
the gateway in one process with the Twilio dial stubbed, and a simulated
media stream that plays a TTS voicemail greeting (a different voice) from the
FIRST frame while the opening streams out, with marks, clears and a 0.3-gain
line echo. Three calls, all with the seat on:

| run | what the far end did | what happened |
|---|---|---|
| voicemail, default barge | "Hi, you've reached Alex Morgan. I can't take your call right now, so leave a message..." + beep at 8.4 s | opening audible 0.9–6.6 s; the greeting's STT final landed at 8.8 s tagged `intro_absorb` (source floor_controller); **no utterance ever reached the gate, the question never ran**; silence probe at 18 s, silence exit at 49 s, `call_ended reason=silence_timeout` at 53 s. No voicemail outcome, no message left. |
| live control | "Hello?" at 0.3 s, then "Yes, this is Alex Morgan." 0.8 s after the opening finished | "Hello?" absorbed with the opening (by design); the confirmation reached the gate at turn 1: Jev p=0.02 in 347 ms, LLM borrower, reconciled `live` → borrower; the call went on to the mini-Miranda and availability. |
| voicemail, energy gate disabled | same greeting, `barge_in_rms_threshold` unreachable | identical: absorbed at 9.0 s; the silence probe's own echo was classified `echo` and absorbed too. |

**The finding.** The opening is `SpeakCommand(protected=True,
absorb_barge=True)` (lifecycle.py): every caller-speech verdict except
`safety` is absorbed while it plays, so a greeting that starts at answer and
ends before or shortly after the opening is one absorbed final. The seat, the
party field and the barge gate are all downstream of that door and never see
it. Disabling barge-in changes nothing: the absorb is applied to the STT
final by the opening's policy, not by the energy gate. Today's production
behaviour on a voicemail is therefore a ~53 s call ended by the silence
ladder, with the outcome recorded as `silence_timeout`.

**What has to change (decision pending).** The opening's absorb policy must
distinguish the far end talking OVER our greeting from the far end having
spoken FIRST: a substantive utterance whose onset precedes our first outbound
audio should be deferred (queued, answered right after the opening — the
protected non-absorb path already does exactly this), not absorbed;
backchannel-length "Hello?" stays absorbed and `safety` still cuts. With that,
the gate hears the greeting ~2 s after the opening ends and the question
decides. Two things it does not solve: the voicemail message would begin
whenever the gate answers, not after the beep (only AMD's DetectMessageEnd
knows the beep), and a recording that is still playing when we speak will be
heard as barge by the next playout.

**Harness caveats.** (1) `_finalize_call` logs an analytics FK failure because
the initiate route was bypassed and no CallRecord exists — a harness artifact.
(2) At echo gain 0.3 the far end's transcript carried fragments of our own
opening ("-- a recorded line"); in the live run the whole opening was
transcribed inside the caller's turn. The simulated echo is a clean 180 ms
copy the AEC does not fully cancel; a real line's residual is unmeasured here.

## 34. The escalation offer's answer (2026-10-01)

The platform's `EscalationAgent` (`app/framework/agents/escalation.py`) offers
a human specialist after repeated re-asks and reads the caller's reply. Until
2026-10-01 the reply was a substring keyword list (`yes, yeah, sure, please,
okay, ok, go ahead, connect, transfer, do that, …`), so "I'm broke" ("ok"
inside "broke"), "No, please don't", "That's not okay" and "I don't want a
transfer" each TRANSFERRED the caller.

It is now one `choice` question, `accept | decline | unclear`, over a state of
the definitions, the last four spoken lines (ending with the offer) and the
reply. The contrast pairs live in the option text ("a 'please don't', 'not
okay', or 'I don't want a transfer' is a decline, whatever words it
contains"); `unclear` names the non-answers. `unclear` → one re-ask
(`OFFER_REASK_CAP`), then a decline. A failed decisions call is `unclear` —
the seat never guesses, and the safe default is no transfer. There is no LLM
seat for this question: the old path made no model call at all, and the
fallback is the unclear rule, not another classifier.

**Probe, N=5 direct calls, 17 replies with the real offer line as the
preceding agent turn** (`data-plane/benchmarks/jev_offer_probe.py`, run with the venv Python from `data-plane/`, 2026-10-01):

| expected | reply | held | mean p |
|---|---|---|---|
| decline | No, please don't. | 5/5 | 1.00 |
| decline | That's not okay. | 5/5 | 0.80 |
| decline | I don't want a transfer. | 5/5 | 1.00 |
| decline | No thanks, I'll keep going. | 5/5 | 1.00 |
| decline | Nah, let's just continue with you. | 5/5 | 1.00 |
| decline | I'm broke. | **0/5 → unclear 5/5** | unclear 0.99 |
| accept | Yes please. / Sure, go ahead. / That would be great, connect me. / Yeah, I guess so. | 5/5 each | 1.00 |
| accept | Okay. | 5/5 | 0.79 |
| accept | Only if it's quick. | 5/5 | 0.97 |
| unclear | Hello? / What? / Sorry, who is this? / Can you repeat that? | 5/5 each | 1.00 |
| unclear | Do I have to? | 5/5 | 0.97 |

16/17 held 5/5; latency 120–400 ms. The one miss is a label dispute, not a
flake: "I'm broke" is a hardship statement, not an answer, and `unclear`
re-asks once and then declines — the outcome that matters is that it is no
longer an accept. Not tuned toward the probe's own label.
