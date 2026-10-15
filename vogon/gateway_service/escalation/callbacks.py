"""Bodies for Twilio's escalation callbacks — the gateway's /v1/escalation
routes (whisper / keypress / agent-status / audio) and the caller's own
status webhook (/v1/twilio/status), which is where a hang-up on hold
arrives.

Every agent-leg callback names its leg (Twilio's ``CallSid`` form/query
field); a callback for a leg that is no longer the transfer's current leg
changes nothing. Each step is reported as a post-call fact (report.py)."""
from __future__ import annotations

import asyncio
import logging
import time

from vogon.gateway_service.escalation import report
from vogon.gateway_service.escalation.state import (
    claim_transfer,
    get_audio,
    get_transfer,
)
from vogon.gateway_service.escalation.transfer import DIALING
from vogon.gateway_service.escalation.twiml import (
    DEFAULT_SAY_VOICE,
    agent_connect_twiml,
    callback_twiml,
    whisper_twiml,
)

logger = logging.getLogger(__name__)

_LEG_ENDED = ("no-answer", "busy", "failed", "canceled")
_CALL_ENDED = ("completed", "busy", "no-answer", "failed", "canceled")
_NOT_WAITING = "The caller is no longer waiting. Goodbye."

# The agent answered before the whisper's persona-voice render landed (a
# ~20 s whisper renders in ~9 s): wait this long for it, then <Say> it.
# Twilio's webhook timeout is 15 s. docs/voice-timing-constants.md §9.
_WHISPER_AUDIO_WAIT_SEC = 5.0
_WHISPER_AUDIO_POLL_SEC = 0.25


async def _whisper_audio(call_sid: str, state):
    """The whisper's audio token: at once if rendered, after a bounded wait
    if it is still rendering, else None (<Say>)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WHISPER_AUDIO_WAIT_SEC
    while True:
        token = (state.audio or {}).get("whisper")
        if (token or "whisper" not in (state.audio_rendering or [])
                or "whisper" in (state.audio_failed or {})):
            return token
        if loop.time() >= deadline:
            logger.warning("whisper audio for %s not ready in %.1fs — <Say>",
                           call_sid, _WHISPER_AUDIO_WAIT_SEC)
            return None
        await asyncio.sleep(_WHISPER_AUDIO_POLL_SEC)
        state = await get_transfer(call_sid) or state


def _is_current_leg(state, agent_call_sid: str) -> bool:
    return not agent_call_sid or state.agent_call_sid in (agent_call_sid, DIALING)


async def whisper_body(call_sid: str, base_url: str, prefix: str,
                       agent_call_sid: str = "",
                       voice: str = DEFAULT_SAY_VOICE) -> str:
    state = await get_transfer(call_sid)
    if not state or not _is_current_leg(state, agent_call_sid):
        logger.warning("Whisper requested for %s with no live transfer (leg %s)",
                       call_sid, agent_call_sid or "?")
        return callback_twiml(_NOT_WAITING, voice=voice)
    # The leg answered: if the caller now hangs up on hold, this leg is
    # ended (status=completed), not cancelled. Reported once per leg, with
    # the voice the agent hears the whisper in.
    first = await claim_transfer(call_sid, {"agent_call_sid": state.agent_call_sid,
                                            "agent_answered": False},
                                 {"agent_answered": True})
    token = await _whisper_audio(call_sid, state)
    if first:
        await report.leg(call_sid, state, "answered",
                         detail="whisper: persona voice" if token
                         else "whisper: twilio <Say>")
    base = f"{base_url.rstrip('/')}{prefix}"
    return whisper_twiml(summary_text=state.summary_text,
                         gather_action_url=f"{base}/agent-keypress/{call_sid}",
                         voice=voice,
                         audio_url=f"{base}/audio/{token}.mp3" if token else None)


async def keypress_body(call_sid: str, agent_call_sid: str = "",
                        voice: str = DEFAULT_SAY_VOICE) -> str:
    state = await get_transfer(call_sid)
    expect = {"agent_call_sid": agent_call_sid} if agent_call_sid else {}
    if not state or not await claim_transfer(
            call_sid, expect, {"connected": True, "connected_utc": time.time()}):
        logger.warning("Agent keypress for %s with no live transfer (leg %s)",
                       call_sid, agent_call_sid or "?")
        return callback_twiml(_NOT_WAITING, voice=voice)
    logger.info("Agent pressed key for call %s — connecting to Conference %s",
                call_sid, state.conference_name)
    await report.leg(call_sid, state, "connected")
    return agent_connect_twiml(state.conference_name)


async def status_body(call_sid: str, call_status: str, orchestrator,
                      agent_call_sid: str = "") -> dict:
    """The agent leg's final status (Twilio's ``completed`` event)."""
    state = await get_transfer(call_sid)
    if not state:
        return {"status": "ignored"}
    if not _is_current_leg(state, agent_call_sid):
        logger.info("Agent status %s for %s on stale leg %s (current %s) — ignored",
                    call_status, call_sid, agent_call_sid, state.agent_call_sid)
        return {"status": "ignored"}
    logger.info("Agent status for call %s: %s (leg %s)", call_sid, call_status,
                agent_call_sid or "?")
    if call_status in _LEG_ENDED:
        await orchestrator.handle_no_answer(call_sid, agent_call_sid,
                                            leg_status=call_status)
    elif call_status == "completed":
        await orchestrator.handle_agent_completed(call_sid, agent_call_sid)
    return {"status": "ok"}


async def caller_status_body(call_sid: str, call_status: str,
                             orchestrator_factory) -> None:
    """The CALLER's call status — this runs for EVERY call's status webhook,
    so it costs one lookup and nothing else unless a transfer is open. A
    terminal status while one is open means the caller left: on hold, the
    pending agent leg is stopped."""
    if call_status not in _CALL_ENDED or await get_transfer(call_sid) is None:
        return
    await orchestrator_factory().handle_caller_gone(call_sid)


async def audio_body(token: str) -> bytes | None:
    """A line rendered in the persona's voice, by its unguessable token —
    None once the transfer (and with it the audio) is gone."""
    return await get_audio(token)
