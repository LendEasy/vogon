# Architecture

vogon is one process with two halves and a seam between them.

```
 vogon/gateway_service/        the SERVICE: HTTP routes, call creation, the session
   routes.py                   store, Twilio webhooks, artifact routes, escalation
   session_store.py            callbacks. Knows tenants and tokens.
   escalation/                 warm transfer (Twilio REST, whisper, bridge, report)

 vogon/websocket/              the DRIVER: one call, one coroutine. Owns the
   handler.py                  transport, the Turn API client, the filler hold,
   turn_client.py              the failure ladder. Translates physical events into
   line_instruments.py         ProcessEventsRequests and SpeakCommands into playouts.

 vogon/gateway/                the AUDIO half. Transports (Twilio μ-law, browser PCM,
   transport/                  text; GPT-Live, experimental), the barge gate, the AEC, the floor
   pipeline/                   controller, the silence clock, the line detector, the
   audio/                      front model's move reader; STT and TTS sessions; the
   metrics/                    recorder, event log and timeline; the metrics sink.
   settings.py                 every runtime setting (CONFIGURATION.md)
   jev.py                      the decisions client the gateway's own questions use
```

The two halves share nothing with the turn service but the **Turn API**
(`vogon-contract`): dataclasses and a JSON codec. The fence test in the
original monorepo asserted that no module under `vogon/` imports the
application, and the application imports nothing of vogon.

## One call, end to end

1. **Create.** The application `POST`s `/v1/tenants/{tenant}/calls` with the mode,
   where its turn service lives (`app.base_url`), and an opaque `app_payload`.
   The gateway mints a session token and, for phone calls, places the Twilio
   call with a media-stream URL pointing back at itself.
2. **Attach.** The media stream (or the browser WebSocket) connects; the driver
   builds the transport and calls `start_call`. The turn service answers with
   the `CallConfig` — recording on or off, barge policy, filler pool, line
   detection, silence thresholds — and the opening lines.
3. **Turns.** STT finals, eager ends of turn, delivery outcomes, silence
   thresholds and line verdicts queue on the driver. Facts are batched in front
   of the next trigger ("facts first, trigger last") into one
   `ProcessEventsRequest`. One request is outstanding per call; while it is,
   the filler hold may speak one approved line.
4. **Playout.** Each `SpeakCommand` becomes a TTS stream, paced sentence by
   sentence against a bounded playout buffer so that a barge-in yields at a
   sentence boundary and the gateway knows exactly which sentences were heard.
   The outcome goes back as a `DeliveryOutcome` fact.
