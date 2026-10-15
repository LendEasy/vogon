# Barge-in Compliance Protection — Design

**Status:** Design accepted; **intro/farewell slice implemented** (2026-07 —
`BARGE_PROTECT_INTRO` / `BARGE_PROTECT_FAREWELL`, see §8). Per-line protection
for mid-call compliance lines (Class A2+/B) remains future work.
**Scope of this doc:** *What we want barge-ins to do* around compliance and
consent speech. Implementation mechanics are catalogued but deferred to later
tasks.

---

## 1. Problem statement

When a caller talks over the agent (barge-in), the agent's audio is cancelled so
the caller isn't talked over. That is correct for ordinary conversation. It is
**wrong** for two kinds of speech:

- **Compliance disclosures** the borrower is legally/▪policy required to *hear*
  (recording disclosure, security refusals, stop-action acks).
- **Consent questions** whose "yes" moves money or sends PII, where the borrower
  must hear the material terms (amount / card / date) *before* the answer counts.

A barge-in must not be allowed to skip over these.

---

## 2. Key finding — barge-in is content-blind

There is **no `uninterruptible` / `protected` / `must_complete` concept anywhere**
in the transport layer (grep across `app/transport/` returns nothing). Barge-in
cancels a recording disclosure or a payment read-back exactly as readily as an
empathy filler.

The only speech-suppression that exists today is uniform and tiny:

| Mechanism | Where | Effect |
|---|---|---|
| Start-grace (100 ms) | `barge_gate.py` `on_frame` (`grace_sec`, `settings.barge_in_grace_ms=100`) | Blocks barge for first 100 ms of each TTS turn |
| Turn-1 intro guard (4 s) | `openai_realtime_transport.py` (`_intro_barge_in_min_secs=4.0`) | Realtime path only; play intro fully before allowing interruption |
| Echo cooldown (0.25 s) | `twilio_transport.py` (`_TTS_ECHO_COOLDOWN_SEC`) | Post-TTS echo suppression, not barge protection |

**So this is not "re-enable guards that got turned off." It is "add the first
content-aware barge protection."** The realtime intro guard is the nearest
existing precedent for "play this fully before allowing interruption."

---

## 3. The core model — two behaviors, not one

"Protect from barge-in" is **two distinct requirements** depending on what is
being spoken:

### 3a. Disclosures — must be *heard*
Play the audio to completion. The caller's concurrent speech is captured as the
next turn — that is fine; they are not answering a question.

### 3b. Consent questions — must be heard *before the answer is valid*
Play to completion **AND** reject any "yes" that began before the question
finished. Audio protection alone is **insufficient** here, because of an existing
early-capture path:

> `payment_authorization` has a **`pre_question` capture path**
> (`payment_authorization.py:308-316`) where a compound *"yes, I authorize"* is
> captured **from the call log before the read-back is even spoken**. Protecting
> the audio does nothing for that path.

So the consent rule is two-part:
1. Don't let barge cut the question.
2. Don't accept a consent whose material terms (amount / card / date) were not
   heard first — including the `pre_question` log-capture path.

This two-part requirement is why the overall change is multipart, not a single
transport flag.

---

## 4. Decisions (Task 1)

Each is a behavioral decision with a recommendation. **Pending user sign-off.**

### Decision 1 — What still breaks through protected speech?
**RESOLVED (intro/farewell slice): safety-only.** The heuristic safety class
(`stop calling`, abuse, wrong-number phrases — `barge_semantics.py`) cuts a
protected playout immediately; nothing else does. Backchannels absorb as usual;
substantive speech is deferred (queued, answered right after the playout), never
cut, never swallowed. No Cerebras verdict call is made while protected.

A line the caller *cannot* interrupt while they are saying "wrong number / stop
calling me / [abuse]" is itself a compliance failure (ignoring a stop/DNC,
disclosing to a wrong party). "Play to completion" is therefore never absolute.

Because STT keeps flowing during protected playout (`forward_frames` still reach
the recognizer — `barge_gate.py:157-159`), safety signals remain **detectable
mid-line** — we are deferring the audio cut, not going deaf.

- **Recommended:** protected speech is interruptible only by the **safety-terminal
  intent class the planner already prioritizes** — DNC, abuse, safeguarding,
  human-request, deceased, legal-hold (the `route=terminating`, top-priority set
  in `collections_demo_v2.json`). A premature "yes", a question, or filler does
  not cut it.
- Alternatives: (a) nothing breaks through (simplest, but traps the caller);
  (b) safety set **plus** explicit "wrong number"/"stop" even if not a formal
  DNC (widest escape hatch).

### Decision 2 — Per-line or per-turn protection?
Granularity today is whole-`send_response` (TTS sends the full string at once —
no sentence splitting; `tts.py:143-144`), and compliance lines are `ensure()`-
**prepended into a merged speak string**. So protection is a fork:

- **Recommended — per-line:** emit each protected line as its own `send_response`
  so the protection bit can't be diluted/dropped in a merge/dedup path, **and**
  the consent question gets its own playout boundary (which the answer-timing rule
  in §3b needs). Requires splitting the merged speak.
