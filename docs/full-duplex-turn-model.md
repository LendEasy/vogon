<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/full-duplex-turn-model.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# Full-Duplex Turn Model — Heard-Horizon Anchoring

**Status:** design approved in principle 2026-07-16; implementation staged A–E
(see Rollout). Companion docs: `../../voice-gateway/docs/full-duplex-semantic-barge-in.md` (the absorb
layer), `../../voice-gateway/docs/speech-delivery-completion.md` (the delivery gate this builds on),
`../../voice-gateway/docs/barge-in-compliance-protection.md`.

**Motivating call:** 2026-07-16 21:39 (call `5c171d0b`, "Deep Kumar"). Five
misfires in one call, all with the same root cause. Traces referenced below as
T1–T5.

---

## 0. The root cause, stated once

Every interpretation surface in the pipeline — the planner's `LAST AI SAID`
block, `follow_active` routing, per-agent history blocks, the negotiation
stage classifier, CONFIRMATION's "the agent just asked" rule, the silence
detector — assumes:

> *A caller utterance is a response to the most recently **emitted** agent
> speech.*

Under full duplex that assumption is false, sometimes by 20+ seconds. Speech
is **emitted** at one time (text handed to TTS, logged as SPEAK_EMITTED) and
**audited** — actually heard by the caller — over an interval that ends much
later (delivery marks in the motivating call: 1.6s to 19.8s of audio per
turn). Barge-ins, absorbed backchannels, silence probes, and chained
multi-node "mega-turns" all widen the emission/audition gap. The caller
answers the question at their *ear*; the pipeline interprets against the
question at its *log head*.

The transport already measures the truth precisely — delivery marks with RTT,
sentence-paced `heard_text`, barge truncation points, absorb events
(`DeliveryReport`, `annotate_delivery_view`) — but interpretation only
consults it after a *barge*. The ordinary full-duplex case (utterance arriving
mid-playout, or against a superseded question) reads the emission log.

**The fix is not to abolish turns.** The graph, agents, mutations, and evals
need discrete decision points. The fix is to re-anchor what a turn *is*:

> A turn is **one caller utterance interpreted at its own clock** — against
> the speech the caller had heard at utterance onset — not against whatever
> the pipeline most recently emitted.

The five traces:

| # | What happened | Root behavior |
|---|---|---|
| T1 | "Why do you want my identity?" misheard as "I do want my identity", absorbed as backchannel → silence detector counted the caller as silent for 11s → probe fired → caller's repeat got a FALSE `[Silence context…]` annotation → annotation stored verbatim in the unanswered-questions stack → read to the circleback classifier 15 turns later → `deferred_to_team` for a question verification itself had answered | absorb/silence decoupling + annotation leakage |
| T2 | "Yeah, I'm still…" (answering the probe) interpreted against the NEWER deflect+re-ask speak that was still playing | emission-anchored interpretation |
| T3 | Caller re-said the ZIP **1 second before** the 19.8s mega-turn finished playing; consumed against the mega-turn → routed to negotiation → "I'm sorry, I didn't quite get that" for a perfectly clear answer | emission-anchored interpretation + mega-turn |
| T4 | "Okay." (acknowledging *"could you say that again?"*) consumed as **accepting** a $201.42 payment ask buried at the FRONT of the mega-turn two exchanges earlier; the dedup combiner had also REORDERED the ask off the end (deferral rider after the question) | emission anchoring + one-question violation + combiner reorder |
| T5 | "No. I don't want to pay you guys anything. Bye." → FAREWELL won; the co-fired payment decline was never recorded (`card_consent` stayed `pending`, no `no_resolution`); farewell said "It was a pleasure speaking with you" | terminal co-intent loss + register-blind farewell |

---

## A. Heard-horizon anchoring (the architectural core)

### Problem

The planner user message contains `LAST AI SAID: <full emitted text>` and a
history of full emitted turns. Agents' `history_block`s are built the same
way. When the caller starts speaking at time *t*, the text they are actually
responding to is the **prefix of agent speech that had played by *t*** — which
the pipeline can compute but never does outside a barge.

