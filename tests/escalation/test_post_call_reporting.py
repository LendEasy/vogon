"""What the call log learns about a transfer after the call: the post-call
facts the gateway reports for each path (ESCALATION_WARM_TRANSFER.md §3).

Each path pins the ORDER of facts, their numbering (gap-free from 1 per
transfer), the one EscalationResolved, and where they are sent.

    cd voice-gateway
    python3 -m pytest tests/escalation/test_post_call_reporting.py -q
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from vogon.gateway_service.escalation import callbacks, report
from vogon.gateway_service.escalation.state import TransferState, get_transfer, put_audio, update_transfer
from vogon.gateway_service.escalation.transfer import TransferOrchestrator
from vogon.gateway_service.escalation.voice import LineRenders
from vogon_contract import (
    EscalationLegEvent,
    EscalationOrder,
    EscalationResolved,
    EscalationTarget,
)
from vogon_contract.codec import record_transfer_request_from_wire

CALLER = "CAcaller0003"
T1, T2 = "+15550001111", "+15550002222"
BASE = "https://gw.example.com/gw"


def _orch():
    return TransferOrchestrator("AC", "tok", "+15559990000", BASE,
                                callback_prefix="/v1/escalation")


def _order():
    return EscalationOrder(
        escalation_id="esc-1",
        targets=[EscalationTarget(number=T1, label="Tier 1"),
                 EscalationTarget(number=T2, label="Tier 2")],
        whisper_text="Maria wants to dispute a fee.", agent_timeout_sec=20,
        hold_message="Hold please.", voicemail_script="Please call back.",
        callback_message="We will call you back.")


def _execute(audio=None):
    return asyncio.run(_orch().execute(
        CALLER, _order(), call_id="call-9", report_url="http://turn/turn/v1",
        report_token="secret", audio=audio))


def _status(status, leg):
    asyncio.run(callbacks.status_body(CALLER, status, _orch(), agent_call_sid=leg))


def _whisper(leg):
    return asyncio.run(callbacks.whisper_body(CALLER, BASE, "/v1/escalation",
                                              agent_call_sid=leg))


def _keypress(leg):
    return asyncio.run(callbacks.keypress_body(CALLER, agent_call_sid=leg))


def _assert_numbered(reported):
    assert [f.seq for f in reported.facts] == list(range(1, len(reported.facts) + 1))
    assert {f.escalation_id for f in reported.facts} == {"esc-1"}
    assert set(reported.targets) == {("http://turn/turn/v1", "secret", "call-9")}
    assert sum(isinstance(f, EscalationResolved) for f in reported.facts) <= 1


# ── each path, as the call log will show it ─────────────────────────────────

def test_answered_connected_and_ended(twilio, reported):
    assert _execute() == "initiated"
    _whisper("CAleg1")
    _keypress("CAleg1")
    _status("completed", "CAleg1")
    assert reported.kinds() == ["dialled", "answered", "connected",
                                "resolved:connected"]
    _assert_numbered(reported)
    resolved = reported.facts[-1]
    assert resolved.connected_target == "Tier 1" and resolved.attempts == 1
    assert resolved.hold_sec >= 0 and resolved.talk_sec >= 0
    assert reported.facts[0].target == "Tier 1" and reported.facts[0].agent_call_sid == "CAleg1"


def test_no_answer_then_the_second_target_connects(twilio, reported):
    _execute()
    _status("no-answer", "CAleg1")
    _whisper("CAleg2")
    _keypress("CAleg2")
    _status("completed", "CAleg2")
    assert reported.kinds() == ["dialled", "no_answer", "dialled", "answered",
                                "connected", "resolved:connected"]
    _assert_numbered(reported)
    assert reported.facts[2].attempt == 1 and reported.facts[2].target == "Tier 2"
    assert reported.facts[-1].connected_target == "Tier 2"
    assert reported.facts[-1].attempts == 2


def test_answered_without_a_keypress_is_reported_as_such(twilio, reported):
    _execute()
    _whisper("CAleg1")
    _status("completed", "CAleg1")
    assert reported.kinds()[:4] == ["dialled", "answered", "no_keypress", "dialled"]


def test_nobody_answers_voicemail_and_callback(twilio, reported):
    _execute()
    _status("busy", "CAleg1")
    _status("no-answer", "CAleg2")
    assert reported.kinds() == ["dialled", "busy", "dialled", "no_answer",
                                "voicemail_left", "resolved:fallback_callback"]
    _assert_numbered(reported)
    assert reported.facts[-1].attempts == 2 and reported.facts[-1].talk_sec == 0


def test_the_caller_abandons_on_hold_once(twilio, reported):
    _execute()
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    _status("canceled", "CAleg1")          # Twilio's status for the cancelled leg
    assert reported.kinds() == ["dialled", "resolved:caller_abandoned"]
    assert "canceled" in reported.facts[-1].detail


def test_every_dial_failing_is_reported_and_resolved_failed(twilio, reported):
    twilio.create_errors[T1] = RuntimeError("21215 geo")
    twilio.create_errors[T2] = RuntimeError("21215 geo")
    assert _execute() == "failed"
    assert reported.kinds() == ["dial_failed", "dial_failed", "resolved:failed"]
    assert "21215" in reported.facts[0].detail
    assert reported.facts[-1].attempts == 0


def test_a_bridged_transfer_is_resolved_once_whoever_hangs_up_first(twilio, reported):
    _execute()
    _keypress("CAleg1")
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    _status("completed", "CAleg1")
    assert reported.kinds().count("resolved:connected") == 1
    assert "caller hung up" in reported.facts[-1].detail


def test_nothing_is_reported_without_a_report_target(twilio, reported):
    asyncio.run(_orch().execute(CALLER, _order()))      # no call_id / url
    assert reported.facts == []


# ── the persona's voice ─────────────────────────────────────────────────────

def test_rendered_lines_are_played_and_deleted_with_the_transfer(twilio):
    tokens = {"hold": "a" * 32, "whisper": "b" * 32,
              "callback": "c" * 32, "voicemail": "d" * 32}
    for t in tokens.values():
        asyncio.run(put_audio(t, b"ID3audio"))
    _execute(audio=tokens)
    hold = twilio.updates[0][1]["twiml"]
    assert f"<Play>{BASE}/v1/escalation/audio/{'a' * 32}.mp3</Play>" in hold
    assert "<Say" not in hold.split("<Dial>")[0]
    assert f"/audio/{'b' * 32}.mp3" in _whisper("CAleg1")
    assert asyncio.run(callbacks.audio_body("b" * 32)) == b"ID3audio"
    _status("no-answer", "CAleg1")
    _status("no-answer", "CAleg2")                       # → fallback, transfer gone
    vm = twilio.creates[-1]["twiml"]
    assert f"/audio/{'d' * 32}.mp3" in vm
    assert f"/audio/{'c' * 32}.mp3" in twilio.updates[-1][1]["twiml"]
    assert asyncio.run(callbacks.audio_body("b" * 32)) is None


def test_an_unrendered_line_is_spoken_by_twilio(twilio):
    _execute(audio={})
    hold = twilio.updates[0][1]["twiml"]
    assert "<Say" in hold and "Hold please." in hold and "<Play>" not in hold


def _renders(fake_render, voice="aura-2-thalia-en"):
    return LineRenders(_order(), voice)


def test_every_line_renders_in_the_persona_voice(fake_render):
    async def go():
        r = _renders(fake_render)
        return r, {n: await r.wait(n, 1.0) for n in ("hold", "whisper", "voicemail", "callback")}
    fake_render.fail = {"Please call back"}
    r, tokens = asyncio.run(go())
    assert tokens["hold"] and tokens["whisper"] and tokens["callback"]
    assert tokens["voicemail"] is None and "voicemail" in r.failed
    assert {v for _, v in fake_render.calls} == {"aura-2-thalia-en"}
    whisper_text = [t for t, _ in fake_render.calls if t.startswith("Maria")][0]
    assert whisper_text.endswith("Press any key when you are ready to connect the caller.")


def test_no_persona_voice_means_every_line_is_said(fake_render):
    r = _renders(fake_render, voice="")
    assert set(r.failed) == {"hold", "whisper", "voicemail", "callback"}
    assert fake_render.calls == []


def test_late_lines_land_in_the_transfer_and_failures_reach_the_resolution(
        twilio, reported, fake_render):
    """The hold line is awaited; the whisper and the rest land after the
    caller is on hold. A line that fails is <Say>, and the resolution says so."""
    fake_render.fail = {"Please call back"}               # the voicemail script
    gate = {}

    async def slow_render(text, voice):
        if text.startswith("Maria"):                      # the whisper: slow
            await gate["release"].wait()
        return await fake_render(text, voice)

    async def scenario():
        gate["release"] = asyncio.Event()
        r = LineRenders(_order(), "aura-2-thalia-en", render=slow_render)
        hold = await r.wait("hold", 1.0)
        status = await _orch().execute(
            CALLER, _order(), call_id="call-9", report_url="http://turn/turn/v1",
            report_token="secret", audio={"hold": hold},
            audio_rendering=[n for n in r.texts if n != "hold" and n not in r.failed])
        task = r.attach(CALLER)
        await asyncio.sleep(0.05)
        before = (await get_transfer(CALLER)).audio
        gate["release"].set()
        await task
        after = await get_transfer(CALLER)
        return status, hold, before, after

    status, hold, before, after = asyncio.run(scenario())
    assert status == "initiated" and hold
    assert "whisper" not in before and after.audio["whisper"]
    assert after.audio_failed == {"voicemail": "RuntimeError: render down"}
    _status("no-answer", "CAleg1")
    _status("no-answer", "CAleg2")
    assert "voice: twilio <Say> for voicemail" in reported.facts[-1].detail


def test_the_whisper_waits_briefly_for_its_audio(twilio, reported, monkeypatch):
    """The agent answered while the whisper was still rendering: the whisper
    callback waits for it, then plays it in the persona's voice."""
    asyncio.run(_orch().execute(CALLER, _order(), call_id="call-9",
                                report_url="http://turn/turn/v1", report_token="s",
                                audio_rendering=["whisper"]))

    async def scenario():
        async def lands():
            await asyncio.sleep(0.3)
            await update_transfer(CALLER, audio={"whisper": "e" * 32})
        _, twiml = await asyncio.gather(lands(), callbacks.whisper_body(
            CALLER, BASE, "/v1/escalation", agent_call_sid="CAleg1"))
        return twiml
    twiml = asyncio.run(scenario())
    assert f"/audio/{'e' * 32}.mp3" in twiml
    answered = [f for f in reported.facts if getattr(f, "event", "") == "answered"]
    assert answered[0].detail == "whisper: persona voice"


