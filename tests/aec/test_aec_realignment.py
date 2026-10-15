"""Re-alignment from the audio: the real AECProcessor finds a slipped pairing in the near-end
audio itself and shifts onto it — no clock involved (aec.py, RE-ALIGNMENT FROM THE AUDIO).

Two lines on a 200 ms / gain 0.3 echo path, as the transport runs them: the first converges the
filter and the delay estimate; the second starts with its pairing slipped. A slip of +k is k
inbound frames spoken BEFORE the line that arrive after it was sent (a stall — e2e-ladders-6272cade
— or the coin flip of a send landing on Twilio's tick, e2e-ladders-f01258b1); -1 is an inbound
frame lost at the line start. One frame off is as fatal as five without the check.

Requirements: the barge gate never confirms on the second line (no run of
barge_in_consecutive_frames over its threshold), the echo after the check is >= 15 dB down, the
shift found is exactly the slip; and an aligned line, a weak echo, or the caller talking over
the line never moves the alignment.
"""
import audioop
import logging

import numpy
import pytest
from echo_path import line_noise, longest_run_over, mulaw_frames, rms, speech_like

from vogon.gateway.settings import settings
from vogon.gateway.transport.aec import AECProcessor

ECHO_DELAY_FRAMES = 10
FIRST_LINE_FRAMES = 300
SECOND_LINE_START = 450
SECOND_LINE_FRAMES = 75
TAIL_FRAMES = 25
SECOND_LINE = range(SECOND_LINE_START - 10, SECOND_LINE_START + SECOND_LINE_FRAMES + TAIL_FRAMES)
AFTER_THE_CHECK = range(SECOND_LINE_START + ECHO_DELAY_FRAMES + 25, SECOND_LINE_START + SECOND_LINE_FRAMES)
THRESHOLD = settings.barge_in_rms_threshold
CONSECUTIVE = max(1, settings.barge_in_consecutive_frames)


@pytest.fixture(autouse=True)
def quiet_aec_logs():
    logging.getLogger('vogon.gateway.transport.aec').setLevel(logging.WARNING)


def play_two_lines(*, stalled_frames=0, lost_second=(), lost_first=(), caller_over=False, gain=0.3):
    """{stream frame: (raw rms, clean rms)} and the processor. The window is open while a line
    plays (and its echo tail); `stalled_frames` frames spoken before the second line are
    processed after it was fed; `lost_*` frames never arrive."""
    processor = AECProcessor()
    first = mulaw_frames(speech_like(FIRST_LINE_FRAMES * 0.02, 1))
    second = mulaw_frames(speech_like(SECOND_LINE_FRAMES * 0.02, 5))
    caller = mulaw_frames(speech_like(1.0, 9))
    noise = line_noise(97, 20.0)

    def near_end(frame):
        pcm = noise[frame % 97]
        for start, line in ((0, first), (SECOND_LINE_START, second)):
            index = frame - ECHO_DELAY_FRAMES - start
            if 0 <= index < len(line):
                pcm = audioop.add(pcm, audioop.mul(audioop.ulaw2lin(line[index], 2), 2, gain), 2)
        if caller_over and SECOND_LINE_START + 20 <= frame < SECOND_LINE_START + 70:
            pcm = audioop.add(pcm, audioop.ulaw2lin(caller[frame - SECOND_LINE_START - 20], 2), 2)
        return audioop.lin2ulaw(pcm, 2)

    measured = {}

    def process(frames, lost):
        for frame in frames:
            if frame in lost:
                continue
            raw = near_end(frame)
            measured[frame] = (rms(audioop.ulaw2lin(raw, 2)), rms(audioop.ulaw2lin(processor.process(raw), 2)))

    for frame in first:
        processor.feed_reference(frame)
    process(range(FIRST_LINE_FRAMES + TAIL_FRAMES), lost_first)
    for frame in second:
        processor.feed_reference(frame)
    process(range(SECOND_LINE_START - stalled_frames, SECOND_LINE_START + SECOND_LINE_FRAMES + TAIL_FRAMES), lost_second)
    assert processor.is_converged() and processor._delay_frames == ECHO_DELAY_FRAMES
    return measured, processor


def echo_removed_db(measured, frames):
    echo = [frame for frame in frames if frame in measured and measured[frame][0] > 200]
    return 10 * numpy.log10(numpy.mean([measured[frame][0] ** 2 for frame in echo])
                            / numpy.mean([measured[frame][1] ** 2 for frame in echo]))


def barge_run(measured, frames):
    return longest_run_over([measured[frame][1] for frame in frames if frame in measured], THRESHOLD)


@pytest.mark.parametrize('stalled_frames', (1, 3, 5, 7))
def test_a_line_paired_early_by_a_stall_is_realigned(stalled_frames):
    measured, processor = play_two_lines(stalled_frames=stalled_frames)
    assert [shift for _, shift, _, _ in processor.realignments] == [stalled_frames]
    assert barge_run(measured, SECOND_LINE) < CONSECUTIVE, 'our own echo would confirm a barge'
    assert echo_removed_db(measured, AFTER_THE_CHECK) >= 15


def test_a_line_paired_late_by_a_lost_frame_is_realigned():
    measured, processor = play_two_lines(lost_second=range(SECOND_LINE_START, SECOND_LINE_START + 1))
    assert [shift for _, shift, _, _ in processor.realignments] == [-1]
    assert barge_run(measured, SECOND_LINE) < CONSECUTIVE
    assert echo_removed_db(measured, AFTER_THE_CHECK) >= 15


def test_frames_lost_mid_line_are_realigned():
    measured, processor = play_two_lines(lost_first=range(150, 152))
    assert processor.realignments and processor.realignments[0][1] == -2
    assert echo_removed_db(measured, range(170, FIRST_LINE_FRAMES)) >= 12


@pytest.mark.parametrize('case', ('aligned', 'weak echo', 'caller over the line'))
def test_an_aligned_line_never_moves(case):
    kwargs = {'aligned': {}, 'weak echo': {'gain': 0.05}, 'caller over the line': {'caller_over': True}}[case]
    measured, processor = play_two_lines(**kwargs)
    assert processor.realignments == []
    if case != 'caller over the line':
        assert echo_removed_db(measured, AFTER_THE_CHECK) >= 20


def test_a_clear_starts_the_next_playout_unshifted():
    """Twilio's `clear` empties its buffer as discard_reference empties ours: the next playout
    anchors afresh, so a shift found for the old one does not carry over."""
    _, processor = play_two_lines(stalled_frames=3)
    assert processor._alignment_slip == 3
    processor.discard_reference()
    assert processor._alignment_slip == 0
