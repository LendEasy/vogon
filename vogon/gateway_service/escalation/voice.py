"""The transfer's lines in the PERSONA's voice.

The hold message and callback message (heard by the caller) and the whisper
and voicemail (heard by the agent) used to be Twilio <Say> — a different
voice from the one the caller had been talking to. They are now rendered
with the call's own TTS voice (Deepgram REST, MP3), stored under
unguessable tokens with the transfer, and played with <Play>.

Rendering speed decides the shape (measured 2026-10-01, aura-2, MP3):
Deepgram REST renders at about twice real time — a one-sentence hold line
in ~0.8 s, a ~20 s whisper in 8.6–10.5 s. So:

- the HOLD line is the only one needed at the redirect; it renders while the
  handoff line drains and is awaited for at most ``_HOLD_RENDER_WAIT_SEC``;
- the whisper, voicemail and callback lines keep rendering in the background
  after the caller is on hold, and their tokens land in the transfer state as
  each finishes (``attach``) — the agent's phone is still ringing;
- the whisper callback waits briefly for its audio (callbacks.whisper_body).

A line that is not rendered in time is spoken by Twilio <Say> instead; which
lines were, and why, is recorded — the hold line in the initiation outcome,
the others in the transfer's resolution — never only in a log line.
Design: voice-gateway/docs/ESCALATION_WARM_TRANSFER.md §6.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from deepgram import AsyncDeepgramClient

from vogon.gateway.settings import settings
from vogon.gateway_service.escalation.state import (
    delete_audio,
    get_transfer,
    put_audio,
    update_transfer,
)
from vogon.gateway_service.escalation.twiml import WHISPER_PROMPT
from vogon_contract import EscalationOrder

logger = logging.getLogger(__name__)

# The hold line must be ready at the redirect: this bounds the wait for it
# AFTER the handoff drain (it renders during the drain, ~0.8 s).
HOLD_RENDER_WAIT_SEC = 3.0
# Any one line's render — a ~25 s whisper renders in ~10 s.
_LINE_RENDER_TIMEOUT_SEC = 30.0

DEFAULT_HOLD_MESSAGE = "Please hold while we connect you to a specialist."
DEFAULT_CALLBACK_MESSAGE = ("Our specialist is unavailable right now. "
                            "Someone will call you back shortly.")


def line_texts(order: EscalationOrder) -> dict[str, str]:
    """What each line says — the same text whether rendered or <Say>."""
    return {
        "hold": order.hold_message or DEFAULT_HOLD_MESSAGE,
        "whisper": f"{order.whisper_text or 'Incoming transfer.'} {WHISPER_PROMPT}",
        "voicemail": order.voicemail_script,
        "callback": order.callback_message or DEFAULT_CALLBACK_MESSAGE,
    }


async def _render_one(text: str, voice: str) -> bytes:
    client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)
    chunks = []
    async for chunk in client.speak.v1.audio.generate(
            text=text, model=voice, encoding="mp3"):
        chunks.append(chunk)
    audio = b"".join(chunks)
    if not audio:
        raise ValueError("empty audio")
    return audio


def _reason(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    return f"{type(exc).__name__}: {exc}"[:120]


class LineRenders:
    """The four lines rendering concurrently, started at the handoff."""

    def __init__(self, order: EscalationOrder, voice: str, render=None) -> None:
        render = render or _render_one
        self.voice = voice
        self.texts = {k: v for k, v in line_texts(order).items() if v.strip()}
        self.failed: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        if not voice:
            self.failed = {k: "no persona voice" for k in self.texts}
            return
        for name, text in self.texts.items():
            self._tasks[name] = asyncio.create_task(self._one(render, text))

    async def _one(self, render, text: str) -> str:
        audio = await asyncio.wait_for(render(text, self.voice),
                                       timeout=_LINE_RENDER_TIMEOUT_SEC)
        token = uuid.uuid4().hex
        await put_audio(token, audio)
        return token

    def _settle(self, name: str) -> str | None:
        task = self._tasks.get(name)
        if task is None or not task.done():
            return None
        if task.cancelled() or task.exception() is not None:
            exc = task.exception() if not task.cancelled() else asyncio.CancelledError()
            self.failed.setdefault(name, _reason(exc))
            logger.error("escalation line %r not rendered in voice %s (%s) "
                         "— Twilio <Say> for that line", name, self.voice,
                         self.failed[name])
            return None
        return task.result()

    async def wait(self, name: str, timeout: float) -> str | None:
        """The line's token once rendered, or None (it will be <Say>) when it
        failed or is not ready within ``timeout`` — the render goes on."""
        task = self._tasks.get(name)
        if task is None:
            return None
        await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            self.failed[name] = f"not ready within {timeout:g}s"
            logger.error("escalation line %r not ready in %.1fs — Twilio <Say> "
                         "for it", name, timeout)
            task.add_done_callback(_discard_token)     # nobody will play it
            return None
        return self._settle(name)

    def attach(self, call_sid: str) -> asyncio.Task:
        """Hand the still-rendering lines to the transfer: each token is
        written into the transfer state as it lands, each failure into
        ``audio_failed``. After execute, this task is the one writer of
        those two fields."""
        task = asyncio.create_task(self._attach(call_sid))
        _attached.add(task)
        task.add_done_callback(_attached.discard)
        return task

    async def _attach(self, call_sid: str) -> None:
        if await get_transfer(call_sid) is None:
            # No transfer came of it (the hold failed, no targets): nothing
            # will ever play these lines or delete them with the transfer.
            for task in self._tasks.values():
                task.add_done_callback(_discard_token)
            return
        # The hold line's fate is in the initiation outcome; every other
        # line's goes to the transfer state (and from there into the
        # resolution) — including ones that had already failed.
        already = {n: r for n, r in self.failed.items() if n != "hold"}
        if already:
            await update_transfer(call_sid, audio_failed=already)
        pending = {t: n for n, t in self._tasks.items()
                   if n not in self.failed and n != "hold"}
        while pending:
            done, _ = await asyncio.wait(set(pending), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = pending.pop(task)
                token = self._settle(name)
                state = await get_transfer(call_sid)
                if state is None:
                    # The transfer ended before the line was needed: nothing
                    # will delete this audio with it, so delete it now, and
                    # every line still rendering as it lands.
                    if token:
                        await delete_audio([token])
                    for rest in pending:
                        rest.add_done_callback(_discard_token)
                    return
                if token:
                    await update_transfer(call_sid, audio={**state.audio, name: token})
                else:
                    await update_transfer(call_sid, audio_failed={
                        **state.audio_failed, name: self.failed.get(name, "failed")})


_attached: set[asyncio.Task] = set()


def _discard_token(task: asyncio.Task) -> None:
    if task.cancelled() or task.exception() is not None:
        return
    cleanup = asyncio.ensure_future(delete_audio([task.result()]))
    _attached.add(cleanup)
    cleanup.add_done_callback(_attached.discard)
