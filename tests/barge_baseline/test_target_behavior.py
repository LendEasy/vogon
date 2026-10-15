"""Former expected failures, now ordinary regressions for the integrated controller.

The gap IDs preserve the original before/after rationale. Every test drives
the real transport; none imports the isolated barge_sim prototype.
"""
import asyncio
import json

import pytest

from vogon.gateway.transport.floor_controller import FloorController
from vogon_contract import UtteranceFinal


@pytest.fixture(autouse=True)
def integrated_controller(baseline):
    baseline.t._floor = FloorController(baseline.t, baseline.clock.monotonic)


class MissingBehavior(AssertionError):
    pass


def require(condition, explanation):
    if not condition:
        raise MissingBehavior(explanation)


def gap(reason):
    """Retain the original gap ID as documentation; these are ordinary tests."""
    def annotate(fn):
        fn.__doc__ = reason
        return fn
    return annotate


def judge_reply(monkeypatch, decision):
    """Deterministic executor boundary; the transport still owns arbitration."""
    loop = asyncio.get_running_loop()

    def execute(*args):
        future = loop.create_future()
        future.set_result(decision)
        return future

    monkeypatch.setattr(loop, 'run_in_executor', execute)


async def frames(h, count, level=3000):
    for _ in range(count):
        await h.frame(raw=level, clean=level)
        await asyncio.sleep(0)


async def settle_window(t):
    if t._yield_task is not None:
        await t._yield_task


@gap('GAP-01: early absorption prevents a growing correction reopening the decision')
def test_growing_correction_yields_before_final(baseline):
    h, t = baseline, baseline.t

    async def scenario():
        await t.start()
        await t._stt.interim('okay')
        await frames(h, 3)
        await settle_window(t)
        assert not t._tts_cancel.is_set()  # short acknowledgment remains cooperative
        await t._stt.interim('okay but the amount on that statement is wrong')
        await frames(h, 15)
        await settle_window(t)
        assert h.events() == []  # no final or EOT has been supplied
        require(t._tts_cancel.is_set(), 'A decisive correction must reopen the provisional continue before EOT')

    asyncio.run(scenario())


@pytest.mark.parametrize('decision', ['CONTINUE', 'UNCERTAIN'], ids=['wrong-continue', 'uncertain'])
@gap('GAP-02: sustained speech has no deterministic override of CONTINUE or uncertainty')
def test_persistent_speech_yields_within_bounded_audio_budget(baseline, monkeypatch, decision):
    h, t = baseline, baseline.t

    async def scenario():
        judge_reply(monkeypatch, decision)
        await t.start()
        await t._stt.interim('my situation changed')
        # 1.4 s of positive frames; prototype persistence is 1.2 s. The extra
        # 200 ms accommodates gate qualification, not an STT-final dependency.
        await frames(h, 70)
        await settle_window(t)
        assert t._stt.send_audio.await_count == 70
        assert h.events() == []
        require(t._tts_cancel.is_set(), 'Continuous caller speech must yield within 1.4 s despite the judge')

    asyncio.run(scenario())


@gap('GAP-03: an unsuccessful attempt does not lower the threshold for a distinct next attempt')
def test_second_attempt_yields_without_waiting_for_new_text(baseline, monkeypatch):
    h, t = baseline, baseline.t

    async def scenario():
        judge_reply(monkeypatch, None)
        await t.start()
        await t._stt.interim('my situation changed')
        await frames(h, 15)  # first attempt: 300 ms, too short for persistence
        await settle_window(t)
        await t._stt.final('my situation changed')
        h.events()
        assert not t._tts_cancel.is_set()
        await frames(h, 20, level=0)  # 400 ms gap: exceeds prototype episode gap
        # No partial supplied for the second attempt: repeat evidence is audio.
        await frames(h, 15)  # 300 ms allows the prototype's 240 ms repeat budget
        require(t._tts_cancel.is_set(), 'A second speech episode after unresolved speech must yield promptly')
        await settle_window(t)

    asyncio.run(scenario())


@gap('GAP-04: a verdict for an obsolete partial can still cancel current playback')
def test_stale_partial_verdict_cannot_cut(baseline, monkeypatch):
    t = baseline.t

    async def scenario():
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        requested = []
        request_started = asyncio.Event()

        def execute(executor, fn, *args):
            requested.append(args[0])
            request_started.set()
            if len(requested) == 1:
                return pending
            current = loop.create_future()
            current.set_result('CONTINUE')
            return current

        monkeypatch.setattr(loop, 'run_in_executor', execute)
        await t.start()
        await t._stt.interim('the amount seems incorrect')
        await frames(baseline, 3)
        task = t._floor.worker
        await asyncio.wait_for(request_started.wait(), timeout=1)
        assert requested == ['the amount seems incorrect']
        # Both texts are in the ambiguous band, so a decisive heuristic cannot
        # hide whether the stale result was applied.
        await t._stt.interim('that sounds reasonable actually')
        pending.set_result('YIELD')
        await task
        require(not t._tts_cancel.is_set(), 'A cut verdict for the superseded partial must be rejected')

    asyncio.run(scenario())


@gap('GAP-05: the final echo guard does not protect partial-driven cuts')
def test_agent_echo_partial_does_not_cut_before_final_guard(baseline):
    h, t = baseline, baseline.t

    async def scenario():
        await t.start()
        await t._stt.interim('Your account has an outstanding balance')
        await frames(h, 3)
        await settle_window(t)
        assert t._stt.send_audio.await_count == 3
        require(not t._tts_cancel.is_set(), 'An agent-only echo transcript must not cut before the final arrives')

    asyncio.run(scenario())


@pytest.mark.parametrize('protection', ['ordinary', 'protected-defer', 'intro-absorb'])
@gap('GAP-06: an early absorb prevents a later safety final from executing a breakthrough cut')
def test_safety_final_breaks_through_prior_absorption(baseline, protection):
    h, t = baseline, baseline.t
    t._protected_playout = protection != 'ordinary'
    t._absorb_playout = protection == 'intro-absorb'

    async def scenario():
        await t.start()
        await t._stt.interim('okay')
        await frames(h, 3)
        await settle_window(t)
        assert not t._tts_cancel.is_set()
        await t._stt.final('stop calling me')
        events = h.events()
        require(
            t._tts_cancel.is_set() and any(
                isinstance(e, UtteranceFinal) and e.text == 'stop calling me' for e in events
            ),
            'Configured safety breakthrough must both cut playback and reach the next turn after an early absorb',
        )

    asyncio.run(scenario())


@pytest.mark.parametrize('audio_id', ['response-2', 'silence-probe-1'], ids=['response', 'silence-filler'])
@gap('GAP-07: transport playback has no caller-floor guard between a yield and caller EOT')
def test_new_playback_is_held_while_caller_owns_floor(baseline, audio_id):
    h, t = baseline, baseline.t

    async def segments(sentences):
        yield 0, h.mulaw(1000), 0

    t._tts.stream_segments = segments

    async def scenario():
        await t.start()
        await t._stt.interim('wait')
        await frames(h, 3)
        await settle_window(t)
        assert t._tts_cancel.is_set()  # establish a real semantic yield
        await t._stt.interim('I am still explaining')
        await frames(h, 3)
        await t.send_response('Please let me explain the available options.', audio_id)
        wire = [json.loads(c.args[0]) for c in t._send_raw.await_args_list]
        require(not any(m['event'] == 'media' for m in wire),
                'Neither a response nor a silence filler may restart speech before caller EOT and silence')

    asyncio.run(scenario())
