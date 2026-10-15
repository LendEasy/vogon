# The complementing front voice — survey first, inventory second

Design only. No code changes.

The gateway speaks **before** the desk has answered. So what it may safely say
is decided entirely by what the desk is **about to** say. This document is
therefore built backwards: survey the desk's 204 recited lines, derive what is
safe against each, and only then write the inventory of things the gateway may
utter. The classification follows from the inventory, not the other way round.

---

## 1. The survey

Every line in `app/agents/collections/v3_lines.yaml` classified by the move its
**opening sentence** makes. Jev, one `choice` over 12 moves, 204 requests,
$0.0093. Raw in `voice-gateway/prototypes/complement/survey.json`.

| desk opening move | lines | share | what it does to the gateway |
|---|---|---|---|
| REFUSAL | 30 | 14.7% | **contradiction risk** |
| ASSENT | 29 | 14.2% | duplicate risk |
| FIGURE | 25 | 12.3% | safe — a look-up complements |
| READBACK | 18 | 8.8% | **contradiction risk** |
| EXPLANATION | 17 | 8.3% | safe |
| DONE | 16 | 7.8% | **contradiction risk** |
| CORRECTION | 15 | 7.4% | **contradiction risk** |
| THANKS | 13 | 6.4% | duplicate risk |
| OFFER | 13 | 6.4% | safe |
| PROCEDURE | 12 | 5.9% | safe |
| SYMPATHY | 11 | 5.4% | duplicate risk |
| PRESENCE | 5 | 2.5% | duplicate risk |

**Three findings decide the design.**

**(a) The desk's most common opening move is to say no.** Thirty lines, 14.7%,
more than any other. *"That one's outside what I can help with, I'm afraid."*
*"I can't take more than the loan is worth."* *"That date has already passed,
so I cannot set a payment for it."* Anything the gateway says beforehand that
raises expectation is contradicted one sentence later.

**(b) Sympathy duplicates precisely where it is most wanted.** Of the 11
hardship-family lines, **5 open with SYMPATHY** — the desk already says *"I'm
sorry — that's a lot to deal with"*. A gateway that offers sympathy on every
distress turn will double up about half the time.

**(c) Risk is not spread evenly across the call.** By family:

| family | lines | dominant opening moves |
|---|---|---|
| rails / payment | 54 | READBACK 10, ASSENT 7, REFUSAL 7, CORRECTION 7, DONE 6 |
| assist / FAQ | 25 | **FIGURE 14**, REFUSAL 6 |
| referral | 22 | THANKS 6, REFUSAL 4, DONE 3 |
| hardship | 11 | **SYMPATHY 5**, OFFER 3 |

The money path is almost entirely risk. The FAQ path is almost entirely safe —
fourteen of twenty-five lines just state a figure, and *"let me pull that up"*
complements a figure perfectly.

---

## 2. Three ways to be wrong, not one

The existing guard catches only the third.

| failure | what it sounds like | caught today? |
|---|---|---|
| **DUPLICATE** | gateway: *"I'm sorry, that's a lot to be carrying."* desk: *"I'm sorry — that's a lot to deal with."* | no |
| **CONTRADICT** | gateway: *"Let me take a proper look at that for you."* desk: *"That one's outside what I can help with."* | no |
| **CLAIM** | gateway: *"Noting that authorization."* — the gateway asserting the desk's act | yes, `ACTION_CLAIMS` |

Contradiction is the subtler one and it has no current defence. A look-up line
before a refusal is a small false hope; a warm line before a correction sounds
like agreement with something the desk is about to deny.

---

## 3. Working backwards — what survives against all twelve moves

For each desk move, what the gateway may say **before** it:

| desk move | ultra-short beat | one-sentence look-up | sympathy | room | courtesy |
|---|---|---|---|---|---|
| REFUSAL | yes | **no** — false hope | no | yes | — |
| CORRECTION | yes | **no** — reads as agreement | no | yes | — |
| READBACK | yes | no | no | yes | — |
| DONE | yes | yes | no | yes | — |
| ASSENT | yes | yes | no — duplicate | yes | — |
| THANKS | yes | yes | no | yes | **no** — duplicate |
| SYMPATHY | yes | yes | **no** — duplicate | yes | — |
| PRESENCE | yes | yes | no | no — duplicate | — |
| FIGURE | yes | **yes** — best fit | no | yes | — |
| EXPLANATION | yes | **yes** | no | yes | — |
| OFFER | yes | **yes** | no | yes | — |
| PROCEDURE | yes | yes | no | yes | — |

