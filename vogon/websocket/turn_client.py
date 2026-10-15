"""TurnClient — the driver's seam to the turn service (Phase 1, M1 item 3).

The driver never calls ``app.turns.api``: it drives ``HttpTurnClient``, which
speaks the Turn API triad as ``POST /turn/v1/*`` against the application's
``base_url`` (``app/turns/http_service.py`` serves it). One outstanding
request per call (a lock — the gateway's ordering guarantee), a bounded
retry ladder on transport errors and 5xx (safe: start_call replays the
stored opening, process_events replays by request_id, end_call is
idempotent), bearer auth from the per-call binding or settings.

The in-process client is GONE (2026-08-29). It wrapped ``api.*`` in the same
process and quarantined the residue of sharing one — TurnContext, config
dict, DB session, redis handle, the on_context binding, the teardown
ordering. Production has run over HTTP since M1, and the gateway now refuses
a call whose ``app.base_url`` is empty, so nothing selected it any more:
what it bought was a second, unexercised execution path with different
failure semantics from the one that serves calls. Driver tests reach the
turn side either through a scripted client double or through the real
handlers over ASGI (``tests/gateway_dependency_kit.py``).

This module still lives driver-side (app/websocket); with no reason left to
import ``app.turns`` it can move into the gateway package proper.
"""
from __future__ import annotations

import asyncio
import dataclasses as _dc
import logging
import time
from typing import Optional, Protocol

import httpx

from vogon.gateway.settings import settings
from vogon_contract import (
    EndCallRequest,
    EndCallResponse,
    ProcessEventsRequest,
    ProcessEventsResponse,
    StartCallRequest,
    StartCallResponse,
)
from vogon_contract.codec import (
    end_call_response_from_wire,
    process_events_response_from_wire,
    shift_event_times,
    start_call_response_from_wire,
    to_wire,
)

logger = logging.getLogger(__name__)


class TurnClient(Protocol):
    # True once start_call has RETURNED. The driver's finally block reads it
    # to decide whether there is a call for it to finalize; an opening that
    # failed inside the turn service is finalized there (http_service's
    # _finalize_failed_start), on the context only that side ever held.
    started: bool

    async def start_call(self, request: StartCallRequest) -> StartCallResponse: ...
    async def process_events(self, request: ProcessEventsRequest) -> ProcessEventsResponse: ...
    async def end_call(self, request: EndCallRequest) -> EndCallResponse: ...
    async def close(self) -> None: ...


class TurnServiceNotConfigured(RuntimeError):
    """No turn-service address resolved for this call — a deployment or
    call-creation mistake, never an outage. Kept distinct from
    TurnServiceUnavailable so the failure policy's trouble farewell (which
    retries and then apologises for an outage) never fires for it."""


class TurnServiceUnavailable(RuntimeError):
    """The retry ladder exhausted without an answer — the turn service is
    unreachable or persistently failing. The driver's failure policy owns
    what happens next (trouble farewell + best-effort end_call); this type
    exists so that policy never triggers on an ordinary bug's exception."""


