# The transport package

One call's audio path, from the wire to the driver's queue of typed events.
ARCHITECTURE.md draws these as a stack; this file maps the drawing to the
modules.

| Module | Layer | What it does |
|---|---|---|
| `base.py` | — | `BaseTransport`: the surface the driver speaks to (`send_response`, the utterance queue, teardown). |
| `twilio_transport.py` | the phone path | μ-law frames in and out of a Twilio media stream; owns the inbound chain below and the outbound pacing with per-sentence marks. |
| `voice_transport.py` | the browser path | PCM over the widget's WebSocket. Transcription and synthesis only: the widget's own voice detector sends the barge signal, and `protected` is ignored. |
| `text_transport.py` | the text path | Typed lines in, text out; no audio layers. Used by tests and `examples/text_call.py`. |
| `live_transport.py` | experimental | `TwilioTransport` with the GPT-Live model as the listener (`pipeline/live_session.py`). |
| `aec.py` | L1 | SpeexDSP MDF echo cancellation with GCC-PHAT delay estimation; the reference is every chunk sent, as a FIFO paced at the inbound rate. |
| `barge_gate.py` | L2, L3 | Pure decision core: RMS energy gate with an EMA noise floor, N-frame debounce, start grace; WebRTC VAD confirmation M-of-N in a provisional window. No I/O, no clock reads. |
| `floor_controller.py` | L5 | Episodes from onsets and transcripts; one verdict in flight; deterministic floor arbitration from the audio event stream. |
| `barge_semantics.py`, `barge_judge.py` | L5 | The 0 ms heuristic verdict (safety and floor-request phrases, token counts) and the Cerebras judge for the ambiguous middle, under a hard timeout, default ABSORB. |
| `line_detector.py` | L6 | Who answered: the Jev recording question on the first episode; one `LineVerdict` per call. |
| `clocks.py` | the driver row | Silence ladder, duration ladder, filler hold. |
| `delivery.py` | S2 | `DeliveryReport`: what actually reached the caller, from the marks. |
| `front_source.py`, `front_moves.py`, `front_pick.py`, `front_guard.py` | S1 | The filler hold's words: the Jev read of the coming move, the deterministic pick from the shipped pools, the guards every line passes. |
| `outbound_queue.py` | S5 | Keeps a Twilio `clear` ahead of unsent audio so a cut lands at a sentence boundary. |
| `ambience/` | S4 | Room tone, typing in the gaps, one-shots; ducked under speech. Pure DSP. |
| `audio_event_log.py` | the taps | The sparse per-call event log, rebased to the recording's first sample. |

## The echo canceller

`AECProcessor` wraps SpeexDSP. Each agent chunk sent to Twilio is fed to the
reference FIFO (`feed_reference`); each inbound frame passes `process` before
the gate and the transcriber see it. Validation, failed attempts and the
re-alignment design: `docs/aec-design.md`, `docs/aec-validation-harness.md`,
`docs/aec-runbook.md`.

The binding does not build as a pip extra; `deploy/build_speexdsp.sh` installs
it (`docs/aec-installation.md`). Without it `create_aec()` returns `None`, the
capability check reports `aec: false`, and the gateway refuses to start under
`GATEWAY_CAPABILITY_POLICY=refuse`; `warn` runs browser and text calls without
echo cancellation.

At the start of each phone call the log shows the filter's configuration, and
about once a second its echo-return-loss enhancement:

```
AEC init | filter_tail_ms=400 max_delay_ms=500 warmup_ms=600
AEC ERLE | erle_db=31.4 delay_frames=9 ref_fifo=0 total_frames=150
```

Above 20 dB the filter is converging. Below 5 dB there is no echo to cancel
(the far end has its own canceller) or the reference is misaligned.
