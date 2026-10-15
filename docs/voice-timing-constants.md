# Voice timing constants — what bounds what, and why

**Companion to [`barge-in-tuning.md`](barge-in-tuning.md).** That doc covers the
**env-var settings** you are expected to tune per deployment. This one covers the
**hardcoded constants** that bound delivery, silence and the TTS socket — values
that encode a judgment, most of them bought with a production incident. Changing
one is a design decision, not tuning.

Every value in the tables below is checked against the code by
`tests/test_voice_timing_constants.py`. If you change a constant, that test fails
until this doc agrees — and if you add one, it fails until this doc lists it.

Audio-format constants (`_TWILIO_TTS_FRAME_BYTES`, `_TTS_SAMPLE_WIDTH`,
`_TTS_SOURCE_RATE`, AEC frame sizes) are deliberately **not** here. They are
facts about μ-law and Twilio's packetisation, not decisions, and listing them
would imply someone could pick different ones.

---

## 1. How long a playout can be believed to last

The silence ladder pauses while agent audio is playing, and resumes when Twilio
echoes the end-of-playout mark. When that mark is lost, these bound the wait.

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `PLAYOUT_STUCK_CAP_SEC` | `90.0` | `voice-gateway/vogon/gateway/transport/clocks.py` | the wait when the audio length is **not yet known** |
| `PLAYOUT_MARK_MARGIN_SEC` | `10.0` | `voice-gateway/vogon/gateway/transport/clocks.py` | telco buffering allowed *after* the known audio length |

**Why.** A swallowed mark once froze the ladder for 9.5 minutes and the call ran
to the max-duration watchdog (2026-07 audit, P3). Once the transport reports how
much audio it handed the telco, that length **replaces** the blanket cap — it is
not `min()`'d with it. A playout can legitimately exceed 90s: call `18b3f7a3`
carried 84.6s across two attempts of one playout, and clamping there ends the
playout while the caller is still listening.

**What breaks if wrong.** Too short: the ladder resumes over live audio and asks
"Are you still there?" on top of the agent. Too long: a genuinely lost mark
leaves a dead call running to the duration watchdog.

The length reported must cover **every attempt** of the playout — a mid-stream
retry does not replace the frames already buffered from the failed attempt.

**Overlapping playouts (2026-09-24).** Two playouts can be live at once — a front
line and the desk's answer behind it. The pause is held while ANY playout is
live (a depth, not a flag), and the watchdog releases only at the LATEST
deadline among the live playouts. Before, each new playout overwrote the
watchdog's timing, so a 1 s line starting behind a 30 s answer released the
pause about 11 s in, and the ladder asked "Are you still there?" over the rest
of the answer — the "too short" failure above. An earlier playout whose length
was never reported keeps the blanket cap, so its release is bounded, not frozen.

---

## 2. What counts as the caller's silence

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `_SILENCE_WARN_SEC` | `8` | `data-plane/app/turns/lifecycle.py` | idle before the first log-only threshold |
| `_SILENCE_ALERT_SEC` | `10` | `data-plane/app/turns/lifecycle.py` | idle before the "Are you still there?" probe |
| `_SILENCE_EXIT_SEC` | `37` | `data-plane/app/turns/lifecycle.py` | idle before the call is ended gracefully |
| `SILENCE_PAUSE_CAP_SEC` | `30.0` | `voice-gateway/vogon/gateway/transport/clocks.py` | how long an outstanding turn request may hold the ladder paused |

**The window measures one thing:** how long the caller has been quiet *since we
stopped talking*. Three events touch it, and the difference is the whole design
(see the header of `clocks.py`): caller activity **re-arms** it, a real agent
turn finishing **resets** it, and playback — including the probe's own — only
**pauses** it, so accumulated idle survives and a silent call still reaches exit.

**Why the pause cap.** The reasoning gap is neither of those things: the caller
answered and we have not replied yet. It is paused across every caller-turn
request, and the request's `finally` releases it — but the turn client's ladder
is bounded and long (`turn_service_timeout_sec` × attempts + backoff, over four
minutes), and this ladder is what hangs up a call nobody is on. Past 30s the
pause lifts itself with a WARNING. No real reasoning gap approaches it.

