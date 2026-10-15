# Voicemail detection in the voice gateway — design sketch (2026-09-23)

Status: SKETCH, nothing built. Phone (Twilio) path only.

## 1. Terms

- **Recording** — a voicemail greeting, answering machine or carrier
  announcement heard as the far end's first speech.
- **Line verdict** — a physical fact about who answered the line:
  `human`, `machine` or `unknown`, with a probability, a source and a time.
  Twilio's AMD reports the same fact (`AnsweredBy`). The gateway owns it.
- **Detector** — the gateway component that produces the line verdict from
  the post-AEC transcript. First instrument: the Jev recording question.
  Later instruments in the same slot: Twilio AMD's callback, a beep (tone)
  detector on the inbound audio.
- **Opening** — the agent's first line, played as a protected, absorb-all
  playout. Unchanged by this design.
- **Voicemail message** — the domain's `v3_voicemail_message` line
  (identifies us, gives a callback number, discloses no debt).

## 2. Why the gateway

The evidence from the duplex harness (JEV_CLASSIFICATION_DESIGN.md §33): a
greeting that starts at answer is one STT final, absorbed by the opening's
policy; the turn service never sees it and the call ends by the silence
ladder at 53 s. The gateway sees the interims from the first seconds, knows
when its own audio started, holds the inbound audio, and is where AMD would
report. The barge policy stays as it is.

## 3. The pipeline

```
Twilio media frame (mulaw 8 kHz, 20 ms)
  │
  ├─ while our audio is active ──► AEC.process ──► clean frame     (existing)
  └─ otherwise ───────────────────────────────────► raw frame       (existing)
  │
  ▼
Flux STT (open feed, keyterm = name_hint)                           (existing)
  │  interims / eager-end / final
  ▼
FloorController.transcript(text, final)                             (existing)
  │  episodes: onset, revision, echo / possible_echo, decision
  ▼
LineDetector.observe(episode, our_playout)                          NEW
  │  skips: episodes marked echo/possible_echo; text under MIN_WORDS;
  │         any episode after a verdict has been issued
  │  state → Jev (async, one in flight, changed text replaces pending —
  │          the floor controller's own discipline):
  │            account_holder  = CallConfig.name_hint or "the account holder"
  │            our_opening     = transport._last_speak_text (so our own
  │                              leaked words are discounted, not read as
  │                              the far end's)
  │            far_end_onset   = "before our first audio" | "during our audio"
  │            first_turn      = episode.text (post-AEC transcript so far)
  │  answer → LineVerdict(answered_by, p, source="jev", at, transcript)
  ▼
Verdict policy (code, gateway)
  │  machine  p ≥ 0.60 ─► (1) protected window: `machine` CUTS the opening,
  │                            exactly as `safety` does; the episode is
  │                            marked `line_verdict` so its final is NOT
  │                            absorbed
  │                        (2) enqueue, in this order:
  │                              LineVerdict fact
  │                              UtteranceFinal(text=transcript,
  │                                annotations.answered_by={...})
  │  human    p ≤ 0.30 ─► LineVerdict fact only; the opening's absorb
  │                        policy runs unchanged
  │  between / no answer ► keep listening; the next revision re-asks;
  │                        after the opening ends with no verdict, the
  │                        detector retires (the gate's own party field
  │                        still exists for later turns)
  ▼
Turn service  process_events([LineVerdict, UtteranceFinal])
  │  LineVerdict → ledger LINE_VERDICT (p, source, latency, transcript)
  │  UtteranceFinal → the gate, party stage: annotation answered_by=machine
  │                   is honoured BEFORE the classifiers → _terminal
  │                   (voicemail) → v3_voicemail_message,
  │                   gate_outcome=voicemail → edge to end, VOICEMAIL_LEFT
  ▼
ProcessEventsResponse
     speak = [SpeakCommand(v3_voicemail_message, protected=True,
                           absorb_barge=True, play_after="line_quiet")]
     action = end_call, end_reason = VOICEMAIL_LEFT
  ▼
Gateway playout
     holds the speak until the line is quiet (§6), plays it protected and
     absorb-all (the recording's tail cannot cut it), then end_call.
```

