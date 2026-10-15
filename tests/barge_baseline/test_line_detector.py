"""The line detector behind the AEC and the floor controller
(docs/VOICEMAIL_DETECTION_DESIGN.md §4, §5, §6, §13), on the offline
transport: fake STT, fake instrument, fake clock. What it pins:

  1. a machine verdict cuts the opening through the break-through path and
     queues the LineVerdict fact BEFORE the transcript, which is not absorbed
  2. a verdict that lands after the final re-issues the transcript
  3. a human verdict is the fact alone; the absorb policy runs unchanged
  4. "Hello?" never asks; an echo-marked episode never asks; a second
     episode never binds; the detector is closed after one verdict
  5. an instrument failure is `unknown` with source `none`, never a guess
  6. the hold releases on quiet after the final, on the cap, or on release
  7. no line_detection on the CallConfig = no detector at all
"""
import asyncio
from unittest.mock import AsyncMock

from vogon.gateway.transport.delivery import DeliveryReport
from vogon.gateway.transport.floor_controller import FloorController
from vogon.gateway.transport.line_detector import LineDetector
from vogon_contract import LineDetection, LineVerdict, UtteranceFinal

GREETING = "Hi, you've reached Alex Morgan. I can't take your call right now, so leave a message."
OPENING = "Hi, this is Maya calling from LendEasy Finance on a recorded line. May I speak with Alex Morgan?"


def _detector(h, answers, **cfg):
    """answers: text -> probability (or an exception to raise)."""
    t = h.t
    t._floor = FloorController(t, h.clock.monotonic)
    t._last_speak_text = OPENING
    t._protected_playout = True
    t._absorb_playout = True
    asked = []

    async def ask(state):
        asked.append(state)
        a = answers.get(state["first_turn"], 0.5)
        if isinstance(a, Exception):
            raise a
        return a

    line = LineDetector(t, LineDetection(enabled=True, **cfg), now=h.clock.monotonic,
                        ask=ask, sleep=h.clock.sleep)
    line.asked_states = asked
    t._line = line
    return line


async def _settle(n=6):
    for _ in range(n):
        await asyncio.sleep(0)


def _queued(t):
    out = []
    while not t._utterance_queue.empty():
        out.append(t._utterance_queue.get_nowait())
    return out


# ── 1. machine: cut, fact first, transcript not absorbed ───────────────────

def test_a_machine_verdict_cuts_the_opening_and_queues_fact_then_transcript(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {"Hi, you've reached Alex": 0.35, GREETING: 0.92})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim("Hi, you've reached Alex")
        await _settle()
        assert line.verdict is None and line.asks == 1
        await t._stt.interim(GREETING)
        await _settle()
        assert line.verdict is not None and line.verdict.answered_by == "machine"
        assert t._floor.caller_floor, "the machine verdict must take the floor"
        assert t._tts_cancel.is_set(), "the opening must be cut"
        assert not t._absorbing_utterance
        await t._stt.final(GREETING)
        await _settle()
        items = _queued(t)
        kinds = [type(i).__name__ for i in items]
        assert kinds == ["LineVerdict", "UtteranceFinal"], kinds
        assert items[0].transcript == GREETING and items[0].source == "jev"
        assert items[1].text == GREETING
        assert line.closed
        await t._floor.close()

    asyncio.run(scenario())


# ── 2. the verdict lands after the final ────────────────────────────────────

def test_a_verdict_after_the_final_reissues_the_transcript(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: 0.9})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.final(GREETING)           # one shot: the final IS the first text
        await _settle()
        items = _queued(t)
        kinds = [type(i).__name__ for i in items]
        # The final went by under the opening (absorbed) before the answer;
        # the detector re-issues it behind the fact.
        assert kinds[-2:] == ["LineVerdict", "UtteranceFinal"], kinds
        assert items[-1].text == GREETING
        assert items[-1].playback_generation == t.playback_generation()
        assert line.verdict.answered_by == "machine"
        await t._floor.close()

    asyncio.run(scenario())


# ── 3. human: the fact alone ────────────────────────────────────────────────

def test_a_human_verdict_is_the_fact_alone_and_absorbs_as_today(baseline):
    h, t = baseline, baseline.t
    text = "Hello? Who is this calling please?"
    line = _detector(h, {text: 0.04})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim(text)
        await _settle()
        assert line.verdict is None                  # a human is only decided on the final
        await t._stt.final(text)
        await _settle()
        items = _queued(t)
        assert [type(i).__name__ for i in items if isinstance(i, LineVerdict)] == ["LineVerdict"]
        assert line.verdict.answered_by == "human"
        assert not t._tts_cancel.is_set() and not t._floor.caller_floor
        assert not any(isinstance(i, UtteranceFinal) for i in items), "absorbed, as today"
        await t._floor.close()

    asyncio.run(scenario())


# ── 4. what never asks, what never binds ────────────────────────────────────

def test_hello_never_asks_and_the_window_retires_to_unknown_none(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {}, ask_window_s=2.0)

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim("Hello?")
        await t._stt.final("Hello?")
        await _settle()
        assert line.asks == 0 and line.verdict is None
        line.note_playout_end()                      # the opening finished
        h.clock.value += 2.1
        await line.tick()
        assert line.verdict.answered_by == "unknown" and line.verdict.source == "none"
        assert isinstance(_queued(t)[-1], LineVerdict)
        await t._floor.close()

    asyncio.run(scenario())