def test_a_whisper_not_ready_in_time_is_said_and_reported(twilio, reported, monkeypatch):
    monkeypatch.setattr(callbacks, "_WHISPER_AUDIO_WAIT_SEC", 0.2)
    asyncio.run(_orch().execute(CALLER, _order(), call_id="call-9",
                                report_url="http://turn/turn/v1", report_token="s",
                                audio_rendering=["whisper"]))
    twiml = _whisper("CAleg1")
    assert "<Say" in twiml and "Maria wants to dispute a fee." in twiml
    answered = [f for f in reported.facts if getattr(f, "event", "") == "answered"]
    assert answered[0].detail == "whisper: twilio <Say>"


# ── delivery ────────────────────────────────────────────────────────────────

def _state():
    return TransferState(call_sid=CALLER, owner_id="", workflow_id="",
                         conference_name="c", summary_text="w", config={},
                         targets=[], call_id="call-9", escalation_id="esc-1",
                         report_url="http://turn/turn/v1", report_token="secret")


def _fact(seq=1):
    return EscalationLegEvent(escalation_id="esc-1", seq=seq, event="dialled")


def _deliver(monkeypatch, responses):
    monkeypatch.setattr(report, "_POST_CALL_RETRY_DELAYS_SEC", (0.0, 0.0, 0.0))
    seen = []

    def handler(request):
        seen.append(request)
        return responses.pop(0) if responses else httpx.Response(200, json={"appended": 1})

    rep = report.HttpFactReporter(transport=httpx.MockTransport(handler))

    async def go():
        rep.send(_state(), _fact())
        await asyncio.gather(*rep._tasks)
    asyncio.run(go())
    return seen


