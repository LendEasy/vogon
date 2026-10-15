"""Startup capability checks — M1 item 11, decision D5.

The three native audio capabilities all DEGRADE SILENTLY today when their
libraries are missing: `create_aec()` returns None (calls run with echo),
`webrtcvad` import failures drop the speech-gated barge confirm to the
energy-only tier, and `audioop` disappears entirely on Python ≥ 3.13
(PEP 594). A phone gateway running without them is the worst version of
each failure — audible to callers, invisible to operators.

Policy (GATEWAY_CAPABILITY_POLICY): ``refuse`` (default, per D5 — a phone
deployment must not boot degraded) or ``warn`` (dev boxes, text-only use).
Either way, every capability's status is logged at startup — one line each,
never silence.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capability:
    name: str
    ok: bool
    detail: str


def check_aec() -> Capability:
    try:
        from vogon.gateway.transport.aec import create_aec  # noqa: PLC0415 — the import is the capability probe
        ok = create_aec() is not None
        return Capability("aec", ok,
                          "SpeexDSP echo canceller ready" if ok else
                          "speexdsp missing/unbuildable — calls would run "
                          "WITH ECHO (needs: git g++ swig libspeex-dev "
                          "libspeexdsp-dev, see DEPENDENCIES.md)")
    except Exception as exc:  # noqa: BLE001
        return Capability("aec", False, f"AEC probe failed: {exc}")


def check_vad() -> Capability:
    try:
        import webrtcvad  # noqa: F401, PLC0415 — the import is the capability probe
        return Capability("vad", True, "webrtcvad ready")
    except ImportError as exc:
        return Capability("vad", False,
                          f"webrtcvad missing ({exc}) — barge confirm "
                          "degrades to energy-only")


def check_audioop() -> Capability:
    try:
        import audioop  # noqa: F401, PLC0415 — the import is the capability probe
        return Capability("audioop", True,
                          f"audioop ready (py{sys.version_info.major}."
                          f"{sys.version_info.minor})")
    except ImportError:
        return Capability("audioop", False,
                          "audioop missing (removed in Python 3.13, PEP 594)"
                          " — install audioop-lts or pin Python <= 3.12; the"
                          " entire mulaw transcode path depends on it")


def check_all() -> list[Capability]:
    return [check_aec(), check_vad(), check_audioop()]


def enforce(policy: str) -> list[Capability]:
    """Run the checks, log every result, and apply the policy. Returns the
    results (for /health). Raises RuntimeError on failure under 'refuse'."""
    results = check_all()
    for cap in results:
        (logger.info if cap.ok else logger.error)(
            "capability %-8s %s | %s", cap.name,
            "OK" if cap.ok else "MISSING", cap.detail)
    failed = [c for c in results if not c.ok]
    if failed and policy == "refuse":
        raise RuntimeError(
            "voice gateway refusing to start — missing capabilities: "
            + ", ".join(c.name for c in failed)
            + " (set GATEWAY_CAPABILITY_POLICY=warn for text-only/dev use)")
    return results
