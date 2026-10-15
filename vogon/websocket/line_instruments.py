"""The line detector's instruments — the reasoning-side calls the gateway
tree may not make itself (docs/VOICEMAIL_DETECTION_DESIGN.md §4). The driver
installs one on the transport; the detector only knows ``ask(state) -> p``.

    jev_recording_probability   the Jev recording question over OpenRouter's
                                decisions endpoint (vogon.gateway.jev)
"""
from __future__ import annotations

from vogon.gateway import jev
from vogon.gateway.transport.line_detector import RECORDING_QUESTION


async def jev_recording_probability(state: dict) -> float:
    result = await jev.decide_async(state, {"recording": RECORDING_QUESTION},
                                    tag="line_detector")
    return float(result.noul("recording").probability)
