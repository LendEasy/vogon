"""Residual echo after the real AEC, against the barge gate that judges it.

The gate (``BargeGate``, ``barge_in_rms_threshold`` / ``barge_in_consecutive_frames``)
confirms a barge when the AEC-cleaned RMS exceeds the threshold for N
consecutive 20 ms frames; the floor controller reads the same comparison. So
residual echo "is treated as caller speech" when it stays over the threshold
for N frames in a row. With adaptive thresholding on, the threshold can fall
to ``barge_in_adaptive_min``.

What this does NOT cover: in ``stt_feed_mode="open"`` every cleaned frame goes
to Deepgram whatever its RMS, so a residual far under the gate can still be
transcribed — the gate only decides whether playback is cut.

Run directly for the measured table:

  cd voice-gateway && python tests/aec/test_aec_residual_floor.py
"""
import logging

import numpy
import pytest
from echo_path import FRAME_MS, longest_run_over, rms, run_echo_path, speech_like

from vogon.gateway.settings import settings

DELAYS_MS = (0, 60, 120, 200, 300)
GAINS = (0.1, 0.3, 0.5)
FRAMES_PER_SECOND = 1000 // FRAME_MS
CONVERGED_FROM = 3 * FRAMES_PER_SECOND
THRESHOLD = settings.barge_in_rms_threshold
CONSECUTIVE = max(1, settings.barge_in_consecutive_frames)
ADAPTIVE_MIN = settings.barge_in_adaptive_min
# The delay estimate lands after 0.6 s of reference speech (the far end starts
# speaking on frame 0 here); its effect reaches the near end one echo delay later.
ESTIMATE_LANDS_FRAME = 30


def measure(delay_ms, gain, seconds=12.0):
    run = run_echo_path(speech_like(seconds, 1), delay_ms, gain)
    landed = ESTIMATE_LANDS_FRAME
    converged_residual = run.residual_rms(CONVERGED_FROM)
    return {
        'delay_ms': delay_ms,
        'gain': gain,
        'estimate_ms': run.processor._delay_frames * FRAME_MS,
        'erle_before_estimate_db': run.erle_db(0, landed + delay_ms // FRAME_MS),
        'erle_estimate_to_3s_db': run.erle_db(landed + delay_ms // FRAME_MS, CONVERGED_FROM),
        'erle_converged_db': run.erle_db(CONVERGED_FROM),
        'raw_p95': int(numpy.percentile([rms(frame) for frame in run.near[CONVERGED_FROM:]], 95)),
        'residual_p95': int(numpy.percentile(converged_residual, 95)),
        'residual_max': max(converged_residual),
        'early_residual_max': max(run.residual_rms(0, CONVERGED_FROM)),
        'run_over_before_estimate': longest_run_over(run.residual_rms(0, landed + delay_ms // FRAME_MS), THRESHOLD),
        'run_over_after_estimate': longest_run_over(
            run.residual_rms(landed + delay_ms // FRAME_MS, CONVERGED_FROM), THRESHOLD),
        'run_over_converged': longest_run_over(converged_residual, THRESHOLD),
    }


@pytest.fixture(autouse=True)
def quiet_aec_logs():
    logging.getLogger('vogon.gateway.transport.aec').setLevel(logging.WARNING)


@pytest.mark.parametrize('gain', GAINS)
@pytest.mark.parametrize('delay_ms', DELAYS_MS)
def test_converged_residual_echo_is_not_caller_speech(delay_ms, gain):
    row = measure(delay_ms, gain)
    assert row['run_over_converged'] < CONSECUTIVE, row
    # under the adaptive gate's lowest threshold too, with room to spare
    assert row['residual_p95'] < ADAPTIVE_MIN / 2, row


@pytest.mark.parametrize('gain', GAINS)
@pytest.mark.parametrize('delay_ms', DELAYS_MS)
def test_residual_after_the_delay_estimate_never_confirms_a_barge(delay_ms, gain):
    row = measure(delay_ms, gain)
    assert row['run_over_after_estimate'] < CONSECUTIVE, row


_UNCANCELLED_START = pytest.mark.xfail(strict=True, reason=(
    'NOT FIXED: before the delay estimate lands (0.6 s of reference speech '
    'plus the echo delay) the SpeexDSP filter has not converged (ERLE ~4-6 dB '
    'at 60-300 ms), so a loud echo of the first words of a call stays over the '
    'barge threshold for >= barge_in_consecutive_frames frames'))


@pytest.mark.parametrize('delay_ms, gain', [
    (0, 0.3), (0, 0.5), (60, 0.3), (120, 0.3),
    pytest.param(200, 0.3, marks=_UNCANCELLED_START),
    pytest.param(300, 0.3, marks=_UNCANCELLED_START),
    pytest.param(60, 0.5, marks=_UNCANCELLED_START),
    pytest.param(120, 0.5, marks=_UNCANCELLED_START),
    pytest.param(200, 0.5, marks=_UNCANCELLED_START),
    pytest.param(300, 0.5, marks=_UNCANCELLED_START),
])
def test_residual_before_the_delay_estimate_does_not_confirm_a_barge(delay_ms, gain):
    row = measure(delay_ms, gain)
    assert row['run_over_before_estimate'] < CONSECUTIVE, row


@pytest.mark.parametrize('noise_rms', (30.0, 100.0, 300.0))
def test_line_noise_passes_the_canceller_unamplified(noise_rms):
    """Reference playing, no echo, only line noise: the canceller must not add
    energy (the live harness logged clean_rms above raw_rms on barge events)."""
    run = run_echo_path(speech_like(6.0, 3), 200, 0.0, noise_rms=noise_rms)
    raw_p95 = numpy.percentile([rms(frame) for frame in run.near], 95)
    clean_p95 = numpy.percentile(run.residual_rms(0), 95)
    assert clean_p95 <= 1.15 * raw_p95, (clean_p95, raw_p95)
    assert clean_p95 < THRESHOLD


def table():
    header = ('| delay ms | gain | estimate ms | ERLE before est. dB | ERLE est.->3 s dB | ERLE 3-12 s dB '
              '| raw p95 | residual p95 | residual max | early residual max '
              f'| frames over {THRESHOLD} (before / after est. / converged) | confirms a barge ({CONSECUTIVE} in a row)? |')
    lines = [header, '|' + '---|' * 12]
    for gain in GAINS:
        for delay_ms in DELAYS_MS:
            row = measure(delay_ms, gain)
            crossing = [phase for phase, key in (('before est.', 'run_over_before_estimate'),
                                                 ('after est.', 'run_over_after_estimate'),
                                                 ('converged', 'run_over_converged'))
                        if row[key] >= CONSECUTIVE]
            lines.append(
                f"| {delay_ms} | {gain} | {row['estimate_ms']} | {row['erle_before_estimate_db']:.1f} "
                f"| {row['erle_estimate_to_3s_db']:.1f} | {row['erle_converged_db']:.1f} | {row['raw_p95']} "
                f"| {row['residual_p95']} | {row['residual_max']} | {row['early_residual_max']} "
                f"| {row['run_over_before_estimate']} / {row['run_over_after_estimate']} / {row['run_over_converged']} "
                f"| {', '.join(crossing) or 'no'} |")
    return '\n'.join(lines)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    print(f'gate: barge_in_rms_threshold={THRESHOLD} consecutive_frames={CONSECUTIVE} '
          f'adaptive_min={ADAPTIVE_MIN}')
    print(table())
