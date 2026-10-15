# Integrated episode and floor controller

The live Twilio transport now uses `FloorController` when both
`BARGE_CONTROLLER_ENABLED` (default true) and `SEMANTIC_BARGE_ENABLED` are true.
Set the former to false to restore the existing semantic-window behavior.
The original 83 characterization tests explicitly select that legacy path;
the 11 previously expected failures now run as ordinary passing regressions
against the integrated controller. Additional integration cases cover floor
release, protected playback marks, stalled TTS cancellation, final-only
classification, ordered publication and the timeline page.

## Runtime flow

Existing AEC and the clean-RMS gate remain in place. A speech episode collects
positive-frame duration and ends after 350 ms of quiet; STT revisions do not
create new attempts. Gate confirmation qualifies evidence for interruption.
Each changed partial reopens semantic consideration. Configured policy phrases
and the existing clear heuristics are fast paths; ambiguous text goes to a
three-decision classifier (`CONTINUE`, `YIELD`, `UNCERTAIN`). The classifier sees
heard/unheard context captured at episode onset and recent attempts. The
controller rejects results for an obsolete revision, response, or generation.
One provider call runs at a time, with one latest pending revision. A timed-out
executor call is drained before another starts; audio and deterministic
deadlines continue during that drain.

An unresolved episode records one unsuccessful attempt. A second qualified
episode can yield after 240 ms of positive frames; persistent speech with text
can yield after 1200 ms even after CONTINUE. Previous failed attempts expire
after 10 seconds. These values are configurable and are not production latency
SLOs. Ordinary protected playback defers requests until actual playback
completion, including the drain tail; intro absorb-all remains intact.
Configured safety phrases break through even after an earlier absorption.

Yield clears Twilio immediately, drops unsent media and marks from the
gateway's queue, wakes a stalled TTS iterator, and cancels provider generation
through the existing TTS cancellation API. Subsequent responses and clock
fillers are guarded by caller-floor ownership. A current response waits for
caller EOT plus 500 ms of silence; a response for an old generation is dropped
with a zero-delivery outcome. Eager/final events carry their generation across
queue delays, and the driver preserves it across the Turn API request.

The open STT feed stays independent of interruption decisions. Existing
operational fallbacks remain: `STT_FEED_MODE=gated` restores gated forwarding,
and missing AEC retains playback-time suppression. `audio_route` makes these
conditions explicit. No AGC was added.

## Published observability

`AudioEventLog.emit` writes each event to the existing local
`<call_id>.audio_events.json` artifact and sends it through the gateway's
`AudioEventPublisher`. Detailed rows go to `call:{call_id}:audio` (approximately
10,000 entries maximum, 24-hour TTL) and `<call_id>.audio_events.jsonl` under
the recordings directory. They do not go to the conversational call log or
its Redis stream. The timeline continues to read `.audio_events.json`.
The publisher drains before `end_call`; Redis failures remain locally recorded.

The call-ended row includes `barge_in_suppressed_speech`: final and pending
counts plus text, reason, audio/episode IDs and final/partial status. This
snapshot travels through transport analytics, so it survives an unconsumed
utterance queue at hangup. Revised attempts that were eventually allowed
are excluded from pending suppression. Normal `barge_absorbed` final rows
remain available during the call.

Each controller event includes schema version, episode/revision, playback
generation, audio ID, floor owner, protected/intro policy, accumulated speech
duration, and a reason. Decision events distinguish `KEEP_PLAYING`,
`GATHER_MORE`, `DEFER`, `CUT` and `TAKE_FLOOR`. Separate transcript events say
`ALLOW_TURN` or `SUPPRESS_TURN`; playback events explain `HOLD` or `DROP`.
Classifier request/result events include heard context, decision and latency;
stale/deadline results have explicit rejection reasons. Gate events and routing
changes remain visible alongside them.

The existing `/audio-timeline` pages now show:

- Energy candidate confirmation separately from an actual cut.
- A floor-decision lane with reason/evidence tooltips and caller-floor spans.
- A filterable event table; click a row to seek the recording.
- A JSON download of the aligned event stream.

Both the standalone gateway and data-plane timeline routes use this builder.
Transcript text is inserted safely into the generated page.

## Validation scope

Run the baseline, acceptance and new integration cases with:

```bash
python -m pytest tests/barge_baseline -q   # from voice-gateway/
```

These are deterministic transport tests with controlled provider boundaries.
They verify real controller, player, delivery and publisher behavior, plus
generated JavaScript syntax. They do not establish native AEC suppression,
real classifier/STT accuracy, Twilio network latency or a browser visual review.
The prototype's acoustic cold-start echo scenario remains a separate live
evaluation; the integrated echo regression pins text arbitration behavior.

## Live provider validation

`live_transport.py` runs the production Twilio transport, native AEC, real
Deepgram caller/agent TTS, Flux STT, and the configured semantic classifier.
It simulates Twilio's 20 ms playback buffer and 40 ms clear propagation;
it does not place a phone call or invoke the data-plane planner.

From `voice-gateway/`, with provider credentials in `.env`:

```sh
python tests/barge_baseline/live_transport.py --out /tmp/barge-live-new
```

Use a new output directory for each run. The six scenarios cover backchannels,
floor requests, protected deferral, safety breakthrough, echo-only playback,
and ambiguous speech sent to the classifier. TTS/STT setup failures fail the
run; classifier timeouts exercise the controller's bounded uncertainty path.
The harness asserts continuous STT delivery, outbound sample accounting,
clear latency, playback marks, blocked fillers/stale replies, and exact
agreement between the existing publisher's local JSONL and timeline events.
Redis publication is covered by the deterministic integration suite; live
runs pass `redis=None` and write only local call logs.

Each scenario includes `timeline.html`, the transport recording, actual
endpoint `audible_stereo.wav`, STT input, raw Flux messages, wire events,
and machine-readable checks. The transport recording estimates playback
from sent samples and trims cleared audio at clear dispatch; the actual
endpoint WAV includes simulated clear propagation. Neither is a recording
from a real telephone endpoint.

Validation on 2026-09-08: 335 regression tests passed, with two pre-existing
expected failures; all six live scenarios passed 61 checks. Live testing
found and fixed premature cuts on short echo partials and approximately
500 ms of cleared audio remaining in the timeline recording. Deepgram Clear
acknowledgements hit the existing 300 ms drain timeout on interrupted TTS;
Twilio clear was dispatched immediately and audible playback still stopped.
One classifier request timed out; a later revision returned YIELD successfully.
