"""Phase 3 — bounded playout buffer (full-duplex-semantic-barge-in §6).

Covers: the pure sentence splitter, multi-segment TTS demux, exact heard
tracking, the graceful-yield verdict mapping, and the config defaults
(tts_pacing_mode="sentence", tts_buffer_lead_ms=500 — user decisions
2026-07-16).
"""
from __future__ import annotations

import asyncio
import time

from deepgram.speak.v1.types.speak_v1flushed import SpeakV1Flushed
from transport_fixtures import bare_twilio_transport

from vogon.gateway.pipeline.sentence_split import split_sentences
from vogon.gateway.pipeline.tts import TTSClient
from vogon.gateway.settings import GatewaySettings as Settings
from vogon.gateway.settings import settings
from vogon.gateway.transport.delivery import DeliveryReport

# ── split_sentences (pure) ──────────────────────────────────────────────────────

def test_split_never_breaks_amounts():
    text = "Your past-due balance is $201.42. Are you able to take care of that today?"
    got = split_sentences(text)
    assert got == [
        "Your past-due balance is $201.42.",
        "Are you able to take care of that today?",
    ]


def test_split_the_payment_readback():
    text = (
        "Sure, I can process a $100.00 payment today using your Visa card ending in 2488; "
        "one moment while I update that. After that, $101.42 of the past-due balance will "
        "remain, and you'll receive a confirmation email shortly. Does that sound correct?"
    )
    got = split_sentences(text)
    # "Does that sound correct?" (24 chars) merges backward per the min-chars
    # rule — the consent question rides with the terms sentence, never alone.
    assert len(got) == 2
    assert got[-1].endswith("Does that sound correct?")
    assert "".join(got).count("$") == 2         # amounts intact


def test_short_fragments_merge_forward():
    got = split_sentences("Okay. Let me pull up the details on your account now.")
    assert got == ["Okay. Let me pull up the details on your account now."]


def test_short_trailing_fragment_merges_backward():
    got = split_sentences(
        "I have verified your identity and updated the record on file. Thank you."
    )
    assert got == [
        "I have verified your identity and updated the record on file. Thank you."
    ]


def test_single_sentence_and_empty():
    assert split_sentences("Hello there, how are you today?") == ["Hello there, how are you today?"]
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_reassembly_preserves_all_words():
    text = "First sentence here is long enough. Second sentence also long enough! Third one asks a question?"
    got = split_sentences(text)
    assert " ".join(got).split() == text.split()


# ── DeliveryReport sentence fields ──────────────────────────────────────────────

def test_report_sentence_fields_default_off():
    r = DeliveryReport(audio_id="a", status="complete")
    assert r.sentences_total == 0 and r.sentences_sent == 0 and r.heard_text == ""


# ── stream_segments demux (fake ws — Flushed events demarcate segments) ─────────

def test_stream_segments_demux():
    audio = [b"\x00" * 160, b"\x01" * 160, b"\x02" * 160]

    class _FakeWS:
        def __init__(self):
            self.sent = []
            self._messages = [
                audio[0],
                SpeakV1Flushed(type="Flushed", sequence_id=0),   # segment 0 done
                audio[1], audio[2],
                SpeakV1Flushed(type="Flushed", sequence_id=1),   # segment 1 done
            ]

        async def send_text(self, msg): self.sent.append(("text", msg))
        async def send_flush(self): self.sent.append(("flush", None))

        def __aiter__(self):
            async def gen():
                for m in self._messages:
                    yield m
            return gen()

    tts = object.__new__(TTSClient)
    tts._ws_client = _FakeWS()
    tts._voice = "test"

    async def run():
        out = []
        async for seg, chunk, lat in tts.stream_segments(["seg zero.", "seg one."]):
            out.append((seg, chunk, lat is not None))
        return out

    got = asyncio.run(run())
    assert got == [(0, audio[0], True), (1, audio[1], False), (1, audio[2], False)]
    # All segments + flushes sent UPFRONT (prefetch by design).
    kinds = [k for k, _ in tts._ws_client.sent]
    assert kinds == ["text", "flush", "text", "flush"]


# ── Exact heard tracking + graceful yield (transport attribute-level) ───────────

def _bare_transport():
    return bare_twilio_transport(
        _last_speak_text="First sentence is long enough here. Second sentence is also long enough.",
        _current_sentences=[
            "First sentence is long enough here.",
            "Second sentence is also long enough.",
        ],
        _sentences_played=1,
        _tts_started_at=time.monotonic(),
    )


def test_heard_split_exact_in_sentence_mode():
    t = _bare_transport()
    heard, unheard = t._playout_heard_split()
    assert heard == "First sentence is long enough here."
    assert unheard == "Second sentence is also long enough."


class _FakeAudioLog:
    def __init__(self): self.events = []
    def emit(self, ts, name, **kw): self.events.append((name, kw))


def test_answer_to_heard_yields_gracefully_not_cut(monkeypatch):
    monkeypatch.setattr(settings, "tts_pacing_mode", "sentence")
    t = _bare_transport()
    t._graceful_yield = False
    t._tts_playing = True
    t._tts_cancel = asyncio.Event()
    t._audio_log = _FakeAudioLog()
    asyncio.run(t._act_on_barge_verdict("answer_to_heard", "llm", "yes it's a good time", time.monotonic()))
    assert t._graceful_yield is True            # stop after current sentence
    assert not t._tts_cancel.is_set()           # NO destructive cut
    assert any(name == "barge_yield" for name, _ in t._audio_log.events)


def test_answer_to_heard_hard_cuts_in_burst_mode(monkeypatch):
    monkeypatch.setattr(settings, "tts_pacing_mode", "burst")
    t = _bare_transport()
    t._graceful_yield = False
    t._tts_playing = True
    t._tts_cancel = asyncio.Event()
    t._audio_log = _FakeAudioLog()
    t._candidate_raw_rms = 0
    t._candidate_clean_rms = 0

    class _S:
        config = {"assistant": {"workflow_id": "wf"}}
        call_id = "t"
    t._session = _S()

    class _Gate:
        threshold = 1200
        class cfg:
            confirm = "off"
    t._barge_gate = _Gate()
    asyncio.run(t._act_on_barge_verdict("answer_to_heard", "llm", "yes", time.monotonic()))
    assert t._tts_cancel.is_set()               # kill switch restores Phase-1 cut


def test_config_defaults_phase3():
    f = Settings.model_fields
    assert f["tts_pacing_mode"].default == "sentence"
    assert f["tts_buffer_lead_ms"].default == 500
