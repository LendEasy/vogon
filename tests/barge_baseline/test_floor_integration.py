"""Lifecycle, physical cancellation and publication tests for the live controller."""
import array
import asyncio
import io
import itertools
import json
import subprocess
import wave
from unittest.mock import AsyncMock

import pytest

from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.audio.audio_timeline import build_timeline_html
from vogon.gateway.audio.event_publisher import AudioEventPublisher
from vogon.gateway.settings import settings
from vogon.gateway.transport.audio_event_log import AudioEventLog
from vogon.gateway.transport.floor_controller import FloorController
from vogon.gateway.transport.outbound_queue import enqueue_twilio
from vogon_contract import UtteranceFinal


@pytest.fixture(autouse=True)
def controller(baseline):
    baseline.t._floor = FloorController(baseline.t, baseline.clock.monotonic)


async def frames(h, count, level=3000):
    for _ in range(count):
        await h.frame(raw=level, clean=level)
        await asyncio.sleep(0)


async def yield_to_caller(h):
    await h.t.start()
    await h.t._stt.interim('wait')
    await frames(h, 3)
    assert h.t._floor.caller_floor


def test_short_echo_partial_waits_for_disambiguation(baseline, monkeypatch):
    h, t = baseline, baseline.t
    t._last_speak_text = 'Let me explain the available appointment options.'
    calls = []
    def judge(*args):
        calls.append(args)
        return 'YIELD'
    monkeypatch.setattr('vogon.gateway.transport.floor_controller.classify_floor', judge)

    async def scenario():
        await t.start()
        await frames(h, 3)
        await t._stt.interim('Let me')
        await frames(h, 65)
        assert not t._floor.caller_floor
        assert not calls
        await t._stt.interim('Let me explain the available')
        assert t._floor.current.echo
        assert not t._floor.caller_floor
        await t._floor.close()

    asyncio.run(scenario())


def test_short_matching_final_can_still_request_floor(baseline, monkeypatch):
    h, t = baseline, baseline.t
    t._last_speak_text = 'Let me explain the available appointment options.'
    monkeypatch.setattr('vogon.gateway.transport.floor_controller.classify_floor', lambda *args: 'YIELD')

    async def scenario():
        await t.start()
        await frames(h, 3)
        await t._stt.interim('Let me')
        await t._stt.final('Let me')
        assert t._floor.caller_floor
        await t._floor.close()

    asyncio.run(scenario())


def test_clear_removes_only_unplayed_recording_tail(baseline):
    h, t = baseline, baseline.t
    recorder = t._recorder = AudioRecorder(stores=[])
    recorder.set_stream_epoch(99)
    recorder.add_caller_chunk(99, h.mulaw(3000) * 100)
    recorder.add_agent_chunk(99, h.mulaw(4000) * 75)
    recorder.add_agent_chunk(100.5, h.mulaw(5000) * 25)
    asyncio.run(t._send_clear())  # clock=100; crosses the first agent chunk
    with wave.open(io.BytesIO(recorder.to_wav_bytes())) as f:
        samples = array.array('h', f.readframes(f.getnframes()))
    assert all(samples[0::2])  # caller unchanged
    assert all(samples[1:16000:2])  # elapsed agent preserved
    assert not any(samples[16001::2])  # queued tail discarded
    assert recorder.discard_agent_after(100) == 0  # duplicate clear is harmless


def test_floor_requires_eot_and_silence_and_rejects_old_generation(baseline):
    h, t = baseline, baseline.t

    async def scenario():
        generation = t.playback_generation()
        await yield_to_caller(h)
        await frames(h, 40, 0)
        assert t._floor.caller_floor  # silence alone is not EOT
        await t._stt.final('wait')
        assert not t._floor.caller_floor
        assert not await t._floor.allow_playback(generation)
        assert await t._floor.allow_playback(t.playback_generation())

    asyncio.run(scenario())