- Alternative — per-turn: protect the entire turn containing a protected line.
  Simpler to thread through, but over-protects unrelated appended content (e.g. an
  escalation offer merged onto the same turn at `handler.py:533`).

### Decision 3 — Consent-answer validity (the §3b rule)
- **Recommended — yes:** a consent "yes" is valid only if it arrives after the
  material terms finished playing; close the `pre_question` early-capture path.
  This is the half of the requirement audio-protection alone cannot cover.
- Alternative — audio protection only: still accept a "yes" spoken over the
  question. Simpler, but a borrower can "consent" to a charge whose terms they
  never heard.

---

## 5. The guard list — speech that must not allow barge-in

Ranked by legal exposure. All grounded in the compliance-line catalog
(`collections_lines.yaml`, `renderer.py`, and the agent files below).

### Class A — Disclosures (play to completion)
| # | Line / node | Location | Basis |
|---|---|---|---|
| A1 | Recording + purpose disclosure cluster: opener line, `borrower_confirmed_disclosure`, `recording_disclosure` node | `call_opener.py:166`, `call_opener.py:287` / `preverify_gate.py:111`, `recording_disclosure.py:68` | REQ-CALL-02, legally mandated. `recording_disclosure` node is the cleanest anchor (deterministic, no LLM). |
| A2 | `sensitive_data_refusal` | `account_detail_inquiry.py:187` | Security refusal — truncation strips "can't read out" core |
| A3 | `phone_card_decline` | `payment_link_offer.py:261` | Security refusal — truncation strips "over the phone" core |
| A4 | Stop-action acks: recording-consent revocation; DNC / AI-call revoke | `privacy_check.py:210`; `dnc_handler.py:195` | Stop-contact confirmation must be heard |
| A5 | `hardship_waiver_conditions` | `hardship_explainers.py:236` | "Conditional, not unconditional" qualifier — UDAAP risk if truncated |

### Class B — Consent questions (play to completion + answer-after-question)
| # | Question | Location | What "yes" authorizes |
|---|---|---|---|
| B1 | Payment-authorization read-back | `payment_authorization.py:205` (`_build_auth_question`) | Card charge. **The** money gate — only question with amount + card-last-4 + date in the same sentence as the yes/no. Highest priority. Includes closing the `pre_question` path (`:308-316`). |
| B2 | Hardship self-authorizing asks (bypass payment_authorization) | `hardship_handler.py:1022` (full_cure_authorize), `:508` (over-cure), `:842` (partial-callback), `:935` (split-cure) | Charge captured directly; amount is named in-line |
| B3 | Payment-link send "Shall I send it now?" | `payment_link_offer.py:262` | Texts a payment URL to the borrower's phone (PII send) |

### Explicitly NOT protected
Contact-consent / scheduling / callback tiers (`bad_timing_handler`,
`scheduled_payment`, `promise_to_pay`, `human_request_handler`,
`cancellation_handler`, `legal_hold_handler` follow-ups, dispute detail-collection):
a truncated question there degrades UX but does not produce an uninformed money or
PII action. Leaving them interruptible avoids trapping the caller.

---

## 6. Scope notes

- **Mini-Miranda and FDCPA validation notice are deliberately absent** — this demo
  is first-party servicing (`requirements.txt:75`). Do **not** add them to the
  guard list. REQ-CALL-02's neutral purpose disclosure + recording disclosure is
  the control used instead.
- **Three transports** carry a barge path and all need the guard eventually:
  Twilio (`twilio_transport.py`, AEC + `BargeGate`), widget
  (`voice_transport.py`, STT-interim / `client.barge_in`), and realtime
  (`openai_realtime_transport.py`, OpenAI server VAD). The realtime path's 4 s
  intro guard is a working precedent for the mechanism.

---

## 7. Deferred to implementation tasks (NOT Task 1)

The barge-map surfaced these mechanics; they are real but out of scope for "what
do we want barge-ins to do":

- **Best insertion point:** gate the cancel *action* at the Twilio choke point
  (`twilio_transport.py:434-455`) where a confirmed barge becomes an actual
  cancel — wrap **both** the in-flight (`_tts_cancel.set()`) and drain
  (`_send_clear()`) branches.
- **Flag lifetime:** a transport-instance attribute living for the **playout
  length**, NOT scoped to `send_response`'s `finally` — the drain cancel fires
  *after* `send_response` returns while Twilio still plays the buffered tail. Set
  at the top of `send_response`; clear on the mark-echo playout confirmation
  (`twilio_transport.py:507`).
- **Thread-through:** `SubagentResult.speak` is a plain `str`; a `protected` bit
  must be added at every hop (`SubagentResult` → `_TurnResult` →
  `send_response(...)` on `BaseTransport` and all three transports), OR the
  protected line is emitted as its own `send_response` (see Decision 2, per-line).
- **Merge/dedup:** multi-agent turns merge several `SubagentResult.speak` strings;
  a single bool on the merged string must be OR-ed across contributors, or the
  compliance line emitted separately so the bit isn't lost.
