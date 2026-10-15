# Delivery-Gated Completion — Design Proposal

**Status:** Proposal / decisions pending
**Problem doc:** the emission-vs-delivery timing analysis (summarised in §1); sibling
design: [`barge-in-compliance-protection.md`](barge-in-compliance-protection.md)
(this proposal supplies the mechanism its §3b consent-timing rule needs).

---

## 1. Problem (recap)

All semantic state is finalized at **emission** time; delivery is a fire-and-forget
side effect that a barge-in can truncate to any prefix — including zero words:

| T | What happens | Where |
|---|---|---|
| T1 | `handle()` returns → `apply_mutations` commits `NodeStatusChanged(completed)` + node_outputs | `handler.py:878` |
| T2 | Full text appended to transcript + `SPEAK_EMITTED` written to the call log | `handler.py:1373-1382` |
| T3 | Delivery *begins*: `await transport.send_response(text, audio_id)` | `handler.py:550` |
| T4 | Barge-in cancels the stream mid-chunk (`twilio_transport.py:607`) or clears the buffered tail post-stream (drain path, `:453`) | |
| T5 | `send_response` returns **`None`** — `chunks_sent` / `cancelled` die inside the transport (`twilio_transport.py:516,549,569`) | |

Consequences: a node is COMPLETED though its speech was never heard; every
classifier's history block contains text the borrower didn't hear; a
WAITING_CONFIRM consent question is "asked" at emission even if zero words played.

## 2. Goal — the invariant

> **A speech-bearing terminal transition commits only when the speech has landed
> completely. A truncated delivery is a first-class interrupt to the speaking
> agent: service the barge-in, then re-surface the agent, re-deliver the
> undelivered speech, and only then mark it complete.**

This is deliberately modeled on the existing interrupt system — same
resume-stack shape, same re-surface discipline — but at the **delivery** level
instead of the semantic level.

## 3. What "landed completely" means (per transport)

Three different facts exist at the bottom layer; the design must pick one per
transport:

| Transport | Definition of delivered | Basis |
|---|---|---|
| Twilio | All chunks streamed **and** not cancelled **and** the `tts_end_{audio_id}` **mark echo** received (Twilio's playback confirmation, `twilio_transport.py:576-583`). The mark is required because the drain-path `_send_clear()` can kill the buffered tail *after* the stream loop finished. | strongest available |
| Widget | Stream loop completed without `_tts_cancel` (`voice_transport.py:137-144`). No mark mechanism exists; stream-complete is the best available fact. | best available |
| Realtime | `response.done` without a `response.cancel` / `_cancel_audio` in between. | best available |

The mark echo arrives asynchronously after `send_response` returns. The design
therefore resolves delivery state **at the start of the next turn** (inside
`utterance_lock`, before the planner runs) — by then the barge that caused any
drain-clear has already fired, so the record is final. No new async callback
machinery is needed.

## 4. Core design

### 4a. DeliveryReport — close the one-way boundary

`send_response` (all three transports, `BaseTransport`) returns a report instead
of `None`:

```python
@dataclass
class DeliveryReport:
    audio_id: str
    status: Literal["complete", "truncated", "failed"]
    chunks_sent: int
    audio_ms: float            # chunks_sent × frame duration (what was streamed)
    est_total_ms: float        # ESTIMATE from text length — the true total is
                               # unknowable when the TTS stream is cut mid-flight
    mark_pending: bool         # Twilio: mark echo not yet confirmed at return time
```

`heard_fraction` is always `audio_ms / est_total_ms` — an estimate, labelled as
such wherever logged (audio chunks do not map exactly to text).

The transport also keeps a `last_delivery(audio_id)` accessor so the handler can
re-resolve `mark_pending` records at next-turn start (§3).

### 4b. Pending-delivery record — the "some kind of pending status"

When a turn's speak is assembled and its agents' terminal transitions are known,
the handler records, **before** calling `send_response`:

```
node_outputs["_delivery"]  = {
    "turn": N,
    "audio_id": "resp-7",
    "text": "<full intended text>",
    "agents": ["payment_authorization"],        # terminal-with-speak agents this turn
    "class": "question" | "statement",          # WAITING_CONFIRM ask vs. plain speak
    "state": "pending",                          # → "delivered" | "interrupted"
}
```

Stored in `node_outputs` (via `NodeOutputSet`) — not `internal_state`, not a
handler local — so it obeys the statelessness contract and survives call
recovery exactly like every other cross-turn fact. Underscore-prefixed keys in
node_outputs are an established pattern (`_nlu_entity_*`). Note the edge/tool
context builders flatten **all** node_outputs keys (`handler.py:765,995`), so
`_delivery` will appear there — inert, since no edge condition references it,
but documented deliberately.

**On `status == "complete"`** (the normal case): record flipped to `delivered`,
`SPEAK_DELIVERED` logged (§6), nothing else changes. Zero behavioral delta.

**On `status == "truncated"`** (barge-in): the delivery interrupt begins (§4c).

### 4c. The delivery interrupt — the flow you sketched

```
turn N   agent completes → speak sent → BARGE-IN cuts it at chunk k
         ├─ delivery record → state="interrupted", heard≈k/est_total
         └─ the speaking agent's terminal commit is REVERTED to pending   (§4d)

turn N+1 the barge utterance arrives (it is already in the utterance queue —
         barge frames flow to STT) and is SERVICED FIRST, exactly like any
         interrupt: planner classifies it, its agent(s) run.
         └─ a post-plan validator (§4e) sees the interrupted delivery record
            and appends the re-surface at the END of the plan — the record is
            a single-slot "delivery stack" consulted on every plan.
            (Deliberately NOT planner.push_resume: a stale resume-stack entry
            re-surfacing minutes later is a known production bug class — the
            stale-resume bug fixed via opener idempotence. The validator runs
            every plan, so the record can neither go stale nor be popped by
            the wrong path.)

re-surface: the handler sees the node's delivery record state="interrupted"
         ├─ class="statement"  → RE-SPEAK the stored text verbatim, with a
         │                       re-entry prefix ("Sorry about that — ...") —
         │                       the same convention the mid-stream TTS error
         │                       recovery already uses (twilio_transport.py:552-564)
         └─ class="question"   → re-invoke the agent with utterance="" — the
                                 existing WAITING_CONFIRM re-surface machinery
                                 re-asks the question through the agent's own
                                 resurface wording (agents are idempotent on
                                 re-surface by contract)

completion: the re-delivery gets its own DeliveryReport.
         └─ complete → record→"delivered", terminal status COMMITS, agent is
            now genuinely COMPLETED / WAITING_CONFIRM-with-question-heard.
```

Two re-speak classes, deliberately:

- **`statement`** (disclosures, confirmations spoken on COMPLETED): the handler
  re-speaks the stored text itself — the agent's `handle()` is **not** re-run, so
  its mutations cannot double-apply.
- **`question`** (WAITING_CONFIRM asks): re-surfacing through the agent's own
  empty-utterance path is better than verbatim replay — the agents already have
  purpose-built resurface wording (`payment_authorization._build_auth_question(...,
  resurface=True)`), and the barge utterance may have changed the context. The
  critical bit this design adds: the question's "asked" flags (`opening_spoken`
  etc.) only count once a delivery report confirms the ask **landed** — which is
  precisely the mechanism `barge-in-compliance-protection.md` §3b (answer-after-
  question consent validity) needs.

### 4d. Status semantics — DECIDED: Option A, `AgentStatus.DELIVERY_PENDING`

**User decision (2026-07-14):** a first-class enum status, no provisional
commits/compensation — "new behavior everywhere." Statuses still commit as
today *during* the turn (the in-turn chain routes on `completed` edges); the
new status is applied **at delivery-truncation time** by the handler, and the
held terminal status (stored in the `_delivery` record) commits when the
re-delivery lands.

**Empirical blast-radius verification** (add the status first, see what breaks —
nothing did; full fast sweep identical to the 82-failure pre-existing baseline):

| Surface | Treatment of DELIVERY_PENDING | Change needed |
|---|---|---|
| `graph.available_nodes()` | excludes only COMPLETED/FAILED → pending stays available | none |
| `graph.active_primary()` | matches only IN_PROGRESS/WAITING_CONFIRM → pending is not "active" (desired: the validator owns its re-surface) | none |
| `graph.active_waiting_confirm()` | doesn't match (desired: an unheard question must not capture answers) | none |
| `Planner._is_permanently_closed` | checks COMPLETED only → pending not closed | none |
| `graph.update()` | assigns statuses unconditionally → the question-class re-ask cleanly overwrites | none |
| checkpoint serialize/restore | str-enum round-trip (`AgentStatus(status_str)`) | none |
| simulators | default mode is `shadow` → enforce paths dormant → contracts unchanged | none |

