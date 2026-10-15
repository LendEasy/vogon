"""The real gateway AEC on a known echo path: delay estimate, ERLE, double talk,
reference pacing and the reference after a Twilio ``clear``.

Thresholds are requirements, not observations:
- the delay estimate is within one 20 ms frame of the true echo delay (the
  pre-shift is applied in whole frames);
- converged ERLE is at least 20 dB — the usual floor for a linear echo
  canceller, and below the 26.7 dB this module documents for its own
  synthetic validation;
- near-end speech in double talk comes through as it does with no echo at
  all (within 1 dB, correlation within 5%).
"""
import asyncio
import audioop
import logging
from unittest.mock import AsyncMock, Mock

import numpy
import pytest
from echo_path import FRAME_MS, MULAW_SILENCE, mulaw_frames, rms, run_echo_path, speech_like
from transport_fixtures import bare_twilio_transport

from vogon.gateway.transport.aec import AECProcessor

DELAYS_MS = (0, 60, 120, 200, 300)
GAINS = (0.1, 0.3, 0.5)
FRAMES_PER_SECOND = 1000 // FRAME_MS


@pytest.fixture(autouse=True)
def quiet_aec_logs():
    logging.getLogger('vogon.gateway.transport.aec').setLevel(logging.WARNING)


@pytest.mark.parametrize('delay_ms', DELAYS_MS)
def test_delay_estimate_matches_the_echo_path(delay_ms):
    run = run_echo_path(speech_like(3.0, 1), delay_ms, 0.3)
    assert run.processor.is_converged()
    estimated_ms = run.processor._delay_frames * FRAME_MS
    assert abs(estimated_ms - delay_ms) <= FRAME_MS, (estimated_ms, delay_ms)


@pytest.mark.parametrize('lead_in_sec', (0.4, 1.0, 3.0))
def test_a_silent_reference_lead_in_does_not_lock_a_zero_delay(lead_in_sec):
    """Every e2e duplex run logged ``AEC GCC-PHAT | delay_ms=0`` against the
    harness's 200 ms echo: the correlation window opened on frames processed
    before the opening's first TTS byte (a silent reference) and its lag-0
    argmax was locked for the call. The window must open on reference speech."""
    far = speech_like(4.0 + lead_in_sec, 2, leading_silence_sec=lead_in_sec)
    run = run_echo_path(far, 200, 0.3)
    assert run.processor.is_converged()
    assert run.processor._delay_frames * FRAME_MS == 200


@pytest.mark.parametrize('noise_rms', (0.0, 30.0, 300.0), ids=['digital-silence', 'quiet-line', 'noisy-line'])
def test_no_echo_on_the_line_leaves_the_delay_unlocked(noise_rms):
    """A phone that cancels its own echo returns none: there is no delay to
    find, and a guess must not be locked as if there were."""
    run = run_echo_path(speech_like(6.0, 3), 200, 0.0, noise_rms=noise_rms)
    assert not run.processor.is_converged()
    assert run.processor._delay_frames == 0


@pytest.mark.parametrize('gain', GAINS)
@pytest.mark.parametrize('delay_ms', DELAYS_MS)
def test_converged_erle(delay_ms, gain):
    run = run_echo_path(speech_like(12.0, 1), delay_ms, gain)
    erle_db = run.erle_db(3 * FRAMES_PER_SECOND)
    assert erle_db >= 20.0, f'ERLE {erle_db:.1f} dB at {delay_ms} ms / gain {gain}'