**Note the gate asymmetry, and its history:** `lifecycle._call_config`
configures `silence_thresholds` only for `mode == "twilio"`, and the
`FillerPolicy` only when the assistant's `enable_empathy_fillers` flag is on.
Until 2026-09-18 the policy was also gated to `mode == "voice"`, so a phone call
had the ladder and never the filler hold — which also pauses it. The hold is
still optional (the flag) and the ladder is not, so the pause above is its own
mechanism, not the filler hold.

---

## 3. What counts as "heard"

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `FULLY_HEARD` | `1.0` | `data-plane/app/framework/delivery_pending.py` | the fraction at or above which a line counts as heard in full |
| `MS_PER_WORD_ESTIMATE` | `375.0` | `voice-gateway/vogon/gateway/transport/delivery.py` | the **fallback** words→milliseconds guess |
| `tts_buffer_lead_ms` | `500` | `voice-gateway/vogon/gateway/settings.py` (env-tunable) | audio Twilio may hold ahead of real time |

**The exact signal wins.** In sentence mode the delivery report carries
`sentences_sent` / `sentences_total` — per-sentence marks Twilio echoes. That is
what the arming gate uses. `MS_PER_WORD_ESTIMATE` is reached only for burst mode
and the widget, which count no sentences.

> **Do not retune `MS_PER_WORD_ESTIMATE` without measuring on number-heavy
> text.** It is known-biased: on call `18b3f7a3`, 96 words estimated 36.0s
> against **43.68s** of real audio — 455 ms/word — because `$1,940.47` and
> `888-555-0199` are one word each and many spoken syllables. Deciding on it is
> what made a delivery cut at three sentences of four read as 100% heard. The
> fix was to stop consulting it, not to tighten it.

`tts_buffer_lead_ms` is the bound on *sent vs played*: a barge clears Twilio's
buffer, so up to one lead of already-sent audio is never heard. Every "sent
means heard" claim in this system is accurate to within that lead.

---

## 4. How many times we try again

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `REDELIVERY_MAX_ATTEMPTS` | `2` | `data-plane/app/framework/delivery_pending.py` | re-deliveries of one interrupted line before the gate gives up (§4g) |
| `CONSENT_BLOCK_MAX` | `2` | `data-plane/app/framework/delivery_pending.py` | consent affirmatives rejected for a half-heard ask before falling through (§4g-lite) |

Both bound the same shape from opposite sides: a caller who talks over the same
line every time it is re-spoken produces a truncated report every time, and the
planner's validator re-surfaces the record into every subsequent plan. The
documented exit — "a re-delivery the caller heard in full commits" — is real but
conditional, and a serial barger can deny it indefinitely.

Giving up **commits** the held terminal status, exactly as a landed delivery
does: a node stranded in `DELIVERY_PENDING` is the worse failure. It is not a
delivery, so it is recorded as a degradation — a WARNING, a `guard_trips` entry
(`redelivery_abandoned`, `outcome=committed_unheard`), and `abandoned: true` on
the call-log event.

**Status note:** §4g was recorded as *deliberately deferred (user decision)* and
was implemented in `79f03ab`. If it is deferred again, that commit is the unit to
revert.

---

## 5. The TTS socket

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `_CANCEL_DRAIN_TIMEOUT` | `0.3` | `voice-gateway/vogon/gateway/pipeline/tts.py` | wait for Deepgram's `Cleared` ack after a barge |
| `_DRAIN_WINDOW_MAX_SEC` | `90.0` | `voice-gateway/vogon/gateway/transport/twilio_transport.py` | how long a pending mark may keep the barge gate open |
| `_TTS_FRAME_GAP_WARN_MS` | `60` | `voice-gateway/vogon/gateway/transport/twilio_transport.py` | send-side gap that warns **when running behind real time** |
| `_SEND_QUEUE_DEPTH_WARN` | `25` | `voice-gateway/vogon/gateway/transport/twilio_transport.py` | send-queue depth that warns (~500 ms backed up) |