Everything above the "Turn service" line runs in the gateway process. The
detector reads only post-AEC transcripts (it sits on the same callbacks the
floor controller uses, after AEC, never on raw frames).

## 4. The detector

Module: `voice-gateway/vogon/gateway/transport/line_detector.py`. Domain-free:
it knows "a line", "a recording", "the person we are calling for" (the
CallConfig name hint), and the opening's text. It never names an agent, a
line id or a workflow field.

Instrument interface, so AMD and a tone detector fit the same slot:

    observe(episode, playout_state) -> None        # feed
    verdict: LineVerdict | None                     # latest
    retire()                                        # after the opening

Jev question: the recording question from `collections_v3/jev_gate.py`,
moved to the platform side with the same wording (it is already
domain-free) — the gate's seat then reads the annotation instead of asking
again. One question per changed episode text, `decide_async`, timeout the
existing `jev_timeout_ms`, never awaited inside the frame loop.

Gates before asking, all measured knobs:

| knob | default | why |
|---|---|---|
| `LINE_DETECTOR` | `off` \| `jev` | the switch; off = today's behaviour |
| `LINE_MIN_WORDS` | 4 | "Hello?" and "Hi." never reach Jev |
| `LINE_MACHINE_P` | 0.60 | the seat's upper band (§33.2) |
| `LINE_HUMAN_P` | 0.30 | the seat's lower band |
| `LINE_ASK_WINDOW_S` | until the opening's playout ends + 2 s | after that the gate owns the question |

Echo. Two defences, both needed: the floor controller's echo marking
(`is_echo_of` against our playout text) excludes an episode outright, and the
state carries `our_opening` so a partially leaked line ("-- on a recorded
line --" inside the far end's text, measured in every harness run at echo
0.3) is read as ours. The real line's residual echo is unmeasured; the
harness's `--echo` knob is the instrument.

## 5. The Turn Service API

One new event, one new annotation, one new speak field. All platform
shapes; the nouns stay in the domain.

**Event** (a fact, spooled like `DeliveryOutcome`, facts-first ordering):

    LineVerdict
      kind        = "line_verdict"
      answered_by : "human" | "machine" | "unknown"
      probability : float
      source      : str            # "jev" | "amd" | "tone"
      at          : float          # gateway monotonic
      transcript  : str            # what the verdict was formed on
      latency_ms  : float

The turn service records it (`EventType.LINE_VERDICT`) and does nothing
else with it. It is the audit row and the thing the harness measures.

**Annotation** on the `UtteranceFinal` that carries the recording's
transcript (`turn_annotations`, full-duplex-turn-model.md §D —
metadata beside the text, never in it):

    annotations.answered_by = {"verdict": "machine", "p": 0.91, "source": "jev"}

The gate reads it in `_handle` at the party stage, ahead of `_perceive`:
`machine` → the voicemail terminal, no classifier call, no Jev call.
Absent → today's path (LLM party field, plus the seat when selected).
The precedence is therefore: gateway verdict > gate seat > LLM party field,
and only one of them decides a given turn.

**Speak field** so the domain can say WHEN, not just what:

    SpeakCommand.play_after : "now" | "line_quiet"      (default "now")

Set from a new `SubagentResult.deliver_after` the gate puts on its
voicemail result. Platform shape ("play this once the line is quiet"),
domain choice (the voicemail message wants it; nothing else does today).

Nothing changes in `StartCallRequest`, `CallConfig` (the name hint is
already there) or the end_call path (`VOICEMAIL_LEFT` already exists).

## 6. Message timing — decided for this phase

The message must not start over the recording. Without a beep detector the
gateway has one reliable signal: Flux's end-of-turn for the recording's
episode, i.e. the far end went quiet. Rule for `play_after="line_quiet"`:

1. wait for the detector's episode to reach `final` (EOT), then a further
   `LINE_QUIET_MS` = 700 ms with no interim (a greeting's pause before the
   tone is shorter than a live person's pause between sentences, so this is
   a measured knob, not a constant);
2. if a tone detector exists, its beep event releases the hold immediately;
3. cap: `LINE_QUIET_CAP_S` = 8 s after the verdict — play anyway (a
   carrier announcement with menu options never goes quiet).