def test_delivery_retries_while_the_call_is_finalizing(monkeypatch):
    seen = _deliver(monkeypatch, [httpx.Response(409), httpx.Response(409)])
    assert len(seen) == 3
    req = seen[-1]
    assert str(req.url) == "http://turn/turn/v1/record_transfer"
    assert req.headers["authorization"] == "Bearer secret"
    body = record_transfer_request_from_wire(json.loads(req.content))
    assert body.call_id == "call-9" and body.events == [_fact()]


def test_a_refused_fact_is_logged_not_retried(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    seen = _deliver(monkeypatch, [httpx.Response(401)])
    assert len(seen) == 1
    assert "POST-CALL FACT UNDELIVERED" in caplog.text and "esc-1" in caplog.text


def test_an_undeliverable_fact_is_logged_after_the_last_attempt(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    seen = _deliver(monkeypatch, [httpx.Response(503)] * 10)
    assert len(seen) == 4                    # first try + three retries
    assert "POST-CALL FACT UNDELIVERED" in caplog.text and "seq=1" in caplog.text


def test_the_fallback_lines_outlive_the_transfer(twilio):
    """Twilio fetches the voicemail MP3 when that call is answered and the
    callback MP3 when the TwiML plays — both AFTER the transfer closed. On
    the 2026-10-01 local run both 404ed: the close deleted every line. The
    whisper, which nothing will play again, still goes."""
    tokens = {"hold": "a" * 32, "whisper": "b" * 32, "callback": "c" * 32, "voicemail": "d" * 32}
    for t in tokens.values():
        asyncio.run(put_audio(t, b"ID3audio"))
    _execute(audio=tokens)
    _status("no-answer", "CAleg1")
    _status("no-answer", "CAleg2")                       # → fallback, transfer closed
    assert asyncio.run(get_transfer(CALLER)) is None
    assert asyncio.run(callbacks.audio_body("d" * 32)) == b"ID3audio"   # voicemail
    assert asyncio.run(callbacks.audio_body("c" * 32)) == b"ID3audio"   # callback
    assert asyncio.run(callbacks.audio_body("b" * 32)) is None          # whisper
    assert asyncio.run(callbacks.audio_body("a" * 32)) is None          # hold