### Borrowed defaults we now depend on

These are **not ours**. They belong to `websockets`, and a library upgrade that
changes either one re-arms a production incident, so the drift test asserts them
from the installed package.

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `websockets.max_queue` | `32` | `websockets.legacy.client.connect` | inbound messages buffered before the reader **parks** |
| `websockets.ping_interval` | `20` | `websockets.legacy.client.connect` | seconds between keepalive pings |
| `websockets.ping_timeout` | `20` | `websockets.legacy.client.connect` | seconds to wait for the PONG before killing the socket |

**Why this matters, and it is not obvious.** `legacy`'s `transfer_data()` parks
on `_put_message_waiter` once the queue is full, and PONG frames are processed
**only inside that same parked loop**. So a consumer slower than the producer
stops answering keepalive pings and the client kills its own healthy socket —
`sent 1011 (internal error) keepalive ping timeout`, measured at
`elapsed_ms=40485` (20 + 20). Deepgram synthesizes at ~2× realtime; the paced
sender releases at 1×. Every long turn hit this.

The fix is `max_queue=None` on the **speak** socket only
(`_unbound_speak_receive_queue()` in `tts.py`), applied to
`deepgram.speak.v1.client` and `.raw_client`. STT (`deepgram.listen.v2`) is a
fast consumer and keeps the bounded queue that protects it.
`ping_interval` is deliberately left alone — disabling keepalive would hide a
genuinely dead socket, which is what `TTSClient.reconnect()` exists for.

---

## 6. Where the pause cap's worst case comes from

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `turn_service_timeout_sec` | `40.0` | `voice-gateway/vogon/gateway/settings.py` (env-tunable) | HTTP timeout for one process_events attempt |
| `turn_retry_delays` | `0.25,1,2,4,8` | `voice-gateway/vogon/gateway/settings.py` (env-tunable) | the M2 retry ladder's backoff schedule |

Six attempts at 40s plus ~15s of backoff is over four minutes — which is why
`SILENCE_PAUSE_CAP_SEC` exists rather than trusting the request's `finally` to
be timely.

These two are **code defaults**; a deployment may override them by env var. Do
not infer production behavior from this table — ask.

---

## 7. The session policy

| Constant | Value | Where | Bounds |
|---|---|---|---|
| `max_call_duration_sec` | `600` | `voice-gateway/vogon/gateway/settings.py` (env-tunable) | the hard cap: the controller closes the call here, after one spoken closing line |
| `session_wrap_up_lead_sec` | `60` | `data-plane/app/config.py` (env-tunable) | how long before the cap the one wrap-up notice fires (0 = no notice) |
| `HOLD_GRACE_SEC` | `60.0` | `data-plane/app/agents/collections/hold_request.py` | how long an acknowledged "hold on" defers the silence ladder before it probes |

**Two thresholds, one ladder** (`app/turns/session_policy.py`). The gateway's
duration clock fires `wrap_up` at cap − lead and `max_duration` at the cap; the
turn side speaks one notice at the first and publishes
`session.session_wrap_up_active`, and at the second speaks a closing line and
asks the gateway to end the call. Until 2026-09-06 the cap ended the call with a
fatal `server.error` and nothing said — the schema-v2 smoke's SESSION cases
failed the judge on exactly that. A wrap-up state that agents *honour* (no new
authorization once it is set) is theirs to implement; the platform owns the
slot and the notice.

**The hold grace is not the silence pause.** The pause (§2) covers the
reasoning gap and lifts itself after 30s. The grace is granted by the hold
agent's acknowledgement (`NodeOutputSet(silence_grace_sec)` → the executor →
`ProcessEventsResponse.silence_grace_sec` → `CallClocks.grant_silence_grace`):
every rung of the ladder is pushed out by that much idle, measured from when the
acknowledgement finished playing. Caller activity clears it; a second hold
grants it again in full, so back-to-back holds each get their minute. The probe
itself stays at `_SILENCE_ALERT_SEC` after the grace — a caller who asked for a
minute and then went quiet is still asked once whether they are there.