def test_current_response_waits_for_silence_and_resumed_speech_invalidates_eot(baseline):
    """Resumed SPEECH is words. Energy after the final only restarts the
    quiet timer; it never undoes the EOT (2026-09-30, call b59be90d — the
    old rule held a reply for ten seconds on a 220 ms burst)."""
    h, t = baseline, baseline.t

    async def scenario():
        await yield_to_caller(h)
        await t._stt.final('wait')
        task = asyncio.create_task(t._floor.allow_playback(t.playback_generation(), wait=True))
        await asyncio.sleep(0)
        assert not task.done()
        await frames(h, 3)
        await frames(h, 10, 0)
        assert not task.done()  # quiet timer restarted by the frames, not yet elapsed
        await t._stt.interim('I was still')
        await frames(h, 30, 0)
        assert not task.done()  # words: the EOT is undone, the floor stays the caller's
        await t._stt.final('I was still explaining')
        await frames(h, 30, 0)
        assert not await asyncio.wait_for(task, 1)  # and the reply to 'wait' is stale

    asyncio.run(scenario())


def test_energy_after_the_final_cannot_undo_the_eot(baseline):
    h, t = baseline, baseline.t

    async def scenario():
        await yield_to_caller(h)
        await t._stt.final('wait')
        task = asyncio.create_task(t._floor.allow_playback(t.playback_generation(), wait=True))
        await frames(h, 3)           # a breath inside the quiet window
        await frames(h, 30, 0)
        assert await asyncio.wait_for(task, 1)  # released on quiet, reply plays

    asyncio.run(scenario())


def test_protected_request_waits_for_playback_mark_not_send_completion(baseline):
    h, t = baseline, baseline.t
    t._protected_playout = True

    async def scenario():
        await t.start()
        await t._stt.interim('wait')
        await frames(h, 3)
        await t._stt.final('wait')
        assert not t._tts_cancel.is_set()
        t._tts_playing = False  # sender finished; caller is still hearing tail
        t._pending_marks = {'tts_end_response-1': (h.clock.value, 'protected')}
        task = asyncio.create_task(t._floor.allow_playback(t.playback_generation(), wait=True))
        await frames(h, 30, 0)
        assert not task.done()
        await h.mark('tts_end_response-1')
        assert await asyncio.wait_for(task, 1)
        assert 'floor_released' in h.names()

    asyncio.run(scenario())


@pytest.mark.parametrize('protected', [True, False])
def test_cooperative_backchannels_do_not_create_repeat_pressure(baseline, protected):
    h, t = baseline, baseline.t
    t._protected_playout = protected

    async def scenario():
        await t.start()
        for _ in range(4):
            await t._stt.interim('okay')
            await frames(h, 10)
            await t._stt.final('okay')
            await frames(h, 20, 0)
        assert not t._tts_cancel.is_set()
        assert all(e.failed_at is None for e in t._floor.episodes)
        assert len(t._floor.episodes) == 4

    asyncio.run(scenario())


def test_repeated_speech_never_overrides_protection(baseline):
    h, t = baseline, baseline.t
    t._protected_playout = True

    async def scenario():
        await t.start()
        await t._stt.interim('wait')
        await frames(h, 20)
        await frames(h, 20, 0)
        await t._stt.interim('the amount on that statement is wrong')
        await frames(h, 80)
        assert not t._tts_cancel.is_set()
        assert not t._floor.caller_floor

    asyncio.run(scenario())


def test_protected_defer_can_be_retracted_by_a_cooperative_final(baseline):
    h, t = baseline, baseline.t
    t._protected_playout = True

    async def scenario():
        await t.start()
        await t._stt.interim('wait')
        await frames(h, 3)
        assert t._floor.current.deferred
        await t._stt.final('okay')
        assert not t._floor.current.deferred
        assert t._floor.protection_released.is_set()
        assert not any(isinstance(e, UtteranceFinal) for e in h.events())
        t._tts_playing = False
        await t._floor.playback_finished()
        assert not t._floor.caller_floor

    asyncio.run(scenario())


