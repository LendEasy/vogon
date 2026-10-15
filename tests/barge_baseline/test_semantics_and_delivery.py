"""Pin current decisions and emitted events, including behavior we plan to replace."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest

from vogon.gateway.transport import twilio_transport as tt
from vogon_contract import DrainCleared, PlayoutFinished, UtteranceAbsorbed, UtteranceFinal


@pytest.mark.parametrize('text,verdict', [
    ('okay', 'backchannel'),
    ('uh-huh', 'backchannel'),
    ('uh huh', 'timeout_absorb'),
    ('wait', 'floor_request'),
    ('stop calling me', 'safety'),
    ('why?', 'substantive_interrupt'),
    ('the amount on that statement is wrong', 'substantive_interrupt'),
    ('my situation changed', 'timeout_absorb'),
    ('', 'no_text'),
])
def test_heuristic_and_unavailable_judge_baseline(baseline, text, verdict):
    t = baseline.t
    t._last_interim_text = text
    # Disabled in the window; unavailable provider is exercised separately.
    t._semantic_judge = False
    asyncio.run(t._run_yield_window())
    absorb = verdict in ('backchannel', 'timeout_absorb', 'no_text')
    assert t._absorbing_utterance == absorb
    assert t._tts_cancel.is_set() == (not absorb)
    calls = t._audio_log.emit.call_args_list
    assert calls[-1].kwargs['verdict'] == verdict
    tt.classify_barge.assert_not_called()


@pytest.mark.parametrize('reply,expected', [
    ('backchannel', 'barge_absorbed'),
    ('substantive_interrupt', 'barge_cut'),
    ('answer_to_heard', 'barge_yield'),
    (None, 'barge_absorbed'),
    ('invalid', 'barge_absorbed'),
    (RuntimeError('provider unavailable'), 'barge_absorbed'),
])
def test_classifier_outcome_matrix(baseline, monkeypatch, reply, expected):
    t = baseline.t
    t._last_interim_text = 'my situation changed'
    async def scenario():
        loop = asyncio.get_running_loop()
        def execute(*args):
            future = loop.create_future()
            if isinstance(reply, Exception):
                future.set_exception(reply)
            else:
                future.set_result(reply)
            return future
        monkeypatch.setattr(loop, 'run_in_executor', execute)
        await t._run_yield_window()
        assert expected in baseline.names()
        assert t._tts_cancel.is_set() == (expected == 'barge_cut')
        assert t._graceful_yield == (expected == 'barge_yield')
    asyncio.run(scenario())


def test_changed_ambiguous_partial_does_not_invalidate_inflight_judgment(baseline, monkeypatch):
    t = baseline.t
    async def scenario():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        requests = []
        def execute(executor, fn, *args):
            requests.append(args)
            return future
        monkeypatch.setattr(loop, 'run_in_executor', execute)
        t._last_interim_text = 'my situation changed'
        task = asyncio.create_task(t._run_yield_window())
        await asyncio.sleep(0)
        t._last_interim_text = 'the amount seems incorrect'
        future.set_result('backchannel')
        await task
        assert len(requests) == 1 and requests[0][0] == 'my situation changed'
        assert t._absorb_detail['text'] == 'the amount seems incorrect'
        assert t._absorb_detail['verdict'] == 'backchannel'
        assert not t._tts_cancel.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize('absorb_all', [False, True], ids=['protected-defer', 'intro-absorb'])
@pytest.mark.parametrize('path', ['partial', 'final'])
@pytest.mark.parametrize('text,ordinary_outcome', [
    ('okay', 'barge_absorbed'),
    ('wait', 'barge_deferred_protected'),
    ('the amount on that statement is wrong', 'barge_deferred_protected'),
    ('my situation changed', 'barge_deferred_protected'),
    ('stop calling me', 'barge_cut'),
])
def test_protection_matrix(baseline, absorb_all, path, text, ordinary_outcome):
    t = baseline.t
    t._protected_playout = True
    t._absorb_playout = absorb_all
    t._last_interim_text = text
    expected = 'barge_absorbed' if absorb_all and text != 'stop calling me' else ordinary_outcome
    asyncio.run(t._run_protected_window() if path == 'partial' else t._judge_playback_final(text))
    assert expected in baseline.names()
    assert t._tts_cancel.is_set() == (expected == 'barge_cut')
    assert t._absorbing_utterance == (expected == 'barge_absorbed')
    tt.classify_barge.assert_not_called()


@pytest.mark.parametrize('path', ['partial', 'final'])
def test_echo_guard_differs_between_partial_and_final(baseline, path):
    t = baseline.t
    text = 'Your account has an outstanding balance'
    t._last_interim_text = text
    asyncio.run(t._run_yield_window() if path == 'partial' else t._judge_playback_final(text))
    # Transcript arbitration only: this does not claim an acoustic AEC failure.
    assert t._tts_cancel.is_set() == (path == 'partial')
    if path == 'final':
        assert t._absorb_detail['verdict'] == 'echo'


@pytest.mark.parametrize('initial,final,event_type,cancel', [
    ('okay', 'okay', UtteranceAbsorbed, False),
    ('okay', 'okay but the amount on that statement is wrong', UtteranceFinal, False),
    ('okay', 'stop calling me', UtteranceFinal, False),
    (None, 'wait', UtteranceFinal, True),
    (None, 'Your account has an outstanding balance', UtteranceAbsorbed, False),
])
def test_real_stt_callbacks_after_early_absorb(baseline, initial, final, event_type, cancel):
    t = baseline.t
    async def scenario():
        await t.start()
        if initial:
            await t._stt.interim(initial)
            await t._run_yield_window()
            await t._stt.interim(final)
            assert not t._tts_cancel.is_set()
        await t._stt.final(final)
        events = baseline.events()
        assert len(events) == 1 and isinstance(events[0], event_type)
        assert t._tts_cancel.is_set() == cancel
        assert not t._absorbing_utterance
    asyncio.run(scenario())


def test_repeated_unresolved_turns_do_not_escalate_and_last_carries_forward(baseline):
    t = baseline.t
    t._semantic_judge = False
    async def scenario():
        await t.start()
        for text in ('my situation changed', 'the amount seems incorrect', 'I need assistance'):
            await t._stt.interim(text)
            await t._run_yield_window()
            await t._stt.final(text)
            assert isinstance(baseline.events()[0], UtteranceAbsorbed)
            assert not t._tts_cancel.is_set()
        await t._stt.final('wait')
        event = baseline.events()[0]
        assert isinstance(event, UtteranceFinal)
        assert event.carry_forward.text == 'I need assistance'
        assert t._recent_absorbed is None
    asyncio.run(scenario())


@pytest.mark.parametrize('pacing,expected', [('sentence', 'barge_yield'), ('burst', 'barge_cut')])
def test_answer_to_heard_depends_on_pacing(baseline, monkeypatch, pacing, expected):
    monkeypatch.setattr(tt.settings, 'tts_pacing_mode', pacing)
    t = baseline.t
    asyncio.run(t._act_on_barge_verdict('answer_to_heard', 'test', 'Friday works', 100))
    assert expected in baseline.names()
    assert t._tts_cancel.is_set() == (pacing == 'burst')
    assert t._graceful_yield == (pacing == 'sentence')


def test_drain_cut_clears_and_late_marks_cannot_credit_sentences(baseline):
    h, t = baseline, baseline.t
    t._tts_playing = False
    t._current_sentences = ['First sentence.', 'Second sentence.']
    t._pending_marks = {'tts_s0_response-1': (100, 'First sentence.'),
                        'tts_end_response-1': (100, 'all')}
    t._delivery_ledger = [dict(audio_id='response-1', text='all', start_t=99, end_t=None)]
    async def scenario():
        await t._cut_playback()
        sent = [json.loads(c.args[0]) for c in t._send_raw.await_args_list]
        assert sent == [{'event': 'clear', 'streamSid': 'MZ-baseline'}]
        events = h.events()
        assert any(isinstance(e, DrainCleared) and e.audio_id == 'response-1' for e in events)
        assert any(isinstance(e, PlayoutFinished) for e in events)
        assert not t._playback_draining
        await h.mark('tts_s0_response-1')
        await h.mark('tts_end_response-1')
        assert t._sentences_played == 0
        assert h.events() == []
    asyncio.run(scenario())


def test_marks_advance_heard_context_and_only_end_mark_closes_ledger(baseline):
    h, t = baseline, baseline.t
    t._current_sentences = ['First sentence.', 'Second sentence.']
    t._pending_marks = {'tts_s0_response-1': (100, 'First sentence.'),
                        'tts_end_response-1': (100, 'all')}
    entry = dict(audio_id='response-1', text='all', start_t=99, end_t=None)
    t._delivery_ledger = [entry]
    async def scenario():
        assert t._playout_heard_split() == ('', 'First sentence. Second sentence.')
        await h.mark('tts_s0_response-1')
        await h.mark('tts_s0_response-1')  # duplicate mark is ignored
        assert t._playout_heard_split() == ('First sentence.', 'Second sentence.')
        assert entry['end_t'] is None and h.events() == []
        await h.mark('tts_end_response-1')
        assert entry['end_t'] == 100
        assert isinstance(h.events()[0], PlayoutFinished)
    asyncio.run(scenario())


@pytest.mark.parametrize('method', ['_send_media_frame', '_send_ambience_frame'])
def test_aec_reference_is_identical_to_outgoing_wire_audio(baseline, method):
    t = baseline.t
    frame = bytes(range(160))
    asyncio.run(getattr(t, method)(frame))
    t._aec.feed_reference.assert_called_once_with(frame)
    wire = json.loads(t._send_raw.await_args.args[0])
    assert base64.b64decode(wire['media']['payload']) == frame


@pytest.mark.parametrize('action', ['complete', 'cut', 'yield'])
def test_real_sentence_player_delivery_and_cancellation(baseline, action):
    h, t = baseline, baseline.t
    first, second = h.mulaw(1000), h.mulaw(2000)
    sentences_text = ['This is the first complete sentence.', 'This is the second complete sentence.']
    async def segments(sentences):
        assert sentences == sentences_text
        yield 0, first, 0
        if action == 'cut':
            await t._cut_playback()
        elif action == 'yield':
            await t._act_on_barge_verdict('answer_to_heard', 'test', 'Friday works', 100)
        yield 1, second, None
    t._tts.stream_segments = segments
    t._tts.cancel = AsyncMock()
    report = asyncio.run(t.send_response(' '.join(sentences_text), 'response-2'))
    wire = [json.loads(c.args[0]) for c in t._send_raw.await_args_list]
    media = [base64.b64decode(m['media']['payload']) for m in wire if m['event'] == 'media']
    assert media == ([first, second] if action == 'complete' else [first])
    assert report.status == ('complete' if action == 'complete' else 'truncated')
    assert report.sentences_sent == {'complete': 2, 'cut': 0, 'yield': 1}[action]
    assert report.mark_pending == (action != 'cut')
    assert sum(m['event'] == 'clear' for m in wire) == int(action == 'cut')
    assert t._tts.cancel.await_count == int(action != 'complete')
    assert t._playback_draining == (action != 'cut')
    assert t._sentences_played == 0  # sent sentences have not been mark-confirmed


def test_transport_accepts_new_response_after_cut_without_waiting_for_eot(baseline):
    """Transport-level baseline only; handler/data-plane may impose other guards."""
    h, t = baseline, baseline.t
    async def segments(sentences):
        assert not t._tts_cancel.is_set()
        yield 0, h.mulaw(1000), 0
    t._tts.stream_segments = segments
    async def scenario():
        await t._cut_playback()
        assert t._tts_cancel.is_set()
        report = await t.send_response('New response.', 'response-2')
        assert report.status == 'complete' and report.chunks_sent == 1
        assert not t._tts_cancel.is_set()
    asyncio.run(scenario())