class HttpTurnClient:
    """The same surface over the wire, with the M2 failure policy's retry
    ladder: transport errors and 5xx retry on a bounded backoff schedule
    (settings.turn_retry_delays, ~15 s total — a systemd restart of the
    turn service is survivable, with the filler hold masking the gap on
    utterance turns). Every replay is safe by construction: the server
    serializes per-call and a committed turn's response rides the
    checkpoint, so a retry can never reprocess it.

    end_call uses a SHORT ladder (2 attempts): the caller is already gone,
    the teardown must not stall the socket close, and the turn-side reaper
    finalizes any call whose end_call never landed."""

    def __init__(self, base_url: str, *, api_key: str = "",
                 timeout_sec: Optional[float] = None, transport=None,
                 epoch: Optional[float] = None):
        # base_url is the FULL Turn API root (…/turn/v1) per the plan §2.2:
        # the client appends only the operation name. (The live smoke caught
        # the double-prefix bug the ASGI tests could not: their base had no
        # path, so /turn/v1/turn/v1/start_call never appeared.)
        self._base_url = base_url.rstrip("/")
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_sec
                                  or settings.turn_service_timeout_sec),
            transport=transport,          # tests inject ASGI here
        )
        self._lock = asyncio.Lock()        # one outstanding request per call
        self.started = False
        # The call's t=0 on THIS side's clock (M1 item 7): every event time
        # crosses the wire as seconds since this moment. The gateway's clock
        # is authoritative; the turn service rebases onto its own.
        self._epoch = epoch if epoch is not None else time.monotonic()

    @staticmethod
    def _retry_delays() -> list[float]:
        raw = getattr(settings, "turn_retry_delays", "0.25,1,2,4,8") or ""
        try:
            return [float(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            return [0.25, 1.0, 2.0, 4.0, 8.0]

    async def _post(self, path: str, request, decode,
                    max_attempts: int | None = None):
        events = getattr(request, "events", None)
        if events:
            request = _dc.replace(
                request, events=shift_event_times(events, -self._epoch))
        wire = to_wire(request)
        delays = self._retry_delays()
        attempts = max_attempts if max_attempts is not None else len(delays) + 1
        async with self._lock:
            last_note = ""
            for attempt in range(1, attempts + 1):
                try:
                    r = await self._client.post(path, json=wire)
                except httpx.TransportError as exc:
                    last_note = f"transport error: {exc!r}"
                else:
                    if r.status_code < 500:
                        r.raise_for_status()   # 4xx is a CONTRACT error, not
                        return decode(r.json())  # an outage — never retried
                    last_note = f"HTTP {r.status_code}"
                if attempt < attempts:
                    delay = delays[min(attempt - 1, len(delays) - 1)]
                    logger.warning(
                        "turn service unavailable (%s) — attempt %d/%d, "
                        "retrying in %.2fs | %s",
                        last_note, attempt, attempts, delay, path)
                    await asyncio.sleep(delay)
            raise TurnServiceUnavailable(
                f"{path}: {attempts} attempts failed ({last_note})")

    async def start_call(self, request: StartCallRequest) -> StartCallResponse:
        response = await self._post(f"{self._base_url}/start_call", request,
                                    start_call_response_from_wire)
        self.started = True
        return response

    async def process_events(self, request: ProcessEventsRequest) -> ProcessEventsResponse:
        return await self._post(f"{self._base_url}/process_events", request,
                                process_events_response_from_wire)

    async def end_call(self, request: EndCallRequest) -> EndCallResponse:
        # Short ladder: teardown must not stall the socket close; a lost
        # end_call is the turn-side reaper's job.
        return await self._post(f"{self._base_url}/end_call", request,
                                end_call_response_from_wire, max_attempts=2)

    async def close(self) -> None:
        # end_call already ran the turn side's teardown (tools, log) inside
        # the request; the only local resource is the connection pool.
        await self._client.aclose()


def build_turn_client(*, config: dict) -> TurnClient:
    """The call's turn-service address, most specific first: the per-call
    binding on the session record (the standalone gateway's, §2.1), then
    ``settings.turn_service_url``.

    There is no fallback. An empty address used to select an in-process
    client that ran the reasoning stack inside the gateway; that client is
    gone, so an address that resolves to nothing is a configuration error
    and says so here rather than serving a call down a path production does
    not use. The gateway refuses a blank ``app.base_url`` at call creation,
    so this is the second line, not the first.

    (The db session, redis handle and instance id left this signature with
    the in-process client — they were ITS constructor arguments, the residue
    of sharing a process. The driver keeps its own copies for its own work.)
    """
    binding = (config or {}).get("turn_service") or {}
    url = binding.get("base_url") or getattr(settings, "turn_service_url", "")
    if not url:
        raise TurnServiceNotConfigured(
            "no turn-service address for this call: neither the session "
            "record's turn_service.base_url nor settings.turn_service_url "
            "is set"
        )
    return HttpTurnClient(
        url,
        api_key=(binding.get("auth_token") or settings.turn_service_api_key),
    )
