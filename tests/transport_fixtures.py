"""Shared construction helper for TwilioTransport unit tests.

History: transport tests used to build bare instances with
``object.__new__(TwilioTransport)`` plus a hand-listed attribute set. Every
time the transport grew a new attribute (``_delivery_ledger``,
``_protected_playout``, ...) those hand-lists silently went stale and whole
files failed with AttributeError. This helper runs the REAL ``__init__`` —
so every attribute always exists, present and future — with the three
external surfaces stubbed at construction:

- ``TTSClient`` — patched out (no Deepgram client object)
- ``create_aec`` — returns None (no speexdsp; frame path takes the no-AEC branch)
- ambience — disabled via ``assistant_cfg``

No websocket, no network, no disk. Anything else a test needs (fake STT,
fake gate, playout state) is applied via keyword overrides AFTER init, e.g.::

    t = bare_twilio_transport(
        _tts_playing=True,
        _last_speak_text="the active playout text",
    )
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from vogon.gateway.transport.twilio_transport import TwilioTransport
from vogon_contract import BargePolicy, CallConfig


class FakeSession:
    """Minimal stand-in for CallSession: real dict config (transport code does
    ``.config.get(...)`` chains that must return real values), no-op analytics."""

    def __init__(self):
        self.call_id = "test-call"
        self.config = {
            "assistant": {"workflow_id": "wf-test"},
            "_call_hints": {},
        }

    def record_analytics(self, *args, **kwargs):
        pass


def bare_twilio_transport(session=None, assistant_cfg=None, **overrides):
    """Build a real TwilioTransport via the real __init__, externals stubbed.

    ``overrides`` are set as attributes after construction (last wins), so a
    test states only what it actually cares about.
    """

    # assistant_cfg-shaped overrides still accepted: the ambience dict inside
    # is what the transport actually consumes, now via CallConfig.
    cfg = assistant_cfg if assistant_cfg is not None else {"ambience": {"enabled": False}}
    tcfg = CallConfig(
        persona_voice=cfg.get("persona_voice") or "",
        language=cfg.get("language", "en-US"),
        ambience=cfg.get("ambience") or {"enabled": False},
        # The APPLICATION supplies the break-through set (M1 item 9); the
        # fixture mirrors lifecycle's default so safety-cut behavior in
        # these tests matches a real call.
        barge_policy=BargePolicy(
            safety_phrases=[
                "stop calling", "don't call", "do not call", "take me off",
                "wrong number", "wrong person", "attorney", "lawyer", "sue you",
            ],
            floor_request_phrases=[
                "stop", "stop it", "stop talking", "quit talking",
                "shut up", "be quiet", "quiet",
                "let me talk", "let me speak", "let me finish", "listen to me",
                "hold on", "hang on", "wait",
            ],
        ),
    )
    with patch("vogon.gateway.transport.twilio_transport.TTSClient"), \
         patch("vogon.gateway.transport.twilio_transport.create_aec", return_value=None):
        t = TwilioTransport(
            ws=MagicMock(),
            send_raw=AsyncMock(),
            call_id=getattr(session, "call_id", "fixture-call"),
            call_config=tcfg,
        )
    # STT is normally attached in start(); give tests an inspectable fake.
    stt = MagicMock()
    stt.send_audio = AsyncMock()
    t._stt = stt

    for key, value in overrides.items():
        setattr(t, key, value)
    return t
