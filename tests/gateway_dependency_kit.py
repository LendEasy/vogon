"""Dependency kit for the gateway's driver tests.

These doubles implement the interfaces ``vogon.websocket.handler`` uses: a
WebSocket, a transport, and a Turn API client that returns scripted
responses. They deliberately model no planner, agent or workflow behaviour —
gateway tests script Turn API responses and assert the exact requests crossing
the gateway/turn boundary. The seam tests in the application's tree add
``install_asgi_turn_client`` (the REAL turn service behind ASGI) in their own
copy of this module, which re-exports everything here.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocketDisconnect

from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.delivery import DeliveryReport, estimate_total_ms
from vogon_contract import (
    CallConfig,
    EndCallRequest,
    EndCallResponse,
    ProcessEventsRequest,
    ProcessEventsResponse,
    StartCallRequest,
    StartCallResponse,
)


class MockWebSocket:
    """Queue-backed WebSocket with helpers for app and Twilio JSON frames."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent_text: list[dict] = []
        self.sent_bytes: list[bytes] = []

    def push_json(self, payload: dict) -> None:
        self._inbox.put_nowait({
            "type": "websocket.receive",
            "text": json.dumps(payload),
            "bytes": None,
        })

    def push_twilio_stop(self) -> None:
        self.push_json({"event": "stop"})

    def disconnect(self) -> None:
        self._inbox.put_nowait(None)

    async def receive(self) -> dict:
        item = await self._inbox.get()
        if item is None:
            raise WebSocketDisconnect(1000)
        return item

    async def send_text(self, text: str) -> None:
        self.sent_text.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)


@dataclass
class MockTurnClient:
    """Scriptable double for the ``TurnClient`` surface the driver drives.

    Shaped as the CLIENT (start_call / process_events / end_call / close),
    not as ``app.turns.api``: the driver reaches the turn service through
    one of the clients, and the in-process client that used to make an
    api-module patch visible to the driver is gone. Tests install this by
    replacing ``handler.build_turn_client``, so what they exercise is the
    driver against its real seam.

    ``started`` follows HttpTurnClient: true once start_call has returned.
    A start_call that raises leaves it false, and the driver's finally block
    finalizes nothing — the turn side owns that case (it finalizes its own
    failed opening inside the request).
    """

    call_config: CallConfig = field(default_factory=lambda: CallConfig(
        persona_voice="aura-test",
        persona_name="Mock Agent",
        workflow_id="mock-workflow",
        ambience={"enabled": False},
    ))
    opening: StartCallResponse | None = None
    # Raise this from start_call (the turn service down at the opening).
    start_error: BaseException | None = None
    # M2 failure policy: raise this from end_call (an unreachable turn
    # service at teardown) — the gateway's own teardown must still complete.
    end_call_error: BaseException | None = None
    process_responses: deque[Any] = field(default_factory=deque)
    start_requests: list[StartCallRequest] = field(default_factory=list)
    process_requests: list[ProcessEventsRequest] = field(default_factory=list)
    end_requests: list[EndCallRequest] = field(default_factory=list)
    started: bool = False
    closed: bool = False

    def queue_response(self, response: ProcessEventsResponse | BaseException) -> None:
        self.process_responses.append(response)

    async def start_call(self, request: StartCallRequest) -> StartCallResponse:
        self.start_requests.append(request)
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        return self.opening or StartCallResponse(call_config=self.call_config)

    async def process_events(self, request: ProcessEventsRequest) -> ProcessEventsResponse:
        self.process_requests.append(request)
        if self.process_responses:
            result = self.process_responses.popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        return ProcessEventsResponse()

    async def end_call(self, request: EndCallRequest) -> EndCallResponse:
        self.end_requests.append(request)
        if self.end_call_error is not None:
            raise self.end_call_error
        return EndCallResponse()

    async def close(self) -> None:
        # HttpTurnClient's close only drops the connection pool: the turn
        # side's teardown tail (call-end tools, then the log) ran server-side
        # inside end_call.
        self.closed = True


def install_turn_client(monkeypatch, turn: MockTurnClient, handler_mod=None):
    """Point the driver at ``turn``. The driver builds its client once per
    call through ``handler.build_turn_client``; replacing that is the whole
    seam."""
    if handler_mod is None:
        import vogon.websocket.handler as handler_mod  # noqa: PLC0415 — the default when the caller passes none
    monkeypatch.setattr(handler_mod, "build_turn_client",
                        lambda **kwargs: turn)
    return turn


class MockVoiceTransport(BaseTransport):
    """Controllable transport used for both Twilio and browser-voice modes."""

    instances: list[MockVoiceTransport] = []

    def __init__(self, *args, call_config=None, start_event=None, **kwargs):
        self.call_config = call_config
        self.start_event = start_event
        self._utterance_queue: asyncio.Queue = asyncio.Queue()
        self.started = False
        self.closed = False
        self.sent: list[dict] = []
        self.delivery_reports: deque[DeliveryReport] = deque()
        self.instances.append(self)

    def script_hold(self, report) -> None:
        """Arm the next begin_hold/end_hold span to report this outcome —
        the driver-local filler window (M1 item 4)."""
        self._hold_report = report

    def begin_hold(self) -> bool:
        return getattr(self, "_hold_report", None) is not None

    async def end_hold(self):
        report, self._hold_report = self._hold_report, None
        return report

    def queue_delivery(self, report: DeliveryReport) -> None:
        self.delivery_reports.append(report)

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send_response(
        self,
        text: str,
        audio_id: str,
        protected: bool = False,
        absorb_barge: bool = False,
    ) -> DeliveryReport:
        self.sent.append({
            "text": text,
            "audio_id": audio_id,
            "protected": protected,
            "absorb_barge": absorb_barge,
        })
        if self.delivery_reports:
            report = self.delivery_reports.popleft()
            if report.audio_id != audio_id:
                raise AssertionError(
                    f"scripted delivery {report.audio_id!r} does not match "
                    f"SpeakCommand {audio_id!r}"
                )
            return report
        return DeliveryReport(
            audio_id=audio_id,
            status="complete",
            chunks_sent=1,
            audio_ms=20.0,
            est_total_ms=estimate_total_ms(text),
            heard_text=text,
        )

    async def handle_binary_frame(self, data: bytes) -> None:
        return None

    async def handle_text_frame(self, msg: dict) -> bool:
        return msg.get("event") == "stop" or msg.get("type") == "client.hangup"

    @property
    def utterance_queue(self) -> asyncio.Queue:
        return self._utterance_queue

    def emit(self, event) -> None:
        self._utterance_queue.put_nowait(event)

    def transport_analytics(self) -> dict:
        return {
            "mock_voice_frames": {
                "stage": "transport",
                "latency_ms": 0.0,
                "metadata": {"count": 1},
            }
        }