def test_cancellation_wakes_a_stalled_tts_provider_and_clears_immediately(baseline):
    h, t = baseline, baseline.t
    ready = asyncio.Event()
    never = asyncio.Event()

    async def stream(sentences):
        yield 0, h.mulaw(1000), 0
        ready.set()
        await never.wait()

    t._tts.stream_segments = stream
    t._tts.cancel = AsyncMock()

    async def scenario():
        await t.start()
        playing = asyncio.create_task(t.send_response('This is a sufficiently long sentence.', 'response-2'))
        await asyncio.wait_for(ready.wait(), 1)
        h.clock.value += .2  # beyond playback-start grace
        await t._stt.interim('wait')
        await frames(h, 3)
        report = await asyncio.wait_for(playing, 1)
        assert report.status == 'truncated'
        assert t._tts.cancel.await_count == 1
        assert any(json.loads(c.args[0])['event'] == 'clear' for c in t._send_raw.await_args_list)
        assert t._pending_marks == {}

    asyncio.run(scenario())


def test_final_only_classifier_yield_reaches_the_data_plane(baseline, monkeypatch):
    h, t = baseline, baseline.t

    async def scenario():
        loop = asyncio.get_running_loop()
        def execute(*args):
            result = loop.create_future()
            result.set_result('YIELD')
            return result
        monkeypatch.setattr(loop, 'run_in_executor', execute)
        await t.start()
        await t._stt.final('my situation changed')
        assert t._tts_cancel.is_set()
        assert any(isinstance(e, UtteranceFinal) for e in h.events())

    asyncio.run(scenario())


def test_timeout_keeps_one_provider_call_while_audio_deadlines_continue(baseline, monkeypatch):
    h, t = baseline, baseline.t
    monkeypatch.setattr(settings, 'semantic_barge_llm_timeout_ms', 10)

    async def scenario():
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        calls = []
        def execute(*args):
            calls.append(args)
            return pending
        monkeypatch.setattr(loop, 'run_in_executor', execute)
        await t.start()
        await t._stt.interim('my situation changed')
        await frames(h, 3)
        await asyncio.sleep(.025)
        await t._stt.interim('the amount seems incorrect')
        await frames(h, 70)
        assert len(calls) == 1  # timed-out executor call is still physically running
        assert t._tts_cancel.is_set()  # persistence does not wait for it
        assert t._stt.send_audio.await_count == 73
        pending.set_result('CONTINUE')
        await t._floor.worker
        assert t._floor.caller_floor
        assert 'floor_result_rejected' in h.names()

    asyncio.run(scenario())


def test_resumed_caller_invalidates_a_final_already_in_the_event_queue(baseline):
    h, t = baseline, baseline.t
    t._tts_playing = False

    async def scenario():
        await t.start()
        await t._stt.interim('my situation changed')
        await frames(h, 3)
        await t._stt.final('my situation changed')
        event = h.events()[0]
        assert isinstance(event, UtteranceFinal)
        await t._stt.interim('and there is more')  # no acoustic gap required after EOT
        assert t._floor.caller_floor
        assert event.playback_generation != t.playback_generation()
        assert not await t._floor.allow_playback(event.playback_generation)

    asyncio.run(scenario())


def test_partial_revisions_do_not_multiply_attempts(baseline):
    h, t = baseline, baseline.t
    t._semantic_judge = False

    async def scenario():
        await t.start()
        for text in ('my', 'my situation', 'my situation changed', 'my situation changed'):
            await t._stt.interim(text)
            await frames(h, 3)
        assert len(t._floor.episodes) == 1
        assert t._floor.current.revision == 3
        assert not t._tts_cancel.is_set()
        await frames(h, 20, 0)
        await t._stt.interim('my situation changed recently')
        assert len(t._floor.episodes) == 1  # even a late revision after the gap

    asyncio.run(scenario())


def test_clear_removes_queued_media_and_marks_but_preserves_app_events():
    queue = asyncio.PriorityQueue()
    counter = itertools.count()
    for event in ('media', 'mark'):
        enqueue_twilio(queue, counter, json.dumps(dict(event=event)))
    queue.put_nowait((1, next(counter), ('text', json.dumps(dict(type='server.thinking')))))
    assert enqueue_twilio(queue, counter, json.dumps(dict(event='clear'))) == 2
    assert json.loads(queue.get_nowait()[2][1])['event'] == 'clear'
    assert json.loads(queue.get_nowait()[2][1])['type'] == 'server.thinking'
    assert queue.empty()