---

## Incidents these values are made of

| Call | What it cost |
|---|---|
| `18b3f7a3` | a 45s turn (the payoff answered twice) killed the TTS socket at 40s; the recovery re-spoke all of it; the watchdog ended a playout with 43.68s still to play; two probes over the agent's own voice |
| `c51a075a` | keepalive death 64s into a long turn; the apology, its retry and the probe all streamed 0 frames into a dead socket while the logs said "spoken" |
| `21e65d97` | a block logged "Interrupted 100% heard" re-spoken in full — a ~40s repeat of something the borrower had just heard |
| 2026-07 audit P3 | a swallowed mark froze the silence ladder for 9.5 minutes |

---

## 8. The front model (docs/voice-gateway-front-model.md)

The filler hold's source of words on the gateway. Settings, `FRONT_*` env,
read by the gateway process; the ledger test below checks them.

| constant | value | where | what it bounds |
|---|---|---|---|
| `front_budget_ms` | `500` | `data-plane/app/config.py` | eager end of turn → the model's line ready, provider RTT and generation included; over it, the hold falls back to a beat |
| `front_beat_after_ms` | `900` | `voice-gateway/vogon/gateway/settings.py` | the hold still open and the model LATE or failed → a beat from the `hold` pool; also the model request's timeout (a line at 700 ms still beats a beat at 900) |
| `front_beat_after_empty_ms` | `1500` | `voice-gateway/vogon/gateway/settings.py` | the hold still open and the model deliberately said NOTHING (a plain answer, identity data) → a beat, later: on those turns the desk is usually fast, and a beat at 900 ms landed in the borrower's own mid-sentence pause twice in the 2026-09-20 smoke |
| `front_wait_again_after_ms` | `2500` | `voice-gateway/vogon/gateway/settings.py` | the answer still not back this long after a front line → one beat from the `wait` pool, once per turn |
| `front_min_gap_sec` | `6.0` | `voice-gateway/vogon/gateway/settings.py` | minimum spacing between any two front lines, both tiers — a model line, a hold beat and a wait beat never stack on one slow turn |

**Why these.** The prototype (`voice-gateway/prototypes/front_model/RESULTS.md`)
measured the model at p50 ~230 ms, p90 ~330 ms, max ~560 ms over 46 turns ×
9 runs; 500 ms holds all but one or two turns per run and the late ones fall
to a beat, never to a slow line. 900 ms is where "hello?" starts on a phone.
The model request runs to `front_beat_after_ms`, not to the 500 ms target: the 2026-09-20 smoke lost a sympathy line to a 500 ms client cut while the hold would have waited to 900 anyway. 2.5 s is the interval today's canned continuation used (`interval_ms` 700 was
for three phrases in a row; one beat wants more room). 6 s keeps a front line
from ever being followed by another within a breath.

## 9. The warm transfer (escalation)

The gateway performs an `EscalationOrder` after the handoff line: hold
redirect on the caller leg, then an agent leg per target, driven afterwards by
Twilio's callbacks (`voice-gateway/vogon/gateway_service/escalation/`).