5. **End.** A hangup, the silence ladder, the duration cap or an `end_call`
   action closes the call; `end_call` carries the trailing facts and the
   transport analytics. Facts that resolve later (a transfer's outcome) arrive
   through `record_transfer`.

## The audio pipeline, layer by layer

The phone path, stacked from the wire to the seam. Facts travel up the left
column; audio travels down the right. Each layer names its algorithm and the
one decision it makes.

```
                 THE WIRE — Twilio media stream, 8 kHz μ-law, one frame per 20 ms
   LISTENING (facts go up)                              SPEAKING (audio comes down)
 ┌───────────────────────────────────────────┐   ┌───────────────────────────────────────────┐
 │ L1 Echo cancellation                      │   │ S5 Encode and pace                        │
 │    SpeexDSP MDF; GCC-PHAT delay estimate; │◄──│    24 kHz linear16 → 8 kHz μ-law; a mark  │
 │    reference = every chunk sent, as a FIFO│ref│    after each sentence; bounded playout   │
 │    paced at the inbound rate              │   │    buffer; `clear` drops unsent audio on  │
 │    decision: none — cleans the frame      │   │    a cut, so a yield lands at a boundary  │
 ├───────────────────────────────────────────┤   ├───────────────────────────────────────────┤
 │ L2 Energy gate                            │   │ S4 Ambience mixer (optional)              │
 │    RMS per frame; noise floor = EMA over  │   │    room tone, typing in the gaps,         │
 │    noise-classified frames only;          │   │    one-shots; ducked under speech with an │
 │    threshold = clamp(margin × floor);     │   │    attack/release ramp                    │
 │    N-frame debounce; start grace          │   │    decision: none — deterministic DSP     │
 │    decision: is someone speaking?         │   ├───────────────────────────────────────────┤
 ├───────────────────────────────────────────┤   │ S3 Synthesis                              │
 │ L3 Speech confirmation                    │   │    Deepgram aura-2, one WebSocket per     │
 │    WebRTC VAD; M-of-N speech frames in a  │   │    call; sentence split; first audio      │
 │    provisional window while playback      │   │    ≈ 80 ms after the first chunk          │
 │    continues; the buffered onset is       │   │    decision: on a dead socket, reconnect  │
 │    flushed to STT so nothing is lost      │   │    and re-speak only the missed sentences │
 │    decision: speech, or nothing audible   │   ├───────────────────────────────────────────┤
 ├───────────────────────────────────────────┤   │ S2 Playout policy, per SpeakCommand       │
 │ L4 Transcription                          │   │    protected: barge never cuts it         │
 │    Deepgram Flux on the OPEN feed (every  │   │    absorb_barge: speech over it is logged,│
 │    cleaned frame, gated or not); interims,│   │    never queued as a turn                 │
 │    eager end of turn at a confidence,     │   │    play_after=line_quiet: wait for the    │
 │    final; word starts anchored to the     │   │    far end to stop                        │
 │    media clock recover the true onset     │   │    decision: may this line be cut, and    │
 │    decision: none — it reports            │   │    when may it start?                     │
 ├───────────────────────────────────────────┤   ├───────────────────────────────────────────┤
 │ L5 Floor controller + semantic judge      │   │ S1 Filler hold, while a request is out    │
 │    speech grouped into episodes; one      │   │    Jev reads which move the reply will    │
 │    verdict in flight, new text replaces   │   │    open with; deterministic pick from the │
 │    pending work. Heuristic verdict at     │   │    pools the turn service shipped; guards │
 │    0 ms: safety phrases and floor         │   │    reject digits, dates, commitments      │
 │    requests from BargePolicy, token       │   │    decision: one approved line, or        │
 │    counts; the ambiguous middle goes to   │   │    silence (the default)                  │
 │    the Cerebras judge under a hard        │   └───────────────────────────────────────────┘
 │    timeout, default ABSORB                │
 │    decision: ABSORB (keep talking) or     │
 │    CUT (yield at the sentence boundary)   │
 ├───────────────────────────────────────────┤
 │ L6 Line detector, first utterance only    │
 │    the Jev recording question on the      │
 │    first episode behind the AEC           │
 │    decision: machine / human / unknown,   │
 │    once per call; machine cuts the opening│
 └───────────────────────────────────────────┘
 ═══════════════════════════ THE DRIVER (vogon/websocket/handler.py) ═══════════════════════════
   facts first, trigger last → one ProcessEventsRequest ──► turn service ──► SpeakCommand[] → S2
   clocks (silence ladder, duration ladder, filler hold) fire SilenceThreshold / DurationThreshold
   media clock: one monotonic axis; the mark echoes give the outbound lead, the word starts the
   inbound onset, so "did they start before we finished?" is answered in milliseconds
 ═══════════════════════════════════════════════════════════════════════════════════════════════
   taps on every layer → stereo WAV (caller L, agent R) · frames.csv (RMS + gate state per 20 ms)
                         · audio_events.json (onsets, cuts, marks, verdicts) · OTLP metrics
```

What the other transports change: the layered stack above is the phone
path. The browser path keeps L4 and S3 (Flux with the eager end of turn,
aura-2 streamed to the widget) but none of L1, L2, L3, L5 or L6: the widget's
own voice detector sends a barge signal and the gateway cuts synthesis on it,
so the `protected` flag is accepted and ignored there. The text path has no
audio layers and enters at L4 with typed lines, which is why a text call
exercises the driver and the Turn API but none of the decisions above. The
experimental GPT-Live path replaces L4 with the live model as the listener
and keeps everything else.

Where the thresholds come from: each constant in L2, L3 and L5 is either set
from the line itself (the echo delay, the noise floor) or recorded against the
production call that set it in `docs/voice-timing-constants.md`, held by a
test that fails when the code and the doc disagree.
