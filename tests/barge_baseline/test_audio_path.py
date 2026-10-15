"""Actual media entry point: routing, candidate detection and async arbitration."""
import asyncio

import pytest

from vogon.gateway.transport import twilio_transport as tt
from vogon.gateway.transport.barge_gate import BargeGate, BargeGateConfig


@pytest.mark.parametrize('phase', ['playing', 'draining', 'cooldown', 'silent'])
@pytest.mark.parametrize('aec', [True, False], ids=['aec', 'no-aec'])
@pytest.mark.parametrize('feed', ['open', 'gated'])
def test_audio_routing_matrix(baseline, monkeypatch, phase, aec, feed):
    h, t = baseline, baseline.t
    monkeypatch.setattr(tt.settings, 'stt_feed_mode', feed)
    t._tts_playing = phase == 'playing'
    if phase == 'draining':
        t._pending_marks = {'tts_end_response-1': (100, 'text')}
    if phase == 'cooldown':
        t._tts_ended_at = 100
    processor = t._aec
    if not aec:
        t._aec = None
    asyncio.run(h.frame(raw=3000, clean=100 if aec else None))
    active = phase != 'silent'
    assert processor.process.call_count == int(aec and active)
    expected = [h.mulaw(3000)] if not active else ([h.mulaw(100)] if aec and feed == 'open' else [])
    assert [c.args[0] for c in t._stt.send_audio.await_args_list] == expected
    assert t._barge_in_attempts == int(active and not aec)
    assert not t._tts_cancel.is_set()


def test_gate_uses_clean_rms_not_loud_raw_echo(baseline):
    async def scenario():
        for _ in range(10):
            await baseline.frame(raw=12000, clean=100)
        assert baseline.t._barge_in_attempts == 0
        assert baseline.t._stt.send_audio.await_count == 10
    asyncio.run(scenario())


@pytest.mark.parametrize('speech', [True, False], ids=['vad-confirms', 'vad-aborts'])
def test_local_vad_confirmation_keeps_open_stt_feed(baseline, monkeypatch, speech):
    t = baseline.t
    monkeypatch.setattr(tt.settings, 'semantic_barge_enabled', False)
    t._barge_gate = BargeGate(BargeGateConfig(confirm='local_vad'), confirm_frame=lambda frame: speech)
    async def scenario():
        for _ in range(25):
            await baseline.frame()
        assert t._stt.send_audio.await_count == 25
        assert t._tts_cancel.is_set() == speech
        assert ('barge_confirmed' if speech else 'barge_abort') in baseline.names()
    asyncio.run(scenario())


@pytest.mark.parametrize('adaptive', [False, True], ids=['fixed', 'adaptive'])
def test_quiet_line_adaptation_changes_candidate_threshold(baseline, monkeypatch, adaptive):
    t = baseline.t
    monkeypatch.setattr(tt.settings, 'semantic_barge_enabled', False)
    t._barge_gate = BargeGate(BargeGateConfig(adaptive_enabled=adaptive))
    async def scenario():
        for _ in range(50):
            await baseline.frame(clean=50)
        assert t._barge_gate.threshold == (600 if adaptive else 1200)
        for _ in range(3):
            await baseline.frame(clean=900)
        assert t._tts_cancel.is_set() == adaptive
        assert t._stt.send_audio.await_count == 53
    asyncio.run(scenario())


@pytest.mark.parametrize('levels,expected', [
    ([3000, 3000], 0),
    ([3000, 3000, 3000], 1),
    ([3000, 100, 3000, 3000], 0),
    ([3000] * 80, 1),
], ids=['short-burst', 'three-frames', 'silence-resets-streak', 'persistent-is-one-confirmation'])
def test_debounce_and_latching(baseline, monkeypatch, levels, expected):
    monkeypatch.setattr(tt.settings, 'semantic_barge_enabled', False)
    async def scenario():
        for level in levels:
            await baseline.frame(clean=level)
        assert baseline.t._barge_in_attempts == expected
        assert baseline.t._tts_cancel.is_set() == bool(expected)
    asyncio.run(scenario())


def test_start_grace_delays_mature_streak(baseline, monkeypatch):
    monkeypatch.setattr(tt.settings, 'semantic_barge_enabled', False)
    baseline.t._barge_gate.on_tts_start(100)
    async def scenario():
        for _ in range(4):
            await baseline.frame()
        assert not baseline.t._tts_cancel.is_set()
        await baseline.frame(advance=0.03)
        assert baseline.t._tts_cancel.is_set()
        assert 'barge_grace_blocked' in baseline.names()
    asyncio.run(scenario())


def test_gated_feed_loses_preconfirmation_frames(baseline, monkeypatch):
    monkeypatch.setattr(tt.settings, 'stt_feed_mode', 'gated')
    monkeypatch.setattr(tt.settings, 'semantic_barge_enabled', False)
    async def scenario():
        for _ in range(5):
            await baseline.frame()
        assert baseline.t._stt.send_audio.await_count == 3
    asyncio.run(scenario())


def test_pending_judge_does_not_block_audio_and_timeout_absorbs(baseline, monkeypatch):
    h, t = baseline, baseline.t
    async def scenario():
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        monkeypatch.setattr(loop, 'run_in_executor', lambda *args: pending)
        t._last_interim_text = 'my situation changed'
        for _ in range(3):
            await h.frame()
        task = t._yield_task
        assert task is not None and not t._tts_cancel.is_set()
        for _ in range(10):
            await h.frame()
            await asyncio.sleep(0)
        assert t._stt.send_audio.await_count == 13
        await task
        assert t._absorb_detail['verdict'] == 'timeout_absorb'
        assert not t._tts_cancel.is_set()
        # A result after the window has resolved does not trigger a late cut.
        pending.set_result('substantive_interrupt')
        await asyncio.sleep(0)
        assert not t._tts_cancel.is_set()
    asyncio.run(scenario())


def test_reconfirmed_speech_while_absorbing_does_not_reopen_window(baseline):
    h, t = baseline, baseline.t
    async def scenario():
        t._last_interim_text = 'okay'
        for _ in range(3):
            await h.frame()
        await t._yield_task
        first_task = t._yield_task
        assert t._absorbing_utterance
        t._last_interim_text = 'okay but the amount you quoted is incorrect'
        for _ in range(100):
            await h.frame()
        assert t._barge_in_attempts == 2  # confirmations, not distinct speech episodes
        assert t._yield_task is first_task
        assert not t._tts_cancel.is_set()
        assert t._stt.send_audio.await_count == 103
    asyncio.run(scenario())
