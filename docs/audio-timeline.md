# Per-call audio timeline — see what happened on the wire

The episode/floor controller publishes to the dedicated `call:{call_id}:audio`
Redis stream and local audio artifacts, keeping detailed events out of the
main call log. `call_ended` includes a suppressed-speech summary. The viewer adds
a floor-decision lane and a searchable, seekable event table. See
[the integration guide](../../voice-gateway/tests/barge_baseline/INTEGRATION.md)
for event fields, publication lifecycle and controller behavior.

**Status:** implemented (event log + offline renderer + node-local serving).
**Surface:** transport (`twilio_transport.py`) + `app/services/audio_timeline.py`
+ routes in `app/routers/calls.py` + a link in `frontend/call-log.html`.
Companion to [barge-in-tuning.md](barge-in-tuning.md) — this is the eyes for
that work.

## One clock, three artifacts

Everything aligns on the **recorder's t0** (first media chunk): the recorder,
the Phase 0 frame log, and the barge gate all stamp with the same
`time.monotonic()` inside `handle_text_frame`, and the event log rebases to the
recorder's t0 at flush. Per call, under `settings.recordings_path`:

| Artifact | Producer | When |
|---|---|---|
| `<call_id>.wav` | AudioRecorder (stereo: L=caller, R=agent) | always (when recorder wired) |
| `<call_id>.frames.csv` + `.clean.wav` | Phase 0 frame log | `BARGE_IN_FRAME_LOG=true` |
| `<call_id>.audio_events.json` | **AudioEventLog** (this work) | **always-on** — sparse, ~dozens of events, one list-append each; flushed at close; failures never block teardown |

The events file carries a `meta` header with the gate settings the call ran
under (threshold / debounce / grace / ambience), so the viewer draws the
*correct* threshold line for that call even after defaults change.

## Event catalog (emitted today)

| Event | Where | Meaning |
|---|---|---|
| `tts_start` / `tts_end` | send_response | agent speech span as SENT (`cancelled` flag on end) |
| `mark_echo` | mark handler | caller actually FINISHED HEARING the audio; gap from `tts_end` = jitter-buffer lead |
| `twilio_clear` | _send_clear | buffer clear dispatched; `recording_discarded_ms` removes queued tail from the recording. `recording_cut_basis=clear_dispatch_estimate` identifies the estimate; network propagation may leave a short actual audible tail. |
| `stt_interim` | on_interim | first interim per turn — caller speech-start proxy |
| `eager_eot` / `stt_final` | STT callbacks | speculative / committed end of caller turn (+confidence) |
| `barge_streak_start` | gate | first over-threshold frame of a streak |
| `barge_suppressed` | gate | streak died before the debounce count — a rejected transient |
| `barge_grace_blocked` | gate | streak matured inside the start-grace (emitted once, at maturity) |
| `barge_confirmed` | gate | Energy/VAD candidate confirmed; semantic arbitration decides whether to cut |
| `speech_episode_started` / `speech_episode_ended` | controller | Acoustic attempt boundary; partial revisions do not count as attempts |
| `speech_revision` | controller | Text revision bound to an episode |
| `speech_attempt_unsuccessful` | controller | One unresolved/deferred attempt recorded as repeat evidence |
| `floor_decision` | controller | Decision/action/reason plus policy, episode, generation and evidence |
| `floor_classifier_requested` / `floor_classifier_result` | controller | Heard context and classifier result/latency |
| `floor_result_rejected` | controller | Stale revision/generation or result after deadline |
| `floor_released` | controller | Caller EOT and silence both observed |
| `playback_blocked` / `playback_allowed` | player | Hold/drop or allow with reason |
| `caller_transcript` | STT final | Allow or suppress the caller turn, with reason |
| `audio_route` | inbound media | AEC/open/gated/no-AEC routing change |
| `outbound_queue_cleared` | send queue | Count of unsent audio/mark messages discarded before clear |
| `gap_open` / `gap_close` | ambience | think-gap fill window |
| `typing_start` / `typing_stop` | ambience | liveliness typing cue |
| `clock_silence` | CallClocks (2026-08-27) | silence-ladder firing (`threshold_id` warn/alert/exit, idle sec) |
| `clock_duration` | CallClocks | duration-limit firing (`threshold_id`, elapsed sec) |
| `hold_open` / `hold_close` | CallClocks | the filler hold window (a turn computing; close carries `fillers_played`, `interrupted`) |
| `filler_start` | CallClocks | one filler playout began (`audio_id`, phrase) — its TTS span also appears via `tts_start` |

