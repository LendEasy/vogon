# vogon

A full-duplex voice gateway for AI phone and browser calls. vogon owns everything that is
*audio*: the Twilio media stream or the browser WebSocket, speech-to-text,
text-to-speech, barge-in, the silence clock, voicemail detection, recording.
It turns all of that into typed turns and hands them to **your** turn service
over a small HTTP protocol, the [Turn API](https://github.com/LendEasy/vogon-contract),
then plays back whatever your service decides to say.

Your turn service is any HTTP server that implements four endpoints. It never
touches audio, never sees a provider SDK, and never has to know which phone
network the call came in on.

```
  phone / browser ──audio──▶  vogon  ──POST /turn/v1/process_events──▶  your turn service
                  ◀──audio──         ◀── SpeakCommand[] ─────────────
```

## What vogon offers

vogon is the voice half of a conversational AI system, delivered as a
standalone service. You bring the conversation: the logic that decides what to
say next, in whatever language and framework you prefer. vogon brings the
call: it answers or places it, listens, speaks, handles interruptions, keeps
time, detects voicemail, records, transfers to a human when asked, and reports
back to your service, as typed facts, exactly what happened on the line.

In practice this means your team never writes audio code. There is no
provider SDK in your service, no WebSocket to keep alive, no echo to cancel,
no decision about whether a cough was an interruption. Your service receives
an ordered sequence of events for each call and returns the lines to speak.
Everything between the caller's ear and that HTTP exchange is vogon's
responsibility, and everything about meaning is yours.

## Features

**Full-duplex voice with barge-ins, fillers and delivery reports.** vogon
keeps both directions of the line open at once: it listens while it speaks,
judges in layers whether the caller is interrupting, bridges the pause while
your service thinks, and reports back exactly what the caller heard. It is
algorithm-centric: each feature below names the algorithm it runs on.

**1. Multimodal: phone, browser and text.** One driver runs every call,
whether the audio is Twilio μ-law over a media stream, browser PCM from the
embeddable widget, or a text-only session for tests. One endpoint creates a
call with the mode, your turn service's address and an opaque payload; for
phone calls vogon places the outbound dial and reports status to your
webhook. An **experimental** GPT-Live variant lets the live model replace
Deepgram as the listener only; it has not been exercised on a real call.

**2. Eager end of turn for lower latency.** Transcription runs on Deepgram
Flux with streaming partials and an eager end-of-turn signal at a tunable
confidence, so your service starts composing before the caller has fully
stopped; if the caller resumes, vogon reports it and the final text
supersedes the eager one. Replies stream sentence by sentence over one
persistent aura-2 WebSocket, and first audio plays about 80 ms after the
first chunk.

**3. Layered semantic barge-in.** An interruption passes four layers. The
acoustic echo canceller (SpeexDSP MDF with GCC-PHAT delay estimation) removes
the agent's own voice from phone audio. An RMS energy gate with an adaptive
threshold, an exponential moving average over noise frames only, detects
onset above the line's own noise floor. A WebRTC voice-activity detector
confirms speech M-of-N frames while playback continues, flushing the
buffered onset to the transcriber so nothing is lost. A semantic judge then
tells "mm-hm" from "wait": a backchannel is absorbed and playback continues;
a real interruption yields the floor at the next sentence boundary. A floor
controller groups speech into episodes and arbitrates between them without
holding back the transcription feed.

**4. A monotonic event clock that synchronizes all events.** Every event on
a call is stamped on one monotonic clock. Because the two ends observe late
by different amounts, a media-clock projection corrects both: inbound onsets
are recovered from the transcriber's stream-relative word starts anchored to
the stream epoch, and outbound playout is projected from an offset estimator
over Twilio's mark echoes. "Did the caller start before the reply finished
playing, and by how much?" has an exact answer.

**5. Delivery reports: exactly what played out.** Each sentence carries a
playback mark, so vogon knows which sentences reached the caller. A
`DeliveryOutcome` reports `heard_text`, the words actually heard before the
caller spoke, alongside the thresholds crossed and the marks reached, and
facts are batched ahead of the event that triggers a turn so every request
arrives with its context. If the synthesis socket fails mid-reply, vogon
reconnects and re-speaks only the sentences the caller missed, prefixed with
a brief apology.

**6. Control over delivered outcomes.** Your service decides, per line,
whether barge-in may cut it. A `SpeakCommand` marked `protected` plays to
completion, for a required disclosure or a farewell; `absorb_barge` on the
opening logs speech over the greeting without queueing it as a turn;
`play_after: line_quiet` holds a line until the far end has stopped talking.
Per call, `BargePolicy` names the safety phrases that break through even a
protected line, the floor-request phrases ("stop", "let me talk") that cut at
once without the judge, and whether the semantic judge runs at all.

**7. Semantic fillers from predetermined utterances.** While your service
composes, vogon can bridge the pause with one short line from pools your
service ships in its call configuration: a beat, a lookup line, a courtesy,
or silence, which is the default. A decisions model reads what the caller
just said and picks the move; deterministic code picks the words. Structural
guards reject any line with digits, dates or words that could be heard as a
commitment, so the gateway never improvises.

**8. Lower latency with decider models.** The gateway's own questions, which
filler move fits and whether the first words on the line are a recording, go
to Jev, a decisions model that answers a typed question with probabilities
in one call, rather than to a generating LLM. That keeps the filler inside
its hold budget and gives the voicemail verdict on the first utterance. The
one remaining LLM call, the barge judge, runs under a hard timeout and falls
back to keeping the floor.

**9. Reliable and retriable.** Exactly one request is outstanding per call
and every request carries a `request_id`; your service checkpoints the whole
conversation after each turn, so a retried request returns the response
already committed and never reprocesses a turn. The call log is offloaded to
your service; the gateway's session store, in memory or Redis, holds
transport state only: the mode, the tokens, the Twilio call id, the resolved
call configuration and which replica holds the socket. Any replica answers a
webhook, so a restart of anything around a call is survivable. Transport
errors retry on a bounded schedule, and if your service stays unreachable
the call closes with a farewell you configure and a best-effort `end_call`.

**10. Recording on a duplex channel.** When your service enables it, vogon
records both legs as a stereo WAV, caller on the left and agent on the
right, so an interruption is heard in its true overlap, and stores it on
local disk or Amazon S3 under the tenant's prefix.

**11. State-of-the-art logging of every audio event, on synchronized
streams.** Three per-call streams share one time origin: the stereo
recording, a per-frame log of RMS energy and gate state at 20 ms, and a
sparse log of transport events (onsets, cancels, marks, verdicts) rebased to
the recording's first sample. A self-contained timeline page puts all three
on one axis for review. Metrics, including latency, token and cost figures
for every model call, are written as OpenTelemetry OTLP/JSON files the
Collector reads directly.

**12. Ambience mixed in programmatically.** An optional mixer adds room
tone, typing bursts in the gaps and sparse one-shots, and switches them down
beneath speech in either direction with a configurable attack and release,
so they are never heard over the caller or the agent. It is a deterministic
signal processor with no clock or I/O of its own.

**13. Flexible configuration knobs.** Three layers, by owner. The deployment
sets `GATEWAY_*` variables that tune the transcriber's end-of-turn and eager
thresholds, the energy gate's threshold, debounce and adaptive margin, the
VAD aggressiveness and M-of-N confirmation, the semantic judge's window and
timeout, the filler's timing and word budget, and the ambience gains. The
tenant supplies a profile of defaults. Each call is configured by the
`CallConfig` your service returns from `start_call`: the persona voice and
name, the language, a name hint for recognition, the silence, duration and
opening ladders, line detection, the filler pools, recording, the barge
policy, ambience, and the farewell to use if the service goes away. At boot
vogon verifies its native capabilities and refuses to serve degraded phone
calls.

## Tenets

Five, in order of precedence: when two pull against each other, the earlier
wins.

1. **The gateway is for all listening tasks; everything else is delegated.**
   Hearing the line finishes here and crosses the seam as typed facts. The
   typed facts are delegated to the downstream service, which decides the
   action on its own.
2. **Approved speech over hallucinated responses.** The architecture is
   built not to make up words. We play the words given, and make sure all
   possible information about the speech is sent downstream for decision
   making and uttering the right speech. Your lines play as written; filler
   is picked from pools you ship, and silence is maintained by default.
3. **Reliability and quality, balanced.** A call survives a restart of
   anything around it; one process keeps the media connection for the
   call's life.
4. **Adaptive algorithms where possible, evidence-based tuning otherwise.**
   Mechanisms set themselves from the line where they can; each remaining
   constant records the call that set it.
5. **Lightweight and modular over monolithic packaging.** vogon imports
   nothing from your application, and your application needs nothing of
   vogon beyond the Turn API's dataclasses and codec.

## Your first call

1. Install. The gateway is a service, not a library, so it installs from
   the repository or runs as the Docker image:
   `pip install "le-vogon[front] @ git+https://github.com/LendEasy/vogon"`
   (or `docker build` with the Dockerfile). Your turn service needs only the
   contract, which is on PyPI: `pip install le-vogon-contract`. The echo
   canceller needs `libspeexdsp-dev`, `swig` and a C++ compiler to build;
   without it the gateway runs browser calls only, or phone calls with
   `GATEWAY_CAPABILITY_POLICY=warn`.
2. Put `DEEPGRAM_API_KEY` in `.env` (see `env.example`). Twilio keys only for
   phone calls, `OPEN_ROUTER_API_KEY` only for the filler hold and voicemail
   detection.
3. Run a turn service. The reference one repeats back what you say:
   `python examples/echo_turn_service.py`.
4. Start the gateway: `vogon` (or `uvicorn vogon.gateway_service.main:app`).
5. Create a call and connect the browser widget to the `ws_url` it returns:

```bash
curl -s -X POST localhost:8002/v1/tenants/demo/calls -H 'content-type: application/json' -d '{
  "mode": "widget",
  "app": {"base_url": "http://127.0.0.1:9000/turn/v1", "auth_token": "", "status_webhook": ""},
  "app_payload": {}
}'
```

For a phone call set `"mode": "twilio"` with a `"twilio": {"to": "+1..."}`
block and a public `GATEWAY_PUBLIC_BASE_URL` for Twilio's callbacks.

## Writing a turn service

Four `POST` endpoints under `/turn/v1`, JSON bodies encoded by
`vogon_contract.codec`:

| endpoint | you receive | you return |
|---|---|---|
| `start_call` | the call id, the mode, your opaque `payload` | the `CallConfig` policy for this call and the opening `SpeakCommand`s |
| `process_events` | ordered transport events: utterances (eager and final), delivery outcomes, clock thresholds, line verdicts, playout marks | `SpeakCommand`s, `continue` or `end_call`, optional client events and an escalation order |
| `end_call` | why the call ended, the trailing events, transport analytics | nothing |
| `record_transfer` | after `end_call`, what a warm transfer did: each agent leg's events and how it ended | how many you appended |

The guarantees vogon gives you: events for a call arrive in order, one request
is outstanding at a time, every request carries a `request_id` so a retry can
return your committed response, and `heard_text` on a delivery outcome is
exactly what the caller heard. `record_transfer` is the one exception to the
ordering: each fact is its own request and may arrive out of order or twice,
so your service appends by the fact's sequence number and ignores a repeat;
answer 409 while the call is still closing and vogon retries.
`examples/echo_turn_service.py` is the smallest complete implementation.

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — the pieces and the seam.
- [CONFIGURATION.md](CONFIGURATION.md) — every setting, generated from the code.
- [DEPLOY.md](DEPLOY.md) — systemd, Docker, Redis, Twilio, S3.
- [SECURITY.md](SECURITY.md) — what the gateway stores, who it trusts, how to report.
- [docs/](docs/) — the design notes behind each mechanism: barge-in tuning,
  the silence clock, voicemail detection, the front model, delivery
  completion, the timing constants and the incidents that set them.

Licensed under the Apache License, Version 2.0. Contributions under the DCO,
see [CONTRIBUTING.md](CONTRIBUTING.md).