@pytest.mark.parametrize('delay_ms', (60, 200, 300))
def test_the_filter_restarts_on_the_aligned_reference(delay_ms):
    """Between the estimate (0.6 s of reference speech) and 2 s the canceller
    must already be cancelling: a filter kept from before the pre-shift is off
    by the whole delay and measured 6.5 dB there at 200 ms."""
    run = run_echo_path(speech_like(12.0, 1), delay_ms, 0.3)
    erle_db = run.erle_db(30 + delay_ms // FRAME_MS, 2 * FRAMES_PER_SECOND)
    assert erle_db >= 12.0, f'{erle_db:.1f} dB'


def test_a_reference_sent_ahead_of_playout_is_paired_with_what_plays():
    """The transport sends TTS faster than real time; the FIFO must still pair
    each near-end frame with the frame the far end is playing at that moment."""
    far = speech_like(6.0, 4)
    paced = run_echo_path(far, 200, 0.3, reference_burst_frames=1)
    burst = run_echo_path(far, 200, 0.3, reference_burst_frames=25)
    assert burst.clean == paced.clean


@pytest.mark.parametrize('speech_rms', (900, 3000), ids=['speech-at-echo-level', 'speech-10dB-over-echo'])
def test_near_end_speech_survives_double_talk(speech_rms):
    """The caller talks over the agent after the canceller has converged.
    Measured against the same speech with no echo on the line (the control):
    the canceller's own DC notch / emphasis is in both, so what is compared is
    only what the echo path's adaptation does to the caller."""
    far = speech_like(12.0, 1)
    near_speech = speech_like(3.0, 77, target_rms=speech_rms)
    start = 6 * FRAMES_PER_SECOND
    window = range(start, start + 150)

    def retention(run):
        speech = numpy.concatenate([numpy.frombuffer(run.near_speech[i], numpy.int16) for i in window]).astype(float)
        output = numpy.concatenate([numpy.frombuffer(run.clean[i], numpy.int16) for i in window]).astype(float)
        gain_db = 20 * numpy.log10(abs(numpy.dot(output, speech) / numpy.dot(speech, speech)))
        return gain_db, numpy.corrcoef(output, speech)[0, 1]

    control_gain, control_correlation = retention(
        run_echo_path(far, 200, 0.0, near_speech_pcm=near_speech, near_speech_start_frame=start))
    double_talk_gain, double_talk_correlation = retention(
        run_echo_path(far, 200, 0.3, near_speech_pcm=near_speech, near_speech_start_frame=start))
    assert abs(double_talk_gain - control_gain) <= 1.0, (double_talk_gain, control_gain)
    assert double_talk_correlation >= 0.95 * control_correlation, (double_talk_correlation, control_correlation)


def _reply_after_a_clear(discard_on_clear):
    """Reply A is queued 3 s ahead and cut after 1 s (a barge); reply B starts
    at once (no TTS latency to drain a stale FIFO). Returns B's ERLE."""
    processor = AECProcessor()
    reply_a = mulaw_frames(speech_like(4.0, 21))
    reply_b = mulaw_frames(speech_like(6.0, 22))
    delay_frames = 10
    played = []
    for frame in reply_a[:150]:
        processor.feed_reference(frame)
    near_power = clean_power = 0.0
    for index in range(50):
        played.append(reply_a[index])
        source = played[index - delay_frames] if index >= delay_frames else MULAW_SILENCE
        processor.process(audio_echo(source))
    if discard_on_clear:
        processor.discard_reference()
    for frame in reply_b:
        processor.feed_reference(frame)
    for offset, frame in enumerate(reply_b):
        played.append(frame)
        index = len(played) - 1
        near = audio_echo(played[index - delay_frames])
        clean = processor.process(near)
        if offset >= 2 * FRAMES_PER_SECOND:
            near_power += rms(_pcm(near)) ** 2
            clean_power += rms(_pcm(clean)) ** 2
    return 10 * numpy.log10(near_power / max(clean_power, 1e-9))


def audio_echo(played_mulaw, gain=0.3):
    return audioop.lin2ulaw(audioop.mul(audioop.ulaw2lin(played_mulaw, 2), 2, gain), 2)


def _pcm(mulaw):
    return audioop.ulaw2lin(mulaw, 2)


def test_a_clear_discards_the_reference_that_never_played():
    with_discard = _reply_after_a_clear(discard_on_clear=True)
    without_discard = _reply_after_a_clear(discard_on_clear=False)
    assert with_discard >= 20.0, with_discard
    # the teeth: the stale 2 s of reply A really did misalign reply B
    assert without_discard < with_discard - 10.0, (without_discard, with_discard)


def test_the_transport_discards_the_aec_reference_when_it_clears_twilio():
    transport = bare_twilio_transport()
    transport._stream_sid = 'MZtest'
    transport._send_raw = AsyncMock()
    transport._aec = Mock()
    asyncio.run(transport._send_clear())
    transport._aec.discard_reference.assert_called_once_with()
    assert '"clear"' in transport._send_raw.await_args.args[0]

