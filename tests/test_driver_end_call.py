"""The driver's own ending reaches the turn service (voice-eval-c6819d60, 2026-10-10).

An unanswered opening: the turn side answered a silence tick with end_call, end_reason
NO_ANSWER, and no farewell. _finalize_call marks the call DONE, then awaits the end_call
POST. The read loop — still receiving the far end's frames — saw DONE on the next frame
and returned; asyncio.wait(FIRST_COMPLETED) then cancelled the processor, which was the
task inside _finalize_call. The POST never landed, nothing logged it (CancelledError is
not an Exception), and the call was never finalized: no CALL_ENDED row, no NO_ANSWER.

The turn client here lands end_call only after an await, as an HTTP POST does.

    cd voice-gateway
    python3 -m pytest tests/test_driver_end_call.py -q
"""
from __future__ import annotations

import asyncio

from gateway_dependency_kit import MockTurnClient, MockVoiceTransport, MockWebSocket, install_turn_client

import vogon.websocket.handler as handler
from vogon.websocket.session import CallSession
from vogon_contract import EndCallResponse, ProcessEventsResponse, UtteranceFinal


class _PostingTurnClient(MockTurnClient):
    """end_call lands after a round trip (the request is on the wire, then the reply)."""

    async def end_call(self, request):
        await asyncio.sleep(0.05)
        self.end_requests.append(request)
        return EndCallResponse()


def _run(monkeypatch, response: ProcessEventsResponse, *, far_end: str = "frames") -> _PostingTurnClient:
    turn, ws = _PostingTurnClient(), MockWebSocket()
    MockVoiceTransport.instances.clear()
    monkeypatch.setattr(handler, "TwilioTransport", MockVoiceTransport)
    install_turn_client(monkeypatch, turn, handler)

    async def no_redis():
        return None
    monkeypatch.setattr("vogon.gateway.redis_client.get_redis", no_redis)
    turn.queue_response(response)

    async def far_end_frames():
        if far_end == "hangup":
            # the far end hangs up while the end_call POST is on the wire
            await asyncio.sleep(0.02)
            ws.disconnect()
            return
        # the line stays open: the far end keeps streaming media while the call ends
        while True:
            ws.push_json({"event": "media", "media": {"payload": "", "track": "inbound"}})
            await asyncio.sleep(0.005)

    async def scenario():
        session = CallSession(call_id="end-call-1", session_token="t", config={"owner_id": "o", "assistant": {}})
        call = asyncio.create_task(handler.handle_call(
            ws, session, mode="twilio",
            twilio_start={"event": "start", "start": {"streamSid": "MZ", "callSid": "CA-END"}}))
        for _ in range(300):
            if MockVoiceTransport.instances:
                break
            await asyncio.sleep(0.01)
        frames = asyncio.create_task(far_end_frames())
        MockVoiceTransport.instances[0].emit(UtteranceFinal(text="...", confidence=1.0, low_conf_words=[], seq=1, at=5.0))
        try:
            await asyncio.wait_for(call, timeout=5)
        finally:
            frames.cancel()

    asyncio.run(scenario())
    return turn


def test_an_end_call_with_no_farewell_reaches_the_turn_service(monkeypatch):
    turn = _run(monkeypatch, ProcessEventsResponse(action="end_call", end_reason="NO_ANSWER"))
    assert [request.reason for request in turn.end_requests] == ["NO_ANSWER"], turn.end_requests


def test_the_call_ends_once(monkeypatch):
    """The teardown's own finalize (an unexplained ending) must not add a second end_call."""
    turn = _run(monkeypatch, ProcessEventsResponse(action="end_call", end_reason="NO_ANSWER"))
    assert len(turn.end_requests) == 1, turn.end_requests


def test_a_hangup_while_the_end_call_is_on_the_wire_does_not_cancel_it(monkeypatch):
    turn = _run(monkeypatch, ProcessEventsResponse(action="end_call", end_reason="NO_ANSWER"), far_end="hangup")
    assert [request.reason for request in turn.end_requests] == ["NO_ANSWER"], turn.end_requests
