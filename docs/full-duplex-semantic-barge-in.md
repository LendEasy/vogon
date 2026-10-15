# Full-Duplex Semantic Barge-In — Design

**Status:** Approved direction; Phase 1 in detailed design (inputs pending)
**Companions:** [`barge-in-tuning.md`](barge-in-tuning.md) (the energy gate this
sits above), [`speech-delivery-completion.md`](speech-delivery-completion.md)
(delivery records / re-delivery), [`barge-in-compliance-protection.md`](barge-in-compliance-protection.md)
(uninterruptible lines; §4f consent timing).

---

## 1. Why this feature exists

The transport barge-in now *works* (playback-drain fix): a confirmed barge cuts
the audio. Production immediately showed that cutting correctly is not the same
as cutting *wisely*. Reference call `5f82412d` (2026-07-15) exhibits three
failures:

- **F1 — Cooperative overlap treated as interruption.** "Yeah. It's a good
  time to speak," "Okay." are the caller answering the clause they just heard —
  not demanding the floor. The energy gate cannot know that; it cut the audio
  and destroyed the unheard remainder (identity preamble; payment terms).
- **F2 — Re-delivery is verbatim, whole-turn, moot-blind.** The `_delivery`
  record stores the merged turn text and re-speaks all of it: "Is now a good
  time?" was asked **three times**, twice after being answered; two stale
  records re-delivered in one turn on top of a new ask → a triple-compound
  question.
- **F3 — Answer misbinding → uninformed consent.** The card read-back was
  interrupted twice (record still `redelivering`); the silence probe merged a
  presence check with the pending consent question; "**Yeah. I'm there.**" was
  consumed as `card_consent = given` and the charge processed. The caller never
  heard the full terms in one intact delivery. (This is
  `speech-delivery-completion.md` §4f, previously deferred — now demonstrated
  live.)

**The principle:** the energy gate decides *that* the caller is speaking; a
semantic layer must decide *what that speech means for the floor* — absorb,
yield, or hard-cut — and afterwards *what is still owed* (unheard terms,
unanswered questions).

## 2. Decided parameters (user decisions, 2026-07-15)

| Decision | Value |
|---|---|
| Backchannel policy | **Absorb silently** — audio never stops — and emit **`BARGE_ABSORBED`** to the call log, so absorption volume/quality is measurable and the classifier can be refined from production data |
| Verdict engine | **A new Cerebras call** (accepted cost; most direct path to correct verdicts). Heuristic fast-path + strict timeout wrap it (§4.3) |
| Phases | **All phases implemented, all enabled by default** (each keeps a kill-switch config knob) |
| §4f consent gate | **Un-deferred** — implemented in Phase 2 |
| Sequencing discipline | Per phase: detailed design in this doc → user inputs → code |

## 3. Architecture — two stages

```
caller audio ─► AEC ─► BargeGate (energy FSM)          [exists — unchanged]
                          │ confirmed
                          ▼
                 YIELD-PENDING window (~bounded ms)     [Phase 1]
                  · audio KEEPS PLAYING
                  · frames keep flowing to STT
                  · eager/interim text accumulates
                          │
                          ▼
                 Semantic verdict                        [Phase 1]
                  heuristic floor (0 ms)
                  └► Cerebras classifier (timeout-capped)
                          │
        ┌─────────────────┼───────────────────┬──────────────┐
        ▼                 ▼                   ▼              ▼
   backchannel      answer_to_heard    substantive     safety
   ABSORB:          YIELD:             interrupt       (DNC/abuse/stop)
   don't cut,       cut, bind answer   CUT now         CUT now
   don't run a      to heard clause,
   turn, log        moot it from
   BARGE_ABSORBED   re-delivery
                          │
                          ▼
                 Owed-content resolution                 [Phase 2]
                  · re-delivery only for Class A/B content, per-line
                  · moot rule (answered/advanced-past content is dropped)
                  · §4f: consent valid only on intact delivery
                  · probes never re-embed consent questions
                          │
                          ▼
                 Bounded playout buffer                  [Phase 3]
                  · sentence-paced sends (≤1 sentence at Twilio)
                  · clause-boundary yield without destructive clear
                  · exact per-sentence delivered/heard tracking
```

