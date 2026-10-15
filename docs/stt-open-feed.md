# Open STT Feed — Always Let Deepgram Hear the Caller

**Status:** Implemented (default on, `stt_feed_mode=open`; kill switch `gated`)
**Related:** [`barge-in-tuning.md`](barge-in-tuning.md) (the energy gate),
[`full-duplex-semantic-barge-in.md`](full-duplex-semantic-barge-in.md) (the
verdict machinery this reuses).

---

## Terms

- **AEC (Acoustic Echo Cancellation):** the agent's own voice leaks back into
  the caller's microphone/line. AEC subtracts the agent's voice from the
  incoming audio, leaving (mostly) just the caller.
- **STT feed:** the stream of 20 ms audio frames we send to Deepgram for
  transcription.
- **BargeGate:** the state machine that decides, from audio loudness, "the
  caller is genuinely speaking over the agent."
- **Gated window:** the period when we treat incoming audio with suspicion
  because our own audio is active: agent speech playing, plus the ~500 ms
  still buffered at Twilio, plus a 250 ms cooldown after playback.
- **Echo transcript:** Deepgram transcribing the agent's own leaked voice as
  if the caller said it.

## The problem this fixes

During a gated window, an incoming frame used to go: AEC → BargeGate → and
reached Deepgram **only if the gate had confirmed a barge**. If the gate had
not confirmed — quiet speech, the first ~60 ms before confirmation, or after
an absorb reset — the frame was thrown away. Deepgram never heard it.

That valve was built to keep echo out of STT. Its coverage grew as a side
effect of two correct fixes:

```
An 11-second agent reply.

BEFORE (original system — audio pushed to Twilio in ~1.5 s):
time:      0s      1.5s                                    11s
sending:   ██████
playing:   ████████████████████████████████████████████████
filter:    ON      OFF ─────────────────────────────────────
           → the caller was heard unfiltered for ~9.5 of 11 s
             (barge detection was also dead there — the original barge bug)

NOW (barge fix + sentence pacing — audio released at listening speed):
time:      0s                                              11s  +0.75s
sending:   ████████████████████████████████████████████████
playing:   ████████████████████████████████████████████████
filter:    ON ──────────────────────────────────────────────────
           → the filter covers the whole reply, including the exact
             moment fast repliers start talking
```

Same filter, same rules — its ON-time per reply grew from ~1.5 s to the full
reply duration. Callers who answer promptly or softly got the start of their
sentence eaten; Deepgram heard clipped audio and fragmented the turn
(eager end-of-turn → turn resumed → restarted transcript — the "on the
phone?" call). Barge *detection* and the *STT feed* shared one code path, so
widening the window for detection (correct) also widened the feed filtering
(unintended).

## The change, in one sentence

**During gated windows, always send the AEC-cleaned frame to Deepgram; the
BargeGate keeps deciding whether to cut playback, but no longer decides what
Deepgram hears.**

## The diff from the previous behavior

| # | Place | Before | After |
|---|---|---|---|
| 1 | `twilio_transport.py`, media frames, gated branch | AEC-cleaned frame went to the gate; only confirmed-barge audio (`forward_frames`) was sent to STT; everything else dropped | AEC-cleaned frame is sent to STT **unconditionally, every frame**. The gate still runs on the same frame for barge decisions. `forward_frames` no longer sent (would duplicate audio) |
| 2 | Same file, ungated branch | Raw frame straight to STT | Unchanged |
| 3 | Same file, no-AEC legacy branch | Full suppression during playback | Unchanged — the open feed requires AEC; without it the old valve stays |
| 4 | Finals arriving DURING playback with no confirmed barge | Impossible (the audio never reached Deepgram) | Now possible (quiet speech, echo). New rule in `on_final` → `_judge_playback_final`: echo guard first, then the same verdict flow the yield window uses (heuristic floor → time-capped Cerebras) → absorb (swallow, keep playing) / graceful yield / cut. Without this rule the agent would answer itself over its own audio |
| 5 | Echo protection | Audio-level (drop frames) | Text-level: `is_echo_of` — a final that is a fragment (≥4 words) of what the agent was saying is absorbed with verdict `echo` (always swallowed; visible in the call log as `BARGE_ABSORBED`) |
| 6 | Config | — | `stt_feed_mode: "open" \| "gated"`, default `open`; `gated` restores the old valve exactly |
| 7 | `barge_gate.py` | — | No behavior change (its forward buffers are unused on the open path) |
| 8 | Widget / realtime transports | — | Unchanged (the widget already sends everything; the browser does its own echo cancellation) |
| 9 | Resumed-turn transcript stitching (`stt.py`) | Primary defense against fragmented turns | Stays, demoted to a safety net — with a continuous feed, Deepgram should rarely restart transcripts |

## What this fixes

1. Deepgram hears the caller's whole utterance regardless of when they start
   talking → turns stop fragmenting → resumed turns carry full text at the
   source.
2. The semantic barge verdict gets its text input earlier and more completely
   (interims flow during the whole playback), so absorb/cut decisions improve.
3. Quiet speakers and soft openings are no longer invisible during agent
   playback.

## New costs and risks, stated plainly

1. **Echo can reach Deepgram.** Defenses, in order: AEC removes most of it;
   the echo-text guard catches transcribed leakage (and its `echo` verdict in
   `BARGE_ABSORBED` events tells us the real leak rate in production); garbled
   residue tends to land in the existing low-confidence and absorb paths.
2. **Higher Deepgram bill.** We now stream audio during agent playback too.
   Streaming STT is billed on audio duration, so the increase is roughly the
   fraction of the call where the agent is speaking.
3. **Speculative work on during-playback speech.** An eager end-of-turn during
   playback may start the speculative pipeline for an utterance that is later
   absorbed; the existing absorbed-final path cancels that speculation. Wasted
   compute, bounded, acceptable.

## What deliberately does not change

The BargeGate thresholds and state machine, the yield window and verdict
logic, the delivery/consent machinery, the handler pipeline, sentence pacing,
and the widget/realtime paths.

## Observability

- `echo_discarded` (audio timeline) + `BARGE_ABSORBED` with `verdict: "echo"`
  (call log) — how often AEC leaks.
- `BARGE_ABSORBED` with `source: "playback_final:*"` — quiet-speech finals
  judged by the new rule.
- `STT resumed-turn stitch` log lines should drop to ~zero — the leading
  indicator that the fix worked.
