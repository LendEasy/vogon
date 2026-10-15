"""STT eager end-of-turn event routing."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from deepgram.listen.v2.types import ListenV2TurnInfo

from vogon.gateway.pipeline.stt import STTSession
from vogon.gateway.settings import settings


def _turn_info(event: str, transcript: str = "hello there", eot_conf: float = 0.8):
    msg = MagicMock(spec=ListenV2TurnInfo)
    msg.event = event
    msg.transcript = transcript
    msg.end_of_turn_confidence = eot_conf
    word = MagicMock()
    word.word = "hello"
    word.confidence = 0.9
    msg.words = [word]
    return msg


class TestSttEagerEotEvents:

    def test_eager_mode_passes_eager_threshold_to_connect(self):
        session = STTSession(
            on_interim=AsyncMock(),
            on_final=AsyncMock(),
            eager_eot_threshold=0.7,
            on_eager_end=AsyncMock(),
            on_turn_resumed=AsyncMock(),
            eot_threshold=0.85,
        )
        assert session._eager_mode is True

        captured: dict = {}

        class _FakeConn:
            def on(self, *a, **k):
                pass

            async def start_listening(self):
                pass

        class _FakeCtx:
            async def __aenter__(self):
                return _FakeConn()

            async def __aexit__(self, *a):
                pass

        with patch("vogon.gateway.pipeline.stt.AsyncDeepgramClient") as mock_client:
            mock_client.return_value.listen.v2.connect = lambda **kw: (captured.update(kw), _FakeCtx())[1]
            asyncio.run(session.start())

        assert captured["eager_eot_threshold"] == "0.7"
        assert captured["eot_threshold"] == "0.85"

    def test_eager_end_dispatches_callback_not_final(self):
        on_eager = AsyncMock()
        on_final = AsyncMock()
        on_resume = AsyncMock()
        session = STTSession(
            on_interim=AsyncMock(),
            on_final=on_final,
            eager_eot_threshold=settings.deepgram_eager_eot_threshold,
            on_eager_end=on_eager,
            on_turn_resumed=on_resume,
        )

        async def _run():
            session._on_message(_turn_info("EagerEndOfTurn", "I can pay Friday"))
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        on_eager.assert_awaited_once()
        on_final.assert_not_awaited()

    def test_turn_resumed_dispatches_cancel_callback(self):
        on_eager = AsyncMock()
        on_final = AsyncMock()
        on_resume = AsyncMock()
        session = STTSession(
            on_interim=AsyncMock(),
            on_final=on_final,
            eager_eot_threshold=0.7,
            on_eager_end=on_eager,
            on_turn_resumed=on_resume,
        )

        async def _run():
            session._on_message(_turn_info("TurnResumed", "I can pay next Friday"))
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        on_resume.assert_awaited_once()
        on_final.assert_not_awaited()

    def test_end_of_turn_dispatches_final_immediately_in_eager_mode(self):
        on_eager = AsyncMock()
        on_final = AsyncMock()
        on_resume = AsyncMock()
        session = STTSession(
            on_interim=AsyncMock(),
            on_final=on_final,
            eager_eot_threshold=0.7,
            on_eager_end=on_eager,
            on_turn_resumed=on_resume,
        )

        async def _run():
            with patch.object(settings, "deepgram_eot_hold_ms", 250):
                session._on_message(_turn_info("EndOfTurn", "I can pay next Friday"))
                await asyncio.sleep(0.05)

        asyncio.run(_run())

        on_final.assert_awaited_once()
        assert session._eot_hold_task is None

    def test_legacy_mode_holds_before_final_without_eager_callbacks(self):
        on_final = AsyncMock()
        session = STTSession(
            on_interim=AsyncMock(),
            on_final=on_final,
        )

        async def _run():
            with patch.object(settings, "deepgram_eot_hold_ms", 50):
                session._on_message(_turn_info("EndOfTurn", "done speaking"))
                await asyncio.sleep(0)
                on_final.assert_not_awaited()
                await asyncio.sleep(0.08)

        asyncio.run(_run())
        on_final.assert_awaited_once()