T3 is the cleanest demonstration: utterance onset 21:40:44.5; the mega-turn's
delivery mark landed 21:40:45.6. The caller had heard *at most* the first
~18.7s — and behaviorally was still answering the ZIP exchange from ~40s
earlier. The planner interpreted "It's nine eight one three three" against a
payment ask the caller had not finished hearing, classified IDENTITY_FIELD
correctly, then `follow_active` routed it to the *emission-current* node
(negotiation) instead of the *audition-current* one (identity), producing an
apology for a clear answer.

Stage-2 tagging (`_interrupted_heard`) already rewrites history for **barged**
speaks. Nothing covers: utterance onset during normal playout; utterance onset
after playout of speak N−1 but interpreted after speak N was emitted (T2);
mid-mega-turn responses (T3, T4).

### Proposed solution

1. **Onset timestamps on every utterance.** Stamp each STT final with
   speech-onset time. Sensing candidates (in preference order): Deepgram
   word-level timings (first word start); the BargeGate PROVISIONAL entry time
   (already tracked per energy event); eager-EOT first-partial arrival time.
   Carried on the utterance event through the pipeline.
2. **Heard-horizon computation.** For onset *t*, the horizon is: all fully
   delivered speaks (delivery mark ≤ *t*) plus, for a speak in flight at *t*,
   the sentence-paced heard prefix at *t* (the bounded playout buffer already
   does per-sentence accounting; per-sentence send times give a direct
   estimate). Pure function over the delivery records — testable offline.
3. **Interpretation uses the horizon.** The planner's `LAST AI SAID` becomes
   the last line **heard** at onset, with a marker for undelivered remainder:
   `[the agent was still speaking; the caller had NOT yet heard: "…"]`.
   History builders (planner + `_history_block`s) render heard-text for every
   turn — the barge-only annotated view generalized to always.
4. **Routing uses the horizon.** `follow_active` and the waiting-confirm
   validator resolve against the node whose question was the live one *at
   onset* (the open-question ledger in §B makes this explicit). T3 then routes
   the ZIP to identity (whose §1b idempotence guard re-confirms cleanly), and
   T4's "Okay." anchors to "could you say that again?" — nowhere near an
   acceptance.

**Rollout:** shadow-first, matching the house pattern. Phase A1 computes the
horizon on every utterance and logs a `HORIZON_DIVERGENCE` event whenever
horizon ≠ emission-tail (with both texts) — zero behavior change; production
calls quantify how often this matters. Phase A2 flips the planner's LAST AI
SAID + history to the horizon view behind `turn_anchor_mode = emission |
shadow | horizon`. Phase A3 flips routing. Each phase gates on the full sim
suite + eval diff.

### Sizing
Medium-large. New: onset plumbing, horizon function (+unit tests), history
builders' second view, mode flag. The sensing all exists.

---

## B. One open question per emitted speak, question strictly last

### Problem

Chained nodes concatenate their speaks into a single playout: T3/T4's
mega-turn was `circleback deferral + account summary + payment ask` — 19.8s,
three referents, then LLM-deduped into an order that put the deferral rider
AFTER the payment ask: *"…Are you able to take care of that today? Also,
regarding your other question…"*. Two invariants broke at once:

1. **Multiple/misplaced asks:** the engine's own design rule (account_summary:
   "the handoff question MUST appear verbatim as the LAST sentence") was
   violated by the *combiner*, not by any agent — the dedup prompt already
   instructs "end with the open question or ask" and the LLM did not comply.
   Prompt-only enforcement is demonstrably insufficient.
2. **Unbounded playout:** a 20s monologue guarantees the next utterance is
   ambiguous (which referent?) and maximizes the emission/audition gap that A
   exists to bridge.

### Proposed solution

1. **Deterministic ask-boundary guard on speak assembly** (not a prompt).
   After dedup/combine, deterministically verify: the final ask (the live
   question) is the last sentence; no rider content follows it; at most one
   *ask* (segments may contain rhetorical/ack questions — the guard keys on
   the waiting node's question text, which the engine knows, same mechanism as
   the Class-A/consent line detection). On violation: fall back to the
   pre-dedup ordered join (fail-open to correctness, not to the LLM's
   rewrite), log `DEDUP_GUARD_TRIPPED`.
2. **Rider placement rule:** chain speaks that are not the ask (circleback
   answers/deferrals, confirmations of prior actions) are ordered BEFORE the
   asking node's speak at assembly time — already the chain order (circleback
   → summary); the guard makes the combiner unable to undo it.