Then play protected and absorb-all, then `end_call`. Later, AMD's
`DetectMessageEnd` or the tone detector fills step 2; nothing else moves.

## 7. What is deliberately unchanged, and the fallback

- The opening's absorb-all policy. A human's "Hello?" over the greeting is
  absorbed as today (under `LINE_MIN_WORDS` anyway).
- `LINE_DETECTOR=off`, a Jev outage, or a verdict that never crosses 0.60
  all fall back to today's behaviour: the greeting is absorbed and the call
  ends by the silence ladder (53 s measured). This is the known cost of
  keeping the barge policy; the silence ladder's `warn` at 8 s could be
  shortened for calls with no line verdict, but that is a separate knob.
- The widget path: untouched.

## 8. What gets measured (the harness, `data-plane/tests/gateway_e2e/voicemail_duplex_call.py`)

Per run, from `summary.json` and the LINE_VERDICT row:

| measure | what it answers |
|---|---|
| verdict time from stream start | how early the detector decides |
| cut time (opening truncated at) | dead-air saved vs talking to a recording |
| message start − recording final | the timing rule's gap |
| verdict on the 5 greetings × echo 0.1 / 0.3 | recall behind AEC with leak |
| verdict on the live control and "Hello? who is this?" over the opening | false cuts |
| Jev calls per call, cost, p50 latency | the upstream dependency's price |

Acceptance for turning `LINE_DETECTOR=jev` on: 5/5 greetings end as
`VOICEMAIL_LEFT` with the message starting after the recording's final; 0
cuts on the live controls; no change to any non-voicemail harness run.

---

# Part 2 — the Turn Service API (2026-09-23, after the constraints were set)

Constraints decided: wait for the recording to end (700 ms of quiet after
its final), cap the wait at 8 s, play the message protected and absorb-all;
detection runs on the FIRST far-end utterance only and sets a flag so every
later turn bypasses it; the 53 s fallback is not acceptable; the message
text comes from the Turn Service, defined in the workflow JSON. This part
supersedes §5–§7 where they differ.

## 9. The sequence

```
 gateway                                     turn service
 ───────                                     ────────────
 start_call ─────────────────────────────────►  reads the gate node's config
                                                (workflow JSON, §11)
 ◄──── StartCallResponse
        call_config.line_detection = {enabled, min_words, machine_p,
                                      human_p, ask_window_s, quiet_ms,
                                      cap_s}            (numbers only)
        speak = [opening, protected, absorb_barge]

 opening plays; far end's first episode transcribed post-AEC
 LineDetector asks Jev on each revision (async)

 verdict MACHINE (p ≥ machine_p):
   cut the opening; mark the episode "line_verdict" (never absorbed);
   flag line_checked = true on the transport (no further detection)
 process_events ─────────────────────────────►  facts first, trigger last:
   events = [ LineVerdict{machine, p, "jev", at, transcript, latency_ms},
              UtteranceFinal{text = transcript, confidence, low_conf_words,
                             onset_at, at} ]
                                                ledger LINE_VERDICT
                                                turn_annotations.answered_by
                                                  = {"verdict": "machine",
                                                     "p": p, "source": "jev"}
                                                gate, party stage: honours
                                                  the annotation, recites the
                                                  node's on_machine line,
                                                  gate_outcome = voicemail,
                                                  deliver_after = line_quiet
                                                edge → end, VOICEMAIL_LEFT
 ◄──── ProcessEventsResponse
        speak = [SpeakCommand{text, protected = true, absorb_barge = true,
                              play_after = "line_quiet"}]
        action = end_call, end_reason = VOICEMAIL_LEFT
 hold the speak: recording episode FINAL, then quiet_ms with no interim,
   or cap_s after the verdict — whichever first; a tone event (later)
   releases immediately
 play (protected, absorb-all)
 end_call ───────────────────────────────────►  events = [DeliveryOutcome]
                                                reason = VOICEMAIL_LEFT
```

The request goes out at VERDICT time, not at quiet time: the message text
is in the gateway's hand seconds before the recording ends, and the gate's
handling of an annotated turn makes no LLM call, so the round trip is the
HTTP cost plus a few milliseconds.

## 10. The shapes

