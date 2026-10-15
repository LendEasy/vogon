"""
Smoke test: the gateway's Deepgram Flux STT session end to end — connect,
stream real speech, and receive a FINAL through the current callback contract.

The speech is a short sentence synthesized by the gateway's own TTS client and
resampled to 16 kHz, so the final callback actually fires. The previous version
sent only silence: the final never ran, so a stale callback signature (three
arguments against the four STTSession passes) went unnoticed for months.

Needs DEEPGRAM_API_KEY. Run with:
    PYTHONPATH=. .venv/bin/python tests/test_stt_connection.py
"""
import asyncio
import audioop
import logging
import sys

from vogon.gateway.pipeline.stt import STTSession
from vogon.gateway.pipeline.tts import TTSClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

SENTENCE = "Hello, this is a connection test of the voice gateway."
CHUNK_MS = 100
STT_RATE = 16000
TTS_RATE = 24000


async def synthesize(text: str) -> bytes:
    """``text`` as 16 kHz 16-bit mono PCM, via the gateway's TTS WebSocket."""
    tts = TTSClient(encoding="linear16", sample_rate=TTS_RATE)
    await tts.open()
    try:
        pcm24 = b"".join([chunk async for chunk, _latency in tts.stream(text)])
    finally:
        await tts.close()
    pcm16, _ = audioop.ratecv(pcm24, 2, 1, TTS_RATE, STT_RATE, None)
    return pcm16


async def run_test() -> bool:
    received_interims: list[str] = []
    received_finals: list[str] = []
    callback_errors: list[str] = []

    # A callback that raises does so inside the session's own task; the loop's
    # exception handler is the only place that sees it.
    def on_loop_error(loop, context):
        callback_errors.append(str(context.get("exception") or context.get("message")))
        logger.error("task exception: %s", callback_errors[-1])
    asyncio.get_running_loop().set_exception_handler(on_loop_error)

    async def on_interim(text: str, confidence: float) -> None:
        received_interims.append(text)

    async def on_final(text: str, confidence: float, elapsed_ms: float, words: list[str]) -> None:
        logger.info("FINAL: %r (conf=%.2f, elapsed=%.0fms, low-confidence words=%r)",
                    text, confidence, elapsed_ms, words)
        received_finals.append(text)

    logger.info("Synthesizing %r...", SENTENCE)
    speech = await synthesize(SENTENCE)
    logger.info("Speech: %.2fs of audio", len(speech) / 2 / STT_RATE)

    session = STTSession(on_interim=on_interim, on_final=on_final, sample_rate=STT_RATE)
    logger.info("Opening STT session...")
    elapsed = await session.start()
    logger.info("Session opened in %.0fms", elapsed)

    step = STT_RATE * 2 * CHUNK_MS // 1000
    silence = b"\x00" * step
    for _ in range(5):                                    # 0.5 s lead-in
        await session.send_audio(silence)
        await asyncio.sleep(CHUNK_MS / 1000)
    for i in range(0, len(speech), step):                 # the sentence, at real-time pace
        await session.send_audio(speech[i:i + step])
        await asyncio.sleep(CHUNK_MS / 1000)
    for _ in range(30):                                   # 3 s of silence for Flux's end of turn
        if received_finals:
            break
        await session.send_audio(silence)
        await asyncio.sleep(CHUNK_MS / 1000)
    await asyncio.sleep(0.5)
    await session.close()

    logger.info("--- Results ---")
    logger.info("Interim messages received: %d", len(received_interims))
    logger.info("Final messages received:   %d", len(received_finals))
    ok = bool(received_finals) and not callback_errors
    if not received_finals:
        logger.error("no FINAL received — end of turn never fired")
    if callback_errors:
        logger.error("%d callback error(s): %s", len(callback_errors), callback_errors[0])
    logger.info("Connection test %s", "PASSED" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_test()) else 1)