The revert remains narrow by design: **only the status is held, never the
node_outputs** — the semantic work (staged amounts, classifications) stands,
exactly as it does for semantic interrupts today.

### 4e. Guaranteed re-surface — `_validate_pending_delivery` (the sole mechanism)

Mirror the existing post-plan validator (`Planner._validate_waiting_confirm`,
which already injects a missing WAITING_CONFIRM agent into every plan): add
`_validate_pending_delivery` — if any node's `_delivery.state == "interrupted"`,
append the re-surface step at the **end** of the plan (barge serviced first, per
the required ordering), `use_empty_utterance=True`. If the plan already
re-surfaces that agent, do nothing (no duplicate re-ask). If the plan is a
terminating interrupt (DNC, farewell, safeguarding …), the record is dropped —
terminal interrupts win, same as today.

The semantic resume stack (`planner.push_resume`) is deliberately **not** used:
it is only popped on a `resume` edge, so a barge that routes to a primary would
bypass it and strand the record — and a stale stack entry re-surfacing turns
later is a known production bug class. A validator consulted on **every** plan
has neither failure mode.

### 4f. Barge-answers-the-question case — DEFERRED (user decision 2026-07-14)

If the barge utterance routes to the **pending agent itself** (the borrower
barged with what looks like an answer — "yes do it" over the read-back):

- **`question` class:** the question was not fully heard, so the answer is not
  informed — the agent **re-asks instead of accepting** (this is the concrete
  implementation of compliance-doc §3b; enforcement scope can start with the
  Class-B consent questions listed there).
- **`statement` class:** the barge content is handled normally; the statement is
  still re-spoken afterwards (it was a required disclosure) unless the barge
  made it moot (e.g. DNC/farewell terminates the call — terminal interrupts win,
  same as today).

### 4g. Loop protection — DEFERRED (user decision 2026-07-14)

A borrower who barges every re-delivery must not trap the call. Config knob
`delivery_respeak_max` (default **2**): after N truncated re-deliveries of the
same record, commit the turn anyway, log `SPEAK_INTERRUPTED` with
`gave_up=true`, and let the normal flow proceed. (For Class-A compliance lines
the compliance doc's protection mechanism — uninterruptible playout — is the
real guarantee; this re-speak path is the recovery net, not the primary control.)

## 5. Multi-agent merged speak

A turn's delivered text may merge several agents' speaks (dedup filter). Delivery
is per-turn — one audio stream, one report — so:

- The `_delivery` record lists **all** terminal-with-speak agents of the turn.
- On truncation, only the agents whose class demands re-delivery re-arm:
  the **last** WAITING_CONFIRM asker (there is at most one — WAITING_CONFIRM
  wins speak assembly today) and any `statement` agents whose text carries a
  registered compliance line (`collections_lines.yaml` keys — detectable via
  `satisfies()`).
- Ordinary informational segments from other agents are **not** re-spoken —
  re-delivering an entire merged answer verbatim after an interruption is
  worse UX than losing a partial informational sentence. (Their truncation is
  still *logged*, §6.)
- No text-offset attribution is attempted: audio chunks do not map reliably to
  text offsets (§6 logs an estimate only). Re-delivery granularity is
  line/question, not character.

## 6. Logging design (the observability gaps)

All new events go to the **call log** (`EventType`), not just the audio timeline
— the call log is the ground truth agents and evals read.

**Writer: the handler, never the transport** (the transport has no
`runtime.call_log`). Two resolve points:
1. **Post-send** — immediately after `send_response` returns, the handler logs
   `SPEAK_DELIVERED` or `SPEAK_INTERRUPTED` (+`BARGE_IN`) from the report.
2. **Next-turn drain resolve** — a drain-path clear kills the buffered tail
   *after* `send_response` returned as complete. The transport records it
   (`consume_drain_clear()` API); the handler checks at the start of the next
   final-utterance turn (inside `utterance_lock`, before the planner) and, if
   set, logs the late `BARGE_IN`/`SPEAK_INTERRUPTED` upgrading the prior record.

