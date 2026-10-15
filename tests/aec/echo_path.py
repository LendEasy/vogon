"""A known echo path for the real gateway AEC (`AECProcessor`).

The far end is a deterministic speech-like signal: a jittered glottal pulse
train through three vowel formants, syllable envelopes, fricative bursts and
pauses — speech-shaped in spectrum and rhythm, which is what an adaptive
echo canceller and a phase correlator are sensitive to. No audio is tracked
in git, so the signal is synthesised here.

The echo is built exactly as the e2e duplex harness builds it
(`data-plane/tests/gateway_e2e/ladders_duplex_call.py`, `echo_history`): the
μ-law frame the far end played, decoded to PCM, scaled by `gain`, delayed by a
whole number of 20 ms frames, added to the near-end PCM and re-encoded to
μ-law. The reference is fed to the AEC with `feed_reference` and drained one
frame per `process` call, the same way the Twilio transport does.
"""
from __future__ import annotations

import audioop
import functools
import math
from dataclasses import dataclass, field

import numpy

from vogon.gateway.transport.aec import AECProcessor

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160
FRAME_MS = 20
MULAW_SILENCE = b"\xff" * FRAME_SAMPLES
VOWEL_FORMANTS = ((730, 1090, 2440), (270, 2290, 3010), (530, 1840, 2480),
                  (570, 840, 2410), (300, 870, 2240))


def _resonate(signal: numpy.ndarray, centre_hz: float, bandwidth_hz: float) -> numpy.ndarray:
    radius = math.exp(-math.pi * bandwidth_hz / SAMPLE_RATE)
    first_coefficient = 2 * radius * math.cos(2 * math.pi * centre_hz / SAMPLE_RATE)
    second_coefficient = -radius * radius
    output = numpy.zeros_like(signal)
    previous = before_previous = 0.0
    for index, sample in enumerate(signal):
        value = sample + first_coefficient * previous + second_coefficient * before_previous
        output[index] = value
        before_previous, previous = previous, value
    return output


@functools.lru_cache(maxsize=64)
def speech_like(seconds: float, seed: int, target_rms: float = 3000.0,
                leading_silence_sec: float = 0.0) -> bytes:
    """int16 PCM bytes of speech-shaped audio, `target_rms` over its voiced samples."""
    generator = numpy.random.default_rng(seed)
    total = int(seconds * SAMPLE_RATE)
    output = numpy.zeros(total)
    position = int(leading_silence_sec * SAMPLE_RATE)
    while position < total:
        end = min(total, position + int(generator.uniform(0.12, 0.28) * SAMPLE_RATE))
        length = end - position
        excitation = numpy.zeros(length)
        period = SAMPLE_RATE / generator.uniform(95, 190)
        pulse = 0.0
        while pulse < length:
            excitation[int(pulse)] = 1.0
            pulse += period * generator.uniform(0.97, 1.03)
        excitation += 0.03 * generator.standard_normal(length)
        formants = VOWEL_FORMANTS[generator.integers(len(VOWEL_FORMANTS))]
        syllable = sum(_resonate(excitation, frequency, 80 + 40 * order)
                       for order, frequency in enumerate(formants))
        if generator.random() < 0.3:
            syllable = syllable + 0.3 * _resonate(generator.standard_normal(length), 3200, 900)
        output[position:end] = syllable * numpy.sin(numpy.linspace(0, math.pi, length)) ** 0.7
        position = end + int(generator.uniform(0.02, 0.08) * SAMPLE_RATE)
        if generator.random() < 0.15:
            position += int(generator.uniform(0.2, 0.4) * SAMPLE_RATE)
    # Lip radiation (a first difference): real speech carries little energy
    # below ~200 Hz, and a telephone channel carries none — without it the
    # pulse train's low end is removed by SpeexDSP's DC notch and reads as
    # the canceller "losing" near-end speech.
    output = numpy.concatenate(([0.0], numpy.diff(output)))
    voiced = output[output != 0]
    output *= target_rms / numpy.sqrt(numpy.mean(voiced ** 2))
    return numpy.clip(output, -32000, 32000).astype(numpy.int16).tobytes()


