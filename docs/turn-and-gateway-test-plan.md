# Test plan — turn service and voice gateway

Scope decided 2026-07-31. This plan replaces "run the whole simulate and eval
suite" as the gate for changes to the call loop. Those suites have 900+ tests
and **none of them enter `handle_call` or `app/turns/api.py`** — they start at
the planner. Running them proves the agents still work; it proves nothing
about the loop that every live call goes through.

## Terms

- **Driver** — the gateway-side loop in `vogon/websocket/handler.py`: reads
  WebSocket frames, spools facts, builds a request, plays the response.
- **Turn service** — `app/turns/`: decides what the call should do and say.
- **Trigger** — the thing that starts one `process_events` call: a caller
  utterance, a silence timer, or the call-length timer.
- **Eager transcript** — the speech-to-text guess emitted before the caller has
  clearly stopped talking. Work starts on it speculatively.
- **Final transcript** — the committed version. It either matches the eager
  guess (the speculative work is reused) or replaces it (the work is thrown
  away).
- **Barge-in** — the caller starts speaking while the agent is talking.

## What is in scope

Five areas. Every one is a point where the driver and the turn service meet,
or where two things can happen at once.

### 1. One ordinary turn

The caller speaks, the agent replies. Proves the request is assembled, the
single `process_events` call runs, the response is played, and state is
committed.

Covered by: `tests/test_handler_harness.py::TestOneFullTurn`.

### 2. Timers fire while a turn is in progress

**This is the behaviour that changed and had no test.** The driver must not
hold a lock across `process_events`, or a silence probe and the call-length
limit would wait for an agent turn to finish.

Must prove: with a turn deliberately made slow, a `SilenceThreshold` arriving
mid-turn is still processed, and a `DurationThreshold` still ends the call.

New: `tests/test_turn_loop_concurrency.py`.

### 3. Eager-then-final race, including speech with gaps

The caller pauses mid-sentence, so speech-to-text emits an eager guess, then a
different final. Must prove:

- eager text == final text → the speculative work is reused, the planner runs
  once;
- eager text != final text → the speculative work is discarded and the turn
  re-runs on the final;
- a second eager arriving before the final cancels the first cleanly;
- a `UtteranceResumed` (caller kept talking) discards the speculation.

Partly covered by `tests/test_handler_harness.py::TestSpeculativeEagerReuse`.
Extended in `tests/test_turn_loop_concurrency.py`.

### 4. Barge-in

The caller interrupts. Two separate things to prove, at two levels:

- **Gate level** (already covered, `voice-gateway/tests/test_barge_gate.py`): loud audio
  confirms a barge, quiet does not, the debounce and grace window behave.
- **Turn level** (new): an absorbed barge (the gateway judged it a
  backchannel like "mm-hmm") must **not** drive a turn — no planner call, no
  state change — and must be logged. A real barge must cut the audio and let
  the next utterance run normally.

New: `tests/test_turn_loop_concurrency.py::TestBargeAtTheTurnBoundary`.

### 5. Gateway connection

Proves the audio half can actually talk to its provider — no mocks.
`voice-gateway/tests/test_stt_connection.py` opens a real Deepgram session. It needs
`DEEPGRAM_API_KEY` and is run on demand, not in the normal suite.

## What is explicitly out of scope

| Suite | Tests | Why excluded |
|---|---|---|
| `tests/simulate_*.py` | ~740 | Enters at the planner; never reaches the driver or the turn API. Run it when agent behaviour changes, not when the loop changes. |
| Collections eval suite | 184 cases | Same reason, plus it costs money per run. |
| Everything else in `tests/` | ~700 | Unit tests for pieces that this refactor did not touch. |

Run those when the thing they cover changes. Not as a reflex.

## The command

```bash
cd data-plane
PYTHONPATH=. python3 -m pytest \
  tests/test_handler_harness.py \
  tests/test_turn_loop_concurrency.py \
  tests/test_call_audit_gateway_repros.py \
  tests/test_gateway_contract.py \
  ../voice-gateway/tests/test_barge_gate.py \
  tests/test_barge_semantics.py \
  tests/test_stt_eager_eot.py \
  tests/test_voice_pillar_imports.py \
  -q -p no:randomly > /tmp/loop.log 2>&1; tail -3 /tmp/loop.log
```

Live connection check, separately, when the audio path changes:

```bash
PYTHONPATH=. python3 ../voice-gateway/tests/test_stt_connection.py
```

## What no test covers

Stated so it is not mistaken for covered:

- Real audio timing — echo cancellation, jitter, actual TTS streaming.
- Redis under concurrent load.
- The widget receiving frames at end of turn instead of during it (the
  2026-07-31 change). The frame contents are asserted; the user-visible timing
  is not.
- Anything about how the code reaches the production server.

These need one instrumented phone call, not more unit tests.