| constant | value | where | what it bounds |
|---|---|---|---|
| `_HANDOFF_HEARD_CAP_SEC` | `8.0` | `voice-gateway/vogon/websocket/handler.py` | waiting for the handoff line's mark echo before the hold redirect — a redirect drops whatever Twilio still buffers, so the caller would lose the end of "let me connect you"; the cap covers a mark that never echoes |
| `_ESCALATION_TEARDOWN_CAP_SEC` | `60.0` | `voice-gateway/vogon/websocket/handler.py` | teardown waiting for a transfer already in flight when the media stream stops under it (the redirect itself stops the stream): the drain wait, the hold update and a dial per target, each REST call bounded below |
| `_FINALIZE_TEARDOWN_CAP_SEC` | `90.0` | `voice-gateway/vogon/websocket/handler.py` | teardown waiting for the call's own ending already in flight: `_finalize_call` marks the call DONE before its end_call POST and the read loop returns on DONE, so the processor holding the POST is waited for, not cancelled (voice-eval-c6819d60, 2026-10-10: a NO_ANSWER never reached the turn service). Above the POST's own budget: two attempts at `turn_service_timeout_sec` plus the first backoff, ~80 s |
| `_TWILIO_REST_TIMEOUT_SEC` | `10.0` | `voice-gateway/vogon/gateway_service/escalation/transfer.py` | one Twilio REST call (hold update, dial, cancel), both as the HTTP client's timeout and as the `wait_for` around it |
| `TRANSFER_TTL_SEC` | `14400` | `voice-gateway/vogon/gateway_service/escalation/state.py` | how long a transfer's state survives in Redis — an orphan's bound (a lost final callback), not a limit on a bridged call, which needs no state |
| `_WHISPER_SUMMARY_TIMEOUT_SEC` | `4.0` | `data-plane/app/turns/escalation_summary.py` | the handoff summary (one LLM call over the conversation so far) — it runs before the handoff line is returned, so the caller waits on it; late = the field-built whisper, recorded as `whisper_source=fallback` |
| `HOLD_RENDER_WAIT_SEC` | `3.0` | `voice-gateway/vogon/gateway_service/escalation/voice.py` | waiting, after the handoff drain, for the HOLD line's persona-voice render — the one line needed at the redirect (one sentence renders in ~0.8 s, during the drain); late = Twilio `<Say>`, recorded in the initiation outcome |
| `_LINE_RENDER_TIMEOUT_SEC` | `30.0` | `voice-gateway/vogon/gateway_service/escalation/voice.py` | any one line's render; the whisper, voicemail and callback keep rendering after the caller is on hold — Deepgram REST renders at ~2× real time, a ~20 s whisper took 8.6–10.5 s (measured 2026-10-01); a failed line is `<Say>`, recorded in the resolution |
| `_WHISPER_AUDIO_WAIT_SEC` | `5.0` | `voice-gateway/vogon/gateway_service/escalation/callbacks.py` | the agent answered before the whisper's render landed: the whisper callback waits this long (Twilio's webhook timeout is 15 s), then `<Say>`s it — reported on the `answered` leg fact |
| `KEPT_AUDIO_TTL_SEC` | `600.0` | `voice-gateway/vogon/gateway_service/escalation/state.py` | how long the fallback's voicemail and callback renders outlive the transfer's close in the in-process store (Redis uses the transfer TTL) — Twilio fetches them after the close; deleting them with the transfer 404ed both (local run 2026-10-01) |
| `_POST_CALL_RETRY_DELAYS_SEC` | `(1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)` | `voice-gateway/vogon/gateway_service/escalation/report.py` | waits between attempts to deliver a post-call fact — covers end_call finalizing (409, up to ~25 s while the call-end tools run) and a short turn-service outage; after the last, the fact is logged at ERROR |
| `_POST_CALL_HTTP_TIMEOUT_SEC` | `5.0` | `voice-gateway/vogon/gateway_service/escalation/report.py` | one delivery attempt of a post-call fact |

**Why these.** The handoff line is one sentence (~3 s); the send loop runs
ahead of real time by the TTS buffer lead, so the mark normally echoes within
about a second of the stream ending, and 8 s only ever binds on a lost echo.
The teardown cap is the drain cap plus the hold update plus a few dials at the
REST bound — on a healthy line the whole step takes well under two seconds.
Four hours is longer than any hold plus agent ladder (Twilio caps a dial at
600 s per leg). Measured on the local end-to-end run (2026-10-01, six
scenarios, `data-plane/tests/gateway_e2e/escalation_duplex_call.py`): handoff
line's last audio → hold update 1.0–1.6 s (the drain wait never neared the 8 s
cap); summary 406–649 ms against the 4 s cap; a whisper fetched 0 s after the
dial fell back to `<Say>` after the 5 s wait (the ~20 s whisper renders in ~9
s), one fetched ≥ 4 s after the dial did not. No live Twilio measurement yet.
