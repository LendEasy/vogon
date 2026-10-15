"""Post-call facts about a warm transfer, reported to the turn service.

The agent legs resolve after the call's end_call, so their facts travel on
their own request — POST {turn_service}/record_transfer — which the turn
service appends to the call's closed ledger. Every fact is numbered by the
transfer's own counter (``next_seq``), so the ledger order is the order the
gateway observed, whatever order the requests land in.

Delivery: one background task per fact, retried with bounded backoff while
the turn service answers 409 (the call is still finalizing) or is
unreachable. A fact still undelivered after the last attempt is logged at
ERROR with its escalation id and seq — never dropped quietly.
Design: voice-gateway/docs/ESCALATION_WARM_TRANSFER.md §3.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Protocol

import httpx

from vogon.gateway_service.escalation.state import (
    TransferState,
    claim_transfer,
    get_transfer,
    next_seq,
)
from vogon_contract import (
    EscalationLegEvent,
    EscalationResolved,
    RecordTransferRequest,
)
from vogon_contract.codec import to_wire

logger = logging.getLogger(__name__)

# Waits between delivery attempts (seconds). end_call can take ~25 s to
# finalize (it awaits the call-end tools), during which the turn service
# answers 409; the tail covers a short turn-service outage.
# docs/voice-timing-constants.md §9.
_POST_CALL_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
# One delivery attempt.
_POST_CALL_HTTP_TIMEOUT_SEC = 5.0


class FactReporter(Protocol):
    def send(self, state: TransferState, fact) -> None: ...


class HttpFactReporter:
    """Delivers each fact on its own background task, with retries."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._transport = transport          # tests: an ASGI/mock transport

    def send(self, state: TransferState, fact) -> None:
        task = asyncio.create_task(self._deliver(
            state.report_url, state.report_token, state.call_id, fact))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, base_url: str, token: str, call_id: str, fact) -> None:
        url = f"{base_url.rstrip('/')}/record_transfer"
        body = to_wire(RecordTransferRequest(
            call_id=call_id, request_id=uuid.uuid4().hex, events=[fact]))
        headers = {"authorization": f"Bearer {token}"} if token else {}
        last = ""
        for attempt, delay in enumerate((0.0,) + _POST_CALL_RETRY_DELAYS_SEC):  # noqa: B007 — read after the loop
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=_POST_CALL_HTTP_TIMEOUT_SEC,
                                             transport=self._transport) as c:
                    r = await c.post(url, json=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 — retried, then reported
                last = f"{type(exc).__name__}: {exc}"
                continue
            if r.status_code < 300:
                return
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (409, 429) and r.status_code < 500:
                break           # refused for good (auth, unknown call, bad body)
        logger.error(
            "POST-CALL FACT UNDELIVERED | call=%s escalation=%s seq=%s kind=%s "
            "after %d attempt(s): %s", call_id, fact.escalation_id, fact.seq,
            fact.kind, attempt + 1, last)


_reporter: FactReporter = HttpFactReporter()


def reporter() -> FactReporter:
    return _reporter


def use_reporter(r: FactReporter) -> None:
    global _reporter
    _reporter = r


def _reportable(state: TransferState) -> bool:
    return bool(state.report_url and state.call_id and state.escalation_id)


async def leg(call_sid: str, state: TransferState, event: str, *,
              attempt: int | None = None, agent_call_sid: str = "",
              detail: str = "") -> None:
    """Report one agent-leg fact (EscalationLegEvent)."""
    if not _reportable(state):
        return
    seq = await next_seq(call_sid)
    if not seq:
        return              # the transfer is already gone
    idx = state.current_target_idx if attempt is None else attempt
    target = state.targets[idx]["label"] if 0 <= idx < len(state.targets) else ""
    _reporter.send(state, EscalationLegEvent(
        escalation_id=state.escalation_id, seq=seq, event=event,
        target=target, attempt=idx,
        agent_call_sid=agent_call_sid or (state.agent_call_sid or ""),
        detail=detail, at_utc=time.time()))


async def resolve(call_sid: str, state: TransferState, result: str, *,
                  detail: str = "") -> bool:
    """Report how the transfer ended (EscalationResolved) — exactly once per
    transfer: the first caller to claim it reports, every later one is a
    no-op. Call BEFORE removing the transfer (the seq needs it)."""
    if not await claim_transfer(call_sid, {"resolved": False}, {"resolved": True}):
        return False
    state = await get_transfer(call_sid) or state     # the latest counts
    if not _reportable(state):
        return True
    seq = await next_seq(call_sid)
    now = time.time()
    bridged_at = state.connected_utc if state.connected else 0.0
    hold_end = bridged_at or now
    connected_target = ""
    if state.connected and 0 <= state.current_target_idx < len(state.targets):
        connected_target = state.targets[state.current_target_idx]["label"]
    if state.audio_failed:
        said = ", ".join(f"{k} ({v})" for k, v in sorted(state.audio_failed.items()))
        detail = f"{detail}; " if detail else ""
        detail += f"voice: twilio <Say> for {said}"
    _reporter.send(state, EscalationResolved(
        escalation_id=state.escalation_id, seq=seq, result=result,
        connected_target=connected_target, attempts=state.legs_placed,
        hold_sec=round(max(0.0, hold_end - state.hold_started_utc), 2)
        if state.hold_started_utc else 0.0,
        talk_sec=round(max(0.0, now - bridged_at), 2) if bridged_at else 0.0,
        detail=detail, at_utc=now))
    return True
