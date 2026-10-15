"""Call-liveliness ambience: room tone, typing-in-the-gap, one-shots.

Transport-layer only. See docs/ambience-liveliness.md. Phase 1 is the pure
mixer core (this package); Phases 2–3 wire it into the transports.
"""
from vogon.gateway.transport.ambience.loader import build_mixer
from vogon.gateway.transport.ambience.mixer import (
    AmbienceConfig,
    AmbienceMixer,
    TypingState,
)

__all__ = [
    "AmbienceConfig",
    "AmbienceMixer",
    "TypingState",
    "build_mixer",
]
