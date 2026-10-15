# Escalation — the warm transfer

**Status (2026-10-01):** built and covered offline
(`voice-gateway/tests/escalation/`, `data-plane/tests/test_record_transfer.py`,
`test_escalation_offer.py`, `test_escalation_seam.py`). **Not yet run against
real Twilio** — see [§10 Live test plan](#10-live-test-plan). Not wired into any
collections workflow: this is a platform capability a workflow opts into (§2).

**Why this rewrite happened.** Escalation had never worked on a live call. The
agent dial registered `status_callback_event=["completed","no-answer","busy","failed"]`;
Twilio accepts only `initiated | ringing | answered | completed` and rejects
anything else with **error 21626**, so every agent dial failed. The failure was
only logged — the caller was already redirected onto hold, nobody was ever
dialled, and the turn side recorded `initiated`. The further defects found while
tracing the flow are listed in §11.

What a transfer now gives you: the agent hears a summary of **the call so far**
before taking it (§3); every line is in the **persona's voice** (§7); and the
call log records **every step** — dialled, answered, connected, no answer,
voicemail, how it ended, talk time — even though those happen after the call's
own session is over (§4).

---

## 1. Words used here

| term | meaning |
|---|---|
| **caller leg** | the borrower's/customer's call — the Twilio CallSid the media stream belonged to |
| **hold** | the caller leg redirected (REST `calls(sid).update(twiml=…)`) into a conference that does not start until an agent joins; the caller hears the hold line, then hold music |
| **agent leg** | a NEW outbound call to one escalation target, one per attempt |
| **whisper** | what the agent hears on answering: the handoff summary + "press any key to connect" — the caller does not hear it |
| **bridge** | the agent pressed a key and joined the conference; caller and agent are talking |
| **fallback** | every target failed: a voicemail is left on the last target and the caller hears the callback message, then the call ends |
| **order** | `EscalationOrder` — what the turn side tells the gateway to do |
| **outcome** | `EscalationOutcome` — whether the transfer STARTED, reported at `end_call` |
| **post-call facts** | `EscalationLegEvent` / `EscalationResolved` — what happened after, reported on their own request and appended to the call log after `CALL_ENDED` |

## 2. Who owns what

```
TURN SERVICE (meaning)                          GATEWAY (mechanics)
  decides WHEN: an edge with                      performs the order: hold, dial,
  reason "escalated" finalizes the call           whisper, bridge, ladder, fallback
  composes the order: targets + policy            renders the lines in the persona voice
  from config, the handoff summary of             owns every Twilio callback
  the call so far                                 holds the transfer state
  records ORDERED / RESULT, and appends           reports the initiation outcome and
  the post-call facts to the call log             every post-call fact
```

**A workflow opts in** with three things and no code:

1. an edge to `end` with `"reason": "escalated"` — usually from a node of type
   `escalation` (the generic `EscalationAgent`, `app/framework/agents/escalation.py`),
   but any edge with that reason works (`app/turns/executor.py`, `finalize_reason == "escalated"`);
2. **a declared route to that node**: `"TRANSFER_REQUEST": {"route": "terminating",
   "target": "escalation"}` in `planner.intents`. The planner cannot synthesise a
   terminating route (it does not know the node's name), and a declared
   `intents` block REPLACES the synthesised table — so a workflow on the legacy
   flat planner keys must write its whole table out (the fork now does).
   `appointment_booking` and `appointment_booking_fork` shipped with the node,
   the classifier prompt describing the intent, and no route: the request was
   labelled and went nowhere (found 2026-10-01; pinned by
   `test_every_escalation_node_has_a_declared_transfer_route`);
3. control-plane rows for that owner + workflow: `escalation_config` (hold
   message, voicemail script, callback message, agent timeout, hold music,
   `summary_fields`, the proactive offer's switch and threshold) and one or more
   `escalation_target` (number, label, priority, enabled).

Optional wording in the escalation node's `"config"` object in the workflow
JSON (it becomes the node's `node_config`), each with a neutral platform
default: `proactive_offer_text`, `transfer_speak`, `decline_speak`.

**The proactive offer.** After the caller has been re-asked
`proactive_offer_after_reasks` times, the executor appends "would you like me to
connect you with one of our specialists?" once per call
(`executor.proactive_escalation_offer`). The offer is recorded in
`node_outputs["escalation_offer"]["offered_turn"]` (the executor's namespace,
its only writer); the agent records its answer in
`node_outputs["escalation"]["offer_answered"]`. It used to live in
`internal_state`, which a rebuilt runtime (every request since the gateway went
stateless) does not have — and then "no thanks" read as an explicit transfer
request and transferred the caller (`tests/test_escalation_offer.py`).

The reply is read by one Jev choice question — accept / decline / unclear —
over the offer line, the last exchange and the reply; `unclear` is re-asked
once, then read as a decline, and a failed decisions call takes that same
path (never a guess, never a transfer by default). Probed at N=5: 16/17
replies held on every draw, every substring trap of the old keyword list
("No, please don't", "That's not okay", "I don't want a transfer") a decline
5/5; `JEV_CLASSIFICATION_DESIGN.md` §34. Wording for the
re-ask: `offer_reask_speak` on the node config.

## 3. The handoff summary (the whisper)

`compose_escalation_order` (`data-plane/app/turns/escalation_summary.py`) runs at
the escalated finalize and summarizes **the call so far** for the agent:

- **Input** — the conversation as the caller heard it (`transcript_as_heard`
  over the call log: interruption-aware, last 40 lines / 6,000 chars), the
  confirmed fields (`summary_fields` names the caller), any narrative, and the
  reason (the caller's own words from `EscalationTriggered`).
- **Prompt** — rules in the system prompt (≤ 65 words, spoken English; who,
  identity confirmed or not, what they want, what was discussed or agreed, what
  is open, why transferred; never invent; say if the caller is upset), the call
  in the user message.
- **Bound** — it runs before the handoff line is returned, so the caller waits on
  it: capped at `_WHISPER_SUMMARY_TIMEOUT_SEC` (4 s). Late or failed → a
  field-built fallback ("Incoming transfer. full name Maria Lopez. Reason: …").
- **Record** — `ESCALATION_ORDERED` carries `escalation_id`, the full `whisper`
  text, `whisper_source` (`llm` | `fallback`), `whisper_error`, `summary_ms`,
  the reason and trigger type.

Measured 2026-10-01 (gpt-oss-120b on Cerebras, an 8-line dispute call, N=5):
**350–966 ms**, 47–51 words, every sample naming the caller, the confirmed
identity, the $35 fee in dispute, what was already said, and the caller's
mood — e.g. *"Maria Lopez, identity confirmed. She wants to dispute a $35 late
fee that was promised to be waived. We explained we can't waive fees but can
note the dispute. The fee waiver remains unresolved. She is upset and
requesting a live representative now, so we're transferring her."*

## 4. The contract

```python
EscalationOrder(escalation_id, targets=[EscalationTarget(number, label)], whisper_text,
                agent_timeout_sec, hold_message, hold_music_url,
                voicemail_script, callback_message)       # rides ProcessEventsResponse.escalation
EscalationOutcome(status, target, detail, escalation_id, seq, at)   # rides EndCallRequest.events

# after the call — POST {turn_service}/record_transfer
RecordTransferRequest(call_id, request_id, events=[EscalationLegEvent | EscalationResolved])
EscalationLegEvent(escalation_id, seq, event, target, attempt, agent_call_sid, detail, at_utc)
EscalationResolved(escalation_id, seq, result, connected_target, attempts,
                   hold_sec, talk_sec, detail, at_utc)
RecordTransferResponse(appended)
```

**Initiation outcome** — as facts, not promises:

| status | meaning |
|---|---|
| `initiated` | the caller is on hold **and** an agent leg was placed (ringing) |
| `failed` | the hold update failed (caller already gone, or Twilio refused), or no target could be dialled — in which case the caller has already heard the callback message |
| `no_targets` | the order named nobody |
| `no_provider_call` | not a Twilio call (widget/text) — nothing to transfer |

Its `detail` says the hold line's voice (`hold line: persona voice` or
`hold line: twilio <Say> (reason)`).

**Leg events** (`EscalationLegEvent.event`): `dialled`, `dial_failed`,
`answered` (detail: the whisper's voice), `connected`, `no_answer`, `busy`,
`failed`, `canceled`, `no_keypress` (answered, never pressed a key — or a
voicemail took the whisper), `voicemail_left`, `voicemail_failed`.

**Resolution** (`EscalationResolved.result`, exactly one per transfer that got
past the hold): `connected` (sent when the bridged call ENDS, so `talk_sec` is
known; `detail` says who hung up), `fallback_callback`, `caller_abandoned`
(`detail`: the agent leg was cancelled or ended), `failed`. `hold_sec` = hold
start → bridge (or → the end). `detail` also lists any line spoken by Twilio
instead of the persona voice.

**How the post-call facts reach the call log.**

- The gateway numbers every fact by the transfer's own counter (gap-free from 1,
  an atomic `HINCRBY`) and posts each on its own background task
  (`escalation/report.py`), retrying on 409/429/5xx/unreachable with
  `_POST_CALL_RETRY_DELAYS_SEC` (1, 2, 4, 8, 15, 30, 60 s). A fact still
  undelivered after that — or refused for good (401/404/422) — is logged at
  ERROR as `POST-CALL FACT UNDELIVERED` with its call, escalation id and seq.
- The turn service (`app/turns/post_call.py`) accepts them only once the call is
  finalized (its session state is gone — the signal `end_call` itself uses);
  before that it answers **409** and the gateway retries. That covers the race
  where an agent answers while `end_call` is still running the call-end tools.
- It appends them after `CALL_ENDED` through the ledger's one post-call door,
  `append_after_close` (`app/ledger/substrate.py`; `call-ledger-design.md` §6):
  to the JSONL file and the Redis stream, marked `detail.post_call`, as
  `escalation_leg` / `escalation_resolved` rows. `order_seq` = the closed log's
  highest + the fact's seq, so out-of-order or retried deliveries land in the
  same place; idempotent on `(escalation_id, seq)`.
- Then it re-uploads the S3 call-log archive (`upload_call_log`) on every
  accepted request — the post-call job archived it at `end_call`, before these
  rows existed. A failed upload fails the request; the gateway's retry re-runs
  it (the rows themselves are not appended twice).
- Post-call rows carry `occurred_at = 0.0` (no live call clock) and the wall
  clock in `ts`; nothing sorts the ledger by `occurred_at` (it is ordered by
  `order_seq`), so they sit after `CALL_ENDED` in every view.
- **The call summary does not wait for any of this** (decided 2026-10-01).

**Deploy order.** The codec ignores fields it does not know, so the new
`escalation_id` fields are safe in either order, and the post-call facts never
ride `end_call` (an older turn service would refuse an unknown kind there). A
gateway ahead of the turn service gets 404 on `/record_transfer` and logs each
fact as undelivered; deploy the turn service first.

## 5. The sequence

```
TURN SERVICE                    DRIVER (handler.py)                    TWILIO
────────────                    ───────────────────                    ──────
compose order (summary ≤4 s)
ProcessEventsResponse{
  speak=[handoff line],
  action=end_call,
  escalation=order}      ─────► play the handoff line
                                LineRenders: all 4 lines start rendering
                                wait_playout_heard(≤8 s)       ◄────── mark echo: the caller HEARD it
                                hold line render (≤3 s more)
                                _escalating = True
                                execute(order):
                                  calls(caller).update(hold) ─────────► caller → hold line + Conference
                                                               ◄─────  media stream "stop" (read loop
                                                                       ends; teardown WAITS, §6)
                                  state → store (DIALING)
                                  calls.create(agent #0) ─────────────► agent phone rings
                                  → "initiated"         ─ fact: dialled
                                renders.attach → whisper/voicemail/callback land in the state
                     ◄───────── POST /end_call {EscalationOutcome}
ESCALATION_RESULT row           (session ends; the transfer lives on in the store)
post-call job: summary, S3
                     ◄───────── POST /record_transfer (409 until finalized → retried)
ESCALATION_LEG rows …
```

Ordering guarantees:

- **The handoff line is heard before the redirect.** A call update drops audio
  Twilio still buffers; the driver waits for the stream's end mark to echo.
- **`end_call` is sent after the agent leg is placed**, not before.
- **The redirect's own "stop" does not cancel the transfer** (§6).

## 6. Why teardown waits (and what it replaced)

The hold redirect makes Twilio stop the media stream, which ends the socket read
loop while the turn processor is still inside `execute`. The driver used to
cancel the processor there; the transfer survived only because `execute` caught
the `CancelledError` and treated the stop as proof the hold worked — which it is
not (a caller hanging up stops the stream the same way).

Now `_perform_escalation` sets `_escalating`, and teardown, seeing it, waits for
the processor (`asyncio.shield`, capped at `_ESCALATION_TEARDOWN_CAP_SEC`)
instead of cancelling it. The REST reply alone decides the outcome, and the call
ends `escalated`. Pinned by `test_a_stop_during_the_hold_update_does_not_cancel_the_transfer`;
disabling the flag makes it fail (`'hangup' == 'escalated'`).

## 7. The persona's voice

The hold and callback lines (heard by the caller) and the whisper and voicemail
(heard by the agent) are rendered with the call's own TTS voice
(`CallConfig.persona_voice`, else the deployment default
`DEEPGRAM_TTS_MODEL` — exactly as the call's own TTS resolves it; Deepgram
REST, MP3 — `escalation/voice.py`), stored
under unguessable 128-bit tokens with the transfer (Redis, base64, the
transfer's TTL), served at `GET /gw/v1/escalation/audio/{token}.mp3` and played
with `<Play>`. The audio is deleted with the transfer — the whisper names the
caller.

Rendering speed shapes it (measured 2026-10-01, aura-2): Deepgram REST renders
at about **2× real time** — a one-sentence hold line in ~0.8 s, a ~20 s whisper
in **8.6–10.5 s** (first audio after 0.15–0.25 s). So:

| line | needed when | how |
|---|---|---|
| hold | at the redirect | renders during the handoff drain; awaited ≤ `HOLD_RENDER_WAIT_SEC` (3 s) after it |
| whisper | when an agent answers | keeps rendering after the caller is on hold, lands in the transfer state; the whisper callback waits ≤ `_WHISPER_AUDIO_WAIT_SEC` (5 s) for it |
| voicemail, callback | at the fallback, minutes later | land in the state as they finish; they OUTLIVE the transfer's close (Twilio fetches them after it) until the TTL (`KEPT_AUDIO_TTL_SEC`) |

Any line not rendered in time is spoken by Twilio `<Say>` in
`GATEWAY_ESCALATION_SAY_VOICE` (default `Polly.Joanna-Neural`) instead — and the
record says so: the hold line in the initiation outcome, the whisper on the
`answered` leg fact, the voicemail/callback in the resolution's detail.

Widget and text calls never transfer (no Twilio call) — nothing is rendered.

## 8. Twilio's callbacks, leg by leg

All agent-leg callbacks are under `/gw/v1/escalation/…/{caller_sid}`, carry
Twilio's `CallSid` (the AGENT leg) and are signature-checked (§9).

| # | Twilio sends | the gateway does | returns |
|---|---|---|---|
| 1 | agent answers → `GET whisper/{caller}` | current leg? mark `agent_answered`, fact `answered`; wait briefly for the whisper audio | `<Gather numDigits=1 timeout=60 action=agent-keypress><Play or Say>whisper … press any key</…></Gather><Hangup/>` |
| 2 | key pressed → `POST agent-keypress/{caller}` | `connected=True` (current leg only), fact `connected` | `<Dial><Conference startConferenceOnEnter=true>esc_…` — the bridge |
| 3 | agent leg ends → `POST agent-status/{caller}` (the `completed` event; `CallStatus` carries the result) | see below | `{"status": …}` |
| 4 | `<Play>` → `GET audio/{token}.mp3` | the stored line | `audio/mpeg` (404 once the transfer is gone) |

Row 3, by `CallStatus`:

| CallStatus | connected? | action |
|---|---|---|
| `no-answer` `busy` `failed` `canceled` | — | fact (`no_answer`/…); next target; none left → fallback |
| `completed` | yes | resolved `connected` (agent hung up); state removed |
| `completed` | no (answered, no keypress; or the target's voicemail took the whisper) | fact `no_keypress`; next target; none left → fallback |

The **caller leg**'s status webhook is the one registered at call creation,
`POST /gw/v1/twilio/status`. A terminal status there while a transfer is open
means the caller left:

| transfer state | action |
|---|---|
| bridged | resolved `connected` (caller hung up); state removed |
| on hold, agent leg ringing | resolved `caller_abandoned`; `calls(leg).update(status="canceled")` |
| on hold, agent answered (in the whisper) | resolved `caller_abandoned`; `calls(leg).update(status="completed")` |
| on hold, dial in flight | resolved `caller_abandoned`; the leg the dial returns is cancelled on arrival |

The hook costs one lookup on calls with no transfer and never builds a Twilio
client for them; its failure is logged and does not cost the webhook its session
transition and status relay.

**Fallback.** A voicemail call to the last target with
`machine_detection="DetectMessageEnd"` (Twilio runs the TwiML after the greeting
and beep), then the callback line on the caller leg.

**Transfer state** (`escalation/state.py`): Redis whenever the session store is
Redis (one hash `gw:esc:{caller_sid}`, JSON fields, 4 h TTL, per-field updates,
Lua compare-and-set and counters); in-process otherwise. Advancing to the next
leg is a compare-and-set on `(current_target_idx, agent_call_sid)`; the leg sid
is `DIALING` while a dial is in flight; a duplicate or late status for a
non-current leg is ignored; a leg whose dial returns after the transfer moved on
is cancelled. Exactly one resolution: a `resolved` flag claimed by
compare-and-set.

## 9. Security

- **Twilio's signature is checked on every Twilio webhook that changes state** —
  the call-status webhook (`/v1/twilio/status`) and the escalation callbacks —
  by one helper, `routes._twilio_signed`. Nothing checked it before 2026-10-01.
  The signature is over the PUBLIC URL Twilio called, rebuilt as
  `GATEWAY_PUBLIC_BASE_URL + path + ?query` (nginx's `location /gw/` strips the
  prefix — `proxy_pass http://127.0.0.1:8010/;` — so the app sees a different
  URL). Unsigned or forged → 403. No Twilio auth token configured (dev) → no check.
  **`GATEWAY_PUBLIC_BASE_URL` must be exactly the base Twilio is given**, or every
  signed webhook is refused.
- `/v1/twilio/twiml` is signed too (since 2026-10-01): the dial pins GET, so the
  signature is over the URL with its `?token=` query; a POST signs its form.
  **A `GATEWAY_PUBLIC_BASE_URL` mismatch now refuses every call at answer**
  (the caller hears a hangup) — the live test's L1 is the check.
- **Not signature-checked:** `GET /v1/escalation/audio/{token}.mp3`, where the
  128-bit token is the capability.
- **`/record_transfer`** uses the turn service's bearer (`TURN_SERVICE_API_KEY`),
  like the other Turn API calls.

## 10. Testing

### 10a. Run locally first (done 2026-10-01)

`data-plane/tests/gateway_e2e/escalation_duplex_call.py` runs the live plan's calls
without a phone: the turn service and the gateway in one process, a simulated
Twilio media stream with a TTS caller (name, then "I'd rather talk to a real
person"), the real planner, the real handoff summary, real Deepgram renders,
a recording fake for Twilio's REST client (the hold update stops the stream,
as Twilio does), and the agent's phone as signed HTTP callbacks in Twilio's
order. Metered: Deepgram, Cerebras, OpenRouter — about a minute per scenario.

```bash
cd data-plane && PYTHONPATH=.:../voice-gateway .venv/bin/python3 \
  tests/gateway_e2e/escalation_duplex_call.py --out /tmp/esc/connect --scenario connect
# scenarios: connect no_keypress no_answer abandon dial_fail fast_pickup
```

| scenario | plan | result |
|---|---|---|
| connect | L1 (+ L8) | 18/18 — planner routed the transfer; whisper from the model in 649 ms, 48 words; hold line and whisper `<Play>` in the persona voice; handoff line heard 1.5 s before the redirect; post-call rows `dialled, answered, connected, resolved connected` after `call_ended`, numbered 1–4 |
| no_keypress | L2 | 18/18 — `no_keypress` on Tier 1, Tier 2 dialled and whispered, connected |
| no_answer | L3 | 20/20 — voicemail dial with `DetectMessageEnd`, callback line, `resolved fallback_callback`, both MP3s fetchable after the close |
| abandon | L4 | 15/15 — ringing leg cancelled, `resolved caller_abandoned`, hold 31 s |
| dial_fail | L7 | 14/14 — both dials refused → `failed`, caller hears the callback line |
| fast_pickup | L9 | 17/17 — whisper fetched 0 s after the dial: the callback waited 5 s and served Twilio's voice (`answered` detail says so); the render needs ~9 s, so a phone that rings ≥ 4 s before pickup gets the persona voice |

Timings across the runs: handoff line's last audio → hold update 1.0–1.6 s;
summary 406–649 ms (48–63 words, every one naming the caller, the fee, the
unconfirmed identity and the request); the first dial lands within 1 ms of
the hold update.

**Found by the local run, fixed:** (1) the fallback's voicemail and callback
MP3s were deleted with the transfer before Twilio could fetch them — both
404ed; they now outlive the transfer until their TTL (§7, defect 18). (2)
`appointment_booking` and its fork had no route to their escalation node (§2,
defect 17). **Noted:** the gateway does not close a lingering client socket
after its handler returns; Twilio closes its own after `stop`, so the harness
closes too.

### 10b. Live test plan

Places real calls (costs money); run by hand, not in CI. The local run (10a)
already proves everything but Twilio's own behaviour.

**Setup**

1. A workflow with an escalation node — `appointment_booking` already has one —
   and a persona voice (an aura-2 model) on the assistant.
2. Check existing state first, then add for that owner + workflow:
   - `escalation_config`: a recognisable hold message, voicemail script and
     callback message, `agent_connect_timeout_sec` = 20;
   - two `escalation_target` rows: phone **A** (priority 1), phone **B** (priority 2).
3. Gateway env: `GATEWAY_PUBLIC_BASE_URL=https://<voice_domain>/gw` (exactly what
   Twilio is given), Twilio credentials as today, Redis session store as in
   production. Turn service deployed first (§4).
4. Tail the gateway log. After each call, read its call log (the call-log viewer,
   or `sudo` on the box): `escalation_ordered`, `escalation_result`, then the
   `escalation_leg` / `escalation_resolved` rows marked `post_call`.

**Calls** — on each, talk for a few turns (give a name, a reason), then ask for a person.

| # | scenario | expect on the line | expect in the call log |
|---|---|---|---|
| L1 | A answers, presses a key, talks, hangs up | handoff line heard in full → hold line **in the persona's voice** → A hears a whisper **in the persona's voice** that summarizes the call (name, reason, what was said) → bridged | `escalation_ordered` (whisper text, `whisper_source=llm`); `escalation_result=initiated`, `hold line: persona voice`; legs `dialled`, `answered` (`whisper: persona voice`), `connected`; `resolved connected` with `talk_sec` ≈ the conversation, `agent hung up` |
| L2 | A answers, does NOT press a key | after the 60 s gather A is hung up; B rings and hears the whisper | `no_keypress` for A, `dialled` for B |
| L3 | A and B both decline / let it ring | B gets a voicemail starting AFTER its greeting's beep, in the persona's voice; the caller hears the callback message, then the call ends | `no_answer`/`busy` ×2, `voicemail_left`, `resolved fallback_callback`, `attempts=2` |
| L4 | caller hangs up while A is ringing | A stops ringing | `resolved caller_abandoned`, `agent leg canceled` |
| L5 | caller hangs up while A is in the whisper | A's call is ended | `resolved caller_abandoned`, `agent leg completed` |
| L6 | an undialable number as A | B rings straight away | `dial_failed` (Twilio's error), `dialled` B; `initiated` |
| L7 | both targets undialable | the caller hears the callback message instead of hold music forever | `escalation_result=failed`; `dial_failed` ×2, `resolved failed` |
| L8 | `curl` an agent-status callback without a signature | 403, nothing dialled | — |
| L9 | A answers within ~3 s of ringing (fast pick-up) | whisper after ≤ 5 s of silence, in the persona voice — or Twilio's voice | `answered` detail says which |

**What to measure while there:** handoff-line end → hold line (the drain + hold
render); hold → agent ring; ring → whisper start (L9); post-call rows' arrival
after hang-up; whether any of the handoff line was cut (L1 recording).

**Still unverified until this runs:** DetectMessageEnd holding the TwiML until
the beep (L3); `status=canceled` stopping a ringing leg (L4); the signature URL
behind nginx (L1 would 403); Twilio playing the MP3s (L1); post-call delivery
through the real network (L1's rows).

## 11. Defects fixed

| # | defect | test |
|---|---|---|
| 1 | every agent dial rejected (21626 — invalid status events); float timeout | `test_agent_dial_registers_only_events_twilio_accepts` |
| 2 | `initiated` reported before any dial; a failed dial only logged | `test_initiated_means_the_agent_leg_was_placed`, `test_a_failed_first_dial_tries_the_next_target`, `test_every_dial_failing_is_failed_and_the_caller_hears_the_callback` |
| 3 | a failed later dial stranded the caller on hold | `test_a_failed_later_dial_falls_back_instead_of_stranding_the_caller` |
| 4 | the redirect's stream stop cancelled the transfer; swallowed `CancelledError` treated as success | `test_a_stop_during_the_hold_update_does_not_cancel_the_transfer` |
| 5 | handoff line's buffered tail dropped by the redirect | `test_the_handoff_line_is_heard_before_the_caller_is_redirected`, `test_playout_heard.py` |
| 6 | answered-no-keypress deleted the state under the next leg ("Transfer session not found") | `test_an_unanswered_whisper_hands_off_and_the_next_whisper_finds_state` |
| 7 | stale/duplicate leg status could re-walk the ladder | `test_a_status_for_a_leg_that_is_no_longer_current_is_ignored`, CAS test |
| 8 | a caller hanging up on hold left the agent ringing into an empty conference | `test_a_caller_who_hangs_up_on_hold_cancels_a_ringing_agent_leg` (+ whisper, bridged, mid-dial variants) |
| 9 | transfer state in process memory | `test_transfer_store.py` |
| 10 | no Twilio webhook was signature-checked (status, twiml, escalation) | `test_callback_routes.py` |
| 11 | voicemail spoken over the target's greeting | `test_the_voicemail_fallback_waits_for_the_greeting_to_end` |
| 12 | lines in a different voice from the call | `test_post_call_reporting.py` (persona voice tests), `test_the_driver_hands_the_transfer_its_report_target_and_voice` |
| 13 | the agent leg's lifecycle never reached the record | `test_post_call_reporting.py`, `data-plane/tests/test_record_transfer.py` |
| 14 | the whisper read fields only, not the conversation | `test_the_summary_reads_the_conversation_and_the_record_shows_it` |
| 15 | after a rebuilt runtime, "no thanks" to the proactive offer transferred the caller | `data-plane/tests/test_escalation_offer.py` |
| 16 | the offer's answer was a substring keyword list ("I'm broke" = yes) | `test_escalation_offer.py`, JEV design §34 |
| 17 | `appointment_booking` (+ fork) had the escalation node but no `TRANSFER_REQUEST` route — unreachable except via the offer | `test_workflow_loader.py::test_every_escalation_node_has_a_declared_transfer_route` |
| 18 | the fallback's voicemail and callback audio was deleted with the transfer, before Twilio fetched it (404) | `test_the_fallback_lines_outlive_the_transfer` |

Also: the data plane's legacy `/api/v1/escalation` router (monolith mode) was
deleted — the call sockets moved to the gateway (verified 2026-08-29), so it had
no caller.

## 12. Remaining work

- **Run the live test** (§10) and move the timing constants with what it
  measures (`docs/voice-timing-constants.md` §9).
- **Undelivered post-call facts are not durable**: retries live in the gateway
  process; a restart mid-retry loses them (logged only if the process lives to
  the last attempt). A Redis spool would close that.
- **Wiring escalation into collections_v3** is out of scope; it would be a
  terminal pivot (a path in the working memory) with a transfer-or-callback rule
  for `human_request_handler`.

## 13. Files and tests

| file | role |
|---|---|
| `vogon/gateway_service/escalation/transfer.py` | `TransferOrchestrator`: execute, ladder, fallback, caller gone |
| `vogon/gateway_service/escalation/callbacks.py` | whisper / keypress / agent-status / caller-status / audio bodies |
| `vogon/gateway_service/escalation/report.py` | post-call facts: numbering, the one resolution, delivery with retries |
| `vogon/gateway_service/escalation/voice.py` | the lines in the persona's voice |
| `vogon/gateway_service/escalation/state.py` | `TransferState`, memory + Redis stores, CAS, counters, audio |
| `vogon/gateway_service/escalation/twiml.py` | the five TwiML documents |
| `vogon/gateway_service/routes.py` | the routes, `_twilio_signed`, the status-webhook hook, store boot |
| `vogon/websocket/handler.py` | `_perform_escalation`, the teardown wait |
| `vogon/gateway/transport/twilio_transport.py` | `wait_playout_heard` |
| `vogon_contract/types.py` (+ codec) | order, outcome, post-call facts |
| `data-plane/app/turns/escalation_summary.py` | the handoff summary + the order |
| `data-plane/app/turns/post_call.py`, `http_service.py` | `POST /turn/v1/record_transfer` |
| `data-plane/app/ledger/substrate.py` | `append_after_close` |
| `data-plane/app/turns/executor.py`, `app/framework/agents/escalation.py` | the proactive offer, the agent |

```bash
cd voice-gateway && python3 -m pytest tests/escalation -q
cd data-plane && PYTHONPATH=.:../voice-gateway python3 -m pytest \
  tests/test_escalation_seam.py tests/test_record_transfer.py tests/test_escalation_offer.py \
  tests/simulate_escalation_no_answer.py tests/test_escalation_twiml.py \
  tests/test_escalation_summary.py tests/test_voice_timing_constants.py -q
```