def pcm_frames(pcm: bytes) -> list[bytes]:
    frame_bytes = FRAME_SAMPLES * 2
    return [pcm[start:start + frame_bytes] for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]


def mulaw_frames(pcm: bytes) -> list[bytes]:
    return [audioop.lin2ulaw(frame, 2) for frame in pcm_frames(pcm)]


def line_noise(frame_count: int, noise_rms: float, seed: int = 7) -> list[bytes]:
    generator = numpy.random.default_rng(seed)
    return [(generator.standard_normal(FRAME_SAMPLES) * noise_rms).astype(numpy.int16).tobytes()
            for _ in range(frame_count)]


def rms(pcm: bytes) -> int:
    return audioop.rms(pcm, 2)


@dataclass
class EchoRun:
    """Per-frame PCM of one run: the near end as received, the AEC output,
    and the near-end speech component alone (silence when there is none)."""
    near: list[bytes] = field(default_factory=list)
    clean: list[bytes] = field(default_factory=list)
    near_speech: list[bytes] = field(default_factory=list)
    processor: AECProcessor | None = None

    def power(self, frames: list[bytes], start: int, stop: int | None = None) -> float:
        return float(numpy.mean([rms(frame) ** 2 for frame in frames[start:stop]]))

    def erle_db(self, start: int, stop: int | None = None) -> float:
        """Echo Return Loss Enhancement over frames [start, stop): near-end
        power in, AEC output power out, on frames where the echo is present."""
        active = [index for index in range(start, len(self.near) if stop is None else stop)
                  if rms(self.near[index]) > 50]
        near_power = numpy.mean([rms(self.near[index]) ** 2 for index in active])
        clean_power = numpy.mean([rms(self.clean[index]) ** 2 for index in active])
        return 10 * math.log10(near_power / max(clean_power, 1e-9))

    def residual_rms(self, start: int, stop: int | None = None) -> list[int]:
        return [rms(frame) for frame in self.clean[start:stop]]


def run_echo_path(far_pcm: bytes, delay_ms: int, gain: float, *,
                  near_speech_pcm: bytes | None = None, near_speech_start_frame: int = 0,
                  noise_rms: float = 0.0, reference_burst_frames: int = 1,
                  processor: AECProcessor | None = None) -> EchoRun:
    """Play `far_pcm` out through a `delay_ms` / `gain` echo path into the real AEC.

    `reference_burst_frames` > 1 hands the reference to the AEC that many
    frames at a time, AHEAD of playout, the way the transport sends TTS faster
    than real time — the FIFO must still pair each near-end frame with the
    frame playing at that moment.
    """
    assert delay_ms % FRAME_MS == 0, "the harness echo path delays by whole frames"
    processor = processor or AECProcessor()
    played = mulaw_frames(far_pcm)
    delay_frames = delay_ms // FRAME_MS
    speech_frames = pcm_frames(near_speech_pcm) if near_speech_pcm else []
    noise_frames = line_noise(len(played), noise_rms) if noise_rms else []
    run = EchoRun(processor=processor)
    fed = 0
    for index in range(len(played)):
        while fed < len(played) and fed < index + reference_burst_frames:
            processor.feed_reference(played[fed])
            fed += 1
        echo_source = played[index - delay_frames] if index >= delay_frames else MULAW_SILENCE
        near_pcm = audioop.mul(audioop.ulaw2lin(echo_source, 2), 2, gain)
        speech_index = index - near_speech_start_frame
        speech = speech_frames[speech_index] if 0 <= speech_index < len(speech_frames) \
            else bytes(FRAME_SAMPLES * 2)
        near_pcm = audioop.add(near_pcm, speech, 2)
        if noise_frames:
            near_pcm = audioop.add(near_pcm, noise_frames[index], 2)
        near_mulaw = audioop.lin2ulaw(near_pcm, 2)
        clean_mulaw = processor.process(near_mulaw)
        run.near.append(audioop.ulaw2lin(near_mulaw, 2))
        run.clean.append(audioop.ulaw2lin(clean_mulaw, 2))
        run.near_speech.append(speech)
    return run


def longest_run_over(values: list[int], threshold: float) -> int:
    longest = streak = 0
    for value in values:
        streak = streak + 1 if value > threshold else 0
        longest = max(longest, streak)
    return longest