Reading down the columns gives the inventory:

- **The ultra-short beat is safe against every single move.** Four words or
  fewer, a verb of attention, no object: *"One moment." "Checking it now."
  "Bear with me." "Taking a look."* It cannot duplicate (the desk never opens
  this way) and cannot contradict (it promises nothing).
- **The one-sentence look-up is safe against eight of twelve** and unsafe
  against exactly the three contradiction classes plus READBACK.
- **Sympathy is safe against none of them by default** — it is safe only when
  the desk is *not* about to be sympathetic, which is a prediction, not a rule.
- **Room** (*"Take your time, I'm right here."*) is safe everywhere except
  PRESENCE, because it answers the caller's own pause rather than the desk.
- **Courtesy** is safe except before THANKS.

---

## 4. The inventory — after dropping two tiers

Two candidate tiers were removed because the survey says the desk already
occupies them.

**SYMPATHY is dropped.** Across the 20 hardship and referral-open lines the
desk opens with SYMPATHY 7 times and THANKS 5 times — **12 of 20, 60%, are
already an acknowledgement**. A gateway that offers sympathy whenever the
caller is in distress duplicates three times in five, and *"Thank you for
telling me"* arriving straight after *"I'm sorry, that's a lot to be
carrying"* is the exact double this whole thread started from.

**ROOM is dropped.** `v3_hardship_ladder_clarify` opens *"Take your time to
think about this option."*, and a recorded smoke run has the desk answering
*"Hold on a second, let me grab my card"* with *"Of course — take your time.
Just let me know when you're ready to continue."* The gateway had said *"Take
your time, I'm right here."* — a verbatim duplicate, observed, not theorised.
Two hardship lines also carry `must_say: "not going to push"`, which sits in
the same register.

What survives is four tiers, every line linted by `front_guard`:

```
BEAT      <=4 words. Safe before ALL TWELVE desk moves — the desk never
          opens this way, and it promises nothing, so it can neither
          duplicate nor contradict.
            "One moment."        "Checking it now."
            "Bear with me."      "Taking a look."
            "Looking now."       "One second."
            "Just a moment."     "Still with you."

LOOKUP    One sentence, 6-10 words. Safe before FIGURE, EXPLANATION, OFFER,
          PROCEDURE, DONE, ASSENT, THANKS, SYMPATHY.
          NEVER before REFUSAL, CORRECTION or READBACK — a look-up line in
          front of "that's outside what I can help with" is a false hope.
            "Let me pull that up for you, one moment."
            "Let me take a proper look at that for you."
            "Give me a moment to look at that for you."
            "Let me look into that for you, one second."
            "Let me check that for you now."
            "Let me find that for you."

COURTESY  Only when the speaker is NOT the borrower. Safe because the
          wrong-party lines open PROCEDURE, ASSENT or REFUSAL — never with
          a thank-you. (14 lines elsewhere do open with thanks, which is
          why this tier needs the rider and not just the move.)
            "Thanks for letting me know."
            "Thanks for telling me."
            "Appreciate you letting me know."

SILENCE   The default, and the only answer that is safe against everything.
          Identity data, plain answers, closings, unintelligible audio,
          any low-confidence prediction — and every distress turn.
```

Rejected by the guard during this pass, kept here so nobody re-proposes it:
`"No rush, I'm right here."` trips `commitment` on the bare word "no".

**What dropping sympathy costs.** On the 8 of 20 distress turns where the desk
does *not* acknowledge — it opens OFFER, EXPLANATION, DONE, PROCEDURE or
REFUSAL — the caller now hears 1.5–3.9 s of nothing before the desk speaks.
That is a real loss, and it is the price of never producing the double. A
human agent also pauses after a disclosure; a scripted-sounding repeat is
harder to forgive than a short silence on a recorded line.

## 5. The classification, derived last

The gateway must predict **the desk's opening move**, not "what register the
caller is owed". That is the inversion this survey forces: the same caller
utterance deserves different treatment depending on what the desk will do with
it, and only the desk move distinguishes those.

Jev answers many questions in one request at one request's latency (measured:
35 questions, 276 ms), so granularity is nearly free. One call, at final EOT:

**Q1 — `choice`, the predicted desk move.** The twelve keys above, plus
`UNSURE` as the bail-out. Criteria are written from the surveyed openers, so
each description quotes real desk lines.

**Riders — `noul`, each true alongside any move:**

