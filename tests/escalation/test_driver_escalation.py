"""The driver's side of the transfer: ordering against the media stream.

Redirecting the caller into the hold conference makes Twilio stop the
media stream. That "stop" ends the socket read loop, and the driver used
to cancel the turn that was performing the transfer — the transfer only
survived because the orchestrator swallowed the CancelledError and called
the stop "proof" the hold worked. These pin the ordering without it:

  1. a stop that lands while the hold update is in flight does not cancel
     the transfer: exactly one end_call, reason "escalated", carrying the
     REAL initiation outcome (here: failed)
  2. the handoff line is HEARD before the redirect — the transfer waits
     for the transport's playout to drain (Twilio drops buffered audio on
     a call update)

    cd voice-gateway
    python3 -m pytest tests/escalation/test_driver_escalation.py -q
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from gateway_dependency_kit import (
    MockTurnClient,
    MockVoiceTransport,
    MockWebSocket,
    install_turn_client,
)

import vogon.websocket.handler as handler
from vogon.gateway import settings as gw_settings
from vogon.websocket.session import CallSession
from vogon_contract import (
    EscalationOrder,
    EscalationOutcome,
    EscalationTarget,
    ProcessEventsResponse,
    SpeakCommand,
    UtteranceFinal,
)


def _order():
    return EscalationOrder(
        targets=[EscalationTarget(number="+15550001111", label="T1")],
        whisper_text="w")


class _RecordingTransport(MockVoiceTransport):
    log: list[str] = []

    async def send_response(self, text, audio_id, protected=False,
                            absorb_barge=False):
        self.log.append(f"sent:{audio_id}")
        return await super().send_response(text, audio_id, protected,
                                           absorb_barge)

    async def wait_playout_heard(self, cap_sec):
        self.log.append("heard")


def _run_call(monkeypatch, fake_execute, transport_cls=MockVoiceTransport,
              while_executing=None, order=None, persona_voice=None):

    turn, ws = MockTurnClient(), MockWebSocket()
    if persona_voice is not None:
        turn.call_config = replace(turn.call_config, persona_voice=persona_voice)
    transport_cls.instances.clear()
    monkeypatch.setattr(handler, "TwilioTransport", transport_cls)
    install_turn_client(monkeypatch, turn, handler)

    async def no_redis():
        return None
    monkeypatch.setattr("vogon.gateway.redis_client.get_redis", no_redis)
    monkeypatch.setattr(
        "vogon.gateway_service.escalation.transfer.TransferOrchestrator.execute",
        fake_execute)
    turn.queue_response(ProcessEventsResponse(
        speak=[SpeakCommand(audio_id="handoff-1", text="Connecting you now.")],
        action="end_call", end_reason="escalated", escalation=order or _order()))

    async def scenario():
        session = CallSession(call_id="esc-drv-1", session_token="t",
                              config={"owner_id": "o", "assistant": {}})
        task = asyncio.create_task(handler.handle_call(
            ws, session, mode="twilio",
            twilio_start={"event": "start",
                          "start": {"streamSid": "MZ", "callSid": "CA-ESC"}}))
        for _ in range(300):
            if transport_cls.instances:
                break
            await asyncio.sleep(0.01)
        transport_cls.instances[0].emit(UtteranceFinal(
            text="human please", confidence=1.0, low_conf_words=[], seq=1, at=5.0))
        if while_executing is not None:
            await while_executing(ws)
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    return turn


def test_a_stop_during_the_hold_update_does_not_cancel_the_transfer(monkeypatch):
    gate = {}

    async def fake_execute(self, call_sid, order, **kw):
        gate["in_hold"].set()
        await asyncio.sleep(0.2)       # the REST round-trip; "stop" lands here
        return "failed"                # the caller had already hung up

    async def stop_mid_hold(ws):
        gate["in_hold"] = asyncio.Event()     # created inside the running loop
        await gate["in_hold"].wait()
        ws.push_twilio_stop()

    turn = _run_call(monkeypatch, fake_execute, while_executing=stop_mid_hold)
    assert len(turn.end_requests) == 1, turn.end_requests
    (end_req,) = turn.end_requests
    assert end_req.reason == "escalated"
    outs = [e for e in end_req.events if isinstance(e, EscalationOutcome)]
    assert [o.status for o in outs] == ["failed"]


def test_the_handoff_line_is_heard_before_the_caller_is_redirected(monkeypatch):
    _RecordingTransport.log = []

    async def fake_execute(self, call_sid, order, **kw):
        _RecordingTransport.log.append("redirect")
        return "initiated"

    _run_call(monkeypatch, fake_execute, transport_cls=_RecordingTransport)
    log = _RecordingTransport.log
    assert "redirect" in log, log
    assert log.index("sent:handoff-1") < log.index("heard") < log.index("redirect"), log


def test_the_driver_hands_the_transfer_its_report_target_and_voice(monkeypatch, fake_render):
    """What the driver passes the orchestrator: where post-call facts go
    (the call's turn service), the hold line rendered in the persona voice,
    and which lines are still rendering; the outcome carries the
    escalation id and the hold line's voice."""
    seen = {}

    async def fake_execute(self, call_sid, order, **kw):
        seen.update(kw)
        return "initiated"

    real = handler.handle_call

    async def with_turn_service(ws, session, **kw):
        session.config["turn_service"] = {"base_url": "http://turn/turn/v1",
                                          "auth_token": "tok"}
        return await real(ws, session, **kw)
    monkeypatch.setattr(handler, "handle_call", with_turn_service)
    turn = _run_call(monkeypatch, fake_execute,
                     order=EscalationOrder(escalation_id="esc-7",
                                           targets=[EscalationTarget(number="+1555", label="T1")],
                                           whisper_text="w"))
    assert seen["call_id"] == "esc-drv-1"
    assert seen["report_url"] == "http://turn/turn/v1" and seen["report_token"] == "tok"
    assert set(seen["audio"]) == {"hold"}
    assert set(seen["audio_rendering"]) == {"whisper", "callback"}
    (out,) = [e for e in turn.end_requests[0].events if isinstance(e, EscalationOutcome)]
    assert out.escalation_id == "esc-7" and out.detail == "hold line: persona voice"
    assert {v for _, v in fake_render.calls} == {"aura-test"}     # the call's voice


def test_a_call_without_a_persona_voice_transfers_in_the_default_voice(monkeypatch, fake_render):
    """The call spoke in the deployment's default TTS voice (no persona
    voice): the transfer's lines are rendered in that same voice, not handed
    to Twilio's — resolved exactly as the call's TTSClient resolves it."""

    async def fake_execute(self, call_sid, order, **kw):
        return "initiated"
    _run_call(monkeypatch, fake_execute, persona_voice="")
    assert fake_render.calls
    assert {v for _, v in fake_render.calls} == {gw_settings.settings.deepgram_tts_model}