def test_audio_stream_is_separate_from_call_log_and_matches_timeline_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'recordings_path', str(tmp_path))
    redis = AsyncMock()
    redis.eval.return_value = 1
    redis.xlen.return_value = 2

    async def scenario():
        publisher = AudioEventPublisher('call-floor', redis)
        log = AudioEventLog(publisher.emit)
        log.emit(100, 'speech_episode_started', episode_id=1, reason='speech')
        log.emit(100.3, 'floor_decision', episode_id=1, revision=2, generation=1,
                 decision='YIELD', action='CUT', reason='repeated_speech')
        await publisher.close()
        log.flush(tmp_path/'call-floor.audio_events.json', 100, {'barge_controller_enabled': True})
        rows = [json.loads(c.args[1]['data']) for c in redis.xadd.await_args_list]
        assert [r['event_type'] for r in rows] == ['audio_event'] * 2
        assert [r['detail']['audio_seq'] for r in rows] == [1, 2]
        assert rows[1]['detail']['reason'] == 'repeated_speech'
        assert rows[1]['occurred_at'] == 100.3
        assert redis.xadd.await_args.args[0] == 'call:{call-floor}:audio'
        redis.eval.assert_not_awaited()
        assert not (tmp_path/'calls').exists()
        disk = [json.loads(line) for line in (tmp_path/'call-floor.audio_events.jsonl').read_text().splitlines()]
        assert disk == rows
        artifact = json.loads((tmp_path/'call-floor.audio_events.json').read_text())
        assert artifact['events'][1]['t'] == .3
        assert artifact['events'][1]['reason'] == rows[1]['detail']['reason']

    asyncio.run(scenario())


def test_suppressed_summary_keeps_finals_and_pending_partials_without_revisions():
    log = AudioEventLog()
    log.emit(100, 'speech_revision', episode_id=1, text='ok')
    log.emit(101, 'floor_decision', episode_id=1, action='KEEP_PLAYING', reason='cooperative_backchannel')
    log.emit(102, 'caller_transcript', episode_id=1, action='SUPPRESS_TURN', text='Okay.', reason='backchannel')
    log.emit(103, 'speech_revision', episode_id=2, text='yes')
    log.emit(104, 'speech_revision', episode_id=2, text='yes right')
    log.emit(105, 'floor_decision', episode_id=2, action='KEEP_PLAYING', reason='cooperative_backchannel')
    summary = log.suppressed_speech_summary()
    assert summary['final_count'] == summary['pending_count'] == 1
    assert [(e['text'], e['final']) for e in summary['utterances']] == [('Okay.', True), ('yes right', False)]
    assert log.suppressed_speech_summary() == summary  # repeatable teardown snapshot
    log.emit(106, 'floor_decision', episode_id=2, action='CUT', reason='persistent_speech')
    assert log.suppressed_speech_summary()['pending_count'] == 0
    assert log.suppressed_speech_summary()['final_count'] == 1


def test_allowed_revision_is_not_reported_as_suppressed():
    log = AudioEventLog()
    log.emit(100, 'speech_revision', episode_id=1, text='okay')
    log.emit(101, 'floor_decision', episode_id=1, action='KEEP_PLAYING', reason='cooperative_backchannel')
    log.emit(102, 'caller_transcript', episode_id=1, action='ALLOW_TURN', text='okay but wait')
    assert log.suppressed_speech_summary() == dict(final_count=0, pending_count=0, utterances=[])


def test_timeline_script_and_transcript_embedding(tmp_path):
    page = build_timeline_html('call', b'', None, [dict(t=.3, event='floor_decision',
        reason='repeated_speech', decision='YIELD', action='CUT',
        text='</script><script>alert(1)</script>')], {})
    assert page.count('</script>') == 1
    assert 'floor decisions' in page and 'event-filter' in page
    script = page.split('<script>', 1)[1].split('</script>', 1)[0]
    path = tmp_path/'timeline.js'
    path.write_text(script)
    subprocess.run(['node', '--check', str(path)], check=True, capture_output=True)