- **Gate FSM wedge:** if the cancel is suppressed but the `BargeGate` still reaches
  CONFIRMED it latches until `reset()`; suppress before the state transition, not
  only the action, so the next turn's detection isn't wedged.
- **Capture-vs-suppress:** even when protected, `forward_frames` still deliver the
  caller's utterance to STT as the next turn. Decide (per §4 Decision 3) whether a
  premature consent answer is swallowed/invalidated or just kept.

---

## 8. Multipart task outline

1. **Task 1 (this doc):** decide barge-in behavior — the two-behavior model, the
   break-through set, granularity, consent-answer validity. **← decisions pending.**
2. **Task 2:** transport-layer protection mechanism (Twilio first): the
   uninterruptible flag + cancel-action gate, per the insertion point in §7.
3. **Task 3:** thread the `protected` signal / per-line emission from the compliance
   renderer + consent agents down to the transport (Class A + B lines).
4. **Task 4:** consent-answer validity — invalidate a "yes" whose material terms
   weren't heard, including the `pre_question` log-capture path (Class B).
5. **Task 5:** parity across widget + realtime transports.
6. **Task 6:** safety break-through — let the terminating intent set cut protected
   speech via the still-flowing STT capture.

---

### Source investigations
This design is grounded in three read-only codebase investigations (barge-in
mechanism map, compliance-line catalog, affirmative-consent question enumeration).
Key files: `app/transport/{twilio_transport,voice_transport,openai_realtime_transport,barge_gate,aec}.py`,
`app/agents/collections/{collections_lines.yaml,renderer.py,recording_disclosure.py,call_opener.py,preverify_gate.py,payment_authorization.py,payment_link_offer.py,hardship_handler.py,hardship_explainers.py,account_detail_inquiry.py,privacy_check.py,dnc_handler.py}`,
`app/config.py`, `evals/requirements.txt`.

## 9. Status update (task #45, 2026-07) — protection is now regression-tested

The Class A/B protection this design specified is implemented (delivery gate,
`docs/speech-delivery-completion.md`) and — as of task #45 — regression-tested
end-to-end WITHOUT audio on the event surface:

- `tests/simulate_barge_delivery.py` — Class A re-delivery (barged recording
  disclosure re-spoken verbatim and committed), Class B consent (§4f: a bare
  "yes" to a half-heard read-back is rejected, the ask re-delivered, consent
  only captured after), and the §5.1 zombie guard (ordinary content never
  re-arms).
- Eval scenarios `SCEN-BARGE-DISCLOSURE-REDELIVER-01`,
  `SCEN-BARGE-CONSENT-READBACK-01`, `SCEN-BARGE-ORDINARY-NO-REARM-01`,
  `SCEN-BARGE-ABSORBED-01` (+ the un-na'd `SCEN-BARGE-IN-01` and
  `SCEN-COLLIDE-BARGEIN-AUTH-01`), grounded on `expect.events` call-log
  assertions with a delivery-annotated judge dialogue.

The audio layer below the gate (RMS/VAD discrimination) remains covered by the
Phase-0 harness and the planned Layer-2 recorded fixtures (task #41).

## 10. Intro/farewell protected playouts — IMPLEMENTED (2026-07)

The first slice of transport-level play-to-completion protection (§8 Tasks 2+3
for the A1 intro line and call-ending lines):

**Config** (`app/config.py`, both default **true**):
- `BARGE_PROTECT_INTRO` — the call's first utterance (opening ×2 sites,
  greeting, recovery re-opening in `handler.py`).
- `BARGE_PROTECT_FAREWELL` — call-ending utterances: silence-timeout farewell,
  low-confidence farewell, and any main-loop deliver with `finalize_reason` set
  (farewell, DNC ack, no-resolution close).

**Mechanism** (`twilio_transport.py`): `send_response(..., protected=True)` sets
`_protected_playout` for that playout. A confirmed barge candidate then runs
`_run_protected_window()` instead of the yield window:

| Caller speech during protected playout | Action |
|---|---|
| safety phrase (heuristic class) | cut immediately (Decision 1: safety-only) |
| backchannel / no-text energy | absorb (swallowed, playout continues) |
| substantive / ambiguous | **defer** — no cut, no swallow; the final queues and is answered right after the playout; `barge_deferred_protected` logged |

No Cerebras verdict call is made while protected (the outcome can't change the
cut decision). Guards exist at all three decision points (`_run_protected_window`,
`_act_on_barge_verdict`, `_judge_playback_final`) so a late verdict can never cut
a protected playout. Audio-log events: `protected_playout` (start),
`barge_deferred_protected` (each deferral); barge-candidate events carry
`protected=true`.

Widget/text transports accept and ignore the flag (widget barge is client-side).
Tests: `tests/test_protected_playout.py` (13 cases: defaults, signatures, all
three decision points × safety/backchannel/substantive/echo, unprotected
unchanged). Manual test A10 covers the live-call behavior.

Still future: per-line protection for **mid-call** Class A2+ disclosures and
Class B consent read-backs (requires splitting merged speaks — Decision 2's
per-line fork); realtime-transport parity.