def test_our_own_echo_never_asks(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim("Hi, this is Maya calling from LendEasy Finance on a")
        await _settle()
        assert t._floor.current.echo
        assert line.asks == 0 and not line.bound
        await t._floor.close()

    asyncio.run(scenario())


def test_one_verdict_per_call_and_a_second_episode_never_binds(baseline):
    h, t = baseline, baseline.t
    first = "Alex isn't here right now, can I take a message?"
    line = _detector(h, {first: 0.05, GREETING: 0.99})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.final(first)
        await _settle()
        assert line.verdict.answered_by == "human" and line.closed
        await t._stt.interim(GREETING)
        await t._stt.final(GREETING)
        await _settle()
        assert line.asks == 1
        assert sum(isinstance(i, LineVerdict) for i in _queued(t)) == 1
        await t._floor.close()

    asyncio.run(scenario())


# ── 5. the instrument fails ─────────────────────────────────────────────────

def test_an_instrument_failure_is_unknown_with_source_none(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: RuntimeError("decisions endpoint down")})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.final(GREETING)
        await _settle()
        v = line.verdict
        assert v.answered_by == "unknown" and v.source == "none" and v.probability == 0.0
        assert not t._tts_cancel.is_set()
        await t._floor.close()

    asyncio.run(scenario())


# ── 6. the hold ─────────────────────────────────────────────────────────────

def test_the_hold_releases_on_quiet_after_the_final(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: 0.95}, quiet_ms=700, cap_s=8.0)

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim(GREETING)
        await _settle()
        assert line.verdict.answered_by == "machine"
        t0 = h.clock.value
        await t._stt.final(GREETING)
        await _settle()
        t._floor.last_speech = h.clock.value
        why = await line.wait_quiet()
        assert why == "quiet"
        assert 0.7 <= h.clock.value - t0 < 1.0
        await t._floor.close()

    asyncio.run(scenario())


def test_the_hold_caps_when_the_line_never_goes_quiet(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: 0.95}, quiet_ms=700, cap_s=8.0)

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim(GREETING)
        await _settle()
        t0 = h.clock.value
        why = await line.wait_quiet()                 # no final ever: a menu that talks on
        assert why == "cap" and 8.0 <= h.clock.value - t0 < 8.2
        await t._floor.close()

    asyncio.run(scenario())


def test_the_hold_releases_on_a_tone_event(baseline):
    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: 0.95})

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim(GREETING)
        await _settle()
        line.release_hold()
        assert await line.wait_quiet() == "released"
        await t._floor.close()

    asyncio.run(scenario())


# ── 7. off by absence ───────────────────────────────────────────────────────

def test_no_line_detection_on_the_call_config_means_no_detector(baseline):
    assert baseline.t._line is None


# ── 8. the message must not wait for a floor a beep keeps (production, 2026-09-24) ──

def test_a_beep_after_the_final_must_not_block_the_voicemail_message(baseline):
    """Call 4108c2eb: machine at 0.84, opening cut, final at +8 s, the turn
    service composed the message within 60 ms — and nothing played for the
    50 s until the carrier hung up. The playout waited on the floor
    controller's release, which needs an end-of-turn followed by 500 ms of
    quiet; the box's BEEP after the final reset the end-of-turn flag and no
    words ever set it again. A held playout to a machine owns the floor."""

    h, t = baseline, baseline.t
    line = _detector(h, {GREETING: 0.95}, quiet_ms=700, cap_s=8.0)
    t._send_response = AsyncMock(return_value=DeliveryReport(
        audio_id="vm-1", status="complete", chunks_sent=10, audio_ms=200,
        est_total_ms=200, mark_pending=False))

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim(GREETING)
        await _settle()
        assert line.verdict.answered_by == "machine" and t._floor.caller_floor
        await frames(h, 5)                           # the greeting's own energy
        await t._stt.final(GREETING)
        await _settle()
        assert t._floor.final_seen and t._floor.caller_floor
        await frames(h, 30)                          # the beep: energy, no words
        # 2026-09-30 (call b59be90d): energy no longer undoes the EOT, so the
        # beep cannot strand the floor the way it did on 4108c2eb — the
        # ordinary guarded path releases on quiet too. The held playout
        # stays the right door for a machine: it takes the floor outright.
        assert t._floor.caller_floor and t._floor.final_seen
        h.clock.value += 1.0
        assert await t._floor.allow_playback(t.playback_generation(), wait=True)
        t._send_response.assert_not_awaited()
        await asyncio.wait_for(t.send_held_response(
            "Hello, this message is for Alex Morgan.", "vm-1"), 2.0)
        t._send_response.assert_awaited_once()
        assert t._send_response.await_args.args[2] is True    # protected
        assert t._send_response.await_args.args[3] is True    # absorb-all
        assert not t._floor.caller_floor
        await t._floor.close()

    asyncio.run(scenario())


async def frames(h, count, level=3000):
    for _ in range(count):
        await h.frame(raw=level, clean=level)
        await asyncio.sleep(0)
