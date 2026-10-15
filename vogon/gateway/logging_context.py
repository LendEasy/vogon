"""Per-call logging context (the gateway's own; vogon step 2, slice 2).

A ContextVar holds the current call ID for the duration of a WebSocket
call.  A logging.Filter reads it and stamps every LogRecord with a
``call_id`` field so it appears in every log line without touching
individual logger calls.

asyncio.create_task copies the running context to child tasks at creation
time, so the value set in handle_call propagates automatically to
_send_loop, _utterance_processor, tools, agents, STT, TTS, etc.

Outside a call the variable defaults to "-" so startup/config log lines
are not broken.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

call_id_var: ContextVar[str] = ContextVar("call_id", default="-")


class CallIdFilter(logging.Filter):
    """Injects ``call_id`` into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = call_id_var.get()  # type: ignore[attr-defined]
        return True