New in `vogon_contract/types.py` (platform, domain-free):

    @dataclass(frozen=True)
    class LineDetection:                 # rides CallConfig; numbers only
        enabled: bool = False
        min_words: int = 4
        machine_p: float = 0.60
        human_p: float = 0.30
        ask_window_s: float = 2.0        # after the opening's playout ends
        quiet_ms: int = 700
        cap_s: float = 8.0

    CallConfig.line_detection: Optional[LineDetection] = None

    @dataclass(frozen=True)
    class LineVerdict:                   # a FACT event, spooled like DeliveryOutcome
        kind = "line_verdict"
        answered_by: Literal["human", "machine", "unknown"]
        probability: float
        source: str                      # "jev" | "amd" | "tone" | "none"
        at: float
        transcript: str
        latency_ms: float = 0.0

    SpeakCommand.play_after: Literal["now", "line_quiet"] = "now"

Turn side:

    turn_annotations["answered_by"] = {"verdict", "p", "source"}
        built by process_events from the LineVerdict fact in the SAME
        request (facts are processed before the trigger, so it is known)
    SubagentResult.deliver_after: Literal["now", "line_quiet"] = "now"
        the executor copies it onto the SpeakCommand
    EventType.LINE_VERDICT               # the ledger row
    ctx.line_checked: bool               # session state (checkpointed):
        set on the first LineVerdict of any kind; when true, the annotation
        is never built again and the gate never runs its own seat

Nothing changes in `UtteranceFinal`, `StartCallRequest` or `EndCallRequest`.

## 11. The workflow JSON — where the message and the knobs live

On the gate node (`v3_gate`), read by the gate (domain code) and, for the
numbers, copied into `CallConfig.line_detection` by `begin_call`:

    "config": {
      "line_detection": {
        "enabled": true,
        "min_words": 4, "machine_p": 0.60, "human_p": 0.30,
        "ask_window_s": 2.0, "quiet_ms": 700, "cap_s": 8.0
      },
      "on_machine": {
        "line": "v3_voicemail_message",        # registry id; guards apply
        "deliver_after": "line_quiet",
        "end_reason": "VOICEMAIL_LEFT"
      },
      "unanswered_opening": {                  # §12
        "probe_after_s": 6, "exit_after_probe_s": 8,
        "end_reason": "NO_ANSWER"
      }
    }

The wording stays in the line registry (`v3_lines.yaml`,
`v3_voicemail_message`: must say the callback number and the lender, must
not say "past due", "debt", "$"...), so the rendered text carries the same
guards as every other spoken line. The JSON names WHICH line and WHEN; the
registry says WHAT. A workflow with no `line_detection` block gets
`enabled=false` and today's behaviour exactly.

## 12. The fallback, replacing the 53 s (decided 2026-09-23: the absorb door is untouched)

Two cases, and the opening's absorb-all policy is never touched:

1. **Verdict human.** The episode is treated exactly as today (absorbed
   under the opening, or queued if it ended after it). `line_checked` is
   set; no further detection.
2. **Everything else** — a verdict in the band, Jev unavailable, or nothing
   substantive heard (silence, or "Hello?" under `min_words`). The
   detector still sends its LineVerdict (`unknown`, source `jev` or `none`)
   as a fact so the ledger says detection ran and what it saw; the first
   episode is absorbed exactly as today; and the `unanswered_opening`
   ladder replaces the default one while the gate is still at the party
   stage: probe once at 6 s of quiet, exit 8 s after the probe, end reason
   `NO_ANSWER` (an existing gate outcome and edge). From stream start:
   opening ends ~6.6 s, probe ~12.6 s, exit ~24 s, versus 53 s today. The
   turn service selects the ladder in the `StartCallResponse` (the gate is
   at the party stage by definition then) and switches to the default
   ladder in the first `ProcessEventsResponse` that leaves the party stage
   — the CallConfig's `silence_thresholds` already ride per call, so this
   is a second threshold list, not a new mechanism.

A recording the detector could not call is therefore a 24 s `NO_ANSWER`
call, not a 9 s voicemail: that is the price of leaving the absorb door
alone, taken deliberately. The band misses (§33.1: business answers,
"not around the office", cue-less recordings) are what the harness's
LINE_VERDICT rows will count.