The clock-engine events arrive through `CallClocks(audio_emit=...)` — the
Twilio transport wires its `AudioEventLog.emit`; text/widget transports pass
nothing and the hook no-ops. (Fillers are widget-only today, so
`filler_start` appears on a Twilio timeline only if a `FillerPolicy` is ever
enabled for that mode; the hold/clock events render regardless.)

Adding an event = one `self._audio_log.emit(now, "name", **detail)` line and,
if it should render specially, a marker entry in the viewer's lane code.
Candidates not yet emitted: AEC ERLE samples, frame-gap/underrun warnings,
eager-EOT-cancelled (`turn_resumed`), ambience clamp events, oneshot_fired.

The viewer renders a structured color KEY under the chart (per-lane swatch
glossary, 2026-08-27) — keep it in sync when adding a rendered event.

## The viewer

One builder, two surfaces (`app/services/audio_timeline.py::build_timeline_html`
— fully self-contained page, audio embedded as a data-URI, zero external deps):

- **Served from the node (v2):** `GET /api/v1/calls/{id}/audio-timeline`
  (+ `?vad_mode=0..3`). Reads the local artifacts, builds the page on demand.
  Raw artifacts are also served: `/recording.wav`, `/audio-events.json`,
  `/frames.csv`. `call-log.html` probes `/recording.wav` and shows a
  **🎛 Audio Timeline** link when this node has the recording (independent of
  the S3 artifacts bar).
- **Offline (v1):** `PYTHONPATH=. python3 proto/barge/render_call_timeline.py
  <call_id> [--recordings-dir …] [--vad-mode 2]` → `<call_id>.timeline.html`,
  for tuning sessions off-box.

Lanes (top→bottom), all sharing one time axis with a playhead synced to the
`<audio>` element — click seeks, wheel zooms around the cursor, drag pans,
hover shows detail tooltips:

1. **caller / agent waveforms** — decoded client-side from the embedded WAV.
2. **RMS chart** — raw (blue) + post-AEC clean (green) lines, log scale, with
   the call's barge threshold as a dashed red rule: you *see* background noise
   crossing the gate.
3. **agent lane** — TTS spans (amber-tinted when cancelled), the dim extension
   to `mark_echo` = the jitter-buffer lead the caller was still hearing,
   think-gap underbar, typing markers.
4. **caller lane** — ▲ first interim, □ eager EOT, ● final (+confidence);
   green shading = offline webrtcvad speech track over the clean WAV (1:1 with
   frames.csv rows) — doubling as a visual audition of the Tier-2 VAD candidate.
5. **barge lane** — streak intensity strip (from frames.csv) + markers:
   streak-start, SUPPRESSED (debounce win), grace-blocked, **BARGE CONFIRMED**
   (full-height red), clear-sent.

## Verification

`voice-gateway/tests/test_audio_timeline.py` drives the real transport with synthetic frames
+ a real AudioRecorder → all three artifacts → `build_timeline_from_dir` →
asserts events (incl. a debounce-suppressed and a confirmed barge), meta, and
that the page embeds cleanly; the embedded JS is `node --check`ed in dev. What
no offline test can show — whether the *rendering* reads well on a real call —
is a browser check on the first instrumented calls from the Phase 0 runbook.
