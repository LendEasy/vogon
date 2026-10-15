# The voice gateway with an active response

> **Superseded (2026-09-24).** This shows an intermediate design (five registers including
> sympathy, and a content-replaying repeat). The shipped design is `FRONT_COMPLEMENT_DESIGN.md`;
> the call flow and barge-in sections still describe the gateway accurately.

Line references are `voice-gateway/vogon/websocket/handler.py`; constants are
`data-plane/app/config.py`. Phone thresholds shown (widget differs).

## 1. One turn, end to end

```
 BORROWER  ─────"can I skip this month and move it to the end"──────┐
                                                                     │
 Deepgram  Update…Update…  EagerEndOfTurn        EndOfTurn           │
 Flux      (interims)      p≥0.70                p≥0.85 or 1400ms    │
                │              │                      │              │
                │              │                      │              │
 ═══════════════╪══════════════╪══════════════════════╪══════════════╪═════
                │              │                      │              │
 GATEWAY        │         ┌────▼─────┐           ┌────▼──────┐       │
 handler.py     │         │ prime()  │           │ prime()   │       │
                │         │  :629    │           │  :631-632 │       │
                │         └────┬─────┘           └────┬──────┘       │
                │              │                      │              │
                │              │              ┌───────▼────────┐     │
                │              │              │ begin_hold()   │     │
                │              │              │     :658       │     │
                │              │              └───────┬────────┘     │
                │              │                      │              │
                │   ┌──────────▼──────────────────────▼───────────┐  │
                │   │  TRACK A — the gateway's own voice          │  │
                │   │  register classifier, 14 ms GPU / 134 ms CPU│  │
                │   │  ATTEND → pool line → TTS → borrower        │  │
                │   └──────────────────┬──────────────────────────┘  │
                │                      │                             │
                │   ┌──────────────────▼──────────────────────────┐  │
                │   │  TRACK B — the desk (turn service)          │  │
                │   │  await process_events()   :676              │  │
                │   │  1.5 – 3.9 s                                │  │
                │   └──────────────────┬──────────────────────────┘  │
                │                      │                             │
                │              ┌───────▼────────┐                    │
                │              │  end_hold()    │  lets an in-flight │
                │              │     :699       │  line finish       │
                │              └───────┬────────┘                    │
                │                      │                             │
                │           ┌──────────▼────────────┐                │
                │           │ _execute_turn_response│   :725         │
                │           └──────────┬────────────┘                │
                │                      │                             │
 BORROWER  ◀────┴──"Let me take a proper look at that for you." ─────┘
                       …then… "I can set up a one-payment deferral…"

 TRACK A AND TRACK B RUN CONCURRENTLY on the same event loop. The desk is
 never waited on by the gateway's own voice, and the gateway's voice is never
 waited on by the desk.
```

## 2. The clocks that fire when the desk is slow

```
  final EOT
      │
      ├─ 0 ms ─────── classifier answers (14 ms GPU / 134 ms CPU)
      │                 │
      │                 ├─ register = SILENT  → say nothing, ever
      │                 └─ otherwise          → approved pool line
      │
      ├─ 500 ms ───── front_budget_ms .......... over this is metered "late"
      │
      ├─ 900 ms ───── front_beat_after_ms ...... model late/failed → a BEAT
      │                                          ("One moment.")
      │
      ├─ 1500 ms ──── front_beat_after_empty_ms  model chose silence, hold
      │                                          still open → a beat anyway
      │
      └─ 2500 ms ──── front_wait_again_after_ms  answer STILL not back
                                                 → one more beat

  spacing: front_min_gap_sec = 6.0 s between any two gateway lines

  ── the BORROWER's silence is a different clock, owned by the ladder ──
      8 s warn   →   10 s probe   →   37 s exit
```

## 3. Barge-in

```
  gateway/desk speaking ──────────────────────────────▶
                          ▲
                          │  borrower starts talking
                          │
              ┌───────────┴────────────┐
              │  TRANSPORT LAYER       │   barge is handled BELOW the agent:
              │  cuts playout now      │   the utterance never reaches the
              │  reason='barge_cut'    │   turn service as an interrupt
              └───────────┬────────────┘
                          │
         ┌────────────────┴─────────────────┐
         │                                  │
   a FILLER was cut                   a RESPONSE was cut
         │                                  │
   D4 LEDGER RULE:                    SPEAK_INTERRUPTED +
   the turn commits                   BARGE_IN recorded;
   unconditionally;                   sentences_sent marks
   gateway suppresses                 what they actually heard
   the playout locally                      │
   and reports the                          │
   physical outcome as                      ▼
   a next-request fact            re-speak covers sentences[s_sent:]
         │                        ONLY, prefixed "Sorry about that."
         ▼
   HoldReport(fillers_played, interrupted)

  THE GATEWAY'S VOICE IS NOT ON THE BARGE PATH.
  It only ever plays into OUR silence — the gap where the borrower has
  stopped and we owe a reply. It never plays into THEIR silence.
```

## 4. What the active response changes

```
  ┌──────────────────────── BEFORE ────────────────────────┐
  │                                                        │
  │  final EOT ──▶ LLM generates a sentence ──▶ 4 guards   │
  │                (cloud, 227 ms p50 / 560 ms max)        │
  │                       │                                │
  │                       ▼                                │
  │            free text the borrower hears                │
  │            → can say a commitment word                 │
  │            → can read back an amount                   │
  │            → guard suppresses ~17–28% of it            │
  └────────────────────────────────────────────────────────┘

  ┌──────────────────────── AFTER ─────────────────────────┐
  │                                                        │
  │  final EOT ──▶ classifier names a REGISTER ──▶ code    │
  │                (local, 14 ms GPU / 134 ms CPU)  picks  │
  │                       │                         from a │
  │                       │                         pool   │
  │                       ▼                                │
  │   SILENT   → (nothing)                                 │
  │   ATTEND   → "Checking it now." / "Let me take a…"     │
  │   PATIENCE → "I hear you, and I'm not rushing you."    │
  │   SYMPATHY → "I'm sorry, that's a lot to be carrying." │
  │   COURTESY → "Thanks for letting me know."             │
  │                                                        │
  │   every line PRE-LINTED at load by the same guard      │
  │   → a wrong answer is WRONG, never UNSAFE              │
  └────────────────────────────────────────────────────────┘
```

## 5. The second pass, on the way back

```
        desk answer ready
              │
              ▼
   ┌──────────────────────────────────────────┐
   │  clause: "I'm sorry — that's a lot to    │
   │           deal with."                    │
   │  body  : "I'm not going to push you for  │
   │           a payment today. …"            │
   └────────────────┬─────────────────────────┘
                    │
       compare the MOVE the clause makes
       against the MOVE track A already made
                    │
        ┌───────────┴────────────┐
        │                        │
   same move               different move
   (SYMPATHY on                (ATTEND said
    SYMPATHY)                   nothing of the kind)
        │                        │
        ▼                        ▼
   DROP the clause          KEEP the clause
   borrower hears:          borrower hears:
   "I'm not going to        "I'm sorry — that's a lot
    push you today…"         to deal with. I'm not…"

   THIS PASS IS SERIAL — it sits in front of the answer the borrower
   is actually waiting for, so its latency is dead air. That is the
   tight budget, not track A's.

   STATUS: the measurement for this pass was invalidated (the labels
   paired mismatched moves). Jev's probabilities rank the genuine
   duplicates correctly, but on three real examples. Needs a rebuilt
   dataset before wiring.
```