| Event | When | Detail |
|---|---|---|
| `SPEAK_EMITTED` *(unchanged)* | at emission, as today | full **intended** text — explicitly documented as *intent*, not receipt |
| **`BARGE_IN`** *(new)* | when a confirmed barge cancels/clears audio | `audio_id`, `chunks_sent`, `audio_ms_heard`, `phase` (`in_flight` \| `drain_tail`), transport |
| **`SPEAK_DELIVERED`** *(new)* | delivery record resolves complete | `audio_id`, `chunks`, `audio_ms`, `mark_confirmed` |
| **`SPEAK_INTERRUPTED`** *(new)* | delivery record resolves truncated | `audio_id`, `intended_text`, `heard_fraction` (= chunks_sent/total), `est_heard_text` (text prefix by proportion — **labelled estimate**; audio→text alignment is not exact), `respeak_attempt`, `gave_up` |
| **`SPEAK_REDELIVERED`** *(new)* | a re-speak/re-ask lands | `audio_id`, `original_audio_id`, `class` |

This covers all three of your asks: the **intended** speech (`SPEAK_EMITTED`,
unchanged), the **partial** speech actually heard (`SPEAK_INTERRUPTED.est_heard_text`
+ exact `audio_ms_heard`), and the **barge-in itself** as a first-class event.

**History-builder fix (closes the log-as-ground-truth divergence):** every
classifier history block is built from `SPEAK_EMITTED`. Rule: when a
`SPEAK_INTERRUPTED` exists for the same turn, the history line becomes
`Agent: <est_heard_text> [interrupted — borrower did not hear the rest]` — so
classifiers reason from what was heard, not from the fiction of the full line.
(One shared change in the history builders; the same `log_slice` feeds them all.)

## 7. Interactions with existing/parallel work

- **`barge-in-compliance-protection.md`** — complementary halves:
  that doc *prevents* truncation of protected lines (uninterruptible playout);
  this doc makes the system *truthful and self-healing* when truncation happens
  anywhere else — and supplies the question-delivered fact that its §3b consent
  rule requires. Class-B consent questions should adopt §4f first.
- **STT low-confidence gate** — its re-ask/farewell sends get DeliveryReports
  like everything else, but are `statement`-class with no terminal transition to
  hold; no interaction beyond logging.
- **Exclusive-group guard / composition** — orthogonal (plan-time vs
  delivery-time), no interaction.
- **Silence probes / fillers** — excluded from delivery gating (already tagged
  and excluded from history today; a truncated filler is meaningless). The
  filler path keeps its existing `transport.interrupted` check (`handler.py:1430`).
- **Farewell / `_finalize_call`** — decision pending (§9): gating call-end on
  farewell delivery would keep the call open against a caller who is hanging up;
  recommended to log-only for terminal speaks.

## 8. Rollout — same shadow-first pattern as the STT gate

Config: `delivery_gate_mode: off | shadow | enforce`. Landed as default
`shadow`; flipped to default **`enforce`** with full-duplex-semantic-barge-in
Phase 2 (2026-07-16, user decision: all phases enabled by default — the Phase-2
consent integrity lives in the enforce path).

1. **Stage 1 — plumbing + observability (shadow).** DeliveryReport from all three
   transports; `BARGE_IN` / `SPEAK_DELIVERED` / `SPEAK_INTERRUPTED` call-log
   events; END_OF_TURN gains delivery detail. **Zero behavior change.** Prod
   shadow data quantifies how often truncation actually bites (and calibrates
   `heard_fraction` thresholds).
2. **Stage 2 — history-builder annotation.** Classifiers see
   `[interrupted…]`-annotated lines. Small, high-value, independently shippable.
3. **Stage 3 — the delivery interrupt (enforce).** Pending record, status
   compensation (Option B), resume-stack push, `_validate_pending_delivery`,
   re-speak/re-ask classes, loop cap. Sim: `simulate_delivery_interrupt.py`
   (fake transport returning scripted truncated reports — no real audio needed,
   the handler seam makes this deterministic).
4. **Stage 4 — consent-validity wiring.** Class-B questions (compliance doc)
   reject answers whose question wasn't delivered (§4f). Lands together with —
   or immediately after — the compliance doc's protection mechanism.

