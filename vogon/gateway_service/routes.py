"""The standalone gateway's HTTP + WS surface (plan §2.1, M1 item 5).

Tenant-scoped in the path; every lookup resolves against the SESSION STORE
(§2.4) — this process has no application database, no ORM and no
control-plane client. The application (or any authorized client) creates a
call with an opaque ``app_payload``; the driver relays it verbatim on
``start_call``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from vogon.gateway import redis_client
from vogon.gateway.audio.audio_timeline import build_timeline_from_dir
from vogon.gateway.logging_context import call_id_var
from vogon.gateway.settings import settings
from vogon.gateway_service.config import (
    GatewayConfig,
    gateway_config,
    resolve_call_snapshot,
    tenant_profile,
)
from vogon.gateway_service.escalation import callbacks as esc_callbacks
from vogon.gateway_service.escalation.state import (
    RedisTransferStore,
    use_transfer_store,
)
from vogon.gateway_service.escalation.transfer import TransferOrchestrator
from vogon.gateway_service.session_store import (
    CallSessionRecord,
    GatewaySessionStore,
    MemorySessionStore,
    RedisSessionStore,
)
from vogon.websocket import handler
from vogon.websocket.session import CallSession

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])

# ── Injectable process dependencies (same pattern as http_service) ──────────

_store: GatewaySessionStore | None = None
_instance_id = f"gw-{uuid.uuid4().hex[:8]}"


def store() -> GatewaySessionStore:
    global _store
    if _store is None:
        _store = MemorySessionStore()
    return _store


def use_store(s: GatewaySessionStore) -> None:
    global _store
    _store = s


async def default_store_from_config(cfg: GatewayConfig) -> GatewaySessionStore:
    if cfg.session_store == "redis":
        redis = await redis_client.get_redis()
        if redis is None:
            raise RuntimeError(
                "GATEWAY_SESSION_STORE=redis but no Redis client is "
                "available — the session store is the gateway's one "
                "persistence dependency; refusing to fall back silently")
        # Escalation callbacks outlive the call's socket and land on any
        # worker: the transfer state lives beside the session records.
        use_transfer_store(RedisTransferStore(redis))
        return RedisSessionStore(redis)
    return MemorySessionStore()


# ── Auth (decision 2026-08-25): SERVICE trust, not tenant keys ──────────────
# This surface has one caller — the application — which already
# authenticated its customer against the control plane. The bearer is the
# app↔gateway pair secret; the {tenant} in the path is DATA supplied by
# that trusted caller. Customer keys never travel past the app's front
# door (the double-validation loop this replaces).


def _authorize(tenant: str, request) -> None:
    cfg = gateway_config()
    if not cfg.auth_enabled:
        return
    header = request.headers.get("authorization", "")
    if header.removeprefix("Bearer ").strip() != cfg.service_key:
        raise HTTPException(status_code=401, detail="bad service key")


# ── Call creation ───────────────────────────────────────────────────────────


class AppBinding(BaseModel):
    # The Turn API root this call's turns are posted to. REQUIRED and
    # non-empty: an empty value used to fall through build_turn_client's
    # selection to the in-process client, which ran the reasoning stack
    # inside the gateway process. That client is gone, so an empty base_url
    # now has no fallback — it is a 422 at call creation instead of a
    # failure on the first turn.
    base_url: str = Field(min_length=1)
    auth_token: str = ""
    status_webhook: str = ""

    @field_validator("base_url")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("app.base_url must not be blank")
        return v


class TwilioDial(BaseModel):
    model_config = {"populate_by_name": True}

    to: str
    from_: str = Field(default="", alias="from")


class CreateCallBody(BaseModel):
    mode: str                      # twilio | widget | text
    app: AppBinding
    app_payload: dict = {}
    route: str = ""
    twilio: TwilioDial | None = None
    # Optional CLIENT-SUPPLIED call id (item 5 remainder): the application
    # may bring its own id so its business record (CallRecord) and the
    # gateway's session record share ONE key. Must be unique; a duplicate
    # is a 409. Empty = the gateway mints one.
    call_id: str = ""


@router.post("/v1/tenants/{tenant}/calls")
async def create_call(tenant: str, body: CreateCallBody, request: Request):
    _authorize(tenant, request)
    if body.mode not in ("twilio", "widget", "text"):
        raise HTTPException(status_code=422, detail="mode must be twilio|widget|text")
    if body.mode == "twilio" and body.twilio is None:
        raise HTTPException(status_code=422, detail="twilio block required")

    call_id = body.call_id or f"gwc-{uuid.uuid4().hex}"
    if "/" in call_id or ".." in call_id:
        raise HTTPException(status_code=422, detail="invalid call_id")
    if body.call_id and await store().get(tenant, call_id) is not None:
        raise HTTPException(status_code=409, detail="call_id already exists")
    call_id_var.set(call_id)
    token = uuid.uuid4().hex
    rec = CallSessionRecord(
        tenant=tenant, call_id=call_id, session_token=token,
        mode=body.mode, route=body.route,
        app_base_url=body.app.base_url.rstrip("/"),
        app_auth_token=body.app.auth_token,
        app_status_webhook=body.app.status_webhook,
        app_payload=body.app_payload,
    )
    await store().create(rec)

    cfg = gateway_config()
    base = (cfg.public_base_url or str(request.base_url)).rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

    provider_call_id = ""
    if body.mode == "twilio":
        await store().transition(tenant, call_id, "dialing")
        provider_call_id = await _place_twilio_call(body.twilio, base, token)
        await store().bind_provider_id(tenant, call_id, provider_call_id)

    logger.info(
        "CALL CREATED | tenant=%s | call_id=%s | mode=%s | route=%s | "
        "provider_call_id=%s | app=%s",
        tenant, call_id, body.mode, body.route or "(default)",
        provider_call_id or "(none)", rec.app_base_url or "(none)")
    return {
        "call_id": call_id,
        "session_token": token,
        "ws_url": f"{ws_base}/ws/call?token={token}",
        "provider_call_id": provider_call_id,
    }


@router.get("/v1/tenants/{tenant}/calls/{call_id}")
async def get_call(tenant: str, call_id: str, request: Request):
    _authorize(tenant, request)
    rec = await store().get(tenant, call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "call_id": rec.call_id, "state": rec.state, "mode": rec.mode,
        "route": rec.route, "provider_call_id": rec.provider_call_id,
        "created_at_utc": rec.created_at_utc,
        "connected_at_utc": rec.connected_at_utc,
        "ended_at_utc": rec.ended_at_utc,
    }


async def _place_twilio_call(dial: TwilioDial, public_base: str,
                             token: str) -> str:
    """The REST dial, moved from the application (plan §2.1). Uses the
    shared Twilio credentials (M1: one repo, one env)."""


    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    call = client.calls.create(
        to=dial.to,
        from_=dial.from_ or settings.twilio_phone_number,
        url=f"{public_base}/v1/twilio/twiml?token={token}",
        # PIN THE VERB. Twilio defaults `url` to POST, and the extraction
        # dropped the monolith's explicit method="GET" — so Twilio POSTed to
        # a GET-only route and every answered call got 405 Method Not
        # Allowed, which the caller hears as "an application error has
        # occurred" (measured in production 2026-08-29). The route now
        # accepts both verbs, and the dial states the one it wants rather
        # than inheriting a provider default.
        method="GET",
        status_callback=f"{public_base}/v1/twilio/status",
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    return call.sid


# ── Twilio webhooks (transport-physical: they live HERE now) ────────────────


def _twilio_signed(request: Request, params: dict) -> bool:
    """True when X-Twilio-Signature matches the URL Twilio called — every
    Twilio webhook that changes state checks it (the call-status webhook,
    the escalation callbacks). Twilio signs the PUBLIC URL — behind nginx's
    /gw/ strip the app sees a different one — so it is rebuilt from
    public_base_url, the same base every callback URL is registered with.
    No auth token configured (dev) = no check: such a gateway cannot place
    a call either."""
    token = settings.twilio_auth_token
    if not token:
        return True
    base = gateway_config().public_base_url.rstrip("/")
    url = (f"{base}{request.url.path}" if base else
           str(request.url.replace(query="")))
    if request.url.query:
        url = f"{url}?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature", "")
    ok = RequestValidator(token).validate(url, params, signature)
    if not ok:
        logger.warning("Twilio signature REFUSED | %s %s", request.method, url)
    return ok


@router.api_route("/v1/twilio/twiml", methods=["GET", "POST"])
async def twiml(request: Request, token: str = Query(...)):
    # Signed like every other Twilio webhook (2026-10-01). The dial pins
    # GET, so the signature is over the URL with its ?token= query and no
    # form; a POST (an older dial, or a number pointed here by hand) signs
    # its form too.
    form = dict(await request.form()) if request.method == "POST" else {}
    if not _twilio_signed(request, form):
        raise HTTPException(status_code=403, detail="bad Twilio signature")
    rec = await store().get_by_token(token)
    if rec is None or rec.mode != "twilio":
        # The caller hears a hangup, so this must never be silent in the log.
        logger.warning("TwiML REFUSED | no live twilio call for this token")
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>'
            "This call cannot be completed.</Say><Hangup/></Response>",
            media_type="application/xml")
    call_id_var.set(rec.call_id)
    cfg = gateway_config()
    base = cfg.public_base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    logger.info("TwiML served | call_id=%s | stream=%s/ws/twilio",
                rec.call_id, ws_base)
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f'<Stream url="{ws_base}/ws/twilio"/></Connect></Response>',
        media_type="application/xml")


_TWILIO_STATE = {
    "initiated": "dialing", "queued": "dialing", "ringing": "ringing",
    "in-progress": "connected", "answered": "connected",
    "completed": "ended", "busy": "failed", "no-answer": "failed",
    "canceled": "cancelled", "failed": "failed",
}


@router.post("/v1/twilio/status")
async def twilio_status(request: Request):
    form = dict(await request.form())
    if not _twilio_signed(request, form):
        raise HTTPException(status_code=403, detail="bad Twilio signature")
    sid = str(form.get("CallSid") or "")
    status = str(form.get("CallStatus") or "")
    # A caller in the escalation hold conference is no longer on a session
    # socket; their hang-up arrives only here. Stop the pending agent leg.
    # An independent effect: its failure must not cost this webhook the
    # session transition and status relay below, so it is logged loudly
    # and the webhook carries on.
    try:
        await esc_callbacks.caller_status_body(sid, status,
                                               escalation_orchestrator)
    except Exception:  # noqa: BLE001
        logger.error("escalation caller-status hook failed for %s (status=%s)",
                     sid, status, exc_info=True)
    rec = await store().get_by_provider_id(sid)
    if rec is None:
        logger.warning("Twilio status for an unknown CallSid %s (status=%s)",
                       sid, status)
        return {"ok": False}
    call_id_var.set(rec.call_id)
    state = _TWILIO_STATE.get(status)
    logger.info("Twilio status | call_id=%s | %s -> %s", rec.call_id, status,
                state or "(no mapping — ignored)")
    if state:
        await store().transition(rec.tenant, rec.call_id, state)
        await _relay_status(rec, state)
    return {"ok": True}


async def _relay_status(rec: CallSessionRecord, state: str) -> None:
    """Best-effort relay to the application's status webhook (§2.1) — how
    CallRecord.state stays in sync without the gateway touching the app DB."""
    if not rec.app_status_webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            cfg = gateway_config()
            headers = ({"authorization": f"Bearer {cfg.service_key}"}
                       if cfg.auth_enabled else {})
            await client.post(rec.app_status_webhook, headers=headers, json={
                "call_id": rec.call_id, "state": state,
                "provider_call_id": rec.provider_call_id,
            })
    except Exception as exc:
        logger.warning("status relay failed for %s: %s", rec.call_id, exc)


# ── Call artifacts (M1 item 8) — the gateway serves what it writes ──────────
# Files under recordings_path are written by THIS process (WAV at socket
# close, audio events + frame log at transport close). Access is
# tenant-checked through the session record; the flat call_id-keyed layout
# is safe because call_ids are gateway-minted UUIDs (see artifact_store.py).


async def _artifact_record(tenant: str, call_id: str, request: Request):
    _authorize(tenant, request)
    rec = await store().get(tenant, call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return rec


def _artifact_path(call_id: str, suffix: str):
    if "/" in call_id or ".." in call_id:
        raise HTTPException(status_code=400, detail="invalid call id")
    path = Path(settings.recordings_path) / f"{call_id}{suffix}"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{path.name} not found")
    return path


@router.get("/v1/tenants/{tenant}/calls/{call_id}/recording.wav")
async def artifact_recording(tenant: str, call_id: str, request: Request):
    await _artifact_record(tenant, call_id, request)
    return FileResponse(_artifact_path(call_id, ".wav"), media_type="audio/wav")


@router.get("/v1/tenants/{tenant}/calls/{call_id}/audio-events.json")
async def artifact_audio_events(tenant: str, call_id: str, request: Request):
    await _artifact_record(tenant, call_id, request)
    return FileResponse(_artifact_path(call_id, ".audio_events.json"),
                        media_type="application/json")


@router.get("/v1/tenants/{tenant}/calls/{call_id}/frames.csv")
async def artifact_frames(tenant: str, call_id: str, request: Request):
    await _artifact_record(tenant, call_id, request)
    return FileResponse(_artifact_path(call_id, ".frames.csv"),
                        media_type="text/csv")


@router.get("/v1/tenants/{tenant}/calls/{call_id}/audio-timeline")
async def artifact_timeline(tenant: str, call_id: str, request: Request,
                            vad_mode: int = Query(2, ge=0, le=3)):


    await _artifact_record(tenant, call_id, request)
    _artifact_path(call_id, ".wav")          # 404 early, clear message
    html = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: build_timeline_from_dir(Path(settings.recordings_path),
                                        call_id, vad_mode),
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ── Escalation callbacks (Twilio → gateway; public under /gw/) ──────────────
# The warm-transfer machinery is gateway-owned (2026-08-25); calls created
# through the gateway get callback URLs pointing HERE.

ESCALATION_PREFIX = "/v1/escalation"

_esc_orchestrator: TransferOrchestrator | None = None


def escalation_orchestrator() -> TransferOrchestrator:
    global _esc_orchestrator
    if _esc_orchestrator is None:
        cfg = gateway_config()
        _esc_orchestrator = TransferOrchestrator(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_phone_number,
            data_plane_base_url=cfg.public_base_url,
            callback_prefix=ESCALATION_PREFIX,
            say_voice=cfg.escalation_say_voice,
        )
    return _esc_orchestrator


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@router.get(ESCALATION_PREFIX + "/whisper/{call_sid}")
async def esc_whisper(call_sid: str, request: Request):
    if not _twilio_signed(request, {}):
        raise HTTPException(status_code=403, detail="bad Twilio signature")
    cfg = gateway_config()
    return _twiml(await esc_callbacks.whisper_body(
        call_sid, cfg.public_base_url, ESCALATION_PREFIX,
        agent_call_sid=str(request.query_params.get("CallSid") or ""),
        voice=cfg.escalation_say_voice))


@router.post(ESCALATION_PREFIX + "/agent-keypress/{call_sid}")
async def esc_keypress(call_sid: str, request: Request):
    form = dict(await request.form())
    if not _twilio_signed(request, form):
        raise HTTPException(status_code=403, detail="bad Twilio signature")
    return _twiml(await esc_callbacks.keypress_body(
        call_sid, agent_call_sid=str(form.get("CallSid") or ""),
        voice=gateway_config().escalation_say_voice))


@router.get(ESCALATION_PREFIX + "/audio/{token}.mp3")
async def esc_audio(token: str):
    """A transfer line rendered in the persona's voice, fetched by Twilio's
    <Play>. Not signature-checked: the 128-bit token in the URL is the
    capability (Twilio is the only party that is ever given it), and the
    audio is deleted with the transfer."""
    audio = await esc_callbacks.audio_body(token)
    if audio is None:
        raise HTTPException(status_code=404, detail="no such audio")
    return Response(content=audio, media_type="audio/mpeg")


@router.post(ESCALATION_PREFIX + "/agent-status/{call_sid}")
async def esc_status(call_sid: str, request: Request):
    form = dict(await request.form())
    if not _twilio_signed(request, form):
        raise HTTPException(status_code=403, detail="bad Twilio signature")
    return await esc_callbacks.status_body(
        call_sid, str(form.get("CallStatus") or ""), escalation_orchestrator(),
        agent_call_sid=str(form.get("CallSid") or ""))


# ── The WS endpoints — resolve against the store, then drive the driver ─────


def _session_config(rec: CallSessionRecord) -> dict:
    """What the driver may know, assembled from the record: the opaque
    payload, the per-call turn-service binding, and the metrics scope
    ({tenant}/{route} partitioning — plan §2.6 item 4)."""
    async def _save_snapshot(call_config) -> None:
        # The precedence merge, frozen once at call start (§2.5): any
        # replica, a resume, and the audit trail see the same values.
        profile = tenant_profile(rec.tenant)
        await store().save_call_config(
            rec.tenant, rec.call_id,
            resolve_call_snapshot(profile, call_config))

    return {
        "app_payload": rec.app_payload,
        "turn_service": {"base_url": rec.app_base_url,
                         "auth_token": rec.app_auth_token},
        "metrics_label": f"{rec.tenant}/{rec.route}" if rec.route
                         else rec.tenant,
        # Gateway-mode escalation: callbacks land on the GATEWAY's public
        # surface (rides the /gw/ nginx location).
        "escalation_callback": {
            "base_url": gateway_config().public_base_url,
            "prefix": ESCALATION_PREFIX,
            "say_voice": gateway_config().escalation_say_voice,
        },
        "on_call_config": _save_snapshot,
    }


async def _drive(ws: WebSocket, rec: CallSessionRecord, mode: str,
                 twilio_start: dict | None = None) -> None:

    # Every socket mode funnels through here, so the attach/detach pair is
    # logged once rather than per transport. handler.handle_call binds the call id
    # for its own duration; these two lines sit outside it, so bind it here.
    call_id_var.set(rec.call_id)
    if not await store().claim_socket(rec.tenant, rec.call_id, _instance_id):
        logger.warning("socket for %s already claimed elsewhere", rec.call_id)
        await ws.close(code=4003)
        return
    await store().transition(rec.tenant, rec.call_id, "connected")
    session = CallSession(call_id=rec.call_id,
                          session_token=rec.session_token,
                          config=_session_config(rec))
    _t0 = time.monotonic()
    logger.info("MEDIA ATTACHED | call_id=%s | tenant=%s | mode=%s | route=%s",
                rec.call_id, rec.tenant, mode, rec.route or "(default)")
    try:
        await handler.handle_call(ws, session, mode=mode, twilio_start=twilio_start)
    finally:
        logger.info("MEDIA DETACHED | call_id=%s | mode=%s | duration=%.1fs",
                    rec.call_id, mode, time.monotonic() - _t0)
        await store().transition(rec.tenant, rec.call_id, "ended")
        await _relay_status(rec, "ended")


@router.websocket("/ws/call")
async def ws_call(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()
    rec = await store().get_by_token(token)
    if rec is None:
        await websocket.send_text(json.dumps({
            "type": "server.error",
            "payload": {"code": "invalid_token", "fatal": True}}))
        await websocket.close(code=4001)
        return
    mode = "text" if rec.mode == "text" else "voice"
    await _drive(websocket, rec, mode)


@router.websocket("/ws/twilio")
async def ws_twilio(websocket: WebSocket):
    """Twilio strips WS query params: accept, read frames until 'start',
    resolve the CallSid against the store (main.py did this against the app
    DB — the store replaces it)."""
    await websocket.accept()
    start_msg: dict | None = None
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            raw = await asyncio.wait_for(websocket.receive_text(),
                                         timeout=remaining)
            msg = json.loads(raw)
            if msg.get("event") == "start":
                start_msg = msg
                break
    except (asyncio.TimeoutError, Exception):
        await websocket.close(code=4001)
        return
    if start_msg is None:
        await websocket.close(code=4001)
        return
    call_sid = (start_msg.get("start", {}).get("callSid")
                or start_msg.get("callSid", ""))
    rec = await store().get_by_provider_id(call_sid)
    if rec is None:
        logger.warning("Twilio WS: unknown CallSid %s", call_sid)
        await websocket.close(code=4002)
        return
    await _drive(websocket, rec, "twilio", twilio_start=start_msg)
