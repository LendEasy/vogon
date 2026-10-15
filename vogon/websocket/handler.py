"""The voice gateway's driver — one WebSocket call, from connect to teardown.

This half owns the socket, the transport, the send queue, the audio clocks and
the physical facts they produce. It owns no meaning: every decision about what
the call SAYS or what its state IS comes back from the Turn API
(``app/turns/api.py``), which it calls three ways.

    start_call     once, before the transport exists. Returns the
                   CallConfig this driver builds its transport from, and
                   the opening to play once it is up.
    process_events once per gateway trigger — an utterance, a clock threshold —
                   batched with the physical facts spooled since the last call.
                   Returns speak commands, widget frames, and continue/end_call.
    end_call       once, however the call ends. Carries the gateway's
                   end-of-call counters as data.

Two concurrent tasks run between start and end: ``_ws_read_loop`` feeds frames
to the transport, and ``_utterance_processor`` assembles triggers into
process_events requests and performs the responses.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect

from vogon.gateway import jev, redis_client
from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.audio.event_publisher import AudioEventPublisher
from vogon.gateway.logging_context import call_id_var
from vogon.gateway.metrics import decisions as decision_metrics
from vogon.gateway.metrics import pipeline as pipeline_metrics
from vogon.gateway.metrics.sink import metrics_sink
from vogon.gateway.settings import settings
from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.delivery import estimate_heard_text
from vogon.gateway.transport.front_source import build_front_source
from vogon.gateway.transport.live_transport import LiveTransport
from vogon.gateway.transport.outbound_queue import enqueue_twilio
from vogon.gateway.transport.text_transport import TextTransport
from vogon.gateway.transport.twilio_transport import TwilioTransport
from vogon.gateway.transport.voice_transport import VoiceTransport
from vogon.gateway_service.escalation.transfer import TransferOrchestrator
from vogon.gateway_service.escalation.twiml import DEFAULT_SAY_VOICE
from vogon.gateway_service.escalation.voice import HOLD_RENDER_WAIT_SEC, LineRenders
from vogon.websocket import line_instruments
from vogon.websocket.session import CallSession
from vogon.websocket.state_machine import CallState
from vogon.websocket.turn_client import TurnServiceUnavailable, build_turn_client
from vogon_contract import (
    CallConfig,
    DeliveryOutcome,
    DrainCleared,
    EndCallRequest,
    EscalationOutcome,
    LineVerdict,
    PlayoutFinished,
    ProcessEventsRequest,
    ProcessEventsResponse,
    SpeakCommand,
    StartCallRequest,
    UtteranceAbsorbed,
    UtteranceEager,
    UtteranceFinal,
    UtteranceResumed,
)

logger = logging.getLogger(__name__)

# The gateway's own decisions — the platform's instruments, measured as decision tasks
# (vogon/gateway/metrics/decisions.py) under the call's metrics label.
GATEWAY_TASKS = {
    "front_move": decision_metrics.TaskSpec(task="front.move", kind="decision", result="move", outcomes=("move",)),
    "line_detector": decision_metrics.TaskSpec(task="line.recording", kind="decision", result="recording",
                                               outcomes=("recording",)),
}


def _gateway_decision_tracer(call_id: str, workflow_label: str):
    """A call's tracer for the gateway's own decisions calls (vogon.gateway.jev): their
    latency, cost and decision metrics under the call's metrics label. A record of a tag
    this file does not declare is not measured — the turn service traces its own calls."""
    loop = asyncio.get_running_loop()

    def trace(record) -> None:
        if record.tag not in GATEWAY_TASKS:
            return
        loop.call_soon_threadsafe(lambda: decision_metrics.emit_for_record(
            metrics_sink, record, workflow_id=workflow_label, call_id=call_id, tasks=GATEWAY_TASKS,
            cost_usd=None if record.error else record.cost_usd,
            pricing="api" if record.cost_usd is not None else "rate"))
    return trace

def _heard_text(dr, text: str) -> str:
    """What the caller heard, for the outcome fact: the transport's exact
    fully-sent sentences when it paced by sentence, else this side's own
    proportional estimate from the audio streamed. The turn side reads this
    field and estimates nothing (``DeliveryOutcome.heard_text`` is normative)."""
    return dr.heard_text or estimate_heard_text(text, dr.audio_ms, dr.est_total_ms)


# Contract mode → the driver's internal mode name. The Turn API says "widget"
# for the browser; this file has always called that "voice".
_CONTRACT_MODE = {"voice": "widget", "twilio": "twilio", "text": "text"}

# Warm transfer (docs/voice-timing-constants.md §9). The handoff line must
# be HEARD before the hold redirect, which drops Twilio's buffered audio;
# the cap bounds a mark echo that never comes.
_HANDOFF_HEARD_CAP_SEC = 8.0
# How long teardown waits for a transfer already in flight when the media
# stream stops under it (the redirect itself stops the stream): the drain
# wait, the hold update, and a dial per target, each REST call bounded.
_ESCALATION_TEARDOWN_CAP_SEC = 60.0
# How long teardown waits for the call's own ending already in flight: the end_call POST
# (two attempts at turn_service_timeout_sec plus the first backoff, ~80 s at the defaults).
_FINALIZE_TEARDOWN_CAP_SEC = 90.0

# M2 failure policy: what the caller hears when the turn service stays
# unreachable through the whole retry ladder. Driver-level speech — the
# platform owns the slot and this neutral default; the domain overrides the
# wording via CallConfig.trouble_farewell. Kept parallel to the turn side's
# own agent-error farewell so the two outage shapes sound the same.
_DEFAULT_TROUBLE_FAREWELL = (
    "I'm sorry, I'm having technical difficulties on my end. "
    "Someone will follow up with you shortly. Goodbye!"
)




async def handle_call(
    ws: WebSocket,
    session: CallSession,
    mode: Literal["voice", "text", "twilio"] = "voice",
    twilio_start: dict | None = None,
) -> None:
    """Orchestrate a call session over a WebSocket connection."""

    call_id_var.set(session.call_id)

    # ── Gateway memory ───────────────────────────────────────────────────────
    # `seq` is the protocol-frame counter, and only that: audio_ids are minted
    # turn-side (§4.2).
    seq = [0]
    config = session.config
    assistant_cfg = config.get("assistant", {})

    # ── Driver fact spool (phase0 §4.2/§6) ───────────────────────────────────
    # Physical facts (DeliveryOutcome synthesized at speak completion,
    # PlayoutFinished/DrainCleared off the queue) are SPOOLED and processed as
    # a batch BEFORE each trigger — the in-process equivalent of the Turn
    # API's "facts first, trigger last" request batch. This ordering is what
    # makes the delivery interrupt correct on a barge: the truncated outcome
    # is processed before the barging caller's final, even though the final
    # entered the queue first.
    _pending_facts: list = []
    # A warm transfer is in flight on the turn processor (see
    # _perform_escalation): teardown waits for it rather than cancelling.
    _escalating = [False]
    # The call's ending is in flight on the turn processor (_finalize_call): it marks
    # the call DONE before its end_call POST, the read loop returns on that, and
    # teardown must wait for the POST rather than cancel it (voice-eval-c6819d60).
    _finalizing = [False]

    # ── Response latency (voice-gateway-phase0 §7) ───────────────────────────
    # Caller stopped speaking → first audio chunk out. Both ends are gateway
    # facts, so the whole measurement stays here: the anchor is stamped when
    # the utterance arrives (eager overwrites, final sets it if unset) and
    # consumed by the first playout that produces audio. It used to be a list
    # the turn side wrote into — the gateway telling itself something it
    # already knew, and a subtraction that stops meaning anything the moment
    # the two halves have separate clocks.
    _metric_anchor = [0.0]
    # The front model (docs/voice-gateway-front-model.md): the filler hold's
    # source of words. Built after start_call (it needs the persona), so the
    # send path below reads it through a one-slot holder.
    _front_src = [None]
    # The gateway's per-playout analytics rows; ride the EndCallRequest as
    # data (the turn side writes the DB rows — the driver never touches the
    # session's analytics again).
    _gateway_analytics: list = []

    def _note_utterance_anchor(event) -> None:
        _eager = isinstance(event, UtteranceEager) or (
            isinstance(event, UtteranceAbsorbed) and event.phase == "eager")
        _final = isinstance(event, UtteranceFinal) or (
            isinstance(event, UtteranceAbsorbed) and event.phase == "final")
        if _eager:
            if event.at:
                _metric_anchor[0] = event.at
        elif _final and not _metric_anchor[0]:
            _metric_anchor[0] = event.at or time.monotonic()

    # ── Redis client (Phase 3/4 — stream log writer + job queue) ─────────────
    _redis = await redis_client.get_redis()
    _audio_publisher = AudioEventPublisher(session.call_id, _redis) if mode == 'twilio' else None


    async def _send_with_metrics(text: str, audio_id: str, protected: bool = False,
                                 agent_id: str = "", absorb_barge: bool = False, generation=None,
                                 front: bool = False, held: bool = False):
        """transport.send_response + handler-side metric emission from the
        DeliveryReport timings. The single send path for every playout —
        including the front model's (front=True), which is how a line the
        gateway chose gets a DeliveryOutcome fact and reaches the call log.
        held=True is the voicemail message: the transport holds it until
        the line is quiet and takes the floor from the machine."""
        if held:
            dr = await transport.send_held_response(text, audio_id)
        else:
            dr = await transport.send_response_guarded(text, audio_id, generation=generation,
                                                       protected=protected, absorb_barge=absorb_barge)
        if dr is None:
            return None
        _src = _front_src[0]
        if _src is not None and not front:
            # The source's physical transcript: our line AS HEARD.
            _src.note_agent(dr.heard_text or (text if dr.status == "complete" else ""))
        if front:
            # A front line is not the answer: it must not consume the
            # response-latency anchor, or the layer reports itself as the
            # reply. It gets its own first-chunk metric instead.
            if dr.first_chunk_ms is not None:
                metrics_sink.emit("front_first_chunk_ms", _workflow_label[0],
                                  dr.first_chunk_ms, instance_id=session.call_id)
            _pending_facts.append(DeliveryOutcome(
                audio_id=dr.audio_id, status=dr.status,
                sentences_total=dr.sentences_total, sentences_sent=dr.sentences_sent,
                heard_text=_heard_text(dr, text), heard_fraction=dr.heard_fraction,
                first_chunk_ms=dr.first_chunk_ms,
                audio_ms=dr.audio_ms, chunks_sent=dr.chunks_sent,
                mark_pending=dr.mark_pending, at=time.monotonic(),
                text=text, origin="front",
            ))
            return dr
        # The metric label rides the CONTRACT (CallConfig.workflow_id — an
        # opaque string the application hands back), not turn-side state.
        workflow_id = _workflow_label[0]
        if dr.first_chunk_ms is not None:
            _gateway_analytics.append({
                "event_name": "tts_first_chunk", "stage": "tts",
                "latency_ms": dr.first_chunk_ms, "metadata": None,
            })
            metrics_sink.emit(
                "tts_first_chunk_ms", workflow_id, dr.first_chunk_ms,
                instance_id=session.call_id,
            )
        if dr.first_chunk_t and _metric_anchor[0] > 0:
            # Response latency: caller stopped speaking → first audio out.
            # BOTH ends are this side's own clock, so this side measures it.
            latency_ms = (dr.first_chunk_t - _metric_anchor[0]) * 1000
            _gateway_analytics.append({
                "event_name": "response_latency", "stage": "pipeline",
                "latency_ms": latency_ms, "metadata": None,
            })
            pipeline_metrics.emit_response_latency(
                metrics_sink, workflow_id, agent_id, session.call_id,
                _metric_anchor[0], dr.first_chunk_t,
            )
            _metric_anchor[0] = 0.0
        # Synthesize the DeliveryOutcome fact (phase0 §4.2): the driver holds
        # the report; the turn side consumes the fact in the next batch.
        _pending_facts.append(DeliveryOutcome(
            audio_id=dr.audio_id, status=dr.status,
            sentences_total=dr.sentences_total, sentences_sent=dr.sentences_sent,
            heard_text=_heard_text(dr, text), heard_fraction=dr.heard_fraction,
            first_chunk_ms=dr.first_chunk_ms,
            audio_ms=dr.audio_ms, chunks_sent=dr.chunks_sent,
            mark_pending=dr.mark_pending, at=time.monotonic(),
        ))
        return dr

    async def _execute_turn_response(resp: ProcessEventsResponse, generation=None) -> None:
        """DRIVER: perform a turn-side response — the protocol frame and the
        playout for each SpeakCommand, then the end-call action. The single
        execution path for turn-side speech (phase0 §8 step 6/7): turn code
        composes typed responses; only this function performs them."""
        # A held playout (the voicemail message) answers a MACHINE: the
        # playback generation guard, which drops a response the caller has
        # talked past, does not apply — the line's later energy is the
        # box's beep, not a caller (call 4108c2eb, 2026-09-24).
        _held = any(getattr(cmd, "play_after", "now") == "line_quiet" for cmd in resp.speak)
        if not _held and generation is not None and generation != transport.playback_generation():
            for cmd in resp.speak:
                _pending_facts.append(DeliveryOutcome(audio_id=cmd.audio_id, status='truncated',
                    sentences_total=0, sentences_sent=0, heard_text='', at=time.monotonic()))
            floor = getattr(transport, '_floor', None)
            if floor is not None:
                floor.emit('playback_blocked', 'stale_turn_response', request_generation=generation,
                           requested_audio_ids=[cmd.audio_id for cmd in resp.speak])
            return
        for cmd in resp.speak:
            _cmd_held = getattr(cmd, "play_after", "now") == "line_quiet"
            await safe_send_text(
                "server.response_text", {"text": cmd.text, "audio_id": cmd.audio_id},
            )
            await _send_with_metrics(cmd.text, cmd.audio_id, protected=cmd.protected,
                                     agent_id=cmd.agent_id,
                                     absorb_barge=cmd.absorb_barge, generation=generation,
                                     held=_cmd_held)
        if not _held and generation is not None and generation != transport.playback_generation():
            return  # a caller interruption supersedes escalation/end-call too
        if resp.escalation is not None:
            # Warm transfer (2026-08-25): the turn side COMPOSED the order,
            # this side PERFORMS it — after the handoff playout, before
            # end_call, preserving play → transfer → teardown. The
            # initiation outcome rides end_call as a fact; the agent-leg
            # lifecycle continues via the escalation callbacks afterwards.
            _pending_facts.append(await _perform_escalation(resp.escalation))
        if resp.silence_grace_sec and transport is not None:
            # An acknowledged hold: the ladder waits before probing. Applied
            # after the speaks are queued so it covers the quiet AFTER the
            # acknowledgement, which is what the caller asked for.
            transport.grant_silence_grace(resp.silence_grace_sec)
        if resp.silence_ladder and transport is not None:
            # The far end answered the opening (or the turn side says which
            # ladder applies now): the short pre-answer ladder stops.
            transport.select_silence_ladder(resp.silence_ladder)
        if (resp.action == "end_call" and resp.end_reason
                and not session.state_machine.is_terminal()):
            await _finalize_call(resp.end_reason)

    async def _perform_escalation(order) -> EscalationOutcome:
        """Execute the warm-transfer order; return the initiation outcome
        fact. Never raises — a failed transfer is a recorded fact, not a
        crashed call.

        The redirect into the hold conference stops the media stream, which
        ends the socket read loop while this runs; teardown sees
        ``_escalating`` and lets this turn finish instead of cancelling it,
        so the outcome is the REST reply's, and the call ends "escalated"."""
        _label = order.targets[0].label if order.targets else ""
        if not _twilio_call_sid:
            return EscalationOutcome(status="no_provider_call", target=_label,
                                     detail="no Twilio call to transfer",
                                     escalation_id=order.escalation_id,
                                     at=time.monotonic())
        _escalating[0] = True
        try:
            # The transfer's lines in the persona's voice start rendering
            # now (escalation/voice.py): the hold line is ready by the end of
            # the drain; the agent's lines land while the agent's phone rings.
            # The voice the call itself speaks in — resolved exactly as the
            # transport's TTSClient resolves it (persona voice, else the
            # deployment default), so the transfer never changes voice.
            renders = LineRenders(order, _start.call_config.persona_voice
                                  or settings.deepgram_tts_model)
            # Twilio discards audio it still buffers when the call is
            # redirected: the caller must have HEARD the handoff line first.
            await transport.wait_playout_heard(_HANDOFF_HEARD_CAP_SEC)
            hold_token = await renders.wait("hold", HOLD_RENDER_WAIT_SEC)
            _cb = config.get("escalation_callback") or {}
            _ts = config.get("turn_service") or {}
            orch = TransferOrchestrator(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                from_number=settings.twilio_phone_number,
                data_plane_base_url=(_cb.get("base_url")
                                     or settings.public_base_url),
                callback_prefix=_cb.get("prefix", "/v1/escalation"),
                say_voice=_cb.get("say_voice") or DEFAULT_SAY_VOICE,
            )
            try:
                status = await orch.execute(
                    _twilio_call_sid, order, call_id=session.call_id,
                    report_url=_ts.get("base_url", ""),
                    report_token=_ts.get("auth_token", ""),
                    audio={"hold": hold_token} if hold_token else {},
                    audio_rendering=[n for n in renders.texts
                                     if n != "hold" and n not in renders.failed])
            finally:
                # Always: the lines land in the transfer, or — no transfer
                # came of it — are deleted (the whisper names the caller).
                renders.attach(_twilio_call_sid)
            # The hold line's voice is part of the initiation record (a
            # render that failed is a fact, not just a log line); the other
            # lines' land in the transfer's resolution.
            detail = ("hold line: persona voice" if hold_token else
                      f"hold line: twilio <Say> ({renders.failed.get('hold', 'not rendered')})")
            return EscalationOutcome(status=status, target=_label, detail=detail,
                                     escalation_id=order.escalation_id,
                                     at=time.monotonic())
        except Exception as exc:  # noqa: BLE001
            logger.error("escalation transfer failed: %s", exc, exc_info=True)
            return EscalationOutcome(status="failed", target=_label,
                                     detail=str(exc)[:200],
                                     escalation_id=order.escalation_id,
                                     at=time.monotonic())

    # The Twilio CallSid — physical call metadata; rides the EndCallRequest
    # so turn-side call-end effects (escalation transfer) can act on the call.
    _twilio_call_sid: str | None = None
    if twilio_start:
        _twilio_call_sid = (
            twilio_start.get("start", {}).get("callSid")
            or twilio_start.get("callSid")
        )

    # ── Send helpers (priority queue: 0=audio, 1=text, 2=sentinel) ───────────
    _send_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
    _send_counter = itertools.count()

    async def _send_loop() -> None:
        while True:
            _priority, _, item = await _send_queue.get()
            if item is None:
                _send_queue.task_done()
                break
            kind, data = item
            try:
                if kind == "text":
                    await ws.send_text(data)
                else:
                    await ws.send_bytes(data)
            except Exception as exc:
                logger.warning("Send loop error: %s", exc)
            _send_queue.task_done()

    async def safe_send_text(msg_type: str, payload: dict) -> None:
        seq[0] += 1
        frame = json.dumps({
            "type": msg_type, "seq": seq[0], "ts": time.time(), "payload": payload,
        })
        logger.debug("→ [%d] %s %s", seq[0], msg_type, payload)
        await _send_queue.put((1, next(_send_counter), ("text", frame)))

    async def safe_send_binary(data: bytes) -> None:
        logger.debug("→ [binary] %d bytes (audio)", len(data))
        await _send_queue.put((0, next(_send_counter), ("binary", data)))

    recorder = AudioRecorder()

    # ── TurnClient (Phase 1 M1 items 3+4) ─────────────────────────────────────
    # The driver's seam to the turn service: HTTP, always. The address comes
    # from the call's own turn_service binding (settings.turn_service_url is
    # the fallback); there is no in-process client to fall back INTO any
    # more, so an unresolvable address raises rather than quietly running the
    # reasoning stack in this process. The DriverPort is GONE (item 4): facts
    # ride requests, holds are driver-local around the outstanding request,
    # recording finalizes at socket close, and server.call_ended is this
    # side's own frame.
    turn_client = build_turn_client(config=config)
    # The metric label from CallConfig.workflow_id, set once start_call
    # returns (one-slot holder: _send_with_metrics closes over it above).
    _workflow_label = [""]

    def _build_transport(cfg: CallConfig) -> BaseTransport:
        """The gateway's one job at the start of a call: build the transport
        the backend described. `cfg` is everything it is allowed to know —
        the transports take call_id + CallConfig and nothing else, so
        no reasoning-side object (CallSession, assistant config) crosses
        into vogon/gateway/."""
        if mode == "text":
            return TextTransport(safe_send_text, session.call_id,
                                 call_config=cfg)
        if mode == "twilio":
            # Twilio sends/receives raw JSON frames, not our app protocol.
            # send_twilio_raw puts a raw JSON string on the priority send queue
            # at audio priority (0) so voice frames are ordered ahead of text.
            async def send_twilio_raw(raw_json: str) -> None:
                discarded = enqueue_twilio(_send_queue, _send_counter, raw_json)
                if discarded and transport is not None:
                    transport.note_audio_event('outbound_queue_cleared',
                                               reason='barge_cut', discarded=discarded)
            # {"voice": "live"} in the app's opaque payload selects the
            # parallel GPT-Live listener (same Twilio path otherwise).
            _voice = (session.config.get("app_payload") or {}).get("voice", "")
            if _voice == "live":
                _transport_cls = LiveTransport
            else:
                _transport_cls = TwilioTransport
            return _transport_cls(
                ws, send_twilio_raw, session.call_id,
                call_config=cfg,
                start_event=twilio_start,
                qsize_fn=lambda: _send_queue.qsize(),
                recorder=recorder,
                audio_event_sink=_audio_publisher.emit if _audio_publisher else None,
                # Who answered the line (VOICEMAIL_DETECTION_DESIGN.md §4):
                # the instrument is installed HERE because the gateway tree
                # imports nothing from the reasoning side.
                line_instrument=line_instruments.jev_recording_probability,
            )
        return VoiceTransport(
            safe_send_text, safe_send_binary, session.call_id,
            call_config=cfg,
        )

    # ── Completion ────────────────────────────────────────────────────────────


    async def _finalize_call(reason: str) -> None:
        """DRIVER: the call's one ending — the gateway's end_call (§4.3).

        The transport's end-of-call counters are snapshotted HERE and ride the
        request as data; the turn side writes the rows and never reads the
        transport. Safe on the abnormal-exit path, which finalizes after
        transport.close(): every counter is an accumulated total.
        """
        # The gateway's machine transitions HERE — where the turn side's
        # transition used to fire (top of finalize). Both loops and the
        # double-finalize guards key off it, so the timing must not move.
        session.state_machine.transition(CallState.DONE)
        _finalizing[0] = True
        try:
            await _end_call(reason)
        finally:
            _finalizing[0] = False

    async def _end_call(reason: str) -> None:
        if _audio_publisher is not None:
            await _audio_publisher.close()
        try:
            await turn_client.end_call(EndCallRequest(
                call_id=session.call_id,
                reason=reason,
                provider_call_id=_twilio_call_sid or "",
                # The spool's final flush (M1 item 4): facts that never got a
                # later trigger ride the end request; their audit entries land
                # ahead of CALL_ENDED on the turn side.
                events=list(_pending_facts),
                # No transport yet means the call died inside start_call:
                # there are no counters to report, but the ending is still
                # recorded. The transport's per-call dict flattens to rows
                # next to the driver's own per-playout rows.
                transport_analytics=[
                    {"event_name": _n, **_row}
                    for _n, _row in (
                        transport.transport_analytics() if transport is not None else {}
                    ).items()
                ] + list(_gateway_analytics),
            ))
        except Exception as exc:  # noqa: BLE001 — M2 failure policy
            # The gateway's OWN teardown (call_ended frame, recording,
            # socket close) must complete even when the turn service cannot
            # be told the call is over. The turn side's record completes
            # later: its reaper finalizes any call whose end_call never
            # landed (reason="abandoned").
            logger.error(
                "Call %s: end_call did not reach the turn service (%s) — "
                "the turn-side reaper will finalize it",
                session.call_id, exc)
        _pending_facts.clear()
        # The terminal widget frame is the GATEWAY's own (M1 item 4): this
        # side owns the socket and knows both reason and duration.
        await safe_send_text("server.call_ended", {
            "call_id": session.call_id,
            "reason": reason,
            "duration_sec": round(session.duration_sec(), 2),
        })

    # ── Main WebSocket loop ───────────────────────────────────────────────────

    # Produced inside the try (the transport by start_call's config) and read
    # by the finally block, so a failure in start_call must not turn into a
    # NameError during teardown. Whether the turn side has a call to finalize
    # is the CLIENT's knowledge (turn_client.started), and over HTTP that
    # means start_call RETURNED. An opening that died inside the turn service
    # is finalized there — http_service._finalize_failed_start ends the call
    # and closes the log on the context only that side ever held — so this
    # side correctly finalizes nothing. What it does NOT do is tell the
    # caller: see "A failed opening has no driver failure policy" in TODO.md.
    transport: BaseTransport | None = None
    # Why the socket side ended, when it ended first ("hangup" | "timeout" |
    # "disconnect"). The read loop has always KNOWN this; before the M2
    # failure policy the finally block discarded it and recorded every
    # socket-side ending as abnormal_exit — a clean caller hangup included.
    _ws_exit_reason: str | None = None

    send_loop_task = asyncio.create_task(_send_loop())
    try:
        # ── start_call (§4.1) ────────────────────────────────────────────────
        # One call. The backend loads the config, restores or creates the
        # session state, runs the CALL_START tools and the opening agent, and
        # hands back the CallConfig plus what to say. The gateway builds
        # its transport from that config — so the STT/TTS sockets open AFTER
        # the opening was computed, which is inaudible on a phone call:
        # nothing reads the media socket until the read loop starts below.
        _start = await turn_client.start_call(
            StartCallRequest(
                call_id=session.call_id,
                mode=_CONTRACT_MODE[mode],
                resume=False,
                # The application's identities ride the opaque payload (M1
                # item 1) — the gateway relays them, it does not know them.
                # Standalone gateway (item 5): the payload captured at call
                # creation is relayed VERBATIM. Monolith: assembled from the
                # primed config; session_token is how the HTTP turn service
                # finds that config (decision D3); opaque like the rest.
                payload=config.get("app_payload") or {
                    "owner_id": config.get("owner_id", ""),
                    "workflow_id": assistant_cfg.get("workflow_id")
                    or "appointment_booking",
                    "session_token": session.session_token,
                },
            ),
        )
        # Metrics scope: the standalone gateway partitions {tenant}/{route}
        # (plan §2.6); the monolith keeps CallConfig.workflow_id — for
        # the application's route == workflow_id, so the partitioning is identical.
        _workflow_label[0] = (config.get("metrics_label")
                              or _start.call_config.workflow_id)
        # The gateway's own decisions calls (the front model's move, the line
        # detector's recording question) reach the metrics through a per-call
        # tracer — set before the transport and its tasks start, so they
        # inherit it.
        jev.set_tracer(_gateway_decision_tracer(session.call_id, _workflow_label[0]))
        # Recording policy (M1 item 8): the APPLICATION decides IF this call
        # is recorded (CallConfig.recording); absent policy = record (today's
        # behavior). The gateway decides how/where (the artifact stores).
        _rec = _start.call_config.recording
        recorder.enabled = _rec.enabled if _rec is not None else True
        _on_cc = config.get("on_call_config")
        if _on_cc is not None:
            # Item 6: the gateway service freezes the per-call resolved
            # config snapshot onto its session record.
            await _on_cc(_start.call_config)

        transport = _build_transport(_start.call_config)
        await transport.start()

        # The front model — the filler's source of words (audio modes only;
        # text has nothing to fill). FRONT_MODEL_ENABLED=0 leaves the hold on
        # the canned FillerPolicy exactly as before.
        if mode in ("voice", "twilio"):
            _hint = (_start.call_config.name_hint or "").strip()
            _src = build_front_source(
                agent_name=_start.call_config.persona_name,
                lender_name=assistant_cfg.get("lender_name") or assistant_cfg.get("company_name") or "",
                borrower_first_name=_hint.split()[0] if _hint else "",
            )
            if _src is not None and transport.install_line_source(
                    _src, beat_after_ms=settings.front_beat_after_ms,
                    wait_again_after_ms=settings.front_wait_again_after_ms,
                    beat_after_empty_ms=settings.front_beat_after_empty_ms):
                _front_src[0] = _src
                transport.install_hold_speak(
                    lambda text, audio_id: _send_with_metrics(text, audio_id, front=True))
                logger.info("FRONT | installed | model=%s beat_after=%dms",
                            settings.front_model, settings.front_beat_after_ms)

        session.state_machine.transition(CallState.ACTIVE)

        # Call-liveliness ambience rides the CallConfig as resolved
        # snake_case values; the widget's frame wants its six as camelCase,
        # so the rename happens here — a protocol detail of this frame, not
        # of the config.
        _amb = _start.call_config.ambience or {}
        await safe_send_text("server.session_ready", {
            "call_id":      session.call_id,
            "persona_name": _start.call_config.persona_name,
            "ambience": {
                "enabled":       _amb.get("enabled", False),
                "room":          _amb.get("room", False),
                "typing":        _amb.get("typing", False),
                "duckFactor":    _amb.get("duck_factor", 0.0),
                "typingDelayMs": _amb.get("typing_delay_ms", 0),
                "typingGain":    _amb.get("typing_gain", 0.0),
            },
        })
        for _ce in _start.client_events:
            await safe_send_text(_ce.type, _ce.payload)

        # (the opening plays below, AFTER the read loop starts)

        # Max-duration enforcement lives in the gateway clocks: the transport
        # fires DurationThreshold("max_duration") on its own timer and
        # process_events maps it to the hangup. The value is still needed here
        # as the receive-loop timeout (a socket with no frames for the whole
        # call limit is dead) — read from the same thresholds the clocks run
        # on, so the two can never disagree.
        max_duration = max(
            (t.after_sec for t in _start.call_config.duration_thresholds),
            default=settings.max_call_duration_sec,
        )

        # ── Concurrent read loop + utterance processor ────────────────────────

        async def _ws_read_loop() -> str:
            """Feed WebSocket frames to the transport. Returns the exit reason."""
            while not session.state_machine.is_terminal():
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=max_duration)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Call %s terminated — reason: receive_loop_timeout "
                        "(no WebSocket frame for %ds)",
                        session.call_id, max_duration,
                    )
                    return "timeout"
                except WebSocketDisconnect:
                    logger.info(
                        "Call %s terminated — reason: websocket_disconnect at %.1fs",
                        session.call_id, session.duration_sec(),
                    )
                    return "disconnect"

                if "bytes" in message and message["bytes"]:
                    logger.debug("← [binary] %d bytes (audio)", len(message["bytes"]))
                    await transport.handle_binary_frame(message["bytes"])
                elif "text" in message and message["text"]:
                    try:
                        msg = json.loads(message["text"])
                        logger.debug("← %s", msg.get("type", ""))
                        if await transport.handle_text_frame(msg):
                            logger.info(
                                "Call %s terminated — reason: client_hangup at %.1fs",
                                session.call_id, session.duration_sec(),
                            )
                            return "hangup"
                    except json.JSONDecodeError:
                        logger.warning("← unparseable text frame: %.80s", message["text"])
            return "terminal"

        async def _utterance_processor() -> None:
            """Assemble gateway triggers into Turn API requests.

            Step (d): the driver spools physical facts, batches them with one
            trigger into a ProcessEventsRequest, makes ONE process_events call,
            and executes the response. Every reasoning decision — clock
            thresholds, the STT gate, the duplicate-final guard, utterance
            dispatch — now lives behind that call.
            """
            _req_seq = itertools.count(1)
            _line_machine_seen = False
            while not session.state_machine.is_terminal():
                item = await transport.utterance_queue.get()

                if isinstance(item, LineVerdict) and item.answered_by == "machine":
                    _line_machine_seen = True
                if isinstance(item, (PlayoutFinished, DrainCleared, LineVerdict)):
                    # Physical facts spool; they ride the next request so the
                    # turn side applies them before it reasons (phase0 §6 —
                    # facts first, trigger last).
                    _pending_facts.append(item)
                    continue

                _note_utterance_anchor(item)
                _src = _front_src[0]
                if _src is not None:
                    if isinstance(item, UtteranceEager):
                        _src.prime(item.text)          # the free latency: compose now
                    elif isinstance(item, UtteranceFinal):
                        _src.note_borrower(item.text)
                        _src.prime(item.text)          # no-op when the eager already did
                    elif isinstance(item, UtteranceResumed):
                        _src.cancel()                  # nothing composed may play
                _events = list(_pending_facts) + [item]
                _pending_facts.clear()
                _request = ProcessEventsRequest(
                    call_id=session.call_id,
                    request_id=f"{session.call_id}-{next(_req_seq)}",
                    events=_events,
                )
                # server.thinking is the GATEWAY's own signal: it knows a
                # request is outstanding. It drives the widget's typing
                # animation during the gap, so it must go out now, not with
                # the answer.
                if isinstance(item, (UtteranceFinal, UtteranceEager)):
                    await safe_send_text("server.thinking", {})

                # No lock here. Silence detection, the duration limit and the
                # filler window must fire while a turn is in progress — holding
                # a lock across the whole call would make them wait out an LLM
                # turn. Only the utterance dispatch is serialized, inside
                # process_events, exactly as it was before the extraction.
                #
                # Holds are DRIVER-LOCAL (M1 item 4): the filler window IS the
                # outstanding request, opened only for a final — the turn side
                # knows nothing about fillers any more.
                # No filler over a machine: when this request carries the
                # detector's machine verdict, the answer is the voicemail
                # message, held until the line is quiet — a front-model beat
                # ("Thanks for letting me know.") would be spoken to a
                # recording. Measured 2026-09-23 on the duplex harness.
                _machine_line = _line_machine_seen or any(
                    isinstance(f, LineVerdict) and f.answered_by == "machine" for f in _events)
                _holding = (isinstance(item, UtteranceFinal) and not _machine_line
                            and transport.begin_hold())
                # ...and the silence ladder pauses across ANY caller-turn
                # request, filler policy or not. The reasoning gap is neither
                # the caller being quiet nor us talking: they answered and we
                # have not replied yet, and counting it against them earned a
                # probe on a slow turn. The filler hold cannot be that pause:
                # it exists only when the assistant opted into fillers (and
                # until 2026-09-18 never on a phone call at all), while the
                # ladder exists on every phone call. Clock-driven requests are
                # excluded on purpose: pausing on a SilenceThreshold would have
                # the ladder suppress itself.
                _pausing = isinstance(item, (UtteranceFinal, UtteranceEager))
                if _pausing:
                    transport.pause_silence()
                _playback_generation = getattr(item, 'playback_generation', None)
                if _playback_generation is None:
                    _playback_generation = transport.playback_generation()
                try:
                    _resp = await turn_client.process_events(_request)
                except TurnServiceUnavailable as exc:
                    # M2 failure policy: the retry ladder (fillers masking
                    # the gap for finals) is exhausted — the caller gets a
                    # farewell in words, never dead air then silence. The
                    # synthetic response rides the NORMAL execution path:
                    # play, then _finalize_call (whose end_call is
                    # best-effort — the turn-side reaper is the backstop).
                    logger.error(
                        "Call %s: turn service unreachable — trouble "
                        "farewell (%s)", session.call_id, exc)
                    _resp = ProcessEventsResponse(
                        speak=[SpeakCommand(
                            audio_id="trouble-1",
                            text=(_start.call_config.trouble_farewell
                                  or _DEFAULT_TROUBLE_FAREWELL),
                            protected=True)],
                        action="end_call",
                        end_reason="turn_unreachable",
                    )
                finally:
                    if _pausing:
                        transport.resume_silence()
                    _hold = (await transport.end_hold()) if _holding else None
                for _ce in _resp.client_events:
                    await safe_send_text(_ce.type, _ce.payload)
                if (_hold is not None and _hold.interrupted
                        and _resp.action == "continue" and _resp.speak):
                    # Ledger rule (docs/call-ledger-design.md §3): the caller
                    # took the floor during a filler, so this response answers
                    # a superseded utterance. The turn side COMMITTED it (one
                    # write, unconditional); this side simply never plays it
                    # and records the physical truth — the outcome facts ride
                    # the next request and the heard-horizon machinery
                    # reconciles. Nothing is rolled back anywhere.
                    logger.info(
                        "FILLER | barged after %d filler(s) — suppressing "
                        "playout of %s",
                        _hold.fillers_played,
                        [c.audio_id for c in _resp.speak],
                    )
                    _now = time.monotonic()
                    for _cmd in _resp.speak:
                        _pending_facts.append(DeliveryOutcome(
                            audio_id=_cmd.audio_id, status="truncated",
                            sentences_total=0, sentences_sent=0,
                            heard_text="", at=_now,
                        ))
                elif _resp.speak or _resp.action == "end_call":
                    await _execute_turn_response(_resp, generation=_playback_generation)
                if _resp.action == "end_call" and session.state_machine.is_terminal():
                    return

        # ── Startup order (2026-08-02): frames first, THEN the opening ──────
        # The read loop starts BEFORE the opening plays, so the energy gate,
        # the absorb machinery and the safety cut exist during the call's
        # first seconds — before this, caller audio piled up unread in the
        # socket while the greeting streamed: no absorption, no "stop", and a
        # stale-reference burst into AEC/STT when the loop finally started.
        # Speech over the opening is ABSORBED (SpeakCommand.absorb_barge),
        # never queued. The processor starts AFTER the opening completes, so
        # no turn response can overlap the opening playout; absorbed events
        # that queued during it drain immediately, as log-only entries.
        ws_task = asyncio.create_task(_ws_read_loop())

        await _execute_turn_response(ProcessEventsResponse(speak=_start.speak))

        proc_task = asyncio.create_task(_utterance_processor())

        done, pending = await asyncio.wait(
            [ws_task, proc_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if proc_task in pending and _escalating[0]:
            # The hold redirect stops the media stream, so the read loop
            # ends while the transfer is still being placed. Let the turn
            # finish: it reports the real outcome and ends the call
            # "escalated". Bounded; past the cap it is cancelled like any
            # other pending task.
            try:
                await asyncio.wait_for(asyncio.shield(proc_task),
                                       timeout=_ESCALATION_TEARDOWN_CAP_SEC)
            except asyncio.TimeoutError:
                logger.error("Call %s: escalation still in flight after %.0fs "
                             "— cancelling", session.call_id,
                             _ESCALATION_TEARDOWN_CAP_SEC)
            except Exception:  # noqa: BLE001 — surfaced below via done-task check
                pass
            pending = {t for t in pending if not t.done()}
            done = {t for t in (ws_task, proc_task) if t.done()}
        if proc_task in pending and _finalizing[0]:
            # THE CALL'S OWN ENDING IS IN FLIGHT: _finalize_call marked the call DONE and
            # is awaiting its end_call POST, and the read loop returned on DONE (or the far
            # end hung up). Cancelling the processor here cancelled the POST — silently,
            # CancelledError is no Exception — and the turn side never finalized the call:
            # no CALL_ENDED row, the NO_ANSWER never recorded (voice-eval-c6819d60).
            try:
                await asyncio.wait_for(asyncio.shield(proc_task), timeout=_FINALIZE_TEARDOWN_CAP_SEC)
            except asyncio.TimeoutError:
                logger.error("Call %s: end_call still in flight after %.0fs — cancelling",
                             session.call_id, _FINALIZE_TEARDOWN_CAP_SEC)
            except Exception:  # noqa: BLE001 — surfaced below via done-task check
                pass
            pending = {t for t in pending if not t.done()}
            done = {t for t in (ws_task, proc_task) if t.done()}
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Facts with no later trigger (a cancelled processor, WS death) are
        # NOT dropped: the spool's final flush rides EndCallRequest.events in
        # _finalize_call below, so their audit entries still land.

        # Propagate any unhandled exception from a completed task
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
        if ws_task in done:
            _ws_exit_reason = ws_task.result()

    except WebSocketDisconnect:
        logger.info(
            "Call %s terminated — reason: websocket_disconnect at %.1fs",
            session.call_id, session.duration_sec(),
        )
    except Exception as exc:
        logger.error(
            "Call %s terminated — reason: unhandled_exception (%s) at %.1fs",
            session.call_id, exc, session.duration_sec(), exc_info=True,
        )
    finally:
        if transport is not None:
            await transport.close()
        if _audio_publisher is not None:
            await _audio_publisher.close()
        # Recording finalizes at SOCKET CLOSE (M1 item 4/8 — the turn side
        # no longer holds the recorder): fire-and-forget, beside the frame
        # log and audio event log the transport just flushed.
        asyncio.create_task(recorder.save_and_upload(session.call_id))
        if turn_client.started and not session.state_machine.is_terminal():
            # The read loop's own verdict when it ended the call: a clean
            # client hangup (or Twilio carrier stop) is "hangup", a frame
            # timeout is "timeout"; only a genuinely unexplained ending —
            # socket death, an unhandled exception — stays abnormal_exit.
            _reason = {"hangup": "hangup", "timeout": "timeout"}.get(
                _ws_exit_reason or "", "abnormal_exit")
            _log = logger.info if _reason == "hangup" else logger.warning
            _log(
                "Call %s terminated — reason: %s at %.1fs",
                session.call_id, _reason, session.duration_sec(),
            )
            await _finalize_call(_reason)
        await _send_queue.put((2, next(_send_counter), None))
        try:
            await asyncio.wait_for(send_loop_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            send_loop_task.cancel()
        # The turn side's teardown tail — await the call-end tools (summary,
        # webhooks) so TOOL_COMPLETED/TOOL_FAILED reach the log, then close
        # the call log LAST. The ordering lives inside the client now
        # (in-process: tools -> close_call_log; HTTP: ran server-side inside
        # end_call, close() only drops the connection pool).
        await turn_client.close()
        jev.set_tracer(None)