| rider | why it exists |
|---|---|
| `distress` | the caller described something hard in their life |
| `asked_for_time` | *"hold on, let me grab my card"* — licenses ROOM |
| `not_the_borrower` | wrong party — licenses COURTESY |
| `identity_data` | a DOB, ZIP or last four — forces SILENCE |
| `authorising` | approving specific details after a readback |
| `unintelligible` | forces SILENCE |

**The pick, in code — not in the model:**

```
if identity_data or unintelligible or move == UNSURE  -> SILENCE
elif distress                                         -> SILENCE   # the desk
                                                                   # acknowledges
                                                                   # 12 of 20
elif not_the_borrower                                 -> COURTESY
elif move in {REFUSAL, CORRECTION, READBACK}          -> BEAT
elif move in {FIGURE, EXPLANATION, OFFER, PROCEDURE,
              DONE, ASSENT, THANKS, SYMPATHY}         -> LOOKUP
else                                                  -> SILENCE
```

With SYMPATHY and ROOM gone, the ladder no longer needs the `asked_for_time`
rider at all, and `distress` becomes a reason to stay QUIET rather than a
reason to speak — which also means a wrong `distress` answer costs silence,
not a double.

Every guard in that ladder comes from a column of the §3 table. The one that
earns its keep is `distress and move not in {SYMPATHY, THANKS}` — that single
clause is what stops the doubling the survey measured on 5 of 11 hardship
lines.

**Confidence is a floor, not a tiebreak.** Jev returns a probability per key.
Below a floor on Q1, the answer is SILENCE regardless of which key won —
mispredicting the desk move is how a contradiction happens, so an uncertain
prediction must not speak.

---

## 6. Why this is safer than what exists

| | today | proposed |
|---|---|---|
| what the model outputs | a sentence, or a register | a prediction about the **desk** |
| duplicate defence | none | the move prediction |
| contradiction defence | none | tier gating by move |
| claim defence | `ACTION_CLAIMS` | unchanged |
| worst case | a wrong-tempered line | silence |

The worst outcome of a misprediction is the gateway says a four-word beat when
it could have said more, or says nothing. It cannot say something the desk is
about to deny, because the only line permitted under the contradiction classes
promises nothing at all.

---

## 7. What has to be measured before this ships

1. **Can the desk move be predicted from the caller's utterance?** Unknown.
   The survey labelled desk lines by their own text; predicting that move from
   what the *caller* said is the actual task and is unmeasured. Build the set
   by joining recorded ladder turns to the line the desk actually used — the
   label is free, no hand-labelling.
2. **The `distress` rider against the 11 hardship lines**, which is where the
   duplicate was measured.
3. **Contradiction, as its own metric**: how often a LOOKUP line preceded a
   REFUSAL, CORRECTION or READBACK. Accuracy on the move alone will not show
   it.
4. **Whether the 12 moves collapse.** REFUSAL, CORRECTION and READBACK share
   one tier; if prediction is weak, the honest fallback is a 3-way question —
   *is the desk about to say no, about to state something, or about to
   acknowledge* — which is all the tiering actually needs.

---

## 8. As built — 2026-09-24, enabled by default

`FRONT_ENGINE=jev` is the default. `FRONT_ENGINE=llm` restores the free-text
composer, `FRONT_ENGINE=off` leaves the hold on beats only, and
`FRONT_MODEL_ENABLED=0` disables the whole tier as before.

**Code, all in the gateway** (`voice-gateway/vogon/gateway/transport/`):

| file | role |
|---|---|
| `front_moves.py` | the one Jev call — desk-move `choice` + 7 `noul` riders; any failure reads UNSURE |
| `front_pick.py` | the ladder and the repeat composition. Pure: no network, no clock, no I/O |
| `front_source.py` | `_generate` now dispatches on `front_engine`; adds `note_delivery`, tier-agnostic `_rotate`, and the repeat guard exemption |

**Config at the shared source** (`data-plane/app/config.py`, which the gateway
already imports): `front_engine`, `front_move_floor` (0.45),
`front_repeat_enabled`. Words stay in the domain artifact reached through
`resolved_front_config_path()`; the gateway ships none.

### The repeat short-circuit

The gateway answers "say that again" itself, because it already knows what it
played and how much of it landed (`DeliveryOutcome.heard_text`,
`unheard_text`, `sentences_sent`). It mirrors the TTS re-speak discipline in
CLAUDE.md — replay only what they missed, never re-read what they heard.

