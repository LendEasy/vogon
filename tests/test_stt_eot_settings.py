"""STT end-of-turn tuning — config defaults and transport wiring."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from vogon.gateway.pipeline.stt import STTSession
from vogon.gateway.settings import GatewaySettings as Settings
from vogon.gateway.settings import settings
from vogon.gateway.transport.twilio_transport import TwilioTransport
from vogon_contract import CallConfig


class TestEotConfigDefaults:

    def test_hold_reduced_from_legacy_500ms(self):
        assert Settings.model_fields["deepgram_eot_hold_ms"].default == 250

    def test_browser_eot_timeout_tighter_than_phone(self):
        assert Settings.model_fields["deepgram_eot_timeout_ms"].default == 1000
        assert Settings.model_fields["deepgram_phone_eot_timeout_ms"].default == 1400
        assert (
            Settings.model_fields["deepgram_eot_timeout_ms"].default
            < Settings.model_fields["deepgram_phone_eot_timeout_ms"].default
        )

    def test_phone_eot_timeout_reduced_from_legacy_2000ms(self):
        assert Settings.model_fields["deepgram_phone_eot_timeout_ms"].default == 1400

    def test_eager_threshold_below_definitive_threshold(self):
        assert Settings.model_fields["deepgram_eager_eot_threshold"].default == 0.72
        assert Settings.model_fields["deepgram_phone_eager_eot_threshold"].default == 0.70
        assert (
            Settings.model_fields["deepgram_phone_eager_eot_threshold"].default
            <= Settings.model_fields["deepgram_phone_eot_threshold"].default
        )
        assert (
            Settings.model_fields["deepgram_eager_eot_threshold"].default
            <= Settings.model_fields["deepgram_eot_threshold"].default
        )


class TestSttSessionEotWiring:

    def test_uses_settings_when_not_overridden(self):
        session = STTSession(on_interim=lambda *a: None, on_final=lambda *a: None)
        assert session._eot_timeout_ms == settings.deepgram_eot_timeout_ms
        assert session._eot_threshold == settings.deepgram_eot_threshold

    def test_accepts_explicit_phone_overrides(self):
        session = STTSession(
            on_interim=lambda *a: None,
            on_final=lambda *a: None,
            eot_timeout_ms=settings.deepgram_phone_eot_timeout_ms,
            eot_threshold=settings.deepgram_phone_eot_threshold,
        )
        assert session._eot_timeout_ms == 1400
        assert session._eot_threshold == 0.85


class TestNameHintReachesDeepgram:
    """CallConfig.name_hint → STTSession keyterms → Flux `keyterm=`.
    This path was DEAD on phone calls for months (the dial route stored the
    name under a domain key the platform never read); this pins the whole
    chain so it cannot silently die again."""

    def test_name_hint_tokens_become_keyterms(self):
        captured: dict = {}

        class _FakeSTT:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def start(self):
                return 12.0

        with patch("vogon.gateway.transport.twilio_transport.STTSession", _FakeSTT), patch(
            "vogon.gateway.transport.twilio_transport.TTSClient"
        ) as mock_tts:
            mock_tts.return_value.open = AsyncMock(return_value=None)
            transport = TwilioTransport(
                ws=None,
                send_raw=lambda _: None,
                call_id="t-call",
                call_config=CallConfig(
                    persona_voice="", name_hint="Joe Biden"),
                start_event={"streamSid": "MZt", "start": {"callSid": "CAt"}},
            )
            asyncio.run(transport.start())

        # Word-level split: independent boosts for each token, per the
        # name-recognition design (docs/name_recognition_design.md §Layer 1).
        assert captured["keyterms"] == ["Joe", "Biden"]


class TestTwilioTransportEotWiring:

    def test_start_passes_phone_eot_settings_to_stt(self):
        captured: dict = {}

        class _FakeSTT:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def start(self):
                return 12.0

        with patch("vogon.gateway.transport.twilio_transport.STTSession", _FakeSTT), patch(
            "vogon.gateway.transport.twilio_transport.TTSClient"
        ) as mock_tts:
            mock_tts.return_value.open = AsyncMock(return_value=None)
            transport = TwilioTransport(
                ws=None,
                send_raw=lambda _: None,
                call_id="t-call",
                call_config=CallConfig(persona_voice="", language="en-US"),
                start_event={"streamSid": "MZtest", "start": {"callSid": "CAtest"}},
            )
            asyncio.run(transport.start())

        assert captured["eot_timeout_ms"] == settings.deepgram_phone_eot_timeout_ms
        assert captured["eot_threshold"] == settings.deepgram_phone_eot_threshold
        assert captured["eager_eot_threshold"] == settings.deepgram_phone_eager_eot_threshold
        assert captured["on_eager_end"] is not None
        assert captured["on_turn_resumed"] is not None
