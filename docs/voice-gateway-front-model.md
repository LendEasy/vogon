# Voice Gateway — the Front Model replaces the filler (design, r3)

**Status:** r3, 2026-09-18, amended 2026-09-19 (§3.1 the rules tier, §8 the length caps, §8.1 repetition; ONE sentence, never two; the polarity rule is the reason the model exists; the front speaks only into OUR silence — the borrower's silence stays with the ladder). **v1 BUILT 2026-09-19 — see §0.** Supersedes r2. The analysis is done: the prompt
was judged on 20 ladders across seven prototype runs
(`voice-gateway/prototypes/front_model/RESULTS.md`), the canned filler now
rides the phone path (commit `69ad9c4`), and the wiring below reuses the
filler's own machinery end to end. This revision is the build spec. It
restates the call flow with the functions that carry it, and lists what
changes by file.

**What changed since r2**

- The filler is not *replaced beside* the front model; the front model **is
  the filler's new source of words**. The hold that opens on every final,
  plays phrases through the transport, never cuts them mid-word and reports
  a barge — that is the scheduler r2 designed, and it already exists.
- The front line reaches the call log through the **same three rows every
  playout gets**: `SPEAK_EMITTED` (intended) at the turn, `SPEAK_DELIVERED`
  or `SPEAK_INTERRUPTED` (what was heard) from the outcome fact, joined by
  the existing `inflight_playouts` registration. Today's filler produces
  none of them because the hold plays through `transport.send_response`
  directly instead of the driver's `_send_with_metrics`; that one routing
  change is what puts it in the ledger.
- The r2 open questions are closed: authorizing confirmations get a beat
  (§7 p.12), a beat of four words or fewer may repeat (p.11), the borrower's
  name follows a verified flag the turn service sends, the output shape is
  `{"say": ...}`, the model is `qwen-3.8-27b` with reasoning off.

---

## 0. v1 as built (2026-09-19) — the drop-in, plus the outcome row

What shipped is the drop-in the design's "reused unchanged" table describes,
plus the smallest change that gets the line into the call log. Default ON;
`FRONT_MODEL_ENABLED=0` in the gateway env restores the canned filler.

**Gateway** (`voice-gateway/vogon/gateway/transport/`)

- `front_source.py` — `FrontModelSource`: primes on the eager (or the final
  when no eager preceded it), one guarded sentence from `qwen-3.8-27b` with
  reasoning off, the tier-0 pools with rotation, the ALREADY SAID list, the
  spacing rule, `front-<n>` ids, per-call metrics. `FrontConfig` loads the
  domain artifact and lints every pool at load.
- `front_guard.py` — §8 verbatim, including the commitment rule anywhere
  in the line and the three-word opener rule.
- `clocks.py` — the hold takes a line source (`install_line_source`); with
  one installed it opens on every final whether or not a `FillerPolicy` was
  configured; `_run_source_hold` = the model's line as soon as ready, the
  `hold` beat at `front_beat_after_ms` if not, one `wait` beat after
  `front_wait_again_after_ms`; the hold's speak callable is re-routable
  (`install_hold_speak`).
- `handler.py` — builds the source after `start_call` (audio modes only),
  routes hold playouts through `_send_with_metrics(front=True)` so each one
  spools a `DeliveryOutcome` fact carrying the text, primes on
  `UtteranceEager`, feeds + primes on `UtteranceFinal`, cancels on
  `UtteranceResumed`, feeds our delivered lines to the source's transcript.

**Contract** — `DeliveryOutcome.text` and `.origin` ("front"), additive.

**Turn service** — `executor.apply_gateway_facts`: an outcome with
`origin == "front"` and no registration writes ONE `SPEAK_EMITTED` row
(`reason=front`, the intended text, `delivery_status`, `heard_text`,
`sentences_sent`), adds the persona line to transcript.txt, and appends a
`_front` transcript turn. `_front` is excluded from the agents' history,
the re-ask, and `history_block` (`views.py`) in v1 — the row lands one
request after the line played, so it would sit after the reply it preceded.

**Domain artifact** — `data-plane/app/agents/collections/front.yaml`: the
prompt (the prototype's one-sentence r2), two beat pools, `must_not_say`.

**Config** — `FRONT_MODEL_ENABLED` (default on), `FRONT_MODEL`,
`FRONT_MODEL_EFFORT`, `FRONT_CONFIG_PATH`, and the four timing constants in
`voice-timing-constants.md` §8 (checked by its test).

**Verified** — `tests/test_front_guard.py`, the source-hold tests in
`test_call_clocks.py`, the contract round-trip with the new fields, and
`test_phone_fillers_conversation.py::test_a_phone_call_gets_a_front_line_and_it_reaches_the_call_log`
(the real driver in Twilio mode through the real `/turn/v1`, the model
scripted). Live: `tests/smoke_fillers_live.py` with real Deepgram audio and
the real model — "What are your office hours?" → Qwen in 530 ms → "Let me
pull that up for you, one moment." played as 105,600 bytes of TTS before
the answer.

**The double acknowledgement, resolved differently.** It is a registry
property: the desk's soft templates open with the same sentence the front
just said. `FRONT_EMPATHY_SPLIT.md` proposed splitting the empathy clause out
to the front; it was superseded, not built. `FRONT_COMPLEMENT_DESIGN.md`
dropped the front's sympathy tier instead — on distress the gateway stays
silent and the desk keeps its own acknowledgement.

**Deferred from the spec, on purpose** (the "what am I missing" review):
the `front_line` request annotation (agents do not know the front spoke
this turn — the double-acknowledgement risk, 4 of 37 lines in r6, is
accepted for the trial); the verified flag (the borrower's name is a
forbidden token, never conditional); the executor registration and the
ordered history row; `filler_mode` on the control plane (the env flag
stands in). §3–§6 below remain the full design those would complete.


## 1. Definitions

**Terminology.** The person on the line is **the borrower** (collections
calls are outbound: we called them). "Caller" in the contract types
(`UtteranceFinal`, the STT callbacks) means the same person; it is kept
there because the gateway serves inbound widget calls too. **Our silence**
is the reasoning gap: the borrower has spoken and we owe a reply. **The
borrower's silence** is after our line ended and they have not answered.
The front model speaks only into our silence. The borrower's silence
belongs to the silence ladder (8 s log, 10 s probe with re-ask, 37 s exit),
unchanged.


- **Front model** — a small LLM the gateway calls once per trigger. Its
  output is the *entire* thing the agent will say, as text, or empty. Its
  words come from a domain prompt on `CallConfig.front.prompt`.
- **Front line** — the ONE sentence played as a result, as an ordinary
  unprotected playout with a gateway-minted `audio_id` (`front-<n>`), cut
  by barge-in like any other. Never two sentences: the second is the one
  that turns a filler into a readback, a promise or a ruling.
- **Line source** — the hold's supplier of phrases. `CannedSource` is today's
  random iterator over `FillerPolicy.phrases`; `FrontModelSource` is the
  model, guarded, yielding one sentence at a time. The hold does not know
  which it has.
- **Beat** — a line of four words or fewer from an approved pool in the
  domain's line registry, chosen by RULES on the gateway's own clocks
  inside our silence (the desk is slow; the wait is long), never by reading
  the borrower's words, and never by a model. "One moment." "Still with
  you."
- **The desk** — the prompt's name for the turn service. The borrower never
  hears the word.
- **Physical transcript** — what the gateway already holds without knowing
  meaning: borrower finals and our playout texts *as delivered*. The model's
  only context. No account data, because the gateway has none.

## 2. What it is not

- **Not on the barge path.** A borrower speaking over a front line is a
  barged filler: the playout is cut, `HoldReport.interrupted` is set, and
  the driver suppresses the pending response (the D4 rule, `handler.py`
  "FILLER | barged"). Unchanged.
- **Not an intent classifier.** Its one question is "what does the agent say
  right now, if anything".
- **Not the STT low-confidence gate.** It does not run on a final the gate
  would hold (`CallConfig.front.min_confidence` is the gate's threshold).
- **Not a source of content.** No amounts, dates, account words or rulings.
  Enforced structurally (§8), not by prompt alone.
- **Not on the critical path.** The request leaves at the final exactly as
  today; the model runs beside it; a line the answer beats is dropped
  unheard.

## 3. The call flow, restated

One borrower turn on a phone call, with the function that carries each step.
Names in `code` exist today; **bold** ones are new.

```
 borrower stops speaking
   │
   ├─ Flux EagerEndOfTurn ─────────────────────────────────────────────────────
   │    stt.on_eager_end → UtteranceEager on utterance_queue
   │    _utterance_processor: request(eager) → turn-side speculation  (today)
   │                          **front.prime(eager_text)**              (new, parallel)
   │                             └─ AsyncCerebras qwen-3.8-27b, effort off, ≤ 500 ms
   │                                user msg = physical transcript + ALREADY SAID + eager text
   │
   ├─ (borrower resumes) Flux TurnResumed → stt.on_turn_resumed
   │    cancels speculation (today) and **front.cancel()** (new)
   │
   ├─ Flux EndOfTurn ─────────────────────────────────────────────────────────
   │    UtteranceFinal on utterance_queue
   │    _utterance_processor:
   │      1. _events = spooled facts + [final]
   │      2. ProcessEventsRequest(... , **front_line={text, audio_id}**) if front.ready()
   │      3. turn_client.process_events(request)          ── the request leaves NOW
   │      4. transport.begin_hold()                        ── the same hold as today
   │           CallClocks._run_hold(source):
   │             _wait_or_end(first_after_ms)              first_after_ms = 0 for the model
   │             for sentence in source:                   one sentence per _speak
   │                 _speak(sentence, "front-<n>")   ──▶  driver **_send_with_metrics(…, front=True)**
   │                 if truncated: interrupted, stop            └─ transport.send_response (TTS, marks)
   │                 _wait_or_end(interval_ms)                  └─ _pending_facts.append(DeliveryOutcome)
   │      5. response arrives → transport.end_hold()
   │           the sentence in flight finishes; the next is never sent  (= yield)
   │           a line that had not started is never sent            (= drop)
   │      6. _execute_turn_response(resp)               the real answer, as today
   │         (HoldReport.interrupted → response suppressed, outcomes spooled as truncated)
   │
   └─ next request carries DeliveryOutcome(front-<n>, status, sentences_sent, heard_text)
        turn side, api.process_events facts prologue → executor.apply_gateway_facts:
          ctx.inflight_playouts.pop("front-<n>")         registered from front_line (§5)
          record_delivery_events(...)                     SPEAK_DELIVERED | BARGE_IN + SPEAK_INTERRUPTED
          (enforce_post_delivery skipped: kind == "front")
        annotate_delivery_view / history_block            the agents' history shows what was HEARD
```

### 3.1 Two tiers in one source — rules first, the model only where words are needed

The line source has two tiers. Both play through the same hold and the same
`_speak`, both land in the same call-log rows, both draw from one
per-call ledger of lines played.

| tier | fires on | decides by | words from | latency | cost |
|---|---|---|---|---|---|
| **0 — beats** | inside the hold only: a slow turn with nothing from tier 1 by `beat_after_ms`, and the still-waiting interval | rules on gateway state only: how long the desk has been working, what has already been said | the domain's line registry, a pool per trigger | 0 ms | none |
| **1 — the model** | the eager end of turn | the prompt (§7) | generated, guarded (§8) | ≤ 500 ms | one call per borrower turn |

**Why there is a model at all.** The whole reason a filler is generated
rather than picked from a list is that a fixed phrase lands on the wrong
moment: "Sure, one moment" after "can you waive the fee?" is an answer;
"Got it" after "I'm not paying" is acceptance; "Understood" after a request
is consent on a recorded line. The model's job is to choose words that
cannot be read as a ruling *at this moment*, and the rules encode the same
requirement from the other side: every beat in a pool is polarity-free and
commitment-free by construction (linted at load, §8), so a beat is safe at
ANY moment and the rotation never has to know what the borrower said. Tier 0
is safe because its words are safe everywhere; tier 1 is safe because it
reads the moment. Nothing in either tier may open with, or contain, a word
of assent, refusal, agreement or commitment — that is the invariant, and
§8 enforces it on both tiers identically.

**The rule that keeps this honest:** tier 0 never reads the borrower's
utterance. It knows *that* the borrower stopped and *how long* the desk has
been working — physical facts the gateway owns. A "rule" that matched words ("if they said thanks, say
you're welcome") would be a keyword classifier on speech, which this
codebase does not do; anything that needs the borrower's *meaning* is tier 1.

The pools, per trigger, as the collections registry supplies them (the
gateway ships none):

| trigger | when (all inside our silence) | pool (examples; the registry is the source) |
|---|---|---|
| `hold` | the hold is still open at `beat_after_ms` (900) and tier 1 returned nothing, or missed its budget | "One moment." · "Checking on that." · "Bear with me." |
| `wait` | the answer still not back `wait_again_after_ms` after a front line | "Still with you." · "Bear with me." · "Just a moment longer." |

Selection is a rotation, not a random pick: each pool is walked in a
shuffled order without repeats until exhausted, then reshuffled with the
constraint that the first of the new cycle is not the last of the old one.

**Two things the user asked for that are deliberately NOT in the pools on a
collections call.** "Okay" is a polarity word: after "can you waive the
fee?" it is heard as yes on a recorded line (§8, the same guard that
silences the model). "Hmm" and "mm-hm" are backchannels *while the borrower
is speaking*; that is full-duplex, it lives on the barge path, and this
layer is kept off that path by design (§2). Another domain may put them in
its registry if its compliance posture allows; the gateway will play
whatever the registry says, within the caps.

**Why the `hold` beat is a fallback and not the default.** A beat on every
slow turn is today's filler with better words. Tier 1 exists because the
turns that most need a line (a hardship disclosure, a proposal, a refusal)
need *specific* words, and a generic beat there sounds like the bot. So
the model gets the first 500 ms; the beat covers the case where it had
nothing to say or was late and the desk is still working at 900 ms — the
borrower is waiting either way, and 900 ms of silence on a phone is where
"hello?" starts.

**What tier 0 is NOT for: the borrower's silence.** If we asked a question
and the borrower has not answered, nothing here speaks. The silence ladder
owns that moment and already does the right thing — a presence probe with
the question re-asked at 10 s. The front's clocks all start from the
borrower's end of turn and stop when the answer plays.

One clock-driven trigger rides the same source, and it lives INSIDE the
hold, i.e. inside our silence:

- **Still waiting.** The hold's own `interval_ms` (renamed by policy to
  `wait_again_after_ms`, 2,500) fires a second `source` pull with trigger
  `wait`; `max` = 2 per turn. This is today's continuation filler.

There is no trigger on the borrower's silence. A pause rung was in an
earlier draft and was removed 2026-09-19: the ladder's probe already
covers a borrower who has gone quiet, and a line spoken into a borrower's
thinking pause is pressure, or gets barged by the answer it interrupted.

**Rows in the call log, in order, for a turn where the borrower heard the
front line and then the answer:**

```
UTTERANCE          "Can I skip this month's payment?"
SPEAK_EMITTED      speak="Let me take a proper look at that, bear with me."  reason=front  audio_id=front-3
INTENT_CLASSIFIED  …                                          (planner, as today)
SPEAK_EMITTED      speak="<the real answer>"                  audio_id=resp-7
REQUEST_COMPLETED
--- next request ---
SPEAK_DELIVERED    audio_id=front-3                           (from the outcome fact)
SPEAK_DELIVERED    audio_id=resp-7
```

A yielded line (second sentence never sent) or a barged one produces
`BARGE_IN` + `SPEAK_INTERRUPTED(intended_text, est_heard_text)` instead, and
the delivery-annotated view rewrites the `SPEAK_EMITTED` row's `speak` to
what was heard. A line dropped before its first chunk produces a
`truncated` outcome with `sentences_sent=0`, the same fact a stale response
produces today, so the history shows the line as unheard. The
`SPEAK_EMITTED` row is written when the request is processed — after the
`UTTERANCE` row, before planning — so the history reads Borrower, then Agent's
front line, then Agent's answer.

## 4. What changes, by file

**Reused unchanged** — the machinery the front line runs on:

| function | role in the flow |
|---|---|
| `CallClocks.begin_hold / end_hold / _wait_or_end` | open on the final, drop-before-first-chunk, yield-at-sentence, never cut mid-word |
| `CallClocks._run_hold` loop | one phrase per `_speak`, interval, cap, interruption → `HoldReport` |
| driver `_pending_facts` + "facts first, trigger last" batching | the outcome fact rides the next request |
| driver barged-filler rule (`HoldReport.interrupted` → response suppressed) | D4, unchanged |
| `stt.on_eager_end / on_turn_resumed` | the prime and cancel points already exist as callbacks |
| `api.process_events` facts prologue → `executor.apply_gateway_facts` | the `DeliveryOutcome` join on `inflight_playouts` |
| `delivery_gate.record_delivery_events` | writes `SPEAK_DELIVERED` / `BARGE_IN` + `SPEAK_INTERRUPTED`, tags `transcript_turns` |
| `ledger.views.annotate_delivery_view / history_block / transcript_lines_view` | heard-vs-intended in the agents' history and in transcript.txt |
| `ctx.add_assistant_turn`, the `_silence_probe` marker pattern in `transcript_turns` | a line that is in the history but is not an ask |
| `contract_codec` (unknown fields ignored, required-missing raises) | additive fields decode both ways |
| `lifecycle._call_config` | the policy slot on `CallConfig` |
| `barge_judge.py` client shape (D1) | the gateway-owned provider client pattern |

**Extended** — small, targeted:

| file | change |
|---|---|
| `clocks.py` | `_run_hold(source)` takes a line source instead of `_filler_phrases(policy)`; `first_after_ms` honoured as 0; keeps the one `lines_played` ledger (both tiers) for the ALREADY SAID list and the rotation; the tier-0 selection (`beat_after_ms` fallback, pool rotation, `front_min_gap_sec`); audio ids `front-<n>`. The silence ladder is untouched |
| `handler.py` | the hold's `speak` callable is `_send_with_metrics(front=True)` (not `transport.send_response`), so the outcome fact is spooled — today's fillers never are; `front=True` skips the response-latency anchor and emits `front_first_chunk_ms` instead; on `UtteranceEager` → `front.prime`; on the final → `front_line` on the request if ready; on `UtteranceResumed` → `front.cancel`; `front_context.verified` from each response stored on the source |
| `contract.py` | `FrontPolicy`; `CallConfig.front`; `ProcessEventsRequest.front_line`; `ProcessEventsResponse.front_context` — all optional with defaults |
| `lifecycle._call_config` | builds `FrontPolicy` when the assistant config says `filler_mode: "front"` (prompt and the three beat pools from the domain bundle's line registry, selected by workflow id), `FillerPolicy` for `"canned"`, none otherwise; `filler` and `front` are mutually exclusive |
| `executor` (the utterance path) | after the `UTTERANCE` row: if `request.front_line`, `ctx.inflight_playouts[audio_id] = {text, turn, segments: [], agent_id: "front", finalizing: False, kind: "front"}`, `ctx.add_assistant_turn(text)`, `transcript_turns.append({role: assistant, content, _front: True})`, `call_log.append(SPEAK_EMITTED, detail={speak, reason: "front", audio_id})`; the annotation text goes into the planner/agent annotations as "the agent is already saying: …" |
| `executor.apply_gateway_facts` | `kind == "front"` skips `enforce_post_delivery` (no Class A/B re-delivery for a front line) |
| `api._last_re_ask` and the output dedup | `_front` joins `_silence_probe` in the exclusion predicate |
| `api.process_events` response | `front_context.verified` read through a node-output pointer the workflow JSON declares (`front.verified_from: {node, key}`) — the platform never names the gate node |
| `control-plane` assistant schema + `config_export` | `filler_mode: canned \| front \| front_shadow \| off` beside `enable_empathy_fillers` |
| `voice-timing-constants.md` + its test | four new constants with reasons |

**New** — gateway-side, no `app.*` imports beyond `app.config`:

| file | contents |
|---|---|
| `gateway/transport/front_source.py` | `FrontModelSource`: `prime(text)` starts the generation task under `front_budget_ms`; `ready()`; `line(trigger)` awaited by the hold, split into sentences; `cancel()`; the physical-transcript view; the ALREADY SAID list; the verified flag |
| `gateway/transport/front_guard.py` | the prototype's `guard()` verbatim: shape, length, digits/currency, spelled numbers + calendar words, `must_not_say`, name-unless-verified, polarity, question, repeat (>4 words). Pure; every trip metered |
| `agents/collections/front_prompt.txt` | the prototype's prompt, a domain artifact beside `v3_lines.yaml`; the one-sentence / 12-word / no-commitment-word rules are in it (2026-09-19), re-run before it ships |
| `agents/collections/v3_lines.yaml` `front_beats:` block | the three tier-0 pools, approved lines like every other line in the registry |

**Removed, at the end of the rollout:** `app/pipeline/fillers.py`,
`FillerPolicy`, `enable_empathy_fillers`.

## 5. The call_log path, precisely

The ledger contract is unchanged: `SPEAK_EMITTED` is the intended line, the
outcome rows are the truth, and every consumer-facing history is a view.
The front line joins that contract at three points.

1. **Intent — `SPEAK_EMITTED` at the request.** The driver puts
   `front_line={text, audio_id}` on the final's request when the line is
   ready. The executor writes the row after `UTTERANCE` and registers the
   playout in `inflight_playouts` with `kind: "front"`. This is the same
   registration every reply gets at `_deliver_turn_result`; the difference
   is that the text came from the gateway.
2. **Outcome — `SPEAK_DELIVERED` / `SPEAK_INTERRUPTED` at the next request.**
   Because the hold now plays through `_send_with_metrics`, every front
   playout spools a `DeliveryOutcome` exactly as a reply does, and the join
   in `apply_gateway_facts` finds the registration. A dropped line spools
   `truncated, sentences_sent=0` from the hold's own accounting (the line
   was ready, never started), so the ledger records that the intended line
   was not heard.
3. **Views.** `annotate_delivery_view` rewrites an interrupted front row to
   its heard prefix; `history_block` renders it as an `Agent` record between
   the borrower's line and the reply; `transcript_lines_view` prints it in
   transcript.txt with the interruption annotation. `_front` excludes it
   from `_last_re_ask`, from the asks map (it is never registered as one),
   from the output dedup, and from delivery enforcement.

What the agents see next turn: `Borrower: Can I skip this month's payment?
/ Agent: Let me take a proper look at that, bear with me. / Agent: <the
answer>`. What they see if the borrower barged the front line: `Agent: Let me
take a proper look [interrupted]`. What they see if it was dropped: the
row marked unheard, which the horizon machinery already knows how to read.

Fillers today write **none** of this. Anything the borrower heard from the
canned source is invisible to the agents, the transcript and the evaluator.
That gap closes with the routing change in `handler.py`, and it closes for
the canned source too, since both sources play through the same hold.

## 6. Contract and configuration

```
CallConfig.front: FrontPolicy | None            # absent = no front model
  prompt: str                                    # the domain's system prompt
  front_budget_ms: 500
  wait_again_after_ms: 2500
  beat_after_ms: 900
  max_words: 12                                  # one sentence, no exceptions
  front_min_gap_sec: 6
  min_confidence: <the STT gate's threshold>
  must_not_say: [...]                            # domain tokens; the gateway lists none
  beats: {hold: [...], wait: [...]}              # tier-0 pools from the domain's line registry
  verified_from: {node, key}                     # turn-side only; never crosses

ProcessEventsRequest.front_line: FrontLine | None   # {text, audio_id}; intent, not record
ProcessEventsResponse.front_context: FrontContext | None   # {verified: bool}
```

Assistant config: `filler_mode` = `canned` (today's phrases) | `front` |
`front_shadow` (§10 stage B) | `off`. Workflow JSON: a `front` block naming
the prompt file and the verified pointer. The platform ships no prompt.

## 7. Personality — the principles the prompt is written against

The borrower hears **one agent**. The persona named in the opening is the same
person between the lines as in them.

1. **One voice, no seams.** Same name, warmth and register as the lines the
   turn service writes. Never a system, a backend, a transfer, a team or a
   supervisor. It says *I*.
2. **The agent, with no authority.** Every decision on the call is made at
   the desk. The front grants, refuses, confirms, denies, waives, extends,
   cancels, removes, applies, schedules, sends, issues, withdraws, accepts or
   rejects nothing, and states no account fact.
3. **Promise attention, never outcome.** "I'll have an answer for you in a
   moment" is allowed. "We can work something out" is not. The test: could
   the borrower later say *she told me that…*?
4. **Acknowledge hearing, not agreeing.** Verbs of attention (hear, see,
   note, check, look, pull up) are the front's; verbs of disposition (agree,
   accept, approve, waive, allow, refuse) are the desk's. "Understood" after
   a request reads as acceptance on a recorded line. Polarity words never
   open a line.
5. **Sympathy in proportion, in the borrower's own terms.** Reflect what they
   said, lightly and specifically, without advice, evaluation or gushing.
   Never cheerful into distress, never soft into a stall.
6. **Proactive means present, not leading.** It opens space ("take your
   time", "I'm still here") and asks no questions, proposes no options.
7. **Talkative means narrating the wait.** Three things are worth saying:
   that you heard them, what you are doing, that they should stay with you.
   New information is never a fourth.
8. **Honest about the wait.** No invented actors, timelines or reasons.
   "Let me check with my manager" is a representation.
9. **Safe for whoever picked up.** No borrower name and no account words
   until the desk says the party is verified.
10. **Register follows the borrower; personality does not.** Brisk, slow, or
    level; warmth and the no-authority rule never flex.
11. **Never repeat yourself.** The front sees its own earlier lines and does
    not reuse a phrasing in the same call. A beat of four words or fewer
    ("One moment.") is exempt: the second authorization on a split cure
    deserves the same beat as the first.
12. **Silence is a line, except when the rails run.** Identity data and
    plain answers get nothing. A confirmation that *authorizes* is the
    exception: it is the slowest turn on the call, so it gets one short line
    that claims no action and restates no term. "Checking it now." is the
    shape; "while I process that" is not.
13. **Bounded: one sentence.** At most twelve words; a beat is four. Never
    a second sentence — no "then a beat", no "thanks, then a hold". If two
    things are worth saying, the second one waits for the next trigger.
    Wordy is the failure mode of a talkative agent, and the second sentence
    is where the readbacks and promises were found (r2).

### 7.1 Speech and response categories

The model never emits a category. These exist for the prompt author, the
tests and the judge. Derived from the 46 prototype turns, then generalised.

| # | borrower speech | examples | register |
|---|---|---|---|
| A | identity data | DOB, ZIP, last four, a spelled name | SILENT |
| B | plain answer | "yes", "okay", "go ahead", "tomorrow afternoon" | SILENT |
| C | authorizing confirmation | "Yes, I authorize it", "Yes, that's correct" after a readback | BEAT |
| D | request or proposal | skip a month, pay half, change the amount, send a link | HOLD |
| E | question | the APR, what the fee is for, how long the link lasts | HOLD |
| F | refusal or stall | "I'm not paying anything today", "I won't verify" | PRESENCE |
| G | hardship or distress | hours cut, illness, disability, a death | SYMPATHY |
| H | anger at the call | "you people keep calling me" | PRESENCE (level) |
| I | third or wrong party | "no one here by that name", "my wife handles this" | COURTESY or SILENT |
| J | borrower's own pause | "hold on, let me grab my card" | ROOM |
| K | closing | "goodbye", "that answers everything" | SILENT |
| L | unintelligible | a gated final; the front does not run | SILENT |
| M | nothing said (clock) | the answer still not back after a front line (our silence) | WAIT |

| register | allowed verbs | shape | never |
|---|---|---|---|
| BEAT | checking, looking | ≤ 4 words | an action, a term, a readback |
| HOLD | look at, pull up, check | 1 sentence | assent, a readback, "sorted", thanks-plus-hold in one breath |
| PRESENCE | hear, right here, not rushing you | 1 level sentence | "understood", "that's fine", "I respect that" as opener |
| SYMPATHY | sorry, carrying, dealing with | 1 sentence, slower; the wait beat, if any, comes later from tier 0 | advice, evaluation, cheer, the account |
| COURTESY | thanks for letting me know | 1 short sentence | the name, the reason for the call, sympathy |
| ROOM | take your time, right here | 1 short sentence, only in reply to the borrower's OWN request for a pause (J) | a question, a nudge; never into the borrower's silence |
| WAIT | still with you, bear with me | 1 short sentence, once per turn | an invented reason or timeline |

Every register is a verb of attention. "Checking it now" is allowed;
"processing it now" claims the desk's action and is not. Category I is not
category G: a stranger gets thanks or silence, never sympathy.

## 8. Guards — structural, not phrase lists

| guard | rule | on violation |
|---|---|---|
| shape | no line breaks; exactly ONE sentence — a second sentence silences the whole line, never truncates it | silence |
| length | `≤ max_words` (12); a beat `≤ 4` | silence |
| content class | no digits, `$`, `%`; no spelled numbers; no calendar words; no `must_not_say` token; no name unless verified | silence |
| polarity / commitment | no word of assent, refusal, agreement or commitment ANYWHERE in the line — not only sentence-initial: yes, no, sure, okay, of course, absolutely, understood, got it, agreed, fine, done, approved, confirmed, granted, waived, no problem. Applied to BOTH tiers: the registry pools are linted against it at load, and a pool that fails does not load | silence / refuse to load |
| action claim | no verb of DONE action anywhere in the line: noting, processing, updating, submitting, sending, applying, scheduling, recording, "taking care of", "put that through" — attending (checking, looking, pulling up) is allowed, acting is the desk's. Added 2026-09-20 after "Noting that authorization." slipped past the prompt | silence |
| question | no `?` | silence |
| repeat | §8.1: not a line already played this call (beats rotate instead); not the same first three words as the previous front line | silence |
| gap | no clock-driven line within `front_min_gap_sec` (6) of any front line | drop |
| state | §3 invariants | drop |

**Why the caps got tighter (2026-09-19).** A two-sentence line is 4–6 s of
audio the real answer waits behind, and the r5/r6 runs showed the model
happily filling 22 words. Wordy is the failure mode of a talkative agent:
one sentence of about twelve words says everything the front is allowed to
say ("Give me a moment to look at that."), and anything longer is either a
readback or a promise. There is no register with a second sentence. A line
that comes back with two is silenced whole, not cut to its first: a model
that wrote two sentences was not following the register, and its first
sentence is not to be trusted on its own.

**The polarity rule is the design, not a guard.** The closed list above is
not a phrase list on the agent's output in the sense the codebase forbids —
it is the definition of the content class the front is barred from
(commitment), stated as the words that carry it in English, applied
identically to a registry line and a generated one, and it exists because
a filler that lands one of those words on the wrong moment is a compliance
finding. The model exists to avoid exactly that; the guard makes sure the
model cannot fail at it silently.

### 8.1 Repetition — one ledger, both tiers

- **One `lines_played` per call**, kept by the hold, fed by both tiers,
  handed to the model as ALREADY SAID and to the rotation as its history.
  A beat the rules chose is in the model's list; a line the model wrote
  blocks the rotation from picking the same words.
- **Beats rotate** (§3.1): no repeat until the pool is exhausted, never the
  same beat twice running, even across cycles.
- **The model never repeats a line** (`repeat` guard), and **never repeats an
  opener**: the first THREE words of a front line may not equal the first
  three words of the previous front line on the call ("Let me pull … / Let
  me pull …"). Three, not two: measured on the 2026-09-19 one-sentence run,
  a two-word rule would have silenced 5 good hold lines of 35 ("Let me pull"
  after "Let me look"); a three-word rule silenced none. The prompt asks
  the model to vary its openers; the guard catches the tic, not the style.
- **A four-word beat may recur** once its pool has cycled — a split cure's
  second authorization deserves the same "Checking it now." as the first —
  but never back to back.
- **No stacking.** A model line, a hold beat and a wait beat can all become
  due within seconds of each other on one slow turn; `front_min_gap_sec`
  keeps any two front lines at least six seconds apart. The borrower hears,
  at most, one line and one follow-up per turn, never a running commentary.

Every trip is metered (`front_guard_<name>`). A prompt that keeps tripping
a guard is fixed at the prompt. What no guard can catch — "processing it
now", a third party's first name, "what fits for you" — is the prompt's and
the judge's.

## 9. Timing, metrics, model

| constant | value | bounds |
|---|---|---|
| `front_budget_ms` | 500 | eager → line ready, provider RTT and generation included |
| `wait_again_after_ms` | 2,500 | silence after a front line before a second one |
| `beat_after_ms` | 900 | hold still open and nothing from tier 1 → a beat; below this the desk is "fast" |
| `max_words` | 12 | one sentence, no exceptions |
| `front_min_gap_sec` | 6 | minimum spacing between any two front lines (both tiers) |
| lines per turn | 2 | the cap |

All of these go into `voice-timing-constants.md` with reasons, and its test, in
the commit that introduces them.

Metrics per call, `{tenant}/{route}`: front invocations, decision latency,
empty rate, played / yielded / dropped / resumed-drop / barged counts, hold-beat and wait-beat counts,
guard trips by name, `front_first_chunk_ms`, and response latency with and
without a front line. The response-latency anchor is **not** consumed by a
front playout (`_send_with_metrics(front=True)`), or the layer would report
itself as the answer.

Model: `qwen-3.8-27b` on Cerebras, reasoning off. Measured on the 20
ladders: p50 232 ms, p90 322 ms, max 509 ms, no reasoning-token variance.
gpt-oss-120b works too but its outliers are reasoning spikes.

On the phone the front line competes with **silence** (until `69ad9c4`
there was no filler hold at all), so any line inside the budget is a strict
improvement; the "is there a gap" question of r2 only applies to the
widget's 1.1 s canned filler.

## 10. Rollout — each stage is a config value

- **A. Canned fillers on the phone.** DONE (`69ad9c4`). The baseline. Three
  of its four openers ("Sure", "Of course", "Got it") read as assent on a
  collections line and need neutral wording before that path carries real
  calls.
- **B. Shadow.** `filler_mode: front_shadow`: the source primes on every
  eager, guards the line, logs `{utterance, line, guard trips, latency,
  ready-before-final?, answer-before-ready?}` to the audio timeline, and the
  canned source still plays. Zero borrower-facing change. Two weeks of
  production traffic answer the resume rate and the double-acknowledgement
  rate on real audio.
- **C. Play on the final.** BUILT 2026-09-19 as v1 (§0), gated by
  `FRONT_MODEL_ENABLED` rather than `filler_mode`: primed on the eager, the
  hold opens on the final as today, the ledger row lands from the outcome
  fact. Stage B (shadow) was skipped by decision: run it for real.
- **D. Play on the eager.** Only if B's resume rate is low: the hold opens on
  the eager, `TurnResumed` cancels.
- **E. Retire the canned source.**

## 11. Tests

- `test_call_clocks.py`: the hold with a scripted source — drop before first
  chunk, yield between sentences, second line after `wait_again_after_ms`,
  cap 2, resume cancels, `lines_played` feeds the next prime.
- `test_front_guard.py`: one constructed violation per guard: a second
  sentence silences the whole line; a commitment word mid-line (not only
  sentence-initial) silences it; the registry pools are linted at load and
  a pool containing "Okay" or "Got it" refuses to load.
- `test_call_clocks.py` (tier 0): the `hold` beat fires at `beat_after_ms`
  only when tier 1 gave nothing; nothing fires on the borrower's silence
  (the ladder's probe is the only speech there, unchanged); the rotation
  never repeats until the pool cycles and never twice running;
  `front_min_gap_sec` suppresses a stacked hold/wait; the beat lands in
  `lines_played` and in the next ALREADY SAID list.
- `test_gateway_contract.py`: round-trips for the three new types; an
  old-shape request and response still decode.
- `test_phone_fillers_conversation.py` (exists; extend): with
  `filler_mode: front` and a scripted source, the front line rides the
  request, the `SPEAK_EMITTED` row lands after `UTTERANCE`, the outcome fact
  on the next request produces `SPEAK_DELIVERED`, the history block shows
  Borrower / Agent(front) / Agent(answer), and the transcript view prints it.
- `test_turn_blocks.py` / ledger: `_front` is not an ask and not in the
  re-ask window; a barged front line rewrites to its heard prefix.
- The prototype runner, promoted: the 20 ladders through the real source
  and guard classes, the category shape check, N=1.

## 12. Risks, sized

| risk | evidence | mitigation |
|---|---|---|
| double acknowledgement | r6: 15/37 front lines open with one; the desk's real line also does in 4 | the annotation lets generated lines skip it; recited templates get a `front_aware` variant or SYMPATHY drops to a beat when the next line is soft; measure in shadow |
| play on the eager, borrower resumes | `TurnResumed` emitted and handled today; rate unknown | the shadow pull; fallback = decide on the eager, play on the final |
| the line delays the answer | two sentences ≈ 4–6 s; yield per sentence | bounded by one sentence; BEAT ≤ 4 words on the slowest turns |
| provider spikes | Qwen off: max 509 ms / 46 | the budget → silence |
| compliance leak past the prompt | r6 and the Qwen run: zero guard trips on 71 lines | every trip metered; fix at the prompt |
| the eval suites never see it | `gateway_sim` bypasses the gateway | driver harness over ASGI; the prototype runner |
| cost | one Cerebras call per borrower turn, ~1.5 k prompt / ~30 output tokens | static system prompt, utterance last, cache-friendly |

## 13. Open decisions

| # | decision | proposed |
|---|---|---|
| F1 | stage D at all | decide from the shadow resume rate |
| F2 | recited-line doubling | `front_aware` registry variant vs. SYMPATHY→BEAT rule; decide from shadow |
| F3 | where `front_prompt.txt` is referenced | the workflow JSON `front` block, like node config |
| F4 | canned wording in stage A | replace the three assenting openers now — with the tier-0 `hold` pool, which is the same list |
| F5 | "okay" / "hmm" in the collections pools | proposed NO (§3.1): "okay" is assent, "hmm" is full-duplex; revisit only with a barge-path design |

## 14. Prototype record

`voice-gateway/prototypes/front_model/` — standalone, imports nothing from
the live gateway. Seven runs over the same 20 ladders; each run's prompt
saved beside its results; `RESULTS.md` has the read. Latency inside the
budget on 44–46 of 46 turns per run; the readback defect found in r2 and
fixed in r3; the taxonomy and the ALREADY SAID list settled in r5–r6; Qwen
at least as good as the 120b on the same prompt. The r2 text of this
document is preserved in the session scratchpad, not in the repo.