3. **Playout budget (phase 2):** cap a single emitted speak at N sentences /
   ~M seconds estimated audio (`estimate_total_ms` exists); a chain producing
   more yields at a sentence boundary — the remainder becomes a follow-on
   playout whose delivery is separately tracked (the bounded playout buffer
   already paces per-sentence; this extends it with a semantic boundary).
   Phase 2 because it interacts with barge/delivery accounting.

### Sizing
Guard + placement: small-medium (speak-assembly path + sims with ORDERED
asserts — the eval harness bypasses live assembly, so simulator coverage is
mandatory here). Playout budget: medium, separate change.

---

## C. Absorb ↔ silence coupling + absorbed-text carry-forward

### Problem

T1: the absorb heuristic judged "I do want my identity" (STT's mishearing of
*"Why do you want my identity?"*) a backchannel in 200ms and swallowed the
turn. Correct or not per-decision, the *system* then behaved as if the caller
had said nothing: the silence detector — blind to the absorb — counted 11s of
"silence" and fired *"Are you still there?"* at a caller who had just spoken.
The swallowed text was discarded outright, so when the caller repeated the
question, the pipeline had thrown away its best disambiguation evidence (two
noisy samples of the same question beat one).

### Proposed solution

1. **Absorbs feed the activity clock.** An absorb decision resets/advances the
   silence detector's last-activity timestamp (it was speech judged
   non-turn-worthy — it was not silence). The probe in T1 simply doesn't fire.
2. **Carry-forward buffer.** The absorbed final transcript is kept (with its
   onset timestamp) in a short-lived buffer — TTL of one utterance or ~20s,
   whichever first. The next real utterance's *interpretation input* becomes
   `absorbed-text + new-text` (clearly delimited as metadata, per §D — e.g. a
   `recent_absorbed` field the planner renders as its own context line, NOT
   inline text). STT's two attempts at the same question land in front of the
   classifier together.
3. **Absorb events are already logged** (`BARGE_ABSORBED`); add the swallowed
   text + the carry-forward consumption to the event details so the timeline
   shows the full life of an absorbed utterance.

### Sizing
Small. Transport-local (barge_semantics + silence detector) + one planner
context line.

---

## D. Annotation hygiene — metadata is not speech

### Problem

Two harness annotations are prepended INTO the utterance string
(`handler._annotate_utterance_for_llm`): `[Note: low transcription confidence
on: …]` and `[Silence context: caller was silent for Ns…]`. Once inside the
string they are indistinguishable from caller speech to every downstream
consumer, and in T1 the full silence-context preamble was stored verbatim in
the unanswered-questions stack (`QuestionPushed.question`), displayed in the
question-audit UI, and fed to the circleback classifier 15 turns later — where
"caller was silent for 11s… a connection check was sent…" was (a) false at
write time (the caller had been absorbed, not silent) and (b) meaningless at
read time, plausibly tipping the verdict to `deferred_to_team` for an
identity-purpose question that verification itself had long since answered.

The general rule: **annotations describe the moment of utterance; durable
state and later consumers must never see them as utterance text.**

### Proposed solution

1. **Separate channel.** `_annotate_utterance_for_llm` stops string-prefixing.
   The utterance event carries `annotations: {low_conf_words: […],
   silence_probe: {elapsed_s, probe_text}}`; the planner user message renders
   them as their own context lines ("TRANSCRIPTION NOTE: …", "SILENCE
   CONTEXT: …") adjacent to — not inside — `CALLER JUST SAID`. Planner prompt
   rule (planner.py:326) updated to match.
2. **Durable-state guard.** Every write of utterance text into durable state
   (`QuestionPushed`, dispute detail capture, promise notes, callback notes)
   uses the CLEAN text. Enforced centrally: the raw caller text and the
   annotated view are distinct variables from the moment of creation, and
   agents only ever receive... the clean text plus structured annotations —
   there is no combined string to leak. A `strip_annotations()` helper guards
   legacy paths and log-recovery (`_last_caller_utterance` must return clean
   text).
3. **Truth-accuracy fix inside C:** when the "silence" was actually an absorb,
   the annotation must say so (or be absent) — annotations that lie to the
   classifier are worse than none.

### Sizing
Small-medium (one injection site, planner rendering, a sweep over durable
writes + log recovery, sims for the stack-pollution case).

---

## E. Terminal co-intent resolution + register-aware farewell

### Problem

T5: `[CONFIRMATION, PAYMENT_RESPONSE, FAREWELL]` co-fired on *"No. I don't
want to pay you guys anything. Bye."* The FAREWELL-wins rule (correct — the
caller is leaving) routed ONLY to farewell: the payment decline was never
applied. The call ended `caller_farewell` with `card_consent = "pending"`, no
`negotiation_result`, no `no_resolution` outcome — the business fact "the
borrower refused payment" exists nowhere in the record. And farewell spoke
*"It was a pleasure speaking with you. Have a great day!"* — a warmth template
tone-deaf to an angry refusal.

