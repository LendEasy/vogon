"""TransferOrchestrator — executes the Twilio conference warm-transfer flow.

GATEWAY-OWNED since 2026-08-25 (moved from app/escalation): this module is
Twilio MECHANICS only. It takes an ``EscalationOrder`` the turn service
composed (targets, whisper text — the meaning) and performs it (hold
conference, agent dial, keypress, bridge, fallback). It imports nothing
from the reasoning side; the summary generation lives in
``app/turns/escalation_summary.py`` where the meaning lives.

Design, callbacks, live test plan: voice-gateway/docs/ESCALATION_WARM_TRANSFER.md

The legs, and who drives each step:

  caller leg   execute(): REST update → hold TwiML (the hold line + a
               Conference that waits for the agent). Twilio stops the
               media stream here.
  agent leg    execute()/handle_no_answer(): REST create → Twilio fetches
               the whisper on answer, the keypress bridges, the leg's
               final status (one ``completed`` event, carrying busy /
               no-answer / failed / canceled / completed) walks the ladder.
  fallback     every target failed: voicemail to the last target (after
               its greeting) and the callback message to the caller.
  caller gone  the caller's own status webhook stops a pending agent leg.

Every step is reported to the turn service as a post-call fact
(escalation/report.py): one EscalationLegEvent per leg event, exactly one
EscalationResolved per transfer.

The current leg's sid lives in the transfer state; while a dial is in
flight it is ``DIALING``. Advancing to the next leg is a compare-and-set
on (index, leg sid), so a duplicate or late callback cannot dial twice.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient

from vogon.gateway_service.escalation import report
from vogon.gateway_service.escalation.state import (
    TransferState,
    claim_transfer,
    count_leg_placed,
    get_transfer,
    remove_transfer,
    set_transfer,
)
from vogon.gateway_service.escalation.twiml import (
    DEFAULT_SAY_VOICE,
    callback_twiml,
    hold_twiml,
    voicemail_twiml,
)
from vogon.gateway_service.escalation.voice import line_texts
from vogon_contract import EscalationOrder

logger = logging.getLogger(__name__)

# One Twilio REST round-trip (hold update, dial, cancel). Twilio answers in
# well under a second; the bound exists so a hung connection cannot hold
# the call's teardown (the driver waits for the transfer before end_call).
_TWILIO_REST_TIMEOUT_SEC = 10.0

# The agent leg's sid while its dial is in flight (no sid yet).
DIALING = "dialing"

# Twilio accepts only initiated|ringing|answered|completed here (anything
# else is error 21626 and rejects the dial). The terminal outcome — busy,
# no-answer, failed, canceled, completed — rides the ``completed`` event.
_AGENT_STATUS_EVENTS = ["completed"]

# Twilio's CallStatus → the leg fact's event name.
_LEG_STATUS_EVENT = {"no-answer": "no_answer", "busy": "busy",
                     "failed": "failed", "canceled": "canceled"}


class TransferOrchestrator:
    """Executes the warm-transfer flow for one call's EscalationOrder."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        data_plane_base_url: str,
        callback_prefix: str = "/v1/escalation",
        say_voice: str = DEFAULT_SAY_VOICE,
    ):
        self.client = TwilioClient(
            account_sid, auth_token,
            http_client=TwilioHttpClient(timeout=_TWILIO_REST_TIMEOUT_SEC))
        self.from_number = from_number
        self.base_url = data_plane_base_url.rstrip("/")
        # Where Twilio's whisper/keypress/status callbacks land: the
        # gateway's public surface (/v1/escalation behind /gw/).
        self.callback_prefix = callback_prefix
        self.say_voice = say_voice

    async def _rest(self, fn: Callable):
        """One blocking Twilio REST call, off the event loop, bounded."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, fn),
                                      timeout=_TWILIO_REST_TIMEOUT_SEC)

    def audio_url(self, state: TransferState, line: str) -> str | None:
        """The <Play> URL of a line rendered in the persona's voice, or None
        (that line is a <Say>)."""
        token = (state.audio or {}).get(line)
        if not token:
            return None
        return f"{self.base_url}{self.callback_prefix}/audio/{token}.mp3"

    async def execute(self, call_sid: str, order: EscalationOrder,
                      owner_id: str = "", workflow_id: str = "", *,
                      call_id: str = "", report_url: str = "",
                      report_token: str = "",
                      audio: dict | None = None,
                      audio_rendering: list | None = None) -> str:
        """Perform the warm-transfer ORDER for call_sid.

        ``call_id``/``report_url``/``report_token`` say where the post-call
        facts go (the call's turn service); ``audio`` maps line name → token
        of the lines already rendered in the persona's voice;
        ``audio_rendering`` names the lines still rendering (escalation/
        voice.py attaches them as they land).

        Returns the INITIATION status — the physical fact the driver reports
        at end_call:
          "initiated"   the caller is on hold AND an agent leg was placed
          "no_targets"  the order named nobody to dial
          "failed"      the hold update failed (the caller is gone, or
                        Twilio refused), or no target could be dialled —
                        in which case the caller already heard the callback
                        message
        The agent-leg lifecycle continues via the HTTP callbacks afterwards.
        """
        targets = [{"phone_number": t.number, "label": t.label}
                   for t in order.targets]
        if not targets:
            logger.warning("Escalation ordered for %s but no targets", call_sid)
            return "no_targets"

        texts = line_texts(order)
        conference_name = f"esc_{call_sid[:20]}"
        state = TransferState(
            call_sid=call_sid,
            owner_id=owner_id,
            workflow_id=workflow_id,
            conference_name=conference_name,
            summary_text=order.whisper_text or "Incoming transfer.",
            config={
                "agent_connect_timeout_sec": int(order.agent_timeout_sec),
                "voicemail_script": texts["voicemail"],
                "caller_callback_message": texts["callback"],
            },
            targets=targets,
            current_target_idx=0,
            agent_call_sid=DIALING,
            call_id=call_id,
            escalation_id=order.escalation_id,
            report_url=report_url,
            report_token=report_token,
            audio=dict(audio or {}),
            audio_rendering=list(audio_rendering or []),
        )
        hold = hold_twiml(
            hold_message=texts["hold"],
            conference_name=conference_name,
            hold_music_url=order.hold_music_url or None,
            voice=self.say_voice,
            audio_url=self.audio_url(state, "hold"),
        )
        # The REST reply is the only evidence the hold worked. Twilio also
        # stops the media stream when it redirects the caller, but a stream
        # stops just the same when the caller hangs up — the driver does not
        # cancel this step on a stop, so the reply decides.
        try:
            await self._rest(lambda: self.client.calls(call_sid).update(twiml=hold))
        except Exception as e:
            logger.error("Hold update failed for %s: %s", call_sid, e)
            return "failed"
        logger.info("Caller %s moved to Conference %s", call_sid, conference_name)

        state.hold_started_utc = time.time()
        await set_transfer(call_sid, state)
        if await self._dial_from(call_sid, 0):
            return "initiated"
        state = await get_transfer(call_sid)
        if state is not None:
            await self._close_with_callback(call_sid, state, "failed",
                                            "no target could be dialled")
        return "failed"

    async def _dial_from(self, call_sid: str, start_idx: int) -> bool:
        """Dial targets[start_idx:] in order until one leg is placed. The
        state already says (start_idx, DIALING). True = a leg is ringing."""
        state = await get_transfer(call_sid)
        if state is None:
            return False
        whisper_url = f"{self.base_url}{self.callback_prefix}/whisper/{call_sid}"
        status_cb = f"{self.base_url}{self.callback_prefix}/agent-status/{call_sid}"
        timeout = int(state.config.get("agent_connect_timeout_sec", 30))

        for idx in range(start_idx, len(state.targets)):
            if idx != start_idx and not await claim_transfer(
                    call_sid, {"current_target_idx": idx - 1,
                               "agent_call_sid": DIALING},
                    {"current_target_idx": idx}):
                return True     # someone else moved the transfer on
            target = state.targets[idx]
            try:
                leg = await self._rest(lambda target=target: self.client.calls.create(
                    to=target["phone_number"],
                    from_=self.from_number,
                    url=whisper_url,
                    method="GET",
                    status_callback=status_cb,
                    status_callback_method="POST",
                    status_callback_event=_AGENT_STATUS_EVENTS,
                    timeout=timeout,
                ))
            except Exception as e:
                logger.error("Failed to dial agent %s for %s: %s",
                             target["phone_number"], call_sid, e)
                await report.leg(call_sid, state, "dial_failed", attempt=idx,
                                 agent_call_sid="", detail=str(e)[:200])
                continue
            if await claim_transfer(
                    call_sid, {"current_target_idx": idx,
                               "agent_call_sid": DIALING},
                    {"agent_call_sid": leg.sid, "agent_answered": False}):
                await count_leg_placed(call_sid)
                logger.info("Dialled agent %s for call %s (agent_call=%s)",
                            target["phone_number"], call_sid, leg.sid)
                await report.leg(call_sid, state, "dialled", attempt=idx,
                                 agent_call_sid=leg.sid)
                return True
            # While this dial was in flight the transfer moved on without
            # it: the caller hung up (the state is gone), or the leg failed
            # fast and its status callback already walked past it. Nobody
            # will bridge this leg — stop it ringing. (A leg that already
            # ended makes the cancel fail harmlessly.)
            logger.info("Call %s: agent leg %s placed after the transfer "
                        "moved on — cancelling it", call_sid, leg.sid)
            try:
                await self._rest(lambda leg=leg: self.client.calls(leg.sid).update(
                    status="canceled"))
            except Exception as e:
                logger.info("Cancelling stale agent leg %s: %s", leg.sid, e)
            return await get_transfer(call_sid) is not None
        return False

    async def handle_no_answer(self, call_sid: str, agent_call_sid: str = "",
                               leg_status: str = "") -> None:
        """The current agent leg ended without bridging (``leg_status`` is
        Twilio's CallStatus, or "" for an answered leg with no keypress).
        Try the next target in priority order, or fall back to voicemail +
        callback.

        ``agent_call_sid`` names the leg the status was for; a status for a
        leg that is no longer current changes nothing."""
        state = await get_transfer(call_sid)
        if not state:
            return
        leg = state.agent_call_sid
        if agent_call_sid and leg not in (agent_call_sid, DIALING):
            logger.info("Stale agent status for %s (leg %s, current %s) — ignored",
                        call_sid, agent_call_sid, leg)
            return
        next_idx = state.current_target_idx + 1
        changes = ({"current_target_idx": next_idx, "agent_call_sid": DIALING,
                    "agent_answered": False}
                   if next_idx < len(state.targets) else {"agent_call_sid": DIALING})
        if not await claim_transfer(
                call_sid, {"current_target_idx": state.current_target_idx,
                           "agent_call_sid": leg}, changes):
            return              # a concurrent callback already advanced it
        await report.leg(call_sid, state,
                         _LEG_STATUS_EVENT.get(leg_status, "no_keypress"),
                         agent_call_sid=agent_call_sid or leg or "")
        if next_idx < len(state.targets):
            if not await self._dial_from(call_sid, next_idx):
                state = await get_transfer(call_sid)
                if state is not None:
                    await self._close_with_callback(
                        call_sid, state, "failed",
                        "no further target could be dialled")
        else:
            await self._fallback(call_sid, state)

    async def _fallback(self, call_sid: str, state: TransferState) -> None:
        """Every target was tried: leave voicemail on the last target, tell
        the caller someone will call back."""
        last_idx = len(state.targets) - 1
        last_target = state.targets[last_idx]
        vm_twiml = voicemail_twiml(state.config.get("voicemail_script", ""),
                                   voice=self.say_voice,
                                   audio_url=self.audio_url(state, "voicemail"))
        try:
            # DetectMessageEnd: Twilio runs the TwiML after the greeting and
            # beep, so the script lands in the recording instead of over the
            # greeting. A human who answers hears it straight away.
            vm = await self._rest(lambda: self.client.calls.create(
                to=last_target["phone_number"],
                from_=self.from_number,
                twiml=vm_twiml,
                machine_detection="DetectMessageEnd",
            ))
            await report.leg(call_sid, state, "voicemail_left", attempt=last_idx,
                             agent_call_sid=vm.sid)
        except Exception as e:
            logger.error("Voicemail dial failed for %s: %s", call_sid, e)
            await report.leg(call_sid, state, "voicemail_failed", attempt=last_idx,
                             agent_call_sid="", detail=str(e)[:200])
        await self._close_with_callback(call_sid, state, "fallback_callback")
        logger.info("Escalation fallback complete for %s (voicemail left)", call_sid)

    async def _close_with_callback(self, call_sid: str, state: TransferState,
                                   result: str, detail: str = "") -> None:
        """The transfer ends without an agent: it is resolved, the caller
        hears the callback message, and the call ends."""
        await report.resolve(call_sid, state, result, detail=detail)
        # Twilio fetches the voicemail and callback lines AFTER this point.
        await remove_transfer(call_sid, keep_audio=("voicemail", "callback"))
        cb_twiml = callback_twiml(
            state.config.get("caller_callback_message", ""),
            voice=self.say_voice, audio_url=self.audio_url(state, "callback"))
        try:
            await self._rest(lambda: self.client.calls(call_sid).update(twiml=cb_twiml))
        except Exception as e:
            logger.error("Caller callback TwiML failed for %s: %s", call_sid, e)

    async def handle_agent_completed(self, call_sid: str, agent_call_sid: str) -> None:
        """The current agent leg ended with status ``completed``: after a
        bridge the transfer is over; without one (answered, no keypress —
        or the target's voicemail took the whisper) it is a no-answer."""
        state = await get_transfer(call_sid)
        if not state:
            return
        if state.connected:
            if not agent_call_sid or agent_call_sid == state.agent_call_sid:
                logger.info("Call %s: agent completed after connection", call_sid)
                await report.resolve(call_sid, state, "connected",
                                     detail="agent hung up")
                await remove_transfer(call_sid)
            return
        await self.handle_no_answer(call_sid, agent_call_sid, leg_status="")

    async def handle_caller_gone(self, call_sid: str) -> None:
        """The caller's call ended. Bridged: the conference already ended
        both legs — only the state goes. On hold: a pending agent leg is
        cancelled (ringing) or ended (answered, mid-whisper), so no agent
        joins an empty conference."""
        state = await get_transfer(call_sid)
        if not state:
            return
        leg = state.agent_call_sid
        if state.connected:
            await report.resolve(call_sid, state, "connected",
                                 detail="caller hung up")
            await remove_transfer(call_sid)
            logger.info("Call %s: caller left the bridged call — transfer closed",
                        call_sid)
            return
        status = "completed" if state.agent_answered else "canceled"
        await report.resolve(call_sid, state, "caller_abandoned",
                             detail=f"agent leg {status}" if leg and leg != DIALING
                             else "while dialling")
        await remove_transfer(call_sid)
        if not leg or leg == DIALING:
            logger.info("Call %s: caller hung up while dialling — transfer closed",
                        call_sid)
            return
        logger.info("Call %s: caller hung up on hold — %s agent leg %s",
                    call_sid, status, leg)
        try:
            await self._rest(lambda: self.client.calls(leg).update(status=status))
        except Exception as e:
            logger.error("Cancelling agent leg %s for %s failed: %s", leg, call_sid, e)