Physical constraint shaping everything: **Twilio playout cannot pause — only
play or destructively clear.** Phase 1 therefore *defers* the clear (the
yield-pending window); Phase 3 shrinks what a clear can destroy to ≤1 sentence
and makes graceful yields possible without clearing at all.

## 4. Phase 1 — Semantic barge verdict (DETAILED DESIGN)

### 4.1 Yield-pending window

On BargeGate CONFIRMED, do **not** cut. Enter `yield_pending`:
- Playback continues; `forward_frames` keep feeding STT (already the case).
- The window ends at the earliest of: verdict reached, window budget expiry
  (`semantic_barge_window_ms`, proposed default **900 ms**), or the playout
  ending naturally.
- Window expiry with **no STT text at all** → treat as noise: **no cut**
  (today a confirmed cough clears real audio), log `BARGE_ABSORBED`
  (`verdict="no_text"`).

### 4.2 Heuristic floor (0 ms, always computed)

From the eager/interim text available in the window:
- ≤ `semantic_barge_backchannel_max_tokens` (proposed **3**) **and** all tokens
  in the backchannel lexicon ("yeah", "yes", "okay", "uh-huh", "right", "sure",
  "mhm", "got it", …) → `backchannel`.
- Question-shaped or ≥ `semantic_barge_substantive_min_tokens` (proposed **6**)
  → `substantive_interrupt`.
- Safety lexicon ("stop calling", abuse terms, "wrong number") →
  `safety` (immediate cut; never waits for the classifier — mirrors the
  compliance-doc break-through set).
- Otherwise → `ambiguous` → the Cerebras classifier decides.

### 4.3 The Cerebras barge classifier (new call — user decision)

- **Inputs (user message):** interim caller text so far; the **heard-prefix**
  of the current playout (from the delivery machinery — `est_heard_text` /
  Phase-3 exact); the unheard remainder (labelled); the pending-question class
  (consent / question / statement); last agent line.
- **Output schema:** `{verdict: backchannel | answer_to_heard |
  substantive_interrupt | safety, confidence: float}` — small strict schema.
