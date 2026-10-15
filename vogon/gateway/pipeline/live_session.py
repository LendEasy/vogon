"""GPT-Live 1 as the LISTENER of a Twilio call — a drop-in for STTSession.

docs/GPT_LIVE_TURN_API_DESIGN.md §5, recite-only milestone (M2). The live
model hears the caller over the same G.711 μ-law frames the transport already
forwards, and this class turns its events into the exact callbacks the Twilio
transport wires for Deepgram Flux:

    session.input_transcript.delta  → on_interim(text_so_far, 1.0)
    session.delegation.created      → on_final(text, 1.0, 0.0, [])

The model's own voice is NEVER relayed — every word the caller hears is still
the agent's registry line through Deepgram TTS, so the paraphrase and filler
measured in the design (§3) cannot reach the caller. What the agent said is
appended as private context (`session.thinking.append`) so the model's picture
of the conversation stays two-sided.

Confidence is 1.0 with no low-confidence words: the API exposes no word
confidences (§4, LOSSY), and the contract says pass that explicitly rather
than silently.

Interface parity with STTSession (what TwilioTransport touches):
    start() -> float ms · send_audio(bytes) · close() ·
    last_onset_monotonic() · quality_stats()
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable

from vogon.gateway.media_clock import stream_to_monotonic
from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)

OnInterim = Callable[[str, float], Awaitable[None]]
OnFinal = Callable[[str, float, float, list[str]], Awaitable[None]]

# Measured 2026-09-11: with no instruction the model spoke 4.5 s of filler
# before any appended result, and with this instruction 2/3 runs still
# backchannelled. It is a request, not a guarantee — the guarantee is that
# output audio is dropped, not that the model stays quiet.
_INSTRUCTIONS = (
    "You are the listening half of a phone call. A separate backend decides "
    "every reply and speaks it through its own audio path; you will be told "
    "what it said. Never speak, greet, acknowledge or backchannel. After every "
    "caller utterance, delegate to the backend immediately and wait silently."
)


class LiveSession:
    """One gpt-live-1 client-delegation session for one call."""

    def __init__(
        self,
        on_interim: OnInterim,
        on_final: OnFinal,
        *,
        language: str = "en-US",
        keyterms: list[str] | None = None,
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        **_ignored,   # the Flux-only knobs TwilioTransport passes (eot_*, eager, resumed)
    ) -> None:
        self._on_interim = on_interim
        self._on_final = on_final
        self._language = language
        self._keyterms = keyterms or []     # no keyterm API exists (design §4); kept for stats
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._client = None
        self._cm = None
        self._conn = None
        self._reader: asyncio.Task | None = None
        self._open_task: asyncio.Task | None = None
        self._open_error: Exception | None = None
        self._open_ms: float | None = None
        self._dropped_before_open = 0
        self._quiet_task: asyncio.Task | None = None
        self._started = asyncio.Event()
        self._closed = asyncio.Event()
        self._connected = False
        self._session_id = ""
        self._session_start = time.monotonic()
        # the open caller turn
        self._buf: list[str] = []
        self._last_onset_stream_s: float | None = None
        self._last_delta_t: float = 0.0
        # stats (quality_stats)
        self._turns = 0
        self._fallback_finals = 0
        self._errors = 0
        self._usage_seconds = 0.0
        self._words = 0

    # ── connection (overridable for tests) ───────────────────────────────────

    async def _connect(self):
        """Open the Live WebSocket and return the connection."""
        # only the GPT-Live path needs the openai SDK; the gateway runs without it
        import openai  # noqa: PLC0415
        from openai import AsyncOpenAI  # noqa: PLC0415
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        if not hasattr(self._client, "live"):
            # Production 2026-09-12: the venv had a 2.x SDK, and the call died
            # with an AttributeError three frames deep. Say what is wrong.
            raise RuntimeError(
                f"openai SDK {openai.__version__} has no Live API; the GPT-Live "
                "path needs openai>=3.8 (see requirements.txt)")
        self._cm = self._client.live.connect()
        return await self._cm.__aenter__()

    def _session_config(self) -> dict:
        fmt = ({"type": "audio/pcmu", "rate": 8000} if self._encoding == "mulaw"
               else {"type": "audio/pcm", "rate": self._sample_rate})
        return {
            "model": settings.live_model,
            "instructions": _INSTRUCTIONS,
            "audio": {"format": fmt, "output": {"voice": settings.live_voice}},
            "delegation": {"type": "client"},
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> float:
        """Begin opening the session; return once it is up OR after a short
        wait, whichever is first. Returns the ms spent here (the transport
        reports it as stt_open_ms).

        The pickup fix (2026-09-12): the session took 2.2 s to open against
        Flux's 0.36 s, and TwilioTransport.start() awaited it before the TTS
        socket and the greeting — two seconds added to the first word of
        every call. The open now finishes in the background while the
        greeting plays. Frames arriving before it is up are dropped and
        counted (the opening is a protected playout, whose caller audio is
        absorbed anyway). A failure inside the wait window still raises, as
        before; a failure after it is logged and the call's silence ladder
        owns what happens next — raising into a call that is already
        talking would cut the greeting for a listener nobody has needed yet.
        """
        t0 = time.monotonic()
        self._open_task = asyncio.create_task(self._open(t0))
        wait_s = settings.live_start_wait_ms / 1000.0
        done, _ = await asyncio.wait({self._open_task}, timeout=wait_s)
        if done:
            self._open_task.result()          # re-raise an early failure
        elapsed = (time.monotonic() - t0) * 1000
        if not self._connected:
            logger.info("gpt-live session still opening after %.0fms — "
                        "continuing; the greeting does not wait", elapsed)
        return elapsed

    async def _open(self, t0: float) -> None:
        try:
            self._conn = await self._connect()
            await self._conn.session.start(session=self._session_config(),
                                           event_id="start")
            self._reader = asyncio.create_task(self._read_loop())
            try:
                await asyncio.wait_for(self._started.wait(),
                                       timeout=settings.live_start_timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "gpt-live session did not report session.started") from None
            if not self._connected:
                raise RuntimeError("gpt-live reader ended before session.started")
        except Exception as exc:
            # Every way the open can fail lands here, so the stats and the
            # log always say why (the timeout used to raise past this).
            self._open_error = exc
            self._errors += 1
            logger.error("gpt-live session failed to open: %s", exc)
            await self.close()
            raise
        # The session timeline (start_ms on transcript deltas) starts at
        # session.started; anchor onset conversion there.
        self._session_start = time.monotonic()
        self._open_ms = (time.monotonic() - t0) * 1000
        logger.info("gpt-live session ready | id=%s | model=%s | opened_in=%.0fms"
                    " | frames_dropped_before_open=%d",
                    self._session_id, settings.live_model, self._open_ms,
                    self._dropped_before_open)

    async def send_audio(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._conn is None or not self._connected:
            self._dropped_before_open += 1
            return
        await self._conn.session.input_audio.append(
            audio=base64.b64encode(chunk).decode("ascii"))

    async def close(self) -> None:
        if self._quiet_task is not None:
            self._quiet_task.cancel()
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
            try:
                await self._open_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._conn is not None and self._connected:
            try:
                await self._conn.session.close(event_id="close")
                await asyncio.wait_for(self._closed.wait(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.debug("gpt-live close: %s", exc)
        self._connected = False
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass

    # ── events ───────────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        try:
            async for e in self._conn:
                ty = getattr(e, "type", "")
                if ty == "session.started":
                    self._connected = True
                    self._session_id = getattr(getattr(e, "session", None), "id", "") or ""
                    self._started.set()
                elif ty == "session.input_transcript.delta":
                    await self._on_delta(e)
                elif ty == "session.delegation.created":
                    await self._on_delegation(e)
                elif ty == "session.usage.updated":
                    self._usage_seconds = float(getattr(e.usage, "seconds", 0) or 0)
                elif ty == "session.closed":
                    usage = getattr(e, "usage", None)
                    if usage is not None:
                        self._usage_seconds = float(getattr(usage, "seconds", 0) or 0)
                    self._connected = False
                    self._closed.set()
                    break
                elif ty == "error":
                    self._errors += 1
                    logger.warning("gpt-live error: %s", getattr(e, "error", e))
                # session.output_audio.delta / output_transcript.delta: the
                # model's own voice — never relayed (recite-only). Everything
                # else (appended acks, info) is noise here.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the socket died; the silence ladder owns what happens next
            self._connected = False
            self._errors += 1
            logger.warning("gpt-live reader ended: %s", exc)
            self._started.set()

    async def _on_delta(self, e) -> None:
        if not self._buf:
            start_ms = getattr(e, "start_ms", None)
            self._last_onset_stream_s = (start_ms / 1000.0) if start_ms is not None else None
        self._buf.append(e.delta or "")
        self._last_delta_t = time.monotonic()
        await self._on_interim("".join(self._buf).strip(), 1.0)
        self._arm_quiet()

    def _arm_quiet(self) -> None:
        if self._quiet_task is not None and not self._quiet_task.done():
            self._quiet_task.cancel()
        self._quiet_task = asyncio.create_task(self._quiet_final())

    async def _quiet_final(self) -> None:
        """Fallback end of turn: the model normally delegates at the exact end
        of speech (measured: offset == last delta), but a turn it decides needs
        no backend ("hello?") would otherwise never reach the Turn API."""
        await asyncio.sleep(settings.live_final_quiet_ms / 1000.0)
        if self._buf:
            self._fallback_finals += 1
            await self._emit_final()

    async def _on_delegation(self, e) -> None:
        if self._quiet_task is not None:
            self._quiet_task.cancel()
        if self._buf:
            await self._emit_final()
        # else: the model delegated a turn already emitted by the fallback —
        # nothing new to hand over.

    async def _emit_final(self) -> None:
        text = "".join(self._buf).strip()
        self._buf = []
        if not text:
            return
        self._turns += 1
        self._words += len(text.split())
        await self._on_final(text, 1.0, 0.0, [])

    # ── the agent's side of the conversation ─────────────────────────────────

    async def note_agent_said(self, text: str) -> None:
        """Tell the model what the caller just heard (private context, never
        spoken). delegation_id is None: general session context, not a result
        for a specific delegation."""
        if self._conn is None or not self._connected or not text:
            return
        try:
            await self._conn.session.thinking.append(
                event_id=f"said-{int(time.monotonic() * 1000)}",
                delegation_id=None,
                content="The agent just said to the caller, verbatim: " + text[:1500],
            )
        except Exception as exc:  # noqa: BLE001 — context is best-effort
            logger.debug("gpt-live thinking.append failed: %s", exc)

    # ── STTSession parity ────────────────────────────────────────────────────

    def last_onset_monotonic(self) -> float | None:
        return stream_to_monotonic(self._session_start, self._last_onset_stream_s)

    def quality_stats(self) -> dict:
        return {
            "total_words": self._words,
            "avg_confidence": None,          # not provided by the API
            "low_conf_fraction": None,
            "live_session_id": self._session_id,
            "live_turns": self._turns,
            "live_fallback_finals": self._fallback_finals,
            "live_errors": self._errors,
            "live_usage_seconds": self._usage_seconds,
            "live_open_ms": None if self._open_ms is None else round(self._open_ms),
            "live_frames_dropped_before_open": self._dropped_before_open,
            "live_open_error": str(self._open_error) if self._open_error else None,
        }