The prefix is gateway speech and is linted at load. The replayed body is
**not** re-guarded: it is a verbatim replay of a line the desk composed and
the renderer approved, and carries the figures the guard would reject. That
exemption covers the content guard and the spacing rule only.

`"No problem, ..."` and `"Of course, ..."` were both REFUSED by the guard on
`commitment` and are absent from the pools.

**It does not steal the desk's turn.** `BALANCE_REPEAT` already exists as a
planner intent — *"how much do I owe again?"* is a question for the desk, not
a replay. The `repeat_request` rider draws that line, and the smoke confirms
it: that utterance read FIGURE 0.99 and got a look-up line, not a repeat.

### Live smoke, 20 turns

`data-plane/tests/smoke_front_complement.py`. **p50 112 ms, p95 1401 ms,
$0.0013.**

| caller said | desk move | tier | borrower hears |
|---|---|---|---|
| "What's the APR on this loan?" | FIGURE 0.97 | lookup | "Let me take a proper look at that for you." |
| "Can you waive the late fee?" | REFUSAL 0.50 | beat | "One moment." |
| "ZIP? It's 90210." | — | silence | (identity_data) |
| "My mother passed away last month." | SYMPATHY 1.00 | silence | (distress — the desk will be sympathetic) |
| "My wife handles this account for me." | PROCEDURE 0.96 | courtesy | "Thanks for telling me." |
| "That's everything, thanks, goodbye." | THANKS 0.96 | silence | (closing) |
| "Sorry, you cut out — I didn't hear that last part." | PRESENCE 0.63 | **repeat** | "Here's what you missed. Can you take care of that today?" |
| "How much do I owe again?" | FIGURE 0.99 | lookup | "Let me look into that for you, one second." |

### Two defects the smoke found, both fixed

**A YAML boolean trap.** The rider text used `yes:`/`no:` keys, which PyYAML
parses as the booleans `True`/`False`, so every lookup raised `KeyError('yes')`
and **all twenty reads returned UNSURE**. The fail-safe held — every turn went
silent rather than wrong — but the feature was entirely dead. Keys are now
`yes_when`/`no_when`.

**No closing.** The desk's own closing lines read as THANKS, so the move alone
cannot see a goodbye: *"That's everything, thanks, goodbye."* scored THANKS
0.96 and was answered with *"Give me a moment to look at that for you."* Fixed
with a `closing` rider.

### Known, and accepted

- **~10% of reads time out** at 900 ms (`front_beat_after_ms`, which is also
  the client timeout). That is the intended contract: a read that would miss
  the beat deadline is worth nothing, so it is abandoned and the beat plays.
- **Confidence is often low** (0.13–0.50 on authorisations and offers), so the
  0.45 floor produces a lot of silence. Safe, and under-speaking; the floor is
  the first thing to tune once there is a labelled set.
- **The desk-move prediction is still unmeasured** (§7.1). The smoke shows it
  behaving sensibly on 20 turns; that is not an accuracy number.

---

## 9. Measured — the desk-move prediction, 213 recorded ladder turns

§7.1 asked whether the desk's opening move can be predicted from the caller's
utterance. It can, but not well, and the design survives for a different
reason than the one assumed.

Method: the recorded ladder traces hold what the borrower said AND what the
desk actually replied, so the gold label is free — the move of the desk's real
opener, classified by the same survey question that produced the taxonomy.
213 turns across 83 ladders, two Jev calls each, $0.026.
`voice-gateway/prototypes/complement/measure_prediction.py`.

### The prediction is weak

| | accuracy | majority baseline |
|---|---|---|
| 13-way move | **37.1%** | 15.5% |
| collapsed to 3 (DENY / STATE / ACK) | **49.3%** | 44.1% |

It beats majority by 21.6 points on the fine question and by only **5.2** on
the coarse one. So the answer to "do the twelve moves collapse to three?" is
no — collapsing does not rescue it, because the difficulty is not granularity.

### But the contradiction rate is zero

| | turns the desk denied, corrected or read back | we played a look-up |
|---|---|---|
| a front that always speaks | 57 | 57 (100%) |
| deny-listed look-up (as first built) | 57 | 9 (15.8%) |
| **allow-listed look-up (as shipped)** | 57 | **0 (0%)** |

Speak rate 48.4%, unchanged by the tightening.

### Why it works despite the weak prediction