## 13. First utterance only — the flag, both sides

- Gateway: `transport.line_checked` is set the moment the detector issues
  any LineVerdict, or when `ask_window_s` expires after the opening with no
  verdict (then a LineVerdict `unknown` / `none` is still sent as a fact,
  so the ledger says detection ran and found nothing). After that the
  detector is closed for the call: no Jev calls, no episode marking.
- Turn service: `ctx.line_checked` is set on the first LineVerdict fact and
  checkpointed. With it set, process_events never builds `answered_by`,
  and the gate's `_perceive` skips its seat (`jev_gate`) — the recording
  question is asked at most once per call, by whichever side asked first.

## 14. Open for later, not this phase

- The tone detector or AMD `DetectMessageEnd` releasing the hold early.
- Whether `line_detection` should be a tenant-profile default as well as a
  workflow setting.
- Widget/text modes: `enabled` is ignored there.

---

# Part 3 — implementation plan (2026-09-23)

Ten steps in dependency order. Each is one change with its own offline
test; the paid harness runs come last. Nothing is committed until step 9's
acceptance table is met and seen.

| # | step | files | test that proves it |
|---|---|---|---|
| 0 | Baseline kept | the three 2026-09-23 harness runs (`vm_jev_personal`, `vm_jev_live`, `vm_jev_nobarge`) | the "before" for step 9 |
| 1 | Contract shapes | `voice-gateway/vogon_contract/types.py` (+ `EVENT_TYPES` so the codec's `_EVENT_BY_KIND` sees `LineVerdict`), `contract_codec.py` | `tests/test_gateway_contract.py`: encode/decode round trip of `LineVerdict`, `CallConfig.line_detection`, `CallConfig.silence_ladders`, `SpeakCommand.play_after`, `ProcessEventsResponse.silence_ladder`; every default leaves today's wire byte-identical |
| 2 | Turn side: fact, ledger, annotation, flag | `app/turns/api.py` (`_FACT_EVENT_TYPES`), `executor.process_pending_facts` (ledger `LINE_VERDICT`, sets `ctx.line_checked`), `annotations.build_turn_annotations(line_verdict=)`, `session_io` handler_state (`line_checked` checkpointed), `app/ledger/events.py` | new `tests/test_line_verdict_turn.py` (handler-harness pattern): `[LineVerdict, UtteranceFinal]` → one ledger row, `answered_by` annotation on that trigger only; the next request carries no annotation; a checkpoint restore keeps `line_checked` |
| 3 | Gate honours the annotation | `collections_v3/gate.py` (party stage: `answered_by.verdict == "machine"` → `_terminal(voicemail)` with the node's `on_machine.line`, `deliver_after="line_quiet"`, no classifier, no seat; `line_checked` → `_perceive` skips `jev_gate`), `framework/base.py` (`SubagentResult.deliver_after`, `silence_ladder`), `executor.py:561` (copy onto `SpeakCommand.play_after` / response `silence_ladder`). First task: confirm the executor's annotation path reaches the gate (it reaches the planner and `_llm_utterance` today; if agents do not receive it, stamp it on the invocation config) | extend `tests/test_v3_gate_voicemail_seat.py`: machine annotation → voicemail terminal, zero LLM and zero Jev calls, line id from node config, `deliver_after` set; `unknown` annotation → today's path; borrower resolved → `silence_ladder="default"`; executor test: fields copied onto the response |
| 4 | Turn side: the `unanswered_opening` ladder | `turns/lifecycle.py` (`CallConfig.silence_ladders = {"default": [...], "unanswered_opening": [uo_probe 6 s, uo_exit +8 s]}`, initial `unanswered_opening` when `line_detection.enabled`), `turns/api.py` SilenceThreshold handler (`uo_probe` → the existing party probe; `uo_exit` → end with the JSON's `end_reason`, an opaque string to the platform) | api test: `uo_probe` speaks the probe once, `uo_exit` ends with `NO_ANSWER`; a workflow without the block gets the default ladder only |
| 5 | Gateway detector | new `voice-gateway/vogon/gateway/transport/line_detector.py` (instrument protocol; the Jev instrument via `app.llm.jev.decide_async`; gates: `min_words`, echo-marked episodes, first qualifying episode only, `ask_window_s`; `line_checked`), wired in `twilio_transport.py` beside `FloorController.transcript`; `machine` → `floor.decide(e, 'YIELD', 'line_machine', breakthrough=True)` (the `safety` path) and the episode marked so `on_final` never sets `_absorbing_utterance`; enqueue `LineVerdict` then the `UtteranceFinal`; `human`/`unknown` → the fact alone | offline, `voice-gateway/tests/barge_baseline/` pattern (fake STT, fake Jev): machine cuts the opening and queues the transcript; human → absorbed exactly as today; "Hello?" never asks; an echo-marked episode never asks; a second episode never asks; window expiry sends `unknown`; a JevError sends `unknown` (fail open); detector off → no code path touched |
| 6 | Gateway hold: `play_after="line_quiet"` | `websocket/handler.py` `_execute_turn_response` (await `transport.wait_line_quiet(quiet_ms, cap_s)` before `_send_with_metrics` for such a command), `twilio_transport.py` (`wait_line_quiet`: the detector's episode `final` + `quiet_ms` with no interim, or `cap_s` after the verdict; a `release_line_hold()` hook for a later tone/AMD event) | offline transport test with the fake clock: released at final + 700 ms; released at the 8 s cap; released by the hook; the message plays protected and absorb-all |
| 7 | Gateway ladder switch | `transport/clocks.py` (`select_ladder(id)`: re-sort thresholds, clear `_sil_fired`, keep the idle accumulator), handler applies `resp.silence_ladder` | clocks unit test: switch mid-window fires the new rungs, never re-fires old ones |
| 8 | Workflow JSON | `collections_v3.json` `v3_gate.config`: `line_detection` (enabled **false** until step 9 passes), `on_machine`, `unanswered_opening` | workflow validator accepts the block; a lint that `on_machine.line` is a registry id with the voicemail guards |
| 9 | Measure | harness: read the `LINE_VERDICT` row, add verdict time, cut time, message start − recording final, Jev calls/cost; runs: 5 greetings × echo 0.1 / 0.3 (10), live controls "Hello?" and "Hello? who is this?" over the opening (2), Jev down via a bad key (1, expect 24 s `NO_ANSWER`), `enabled=false` (1, expect identical to step 0) — ~14 calls, ~$0.05 each | acceptance (§8): 10/10 greetings `VOICEMAIL_LEFT` with the message after the recording's final; 0 cuts on the live controls; the disabled run identical to the baseline; then the enable decision and the commit |
| 10 | Deploy notes | gateway env needs `OPEN_ROUTER_API_KEY`; `line_detection.enabled` flipped in the JSON per workflow | — |

Steps 1–4 are turn-side and offline; 5–7 are gateway-side and offline; 8
is data; only 9 spends money. Steps 2 and 5 can be built in parallel once
1 lands; 3 needs 2; 6 and 7 need 1 only.

Risks named up front: (a) the annotation path to agents (step 3's first
task); (b) the echo leak measured at 0.3 gain — if the Jev instrument reads
our own opening as the far end's despite `our_opening` in the state, the
`is_echo_of` exclusion has to tighten before step 9 can pass the live
controls; (c) Flux may split a greeting into more than one episode
("Hi." then the rest) — the first-qualifying-episode rule, not the
first-episode rule, is what handles it, and step 5's tests must include
that split.

---

# Part 4 — the build record (2026-09-23)

Built as planned in Part 3, uncommitted. Offline: 313 tests green across
the gateway baseline suite, the turn side, the contract codec, the clocks,
the planner and the gate (`test_line_detector.py`, `test_line_verdict_turn.py`,
`test_unanswered_opening_ladder.py`, `test_planner_machine_line.py`,
`test_v3_gate_voicemail_seat.py`, `test_gateway_contract.py`).

## 15. What the harness changed in the design, in order

Each of these came out of a duplex run, not out of reasoning:

1. **The detector's state shape.** The design's richer state (our opening
   text, onset before/after our audio) depressed P(recording) by ~0.3 on
   every recording prefix (`prefix_probe`, N=3: "Hi. You've reached Alex
   Morgan." 0.72 → 0.33) and moved no live line. The state is the probe's
   `{account_holder, first_turn}` and nothing else; the question wording is
   the validated one.
2. **Echo at the text level.** At 0.3 echo gain the far end's transcript
   carried our own opening ("Maya calling ... on a recorded line") and the
   AEC did not remove it in the first seconds. `strip_echo` drops runs of
   three or more consecutive words that also occur in our line before the
   detector counts words or asks. At 0.1 gain (a plausible line) the leak
   is fragments ("LendingC Finance on") that the strip cannot match.
3. **Bind the STT turn, not the acoustic episode.** The floor controller
   cuts one greeting into several energy episodes (four in one run);
   binding to the first stopped the asks at 3.5 s and never saw the final,
   so the hold ran to its cap. The detector binds from the first qualifying
   revision to the turn's end-of-turn, whichever episode object carries it.
4. **The eager reads the annotation, the final consumes it.** The
   recording's eager end-of-turn rode with the verdict fact and consumed the
   annotation; the final then had none. Speculation reuse hid it once.
5. **No filler over a machine.** The front model's beat ("Thanks for
   letting me know.") was spoken to the recording during the gate's turn.
   The driver skips the filler hold for the whole call once a machine
   verdict has passed through it.
6. **Quiet is measured on the transcript.** Line energy kept the hold open
   to its cap because our own echo counted as far-end speech; the hold now
   waits for `quiet_ms` with no transcript revision after the end-of-turn.
7. **No classification of a greeting.** Routed through the LLM planner, "I
   can't take your call right now" reached a bad-timing handler first. A
   turn annotated `machine` goes straight to the active primary.
8. **The instrument is injected.** The gateway tree imports nothing from
   the reasoning side (the boundary lint), so the Jev call lives in
   `vogon/websocket/line_instruments.py` and the driver installs it on the
   transport; the detector knows only `ask(state) -> p`.

## 16. The first production call (2026-09-24, call 4108c2eb) — the message never played

The detector worked: machine at 0.84 on "Your call has been", the opening
cut at +1.5 s, the carrier greeting's final at +9 s, both turn requests
answered within 60 ms, the message composed. Then nothing for 50 s until
the carrier hung up. The gateway log's one tell: "silence pause held 30s
— lifting".

**Cause.** The message was sent through the ordinary guarded playout,
which waits for the floor controller to release the caller's floor. That
release needs the caller's end-of-turn flag AND 500 ms of line quiet; any
energy after the final clears the flag (`frame()` → `resume()`), and only
a new end-of-turn sets it again. A voicemail box BEEPS after the greeting's
final. No words follow the beep, so the flag never returned, the floor was
never released, and the playout waited until the carrier hung up. The
harness never hit it because its beep preceded Flux's end-of-turn (~1.5 s
after the words stop); a tone placed in the post-final window reproduces
it exactly (`--beep-after 1.2 --beep-len 1.8`, the new default).

**Fix.** A `play_after="line_quiet"` playout answers a machine, so it does
not take part in human turn-taking: after the line-quiet hold the
transport takes the floor by fiat (`FloorController.take_floor_from_machine`)
and plays protected, absorb-all through `send_held_response`, never
through `allow_playback`; and the driver's playback-generation guard,
which drops a response the caller has talked past, is skipped for a held
response (the later energy is the beep, not a caller). Pinned by
`test_a_beep_after_the_final_must_not_block_the_voicemail_message`, which
also shows the old guarded send timing out on the same state.

**Measured after the fix** (personal greeting, echo 0.1, tone at +1.2 s
for 1.8 s): machine 0.62 at 2.4 s, cut at the verdict, message at 9.6 s
(3.3 s after the recording's end, once the tone had stopped),
VOICEMAIL_LEFT, stream detached at 21.9 s. The live control is unchanged.

## 17. Tone (beep) detection — what we could do (design only, nothing built)

**Terms.** *Beep*: the tone a voicemail box plays when it starts recording,
typically a single narrowband tone (commonly around 1 kHz, boxes vary
from ~800 to ~1,600 Hz) lasting 0.2–1 s, after the greeting's last word.
*Message end*: the moment the box is ready to record — the end of the beep.

**Why it matters.** Today the message starts 700 ms after Flux commits the
greeting's end-of-turn, which itself lands ~1.5 s after the last word, so
roughly 2.2 s after the words stop, or at the 8 s cap. A box that beeps
later than that records our message from its second sentence. Nothing in
the pipeline knows where the beep is: it is energy to the barge gate, a
wordless episode to the floor controller, and nothing to the STT.

**What exists to plug into.** `LineDetector.release_hold()` releases the
line-quiet hold at once; `wait_quiet` already reports why it released
(`quiet` / `cap` / `released`). The hold's precedence would become: tone
release, then the quiet rule, then the cap.

### 17.1 Option A — a tone detector in the gateway (recommended first)

Where: `TwilioTransport.handle_text_frame`, on the AEC-cleaned frame,
beside the barge gate's RMS — the same post-AEC audio the STT receives,
never raw frames. Phone only. Armed ONLY while a line hold is pending
(from a machine verdict until the held playout starts), so it costs
nothing on ordinary calls and cannot fire on a live person.

Method: a Goertzel filter per candidate frequency over each 20 ms frame
(160 samples at 8 kHz), candidates {800, 1000, 1200, 1400, 1600} Hz plus
two half-way bins as controls. A frame is *tonal* when the strongest
candidate holds ≥ 60% of the frame's total power (a narrowband ratio, not
an absolute level — the beep's loudness varies by box) and the frame's
RMS is above the line's noise floor. A *beep* is ≥ 10 consecutive tonal
frames (200 ms) on the same candidate; *message end* is the first
non-tonal frame after a beep, plus 150 ms. Longer than 2 s is a dial tone
or a hold tone, not a beep — ignored. Pure-Python Goertzel on 160 samples
× 7 bins is ~1,100 multiply-adds per frame, well under the frame budget;
`audioop` supplies the RMS.

Speech must never pass: sustained vowels are harmonic, not narrowband —
the ratio test rejects them, and the 200 ms duration rejects the rest.
Validate on the TTS greetings themselves (five voices × the bank) as the
negative set and on the harness tones and real recordings as the
positive set.

Events for the audio log: `tone_started` (freq, ratio), `tone_ended`
(duration_ms), `line_hold_released reason=tone`. The LineVerdict gains
nothing; the release is the whole effect.

### 17.2 Option B — Twilio AMD with DetectMessageEnd

`_place_twilio_call` adds `machine_detection="DetectMessageEnd"`,
`async_amd=True`, `async_amd_status_callback=<public_base>/v1/twilio/amd`;
a new route receives `AnsweredBy` ∈ {human, machine_start, machine_end_beep,
machine_end_silence, machine_end_other, fax, unknown} and, for a
`machine_end_*` value on a call with a pending hold, calls
`release_hold()`. Its verdict can also feed the LineVerdict slot with
source `amd` — the design's "same slot" — but the release is the point
here. Costs: Twilio's per-call AMD fee, and Twilio's own detection delay
after the beep (unstated in the docs; the sync thresholds imply a
verdict 1.5–5 s in). It hears the raw line, before our AEC, which is fine
for a tone. It also cannot be measured in the harness at all — only on
real dials.

### 17.3 Measurement before either is trusted

1. Pull the recordings of real voicemail calls (call 4108c2eb's
   `recording.wav` in S3 is the first) and mark the beep: frequency,
   duration, and its offset from the greeting's last word and from
   Flux's end-of-turn. Five boxes across two carriers is enough to set
   the candidate list and the duration bounds.
2. Harness: `--beep-after` / `--beep-len` already place a 1 kHz tone;
   add `--beep-hz` and run the bank at 800/1000/1400 Hz. Acceptance for
   Option A: `line_hold_released reason=tone` within 300 ms of the tone's
   end on every greeting, zero tone events on the live controls and on
   the greetings' own speech.
3. Only then compare against Option B on real dials (the harness cannot).

**Recommendation.** Build A: it is local, behind the AEC, testable in the
harness, and costs nothing per call. Keep B as the cross-check on real
dials, and as the instrument if A's negative set ever shows speech
passing the ratio test.