- **System prompt:** static verdict definitions + the cooperative-overlap
  discriminator ("an answer to the clause already spoken is not a request for
  the floor") — cache-eligible; per-call data only in the user message.
- **Latency containment (this call gates an audio cut):** per-request
  **timeout `semantic_barge_llm_timeout_ms` (default 700 ms)** — the first
  explicitly time-capped LLM call in the codebase (the 16 s planner incident is
  the cautionary tale). On timeout/error → **fall back to the heuristic floor**;
  if the floor said `ambiguous`, **default to ABSORB** (user decision
  2026-07-15: when unsure, keep speaking). Consequence, accepted: a substantive
  utterance can occasionally be absorbed+swallowed on a classifier timeout —
  it is fully preserved in `BARGE_ABSORBED` (verdict `timeout_absorb`) so the
  rate is measurable, and a caller whose interrupt is ignored naturally
  repeats once the line finishes.
- Tag `barge_verdict`; `max_tokens` per the Cerebras floor table (≥256).

### 4.4 Actions

| Verdict | Transport | Turn pipeline | Log |
|---|---|---|---|
| `backchannel` / `no_text` | no cut; playback continues | **utterance is NOT processed as a turn** (see §4.5) | `BARGE_ABSORBED` {verdict, text, heard_fraction, source: heuristic\|llm} |
| `answer_to_heard` | cut (clear) | utterance processed normally; the heard clause is marked answered → feeds the Phase-2 moot rule | `BARGE_IN` + `SPEAK_INTERRUPTED` (as today) + `answered_heard_clause: true` |
| `substantive_interrupt` | cut (clear) | normal turn | `BARGE_IN` + `SPEAK_INTERRUPTED` (as today) |
| `safety` | cut immediately (pre-classifier) | normal turn | `BARGE_IN` {phase: safety} |

### 4.5 Absorption must swallow the turn

If absorbed audio keeps playing but the "okay" still runs the planner, the
pipeline answers it and collides with the ongoing playout (double-speak). An
absorbed barge therefore also **suppresses the corresponding utterance from the
turn pipeline**: the transport marks the absorbed window; the handler drops the
matching eager/final `UtteranceEvent`s and logs them inside the
`BARGE_ABSORBED` detail (the text is preserved in the log — nothing is lost,
it just doesn't drive a turn). If the caller keeps talking past the window
(backchannel → real content), the new speech is a fresh utterance and processes
normally.

### 4.6 Config (all default-enabled)

| Key | Default | Meaning |
|---|---|---|
| `semantic_barge_enabled` | `True` | master kill-switch for the verdict layer (off = pre-feature cut-on-confirm) |
| `semantic_barge_window_ms` | `900` | yield-pending budget |
| `semantic_barge_llm_timeout_ms` | `700` | Cerebras verdict call cap |
| `semantic_barge_backchannel_max_tokens` | `3` | heuristic floor |
| `semantic_barge_substantive_min_tokens` | `6` | heuristic floor |

### 4.7 Phase-1 decisions (RESOLVED, user 2026-07-15)

1. Window **900 ms** / LLM timeout **700 ms** — accepted.
2. Ambiguous + classifier timeout → **ABSORB** (not cut).
3. Absorption swallows the turn — **confirmed** (§4.5).
4. Scope: **Twilio transport only**; widget/realtime parity later.

Phase-1 implementation note: the classifier's "pending-question class" input is
enriched in Phase 2 (the transport doesn't know consent-class); Phase 1 passes
the last agent line + heard-prefix estimate.

## 5. Phase 2 — Owed content + consent integrity (DETAILED DESIGN)

### 5.1 Re-delivery scope restriction (kills the ×3 re-ask zombie)

The delivery interrupt (`speech-delivery-completion.md` §4c) currently arms for
**any** truncated turn, storing the merged turn text. Phase 2 restricts arming
to content that genuinely must be re-delivered:

- **Class B — consent questions.** Arming keys off a **declarative node flag in
  the workflow JSON**: `"delivery_class": "consent"` on `payment_authorization`,
  `payment_link_offer`, and `hardship_handler` (per the compliance doc's Class-B
  list). Coarse by design — hardship_handler has non-consent asks too, and
  over-covering means *more* re-delivery protection, which is the safe
  direction. (Precise per-ask agent flags are a later refinement if
  over-coverage proves annoying.)
- **Class A — compliance lines.** At arming time, run the registry's
  `satisfies()` over the delivered text for the Class-A line ids
  (`collections_lines.yaml`: `borrower_confirmed_disclosure`,
  `sensitive_data_refusal`, `phone_card_decline`, `hardship_waiver_conditions`,
  …). A hit arms a **statement** record carrying *that line's* text.
- **Everything else: never armed.** Ordinary conversational content relies on
  the agents' own re-ask machinery — the reference call shows it working
  (identity re-asked ZIP cleanly by itself, while the armed availability/opener
  records produced the "is now a good time?" ×3 zombie).

**The moot rule from the outline largely collapses into this restriction**: the
zombie came from non-A/B content being armed at all. What remains of mooting is
already built — terminating plans drop records (planner validator), and a
direct re-engagement supersedes (`resolve_invoke`). One addition: a Phase-1
`answer_to_heard` cut does **not** skip arming for Class A/B — an
answered-but-truncated consent question must still be re-asked (§5.3 makes the
early answer invalid anyway), and a compliance line must still be heard.

### 5.2 Per-line records

`_delivery` records store the **armed agent's own pre-dedup segment** (for
Class B: its question; for Class A: the registry line), not the merged turn
text. The handler already holds per-agent segments (`speaks: list[(text,
agent_id)]`) before the dedup merge; `_TurnResult` threads them to the arming
site. A statement re-speak then replays one line — never the whole turn.

### 5.3 §4f delivered-gate (consent integrity — un-deferred)

**Rule:** a Class-B consent affirmative is valid **only when the question's
`_delivery` record is absent or `delivered`.** If the record is
`interrupted`/`redelivering`, the ask was not fully heard — any affirmative
(including a bare "yes"/"okay"/"I'm there") routes to the agent's existing
re-ask path instead of committing.

**Surface: inside the consent agents** (payment_authorization's
authorized-consumption; hardship's self-authorizing confirms; payment_link_offer's
send-confirm): a small deterministic guard at the top of consent-outcome
handling — read own `get_node_output(node_id, "_delivery")`, downgrade
authorize→re-ask when pending. Agent-level keeps the domain rule where the
domain lives, is unit-testable per agent, and avoids a handler-side veto that
would fight the mutation flow.

**Noted consequence (accepted trade-off to confirm):** with §4g (loop cap)
still deferred, a caller who barges *every* re-ask can never complete consent —
each attempt re-asks. Phase 1's backchannel absorption makes this rare (an
"okay" no longer interrupts the re-ask), but a deliberate serial barger loops.
Option if unacceptable: a §4g-lite escape — after N rejected-consent re-asks,
offer a callback instead (mirrors the existing unclear-×2 ladder).

### 5.4 Probe ban

Silence probes currently re-embed the pending question (`_last_re_ask`) — the
reference call's probe merged "Are you still there?" with the charge question,
making "Yeah. I'm there." ambiguous by construction. Rule: **when the active
WAITING_CONFIRM node is Class B, the probe asks presence only**; the
waiting-confirm machinery then re-surfaces the consent question as its own
intact turn (existing behavior). Non-consent questions keep the embed (it's
good UX there).

### 5.5 What Phase 2 changes where

| Surface | Change |
|---|---|
| `collections_demo_v2.json` | `delivery_class: consent` on the 3 Class-B nodes |
| handler arming site | restriction (5.1) + per-line segment selection (5.2) |
| `delivery_pending.py` | record carries `line_text` + `class_a_line_id`; arming helpers pure |
| consent agents (3) | §4f guard at consent-outcome consumption (5.3) |
| `_last_re_ask` / probe builder | Class-B embed ban (5.4) |
| tests | pure arming-restriction tests; per-agent §4f guard tests; probe-ban test |

### 5.6 Phase-2 decisions (RESOLVED, user 2026-07-16 — "go with recommendations")

1. **Class-B declaration** — workflow-JSON node flag (`delivery_class:
   "consent"` on payment_authorization / payment_link_offer / hardship_handler;
   documented in `workflow_design.md`).
2. **§4f guard surface** — inside the 3 consent agents (shared helpers in
   `framework/delivery_pending.py`; hardship uses one `_consent_gate` method at
   its four self-authorizing sites; progress in node_outputs
   `consent_delivery_blocks`).
3. **§4f × loop cap** — §4g-lite: after `CONSENT_BLOCK_MAX = 2` blocked
   attempts, each agent closes to its own safe ladder (payment_authorization →
   the unclear-×2 low-confidence callback close; link_offer → self-serve +
   servicing callback; hardship → general referral). Never a charge/send on a
   half-heard ask.
4. **Class-A re-delivery text** — the matched segment verbatim (the line as
   actually spoken, placeholders filled), detected via static signatures
   (`renderer.CLASS_A_SIGNATURES`, most-specific first).

Implementation notes: `delivery_gate_mode` default flipped shadow → **enforce**
(all phases default-on; Phase 2 lives in the enforce path). A real utterance
reaching a pending CONSENT question is **guarded**, not superseded — clearing
the record would erase the §4f evidence (`resolve_invoke` → "guarded"; the
turn counts as the re-delivery attempt). New call-log event: `DELIVERY_ARMED`
{node_id, class, line_id, consent}; a SPEAK_INTERRUPTED without a following
DELIVERY_ARMED = ordinary content, deliberately not armed.

## 6. Phase 3 — Bounded playout buffer (DETAILED DESIGN)

### 6.1 The structural problem Phase 3 removes

Today `_do_tts_stream` pushes the ENTIRE utterance to Twilio as fast as
Deepgram generates it; Twilio plays from its own multi-second buffer. Three
consequences: a barge can only *destructively clear* (losing everything
unplayed); delivered/heard is an estimate (frames×20 ms vs elapsed); and there
is no clause granularity for yields or per-line protection.

Phase 3 inverts the buffering: **the audio lives in OUR paced queue; Twilio
holds only a small lead.** The barge vocabulary then grows from
{play, destroy-everything} to {absorb, finish-the-sentence, cut-≤lead}.

### 6.2 Mechanism — three parts

**(a) Sentence segmentation.** Split the assembled speak at sentence
boundaries (pure splitter: sentence-final `.`/`!`/`?` followed by whitespace —
never inside amounts like `$201.42`; fragments under ~25 chars merge forward).
TTS runs **multi-segment on one Deepgram WS**: send sentence *i* + flush, read
audio until its `Flushed` event (the SDK emits one per flush — verified
feasible; `test_tts_ws_cadence.py` is the spike vehicle), prefetching sentence
*i+1* while *i* plays. Exact text↔audio mapping per sentence falls out.

**(b) Paced release.** A local frame queue + a 20 ms-paced sender (the
ambience loop already runs this cadence — Phase 3 unifies outbound audio into
ONE paced loop: TTS-mixed frames when speaking, bed/typing otherwise). The
sender stays `tts_buffer_lead_ms` (default **500 ms**) ahead of real time —
that lead is ALL Twilio ever buffers. Send-side "buffer debt" disappears by
design; the local queue absorbs Deepgram jitter. Side benefit: the AEC
reference is fed at ~playback time (today it runs seconds ahead through a
FIFO).

**(c) Per-sentence marks.** A Twilio mark after each sentence's frames; the
echo confirms that sentence PLAYED. `DeliveryReport` gains
`sentences_delivered / sentences_total` and heard text becomes
`join(sentences[:k])` + a ≤1-sentence estimate for the cut one — near-exact,
replacing the proportional `est_heard_text` everywhere (Phase-1 classifier
inputs, Phase-2 records, history annotation all sharpen for free).

### 6.3 Barge actions under the bounded buffer

| Verdict (Phase 1) | Action | Audio lost |
|---|---|---|
| absorb-class | nothing — producer keeps going | none |
| `answer_to_heard` | **graceful yield**: stop queueing further sentences; the current sentence finishes naturally — NO Twilio clear | only unspoken sentences |
| `substantive_interrupt` | hard cut: stop producer, drop local queue, clear Twilio | ≤ lead (~0.5 s) |
| `safety` | hard cut, immediately | ≤ lead |

The delivery interrupt (Phase 2) then arms with an exact boundary: sentences
1..k delivered, k+1..n owed — a Class-B re-ask can even re-speak only the
unheard suffix (future refinement; Phase 3 keeps whole-line re-asks).

### 6.4 Latency budget (the constraint that gates this phase)

- **First audio must not regress**: sentence 1 goes to TTS immediately (a
  SHORTER first request than today — first chunk should arrive sooner);
  pacing adds ≤ one 20 ms frame period.
- **Inter-sentence gaps target 0**: prefetch depth 1 sentence; the 500 ms lead
  absorbs synthesis jitter. Guard metric: p95 gap at sentence boundaries.
- **A/B plan**: `tts_pacing_mode: "sentence" | "burst"` — burst is the
  pre-Phase-3 path, kept as the kill switch. Compare `tts_first_chunk_ms`,
  frame-gap counts, and boundary-gap p95 across a set of staging calls before
  trusting sentence mode in production (it ships default-on per the feature
  decision, with the kill switch one env var away).

### 6.5 What changes where

| Surface | Change |
|---|---|
| `app/pipeline/sentence_split.py` (new, pure) | conservative splitter + merge rules |
| `app/pipeline/tts.py` | `stream_segments(sentences)` — multi-flush on one WS, yields (segment_idx, chunk) with per-segment `Flushed` boundaries |
| `twilio_transport.py` | producer/paced-consumer split; unified outbound loop (TTS + ambience); per-sentence marks; graceful-yield flag (`stop after current sentence`); hard cut = drop queue + clear |
| `delivery.py` | `sentences_delivered/total`, exact heard join |
| `_act_on_barge_verdict` | verdict → {absorb, yield_boundary, cut_now} mapping |
| config | `tts_pacing_mode="sentence"`, `tts_buffer_lead_ms=500`, `tts_prefetch_sentences=1` |

### 6.6 Risks & mitigations

1. **Deepgram multi-flush cadence** is the load-bearing assumption → spike
   FIRST with `test_tts_ws_cadence.py` extended to two flushes (measure
   inter-segment synthesis latency) before transport surgery.
2. **Recorder timestamps** (`_send_media_frame` feeds the recorder at send
   time) — with pacing, send≈playback: timeline accuracy improves, but the
   recording tap tests pin current timing assumptions and may need updating.
3. **Mark volume**: n marks/turn instead of 1 — Twilio handles multiple marks;
   `_pending_marks` and `_playback_draining` semantics carry over per sentence.
4. Realtime/widget transports out of scope (unchanged burst behavior).

### 6.7 Phase-3 decisions (RESOLVED, user 2026-07-16)

1. **Lead buffer** — `tts_buffer_lead_ms = 500` (worst-case audio lost on a
   hard cut; jitter safety margin).
2. **Verdict mapping** — `answer_to_heard` → graceful yield; in the drain
   phase (≤ lead remaining) the yield is a no-op (let it finish);
   `substantive_interrupt`/`safety` → hard cut. In burst mode (kill switch)
   `answer_to_heard` falls back to the Phase-1 hard cut.
3. **Spike ran first** (2026-07-16, real Deepgram, 3 segments on one WS):
   Flushed events demarcate cleanly; seg-0 first chunk **97.8 ms**;
   inter-segment gaps **59–74 ms** (≪ the 500 ms lead → no audible gaps);
   synthesis ~2× real time. The load-bearing assumption held.
4. **Uniform pacing** — sentence mode for ALL speaks (greeting and farewell
   included). `tts_pacing_mode = "sentence"` default; `burst` is the kill
   switch. Legacy non-mulaw encodings and error retries use burst.

Defaults are documented in the top-level README (env-var form), in
`app/config.py`, and in the splitter (`sentence_split.MIN_SENTENCE_CHARS=25`,
short fragments merge so a consent question never plays as its own tiny
segment). A graceful yield reports as `truncated` with an EXACT sentence
boundary (`sentences_sent/total`, `heard_text`) — Phase-2 arming and §4f read
it exactly like a cut.

## 7. Measurement

- `BARGE_ABSORBED : BARGE_IN` ratio (absorption volume; classifier refinement
  feedstock — every absorbed event carries text + verdict source).
- Re-deliveries per call (expect ≈0 outside Class A/B after Phase 2).
- Repeated-question incidents (same question spoken >1× per call).
- **Consent-committed-while-record-not-delivered: must be zero** (the
  compliance metric; F3's class).
- `barge_verdict` LLM latency distribution + timeout/fallback rate.

## 8. Rollout

All phases default-enabled on land (user decision), each with its own
kill-switch. Phase order: 1 → 2 → 3, each gated on: detailed design in this
doc → user inputs → implementation → the reference-call scenarios re-tested in
production.

## See also — the turn model this layer sits inside

`full-duplex-turn-model.md` (2026-07-16): heard-horizon turn anchoring —
the absorb layer's coupling to the silence detector and the absorbed-text
carry-forward buffer are specified there (§C), motivated by a live call where
an absorbed question cascaded into a false silence probe, a lying annotation,
and a polluted unanswered-questions stack entry.