**Safety comes from abstention and from the tier table, not from accuracy.**
The gateway says nothing on 54% of turns — 30 on a silent-class rider, 25
because confidence was below the floor, 22 deferring to the desk on distress
— and a beat covers most of the rest. The prediction only has to be right
about the *kind* of turn often enough to choose between a beat and a look-up,
and when it is unsure it does not speak.

### ASSENT was the poison class

Deny-listing the look-up (allow it everywhere except REFUSAL, CORRECTION,
READBACK) produced 9 contradictions, and **eight were predicted ASSENT**. That
prediction is close to uninformative about the desk:

```
predicted ASSENT -> the desk actually opened
    ASSENT 14 | READBACK 13 | FIGURE 12 | DONE 9 | REFUSAL 7 | OFFER 6
```

"The caller said something agreeable" says nothing about what the desk will do
with it. The fix was to **allow-list** instead: only FIGURE, EXPLANATION,
OFFER and PROCEDURE earn a look-up, because only there is the desk genuinely
about to go and find something. Everything else that speaks gets a beat, which
is safe before all twelve moves. Contradictions went to zero at the same speak
rate — strictly better, no trade.

### The ceiling, and it is real

Some of the desk's move is **not knowable from the caller's line at all**:

```
caller : Please text me the payment link to this number.
         (predicted ASSENT, 0.78 — a reasonable read)
desk   : The card payment earlier didn't go through — that attempt stays on record
```

The desk is correcting from state the gateway cannot see: a prior declined
card, an amount against the floor, what is already staged. No improvement to
the question will predict that. It is an argument for keeping the allow-list
narrow and the floor honest, not for a better prompt.

### What this does not say

Accuracy figures come from a single greedy pass per turn, and Jev is not fully
deterministic (§ third round: one boundary case flipped across four runs). The
re-measurement after the tightening drew fresh answers — move accuracy moved
36.6% → 37.1% and one turn's gold changed class — so treat ±1 point as noise.
The contradiction count is not noise: it is 0 of 57 twice over.

---

## 10. A repeat is an acknowledgement, not a replay (2026-09-24)

When the caller says they missed what we said, the gateway now says only
"Sorry about that." or "My apologies." and the desk re-delivers the content.
The replay described in §8 is removed.

Measured on two live phone-path calls (`data-plane/tests/gateway_e2e/filler_duplex_call.py
--script repeat`), the replay was worse than nothing:

- **The desk already re-delivers.** On "Can you say that again, please?" the
  gateway replayed the last 34 words and the desk then replayed the whole
  60-word line, so the caller heard the account figures twice back to back.
- **On a cut-off line it dropped the required notice.** The desk's line was cut
  after "Thank you, you're verified." The replay kept the END of the missed text
  to fit its length cap and skipped "Quick required notice: this is an attempt
  to collect a debt…", which is exactly where the caller had lost us.
- **And it suppressed the desk's correct version.** The desk re-delivered from
  the notice, but the gateway's replay was cut short in turn, and the rule that
  suppresses the desk's response after a cut filler threw the desk's version
  away. The notice was never heard.

The desk knows what was heard (the delivery gate's truncation record,
`framework/conversational_repeat`) and which lines are required. The gateway
only fills the gap. The acknowledgement is an ordinary tier (`tiers.repeat` in
front.yaml), linted at load, subject to the normal guard and spacing rule — so
there is no guard exemption any more, and Flux fragments of one request cannot
stack acknowledgements.

Also fixed: `FRONT_REPEAT_ENABLED=0` used to raise the move confidence floor to
1.1, which silenced every tier of the front model, not just the repeat. It now
makes only a repeat request quiet (a deliberate silence, so the hold plays no
beat over it).

**One repeat rider, not two.** A separate `heard_nothing` rider ("Hello? Are you
there?") read the call's EARLIER turns rather than the current line. After the
caller had once said "I couldn't hear you", ordinary questions scored high on it
and got an apology on a live call — "…what's the APR on this loan?" answered
"My apologies." Probed:

| utterance | `heard_nothing`, earlier turns present | same, no history | merged `repeat_request`, earlier turns present |
|---|---|---|---|
| "…what's the APR on this loan?" | 0.60 | 0.02 | 0.04 |
| "Can you waive the late fee?" | 0.64 | 0.02 | 0.04 |
| "Hello? Are you still there?" | — | — | 0.89 |
| "I couldn't hear you." | 0.75 | 0.74 | 0.97 |

The two are merged into `repeat_request`, whose wording says to judge only the
line just said; it probes 9/9 correct with and without the earlier turns.
Perception figures from single probes — treat as rates to re-measure, not pins.