### Proposed solution (as built — revised from the original design)

The original design gated the record on the planner's co-extracted
`authorization_response == "declined"` entity. **Measurement killed that**:
the extractor staged the entity 0/6 on the exact T5 utterance (and 0/5 even on
a plain "yes") — the entity channel is too unreliable to establish polarity,
and claiming "refused" without evidence would fabricate a business fact.

As built, the record is the polarity-free deterministic fact:

1. **Record-then-close:** when FAREWELL wins the terminating resolution while
   `payment_authorization` is WAITING_CONFIRM with `card_consent == "pending"`,
   the planner records `auth_unresolved_at_farewell = True` BEFORE farewell
   speaks. No perception needed — "the authorization was never resolved; no
   arrangement exists" is true whether the goodbye was "No. Bye." or "I'll do
   it tomorrow, bye" (nothing was captured either way). `card_consent` stays
   honest ("pending", not a fabricated "refused").
2. **Register-aware farewell:** the same condition stamps
   `farewell.register = "unresolved_payment"`; the shared farewell agent then
   speaks a neutral close ("Understood — we'll leave it there for today.
   Goodbye.") instead of the pleasure pool — tone-appropriate for ANY
   mid-authorization walk-away.
3. **Outcome mapping:** `auth_unresolved_at_farewell is True` → the
   established `NO_RESOLUTION` (a 1:1 flag mapping per the outcome module's
   design rule).

### Sizing
Small-medium (planner resolver rule + farewell wording + outcome map + sims:
the T5 script becomes the regression pin).

---

## Rollout order and gates

| Stage | Items | Risk | Gate |
|---|---|---|---|
| 1 | **D** annotation hygiene | low | stack-pollution sim + FAQ/PREVERIFY evals |
| 2 | **C** absorb/silence + carry-forward | low (transport-local) | absorb sims + silence-probe unit tests |
| 3 | **E** record-then-close + farewell register | low-medium | T5 regression sim + FAREWELL/CANCEL eval families |
| 4 | **B** ask-boundary guard + rider placement | medium (touches live speak assembly — sims with ORDERED asserts are the only coverage; evals bypass this path) | mega-turn ordering sims + full sim suite |
| 5 | **A1** horizon shadow logging | low | divergence telemetry on live calls |
| 6 | **A2/A3** horizon interpretation + routing | high (the model change) | full sim suite + full eval diff + live-call soak, staged behind `turn_anchor_mode` |

Every stage lands with its sims in the same commit (simulator-tracks-engine).
B's phase-2 playout budget and A3's routing flip are separately gated
decisions once telemetry from earlier stages is in.

## Build status (2026-07-17)

| Stage | Status |
|---|---|
| D annotation hygiene | SHIPPED — structured annotations, strip_annotations guard, T1 pins |
| C absorb/silence + carry-forward | SHIPPED — activity clock, TTL buffer, OVERLAP CONTEXT line |
| E record-then-close + register farewell | SHIPPED (revised: deterministic `auth_unresolved_at_farewell` fact — the entity channel measured 0/6 staging, polarity is never claimed) |
| B ask-boundary guard | SHIPPED — deterministic ends_on_ask + ordered-join fallback + DEDUP_GUARD_TRIPPED; playout budget deferred (phase 2) |
| A1 onset + ledger + divergence telemetry | SHIPPED — UtteranceEvent.onset_t (first-interim proxy), transport delivery ledger, heard_horizon(), HORIZON_DIVERGENCE events (turn_anchor_mode="shadow" default) |
| A2 horizon interpretation | BUILT, flag-gated — apply_horizon_to_history rewrites the last agent line to the heard prefix + unheard marker when turn_anchor_mode="horizon"; NOT yet default — flip after shadow soak quantifies divergence on live calls |
| A3 horizon routing | NOT BUILT — gated on A2 soak. NOTE: the agent-side §1b re-surface guards were REMOVED (they were suppression, not idempotence — see agent-best-practices.md §1b); A3 must therefore solve stale re-surfaces at the ROUTER (never re-invoke completed nodes with empty utterances), which is the correct home for it |

---

# F. The Exchange Model — floor-gated turns (the missing layer)

**Validated 2026-07-17** against the same call. A/B/C/D/E fixed *what text the
interpreter reads* and *what it does at the ends of a call*. They did NOT
change the one thing the user kept seeing: **an utterance answering an earlier
question is stamped into a fresh turn and aligned to a later one.** Root cause,
confirmed in code:

- `handler._process_utterance` opens a new turn unconditionally on every STT
  final: `turn_counter[0] += 1` runs first, before anything else.
- The turn is *logged closed* (`_log_end_of_turn`) BEFORE a single audio frame
  is sent — `_deliver_turn_result` (which starts TTS) is called *after*. So the
  `end_of_turn` event marks **text-pipeline completion, not playback
  completion**.
- `send_response` returns `mark_pending=True` — bytes queued to Twilio, not yet
  heard. `_tts_playing` flips false at stream-end while Twilio drains its buffer
  for seconds. **Nothing waits for the `tts_end_*` mark echo before the next
  utterance opens a new turn.**
- Live proof: the mega-turn's mark echoed at 21:40:45.605; the caller's ZIP was
  consumed + turn-incremented + classified at 21:40:44.575 — one second BEFORE
  the prior response finished playing. It was aligned to the payment ask the
  caller had not finished hearing.

Turn numbering is asymmetric today: several *agent* speaks can share a turn
(chained segments + probes), but each *caller* utterance forcibly opens a new
one. A "turn" is therefore not "one exchange" — it is "one STT final and
whatever text was emitted in reaction."

## F.1 Definitions

- **Floor.** Who may speak into the record. The agent HOLDS the floor from the
  start of a playout until playback completes (the `tts_end_*` mark echo) or a
  barge cuts it. The transport already knows this precisely (the ledger entry's
  `end_t`, stamped at the mark echo — `twilio_transport.py`).
- **Exchange.** The conversational unit = one agent **ask** plus the caller
  answer(s) to it. It has an `exchange_id`, the anchoring **ask** (text + node +
  `playback_complete_t` = the instant the caller could first answer it), and the
  utterances that belong to it. An exchange explicitly permits **N agent speaks
  and M caller utterances** — chained responses, probe replies, corrections,
  interjections — under one id. This is the "multiple speaks per turn" the model
  was missing.
- **Turn (`runtime.turn`) — unchanged.** Stays the monotonic per-event stamp on
  every mutation, log entry, eval trace row, sim `turn_idx`, and checkpoint. We
  do NOT rename or repurpose it; the exchange is layered ON TOP as the
  conversational grouping and the alignment anchor. (Renaming `runtime.turn`
  would touch mutations/evals/sims/checkpoints for no benefit — the bug is
  alignment, not the counter.)

## F.2 The alignment rule (the fix)

Every caller utterance carries an onset time (`UtteranceEvent.onset_t`, the
first-STT-interim proxy — already built in A1). On arrival:

1. **Resolve the live ask.** Among the asks the agent has emitted, the LIVE ASK
   at onset `t` is the most recent one whose playback had COMPLETED by `t`
   (`playback_complete_t <= t`) and which has not yet been answered. That is the
   question the caller is actually answering — what was in their ear and
   unanswered when they began speaking. Asks still in flight at `t` (emitted but
   not finished playing) are NOT candidates: the caller cannot be answering
   speech they have not heard.
2. **Anchor to the live ask, not the emission tail.** When the live-ask node
   differs from the emission-current node (later speech moved "current"
   forward), the utterance is anchored to the live-ask node for:
   - **interpretation** (planner `LAST AI SAID` + history → the live ask's text;
     this generalizes A2 from "heard text prefix" to "the correct ask, by
     node"), and
   - **routing** (`follow_active` / waiting-confirm resolution → the live-ask
     node) — the strong form, phase 2.
3. **Group by exchange.** The utterance joins the live ask's `exchange_id`; the
   call log tags it so the timeline groups the agent speaks and caller
   utterances of one exchange together (delivering the user's mental model).

This subsumes A2/A3: "heard-horizon" was the text view; the exchange is the same
idea made **node-aware and floor-gated**, which is what routing needs.

## F.3 Why not literally block until playback completes?

The tempting reading of "wait for playback to end the turn" is to *stall the
pipeline* until the mark echoes. We do NOT: stalling would defeat full duplex
(the caller can barge, and low latency is the whole point) and a lost mark echo
would hang the turn. Instead the floor is **observed, not enforced** — the
pipeline runs immediately as today, but *alignment* uses the floor timeline
(the ledger) to attach each utterance to the ask it actually answers. Same
latency, correct anchoring.

## F.4 Phasing

- **F-phase 1 (this change, foundation + interpretation, safe/additive):**
  the exchange registry + `exchange_id`, the live-ask resolver (pure, over the
  transport ledger + a handler ask-map), `EXCHANGE_*` call-log events, and the
  interpretation anchor wired through the existing `turn_anchor_mode`
  (shadow logs the mismatch as before; `horizon` applies the node-aware anchor).
  No routing change — the planner still decides; it just reads the right ask.
- **F-phase 2 (gated, next):** the routing anchor — `follow_active` and the
  waiting-confirm validator resolve against the live-ask node. This is A3 under
  a precise definition; it lands after F-phase-1 telemetry confirms the resolver
  matches reality on live calls, and it is the correct home for the stale
  re-surface class (never re-invoke a completed node with an empty utterance —
  the router, not a per-agent guard, owns this).

Gates unchanged: F-phase-1 sims in the same commit; the mode flip and F-phase-2
follow live telemetry.

## Detector calibration (2026-07-17, after the 442-call soak)

The first soak flagged 24 "misalignments" but the --dump revealed 23/24 were
FALSE POSITIVES — normal sequential turns where the agent replied correctly.
Two bugs, both fixed:

- **Baseline (Bug 1).** The detector compared the live-ask node against
  `active_waiting_confirm()`, which is None for every node that asks a question
  and returns IN_PROGRESS (call_opener, availability_check, identity_verification,
  recording_disclosure) — only payment_authorization waits. So any answered
  question tripped "live_ask != emission" on every setup turn. Fixed: a real
  misalignment is answering an OLDER ask than the LATEST emitted one
  (`resolve_misalignment`) — normal flow (answering the latest ask) is not
  flagged. Divergence is implied (the latest ask wasn't fully heard), so no
  separate check.
- **Attribution (Bug 2).** The ask was registered under the FIRST invoke step's
  agent, but in a chained turn the node that SPOKE the question is the LAST
  chained segment's agent — so node labels were wrong (e.g. `availability_check`
  on the identity/ZIP question). Fixed: register under the last non-empty
  segment's agent.

The horizon interpretation anchor and F-phase-2 routing were both correctly
GATED on this signal — building routing on the un-fixed detector would have
misrouted correct turns. Re-measure after the fix before deciding F-phase-2.

## Build status — F (2026-07-17)

| Piece | Status |
|---|---|
| Exchange foundation | SHIPPED — app/framework/exchange.py (AskRecord + resolve_live_ask, pure), transport.playback_complete_t() (ledger floor-release accessor), handler ask registry + exchange_id |
| Live-ask resolution + telemetry | SHIPPED, always-on — every utterance resolves the ask it answered from the ledger; EXCHANGE_ANCHORED event whenever the live-ask node differs from the emission-current node (misalignment made observable per exchange) |
| Interpretation anchor | SHIPPED, flag-gated — in turn_anchor_mode="horizon" LAST AI SAID is set to the live ask's verbatim text (node-aware, sharper than the heard-prefix rewrite). Default "shadow": logs the mismatch, changes nothing |
| F-phase-2 routing anchor | NOT BUILT — follow_active + waiting-confirm resolve against the live-ask node; lands after F-phase-1 telemetry + the mode flip, and is the home for the stale re-surface class (router owns "never re-invoke a completed node with an empty utterance") |