Per the simulator-contract rule: Stage 3 changes what statuses mean between
turns → the affected `simulate_*.py` files are updated in the same commit.

## 9. Open decisions

1. ~~Option A (new enum status) vs Option B (overlay)~~ — **DECIDED: Option A** (§4d).
2. **Truncation threshold** — is `heard_fraction ≥ X` (e.g. 0.95, cut during the
   final word) still "delivered"? Recommended: shadow data first; start strict
   (only `complete` counts) for `question` class, threshold allowed for
   `statement` class.
3. **Terminal speaks** (farewell, no-resolution close): gate call-end on
   delivery, or log-only? Recommended: log-only — never hold a call open to
   re-speak a goodbye.
4. **Widget/realtime parity timing** — Twilio first (only path with mark
   confirmation); others in Stage 1 for reports/logging, Stage 3 later.
5. **Scope of `statement` re-speak** — all compliance-registry lines, or only
   the Class-A list from the compliance doc? Recommended: Class-A list.

## 10. Explicitly out of scope

- Sentence-level partial resume ("continue from where it was cut") — audio→text
  alignment is estimative; re-delivery is whole-line/whole-question.
- Per-sentence TTS streaming (would improve granularity; separate effort).
- The uninterruptible-playout mechanism itself — owned by
  `barge-in-compliance-protection.md`.

## 11. Module layout & the event-surface eval harness (task #45, 2026-07)

The wiring extracted from `handler.py` now lives in one shared module so every
consumer of the engine runs the identical gate logic:

| Layer | Module | Role |
|---|---|---|
| data | gateway `transport/delivery.py`; turn side `app/framework/delivery_report.py` | `DeliveryReport` on each side — what actually played (pure). The gateway's rides the wire as `DeliveryOutcome` with `heard_text` normative; the turn side's is built from that fact and estimates nothing (2026-10-10) |
| decisions | `app/framework/delivery_pending.py` | arm? respeak? commit? (§4c–§4e, pure fns) |
| **wiring** | `app/framework/delivery_gate.py` | apply decisions to a live runtime: events, mutations, status flips |
| live | `vogon/websocket/handler.py` | calls the wiring; `settings.delivery_gate_mode` is the switch |
| sim | `tests/sim_harness.py` | `CallSim(delivery_gate_mode="enforce")` — same wiring, lazy settlement |
| evals | `evals/runner/driver.py` | `barge_in` / `barge_absorbed` injects on the event surface |

`delivery_gate.py` exposes exactly three entry points — `record_delivery_events`
(Stage 1+2), `enforce_post_delivery` (Stage 3 resolve + arm), and
`intercept_invoke` (re-surface actions) — each fully documented in the module.
Any change there is a workflow-engine behavior change: the barge sims
(`tests/simulate_barge_delivery.py`) and the eval barge family (`--filter BARGE`)
gate it in the same commit.

**Event-surface evals (no audio).** `CallSim` in enforce mode models every
speak as a playout attempt settled lazily: complete at the next `turn()`, or
truncated by `apply_barge(barge_at=…| before=…)` — which emits
`BARGE_IN`/`SPEAK_INTERRUPTED` with a sentence-aligned heard prefix and runs
the real arming logic. The eval yaml drives this with:

```yaml
- inject: barge_in          # optional: barge_at: 0.25  |  before: "recorded or monitored"
- inject: barge_absorbed    # semantic backchannel absorption — must disturb nothing
expect:
  events: { barge_in: 1, delivery_armed: 1, speak_redelivered: 1 }   # 0 = must be absent
```

`expect.events` grounds the family deterministically on the call log; barge
cases additionally judge an interleaved **delivery-annotated dialogue** (the
cut agent line is rewritten to what the borrower actually heard) instead of
the agent-only transcript — an agent-only transcript cannot show an
interruption. Scenario family: SCEN-BARGE-* + SCEN-COLLIDE-BARGEIN-AUTH-01;
`accidental_disconnect` remains the only transport-only (na) inject.

Two sim-harness parity fixes landed with this work (both places the simulator
had drifted from the engine): `PlanStep.use_empty_utterance` is honored (re-
surface steps invoke with ""), and ALL planned invoke steps execute with
speak-assembly-only stopping at WAITING_CONFIRM (the handler runs steps
concurrently — an injected re-delivery step must run even when an earlier
step lands waiting).
