"""Build an AmbienceMixer for a call — real assets if present, else synthetic.

Assets live in ``app/transport/ambience/assets/`` as WAVs:
    room_tone*.wav   — one or more seamless room-tone loops
    typing*.wav      — 3–5 keyboard bursts

When no WAVs are found for a layer, a deterministic synthetic source is used so
the feature runs (and tests pass) without committing binary audio. Real assets
supersede synthetic by presence.

Sources are decoded and resampled to the requested rate ONCE here — the mixer's
hot path never touches WAV or resampling.
"""
from __future__ import annotations

import logging
from pathlib import Path

from vogon.gateway.transport.ambience import sources as _src
from vogon.gateway.transport.ambience.mixer import AmbienceConfig, AmbienceMixer

logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).with_name("assets")


def _wavs(prefix: str) -> list[Path]:
    return sorted(_ASSETS.glob(f"{prefix}*.wav"))


def build_mixer(
    sample_rate: int,
    *,
    enable_room: bool = True,
    enable_typing: bool = True,
    enable_oneshots: bool = False,
    config: AmbienceConfig | None = None,
) -> AmbienceMixer:
    """Construct a mixer at ``sample_rate`` (e.g. 8000 phone / 24000 widget).

    One-shots are OFF by default — they are the lowest-priority texture and are
    only worth enabling if calls still feel sterile with room tone + typing.
    """
    room = None
    if enable_room:
        room_wavs = _wavs("room_tone")
        if room_wavs:
            room = _src.build_wav_source(room_wavs, sample_rate)
            logger.info("ambience: room tone from %d asset(s)", len(room_wavs))
        else:
            room = _src.build_synthetic_room_tone(sample_rate)
            logger.info("ambience: room tone synthetic (no assets)")

    typing = None
    if enable_typing:
        typing_wavs = _wavs("typing")
        if typing_wavs:
            typing = _src.build_wav_source(typing_wavs, sample_rate)
            logger.info("ambience: typing from %d asset(s)", len(typing_wavs))
        else:
            typing = _src.build_synthetic_typing(sample_rate)
            logger.info("ambience: typing synthetic (no assets)")

    oneshots = None
    if enable_oneshots:
        oneshot_wavs = _wavs("oneshot_")
        if oneshot_wavs:
            oneshots = _src.build_wav_source(oneshot_wavs, sample_rate)
            logger.info("ambience: one-shots from %d asset(s)", len(oneshot_wavs))
        else:
            oneshots = _src.build_synthetic_oneshots(sample_rate)
            logger.info("ambience: one-shots synthetic (no assets)")

    return AmbienceMixer(
        sample_rate, room=room, typing=typing, oneshots=oneshots, config=config
    )
