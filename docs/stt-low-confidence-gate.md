# STT low-confidence gate — design & tuning

**Detection surface:** `app/pipeline/stt_confidence_gate.py` — pure, no I/O, no
LLM, deterministic wording. Runs on each **final** STT utterance *before* the
planner or any agent is invoked.
**Decision surface:** the same pure module resolves a **policy declared by the
domain** (`unintelligible_policy` in the workflow JSON). The handler executes
the I/O the module prescribes.
**Status:** implemented; default mode **`shadow`** (assess + log only, behaviour
unchanged). Flip to `enforce` after calibrating on real `LOW_CONFIDENCE_GATED`
logs with `proto/stt/analyze_gate.py`.
**Not reachable by the collections eval suite** — the gate sits below the
text-level `RealtimeCallSim`, which starts at the planner and never crosses the
transport gate. Coverage is the pure-module unit tests plus the handler-harness
end-to-end tests (see [Testability](#testability)).

## Purpose

Don't classify or act on a garbled transcript. When Deepgram returns a final
whose *content* words are mostly low-confidence (or whose overall confidence is
below the floor), the safe move is not to guess — it is to **re-ask the caller
to repeat**, and, if the line stays unintelligible, **end the call gracefully**
rather than transact on noise.

Two real production failures this prevents (both pinned as R1 "substance
garbled" regression cases in `tests/test_stt_confidence_gate.py`):

1. **A garbled amount/settlement statement** — e.g. a final like *"Go to go dot
   stop. I keep this fifty dollars only. Are you okay with that?"* where the
   content words around the figure are mis-heard. Acting on it risks reading a
   settlement commitment or a payment amount out of transcription noise.
2. **A garbled payment-channel statement** — e.g. *"…Why should I call you back
   in? Call you send me, like,"* where the mis-heard verbs/nouns could be
   misrouted into a payment-method decision the caller never made.

In both, the correct behaviour is a re-ask, not a classification.

## Who decides what — the gateway seam

Phase 0 (`docs/voice-gateway-phase0.md` §2) fixes the direction of knowledge:
events carry **physical facts** up (gateway → backend), speak commands carry
**meaning** down. Transcript quality is a physical fact. What to do about it is
meaning. That single split decides every placement question here.

| | Voice Gateway | Turn Service |
|---|---|---|
| **Does** | tags per-word confidence, populates `UtteranceFinal.confidence` + `.low_conf_words`, plays whatever `SpeakCommand` comes back | assesses the transcript, resolves the policy, renders the wording, owns the counter, writes the call log |
| **Knows** | `stt_word_conf_threshold` only | every decision knob, the policy artifact, node outputs |
| **Never knows** | that a gate exists, the policy, party type, any node output, any wording | — |

This is enforced, not merely intended: the phase-0 import-boundary test forbids
anything under `vogon/gateway/` importing `app.agents`, `app.framework`, or
`app.turns`. A policy that reads `party_type` out of node outputs therefore
*cannot* live gateway-side.

**No Turn API surface is needed for any of this.** `UtteranceFinal` already
ships `confidence` and `low_conf_words` (`gateway/contract.py:127-128`), and
`ProcessEventsResponse` already expresses every action the policy can produce —
`speak`, `action="end_call"`, `end_reason` (`contract.py:268-271`).

## The decision rule (perception)

`assess_transcript(text, low_conf_words, overall_conf, cfg) -> GateDecision`
tokenizes the transcript, splits tokens into **substantive** (content-bearing)
vs. non-substantive (fillers, pronouns, determiners, prepositions, auxiliaries —
the `NON_SUBSTANTIVE` set), and applies:

- **Precondition:** the gate can fire **only if there is at least one
  low-confidence word**. An empty `low_conf_words` never gates, even at a very
  low overall confidence.
- **R1 — `substance_garbled`:** the number of low-confidence **content** words
  is `>= stt_gate_min_low_substantive` **and** their fraction of all content
  words is `>= stt_gate_substantive_low_frac`. (A mis-heard filler is harmless;
  a mis-heard amount or verb is not — only content words count.)
- **R2 — `overall_floor`:** overall utterance confidence
  `<= stt_gate_overall_conf_floor` **and** the utterance is at least 3 words
  (short blips are never gated).

`gate = bool(low_conf_words) and (R1 or R2)`.

### Critical passthrough (overrides the gate)

Compliance/exit signals must never be swallowed by a re-ask. The rule evaluates
the **clearly-heard (high-confidence) words only**; if those alone contain a
do-not-call, farewell, human-request, or abuse phrase, `assess_transcript` forces
`gate = False` and sets `critical_passthrough = True`, so the real handler sees
the turn. If the compliance phrase itself is the low-confidence part (not clearly
heard), it does **not** pass through — the gate applies normally.

## The policy (resolution)

Perception answers *"is this transcript trustworthy?"* — a question about signal
quality, with no domain content. **What to do about an untrustworthy turn is a
domain decision**, and it is declared in the workflow JSON as
`unintelligible_policy`, a sibling of `error_policy`.

```json
"unintelligible_policy": {
  "farewell_after_consecutive": 2,
  "rules": [
    { "id": "defer_until_party_known",
      "match": { "node_output": "call_opener.party_type",
                 "in": [null, "voicemail", "no_answer", "wrong_party"] },
      "action": "stand_down",
      "reason": "party_unresolved_or_machine" },

    { "id": "protect_consent_ask",
      "match": { "gate_fact": "consent_ask_live", "equals": true },
      "action": "reask",
      "speak": ["Sorry, I didn't catch that clearly, and I don't want to record the wrong answer. Could you repeat it for me?"] },

    { "id": "default",
      "action": "reask",
      "speak": ["I'm sorry, I wasn't able to understand your response. Could you please repeat that for me?",
                "Apologies, I still didn't quite catch that. Would you mind saying it once more?",
                "I'm having a little trouble hearing you clearly. Could you please repeat that one more time?"],
      "on_exhausted": { "action": "end_call",
                        "speak": "I'm sorry, but I'm still not able to hear you clearly... Please call us back at {servicing_phone}...",
                        "end_reason": "low_confidence_no_resolution",
                        "node_outputs": { "<node>.<key>": <scalar> } } }
  ]
}
```

**What the close RECORDS** (`node_outputs`, on `on_exhausted` or on a rule whose
own action is `end_call`). A gated turn runs no agent, so a close reached
through this gate used to leave *nothing* behind: a hardship call that went
garbled ended politely with no safety-net record, and an authorization
abandoned mid-read-back recorded no "never confirmed". The wording of a close
was already the domain's to declare; the facts it leaves on the account are the
same kind of statement.

Keys are dotted `"<node>.<key>"`; values are JSON scalars. The platform writes
them verbatim into `node_outputs` on the ending turn and never reads them — it
cannot, since every name belongs to the domain. A malformed declaration raises
at policy-build time rather than being dropped, because a record that silently
vanishes leaves a call that *sounded* correct and settled nothing. Nothing is
recorded on a re-ask, on a clear turn, or in shadow mode: the record belongs to
the close that declared it.

**Rules are declaration-ordered, first match wins**, like workflow edges. A rule
with no `match` always matches and must be last.

**Matchers.** The platform understands exactly two sources and two comparators,
and treats every value as an opaque string:

| Form | Reads |
|---|---|
| `{"node_output": "<node>.<key>", "in": [...]}` | `node_outputs[node][key]`; a `null` in the list matches missing/None |
| `{"gate_fact": "<name>", "equals": <value>}` | a platform-derived fact (below) |

`in` and `equals` are interchangeable across both sources.

**Gate facts** are the platform's own vocabulary — legal in engine code because
they describe shapes, not domain concepts: `consent_ask_live` (a consent-class
ask is awaiting an answer, derived exactly as the silence probe derives it),
`rule`, `consecutive`, `low_substantive_count`, `overall_conf`.

**Actions:**

| Action | Effect |
|---|---|
| `stand_down` | Do not short-circuit. Log the assessment and fall through to the normal pipeline. Used where a better-informed surface downstream should see the turn. |
| `reask` | NO-OP turn: speak a clarification from `speak` (rotating by attempt), run no agents, apply no mutations. |
| `end_call` | Speak the wording and finalize with `end_reason`. |

**The voicemail case, and why the policy has to exist.** `assess_transcript`
cannot distinguish a carrier voicemail announcement ("call has been forwarded to
voicemail…") from human garble — both are low-confidence content words, and no
threshold separates them. The surface that *can* tell is `call_opener`, whose
party classifier has a `voicemail` bucket. So the first rule defers to it. That
rule names `call_opener` and `party_type` — domain nouns platform code may not
reference, "not even in a string comparison" — which is precisely why the rule
lives in the domain artifact and not in the handler.

**Default when no policy is declared.** The platform falls back to a single
unconditional re-ask rule with neutral built-in wording and `end_call` at the
threshold — the driver-speech contract (platform owns the slot and a neutral
default; the domain registry may override). A new domain works with zero
artifacts.

## The wording

Rules carry their wording literally, in the `speak` field — the same mechanism
`error_policy.transient_speak` already uses to supply driver speech from the
workflow JSON. `speak` is a rotation: attempt N speaks `speak[N % len]`.

The wording deliberately does **not** come from the `*_lines.yaml` registry.
`renderer.recite` lives in `app/agents/collections/`, so a platform handler
calling it would repeat the `framework/delivery_gate.py` boundary violation
CLAUDE.md names as not-to-be-repeated, and a platform-side line loader is the
renderer migration — out of scope here. The workflow JSON is itself a domain
artifact, so wording declared there is already on the correct side of the
boundary.

The only interpolation is `{servicing_phone}`, filled by the handler from
account config (a tenant value, not a domain concept). Because these lines
bypass the registry's `must_say`/`must_not_say` guard, keep them free of account
content by review — they are static declared strings, like `transient_speak`.

When a workflow declares no policy, the platform speaks its own neutral built-in
wording (`CLARIFICATION_REASKS` / `FAREWELL_LINE`).

## Modes & the counter

`stt_gate_mode` selects behaviour:

| Mode | Behaviour |
|---|---|
| `off` | Gate inert. Always `pass`; counter never touched; no event. |
| `shadow` *(default)* | **Never short-circuits.** A gated turn emits its event for calibration, then falls through to the normal pipeline. |
| `enforce` | Policy resolution drives the turn. |

**The counter is uniform within `enforce`.** `consecutive` means *"consecutive
turns we could not understand"* — a fact about line quality, not about which
policy branch ran. It increments on every gated turn **including `stand_down`**,
and resets on any turn that is understood (or passes through as critical). No
branch special-cases it.

`shadow` deliberately never touches the counter, so it stays behaviour-identical
to `off` and its logged `consecutive` is always the pre-flip value. The ladder
counterfactual for a shadow sample comes from the analyzer's per-call replay
(`proto/stt/analyze_gate.py`), not from the logged field.

**Every gated turn is its own turn.** The counter bump that `_process_utterance`
performs on a normal turn also happens on a short-circuited one, after the
cycle-anchor restore (which resets `turn_counter`). Gate events are stamped with
the turn of the utterance they assessed — not `runtime.turn`, which is one behind
at that point.

## Call-log events — the audit record

A gated turn short-circuits before `_process_utterance`, so the normal
`UTTERANCE` / `SPEAK_EMITTED` pair is never written. **The
`LOW_CONFIDENCE_GATED` event is therefore the complete record of that turn** and
carries both halves:

| Field | Meaning |
|---|---|
| `action` | `shadow` · `stand_down` · `reask` · `farewell_end` · `reset` |
| `text` | what the caller said (the garbled transcript, verbatim) |
| `speak` | **what the caller heard in response** (re-ask or farewell wording) |
| `low_conf_words`, `overall_conf`, `rule` | the assessment inputs and outcome |
| `consecutive`, `mode`, `reason` | ladder position, mode, matched rule's reason |

**Why not a normal `UTTERANCE` event.** `_history_block` — replicated across ~20
classifiers — renders every `utterance` event as `Borrower: …`, and
`_last_caller_utterance` recovers the newest one for chain-invoked agents. A
garbled turn appended there would enter the next turn's classifier history as
though the caller had said it. The same reasoning is why the canned re-ask is not
logged as `SPEAK_EMITTED`: it would become the most recent agent line in the next
`log_slice`. Both halves live in the gate event instead, which no history builder
reads.

(A `pass` — clear turn with a zero counter — produces no event.)

## Config reference

The first key is an **upstream tagger** consumed in `vogon/gateway/pipeline/stt.py`
to *produce* `low_conf_words` — the only knob that belongs to the gateway,
because it describes the signal rather than the response. The rest are decision
knobs and live with the reasoning half.

| Key (env var uppercased) | Default | Side | Meaning |
|---|---|---|---|
| `stt_word_conf_threshold` | `0.75` | gateway | Per-word confidence below which a word is tagged "low confidence". |
| `stt_gate_mode` | `"shadow"` | turn service | `off` · `shadow` · `enforce`. |
| `stt_gate_min_low_substantive` | `2` | turn service | R1: minimum low-confidence **content** words. |
| `stt_gate_substantive_low_frac` | `0.25` | turn service | R1: minimum fraction of content words low-confidence. |
| `stt_gate_overall_conf_floor` | `0.60` | turn service | R2 backstop. |
| `stt_gate_farewell_after_consecutive` | `2` | turn service | Fallback threshold when the workflow declares no `farewell_after_consecutive`. |

## Rollout

1. **Ship in `shadow` (the default).** Safe to run in production immediately.
   *Status: live — `STT_GATE_MODE=shadow` in `/etc/codelexin/data-plane.env`.*
2. **Calibrate on real logs** with `proto/stt/analyze_gate.py`: per-event review
   with low-confidence words marked inline, knob distributions, a candidate
   sweep replaying the real `assess_transcript`, and FP/TP scoring from an
   operator labels CSV (`call_id,turn,clean|garbled`).
   *First sample (2026-07-28): 549 calls / 2024 finals / 3 gated = 0.1%.*
3. **Flip to `enforce`** (`STT_GATE_MODE=enforce`) once a multi-day labeled
   sample shows zero clean-turn gates and zero farewell-on-clean in the ladder
   replay. Instant rollback: `shadow` or `off`.

Calibration limits the analyzer prints in every report: only turns that gated
under the as-run knobs carry confidence data, so loosening candidates are lower
bounds; and per-word scores are not logged, so `stt_word_conf_threshold` can only
be judged from the marked words, not swept.

## Testability

- **Pure module** (`tests/test_stt_confidence_gate.py`): `assess_transcript` —
  R1, R2, the empty-`low_conf_words` precondition, the min-3-words R2 qualifier,
  critical passthrough (including "garbled DNC does not pass through"), and the
  two production-failure regression cases. Policy resolution — matcher forms,
  declaration order, line rotation, exhaustion, the platform default, and the
  `off`/`shadow`/`enforce` ladder including reset.
- **Handler harness** (`tests/test_handler_harness.py`): real `handle_call`,
  real transport, faked externals only. Covers the enforce path end to end —
  each gated turn is its own turn, the gate event records both what was said and
  what was heard, the planner never runs, and no history-visible utterance event
  is produced.

The pure module is fully deterministic (no LLM, no sampling), so its tests are
the authoritative correctness proof for the logic; the handler wiring is a thin
execution layer over the resolver, pinned by the harness tests.
