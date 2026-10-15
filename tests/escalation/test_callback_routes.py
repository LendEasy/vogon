"""The gateway's escalation callback ROUTES: Twilio signs every request, and
the signature is over the PUBLIC URL Twilio called — behind nginx's /gw/
strip that is not the URL the app sees. The test signs exactly the URLs
the orchestrator registered, so the two cannot drift apart.

    cd voice-gateway
    python3 -m pytest tests/escalation/test_callback_routes.py -q
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from vogon.gateway import settings as gw_settings
from vogon.gateway_service import routes
from vogon.gateway_service.config import gateway_config
from vogon.gateway_service.escalation.state import TransferState, get_transfer, put_audio, set_transfer
from vogon.gateway_service.main import create_app
from vogon.gateway_service.session_store import CallSessionRecord, MemorySessionStore

AUTH = "test-auth-token"
PUBLIC = "https://voice.example.com/gw"
CALLER = "CAcaller0002"


@pytest.fixture()
def gw(monkeypatch, twilio):
    monkeypatch.setattr(gw_settings.settings, "twilio_auth_token", AUTH)
    monkeypatch.setattr(gw_settings.settings, "twilio_account_sid", "AC1")
    monkeypatch.setenv("GATEWAY_PUBLIC_BASE_URL", PUBLIC)
    gateway_config.cache_clear()
    routes.use_store(MemorySessionStore())
    routes._esc_orchestrator = None
    asyncio.run(set_transfer(CALLER, TransferState(
        call_sid=CALLER, owner_id="", workflow_id="",
        conference_name=f"esc_{CALLER}", summary_text="The whisper.",
        config={"agent_connect_timeout_sec": 25,
                "caller_callback_message": "cb", "voicemail_script": "vm"},
        targets=[{"phone_number": "+15550001111", "label": "A"},
                 {"phone_number": "+15550002222", "label": "B"}],
        agent_call_sid="CAleg1")))
    with TestClient(create_app()) as client:
        yield client
    routes._esc_orchestrator = None
    gateway_config.cache_clear()


def _sign(url, params):
    return RequestValidator(AUTH).compute_signature(url, params)


def test_an_unsigned_status_callback_is_refused(gw, twilio):
    r = gw.post(f"/v1/escalation/agent-status/{CALLER}",
                data={"CallSid": "CAleg1", "CallStatus": "no-answer"})
    assert r.status_code == 403
    assert twilio.creates == []                    # nobody was dialled


def test_a_forged_signature_is_refused(gw, twilio):
    r = gw.post(f"/v1/escalation/agent-status/{CALLER}",
                data={"CallSid": "CAleg1", "CallStatus": "no-answer"},
                headers={"X-Twilio-Signature": "bm90LWEtc2ln"})
    assert r.status_code == 403 and twilio.creates == []


def test_a_signed_status_callback_on_the_registered_url_is_accepted(gw, twilio):
    url = f"{PUBLIC}/v1/escalation/agent-status/{CALLER}"
    form = {"CallSid": "CAleg1", "CallStatus": "no-answer"}
    r = gw.post(f"/v1/escalation/agent-status/{CALLER}", data=form,
                headers={"X-Twilio-Signature": _sign(url, form)})
    assert r.status_code == 200
    assert [c["to"] for c in twilio.creates] == ["+15550002222"]


def test_a_signed_whisper_get_is_accepted(gw):
    url = f"{PUBLIC}/v1/escalation/whisper/{CALLER}?AccountSid=AC1&CallSid=CAleg1"
    r = gw.get(f"/v1/escalation/whisper/{CALLER}",
               params={"AccountSid": "AC1", "CallSid": "CAleg1"},
               headers={"X-Twilio-Signature": _sign(url, {})})
    assert r.status_code == 200 and "The whisper." in r.text


def test_a_signed_keypress_is_accepted(gw):
    url = f"{PUBLIC}/v1/escalation/agent-keypress/{CALLER}"
    form = {"CallSid": "CAleg1", "Digits": "1"}
    r = gw.post(f"/v1/escalation/agent-keypress/{CALLER}", data=form,
                headers={"X-Twilio-Signature": _sign(url, form)})
    assert r.status_code == 200 and f"esc_{CALLER}" in r.text


def test_the_callers_completed_status_cancels_the_ringing_agent_leg(gw, twilio):
    """The original call's status webhook (registered at call creation)
    is where the caller's hang-up on hold arrives — no record of the
    session is needed, the transfer is keyed by the caller's CallSid."""
    url = f"{PUBLIC}/v1/twilio/status"
    form = {"CallSid": CALLER, "CallStatus": "completed"}
    r = gw.post("/v1/twilio/status", data=form,
                headers={"X-Twilio-Signature": _sign(url, form)})
    assert r.status_code == 200
    assert ("CAleg1", {"status": "canceled"}) in twilio.updates
    assert asyncio.run(get_transfer(CALLER)) is None


def test_the_say_voice_comes_from_gateway_config(monkeypatch, gw):
    monkeypatch.setenv("GATEWAY_ESCALATION_SAY_VOICE", "Google.en-US-Neural2-F")
    gateway_config.cache_clear()
    url = f"{PUBLIC}/v1/escalation/whisper/{CALLER}"
    r = gw.get(f"/v1/escalation/whisper/{CALLER}",
               headers={"X-Twilio-Signature": _sign(url, {})})
    assert 'voice="Google.en-US-Neural2-F"' in r.text


def test_an_ordinary_calls_status_webhook_is_untouched_by_the_hook(monkeypatch):
    """The caller-status hook runs on EVERY call's status webhook. With no
    transfer open — and here no Twilio credentials at all — it must cost a
    lookup and nothing else: the session still transitions."""

    monkeypatch.setattr(gw_settings.settings, "twilio_auth_token", "")
    monkeypatch.setattr(gw_settings.settings, "twilio_account_sid", "")
    gateway_config.cache_clear()
    routes._esc_orchestrator = None
    mem = MemorySessionStore()
    routes.use_store(mem)

    async def seed():
        await mem.create(CallSessionRecord(tenant="acme", call_id="c1",
                                           session_token="tok", mode="twilio"))
        await mem.bind_provider_id("acme", "c1", "CAordinary")
    asyncio.run(seed())
    with TestClient(create_app()) as client:
        r = client.post("/v1/twilio/status",
                        data={"CallSid": "CAordinary", "CallStatus": "completed"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert asyncio.run(mem.get("acme", "c1")).state == "ended"
    assert routes._esc_orchestrator is None       # never built for a non-transfer
    gateway_config.cache_clear()


def test_an_unsigned_call_status_webhook_is_refused(gw, twilio):
    r = gw.post("/v1/twilio/status", data={"CallSid": CALLER, "CallStatus": "completed"})
    assert r.status_code == 403
    assert twilio.updates == []                      # nothing cancelled


def test_rendered_audio_is_served_and_unknown_audio_is_404(gw):
    asyncio.run(put_audio("f" * 32, b"ID3x"))
    r = gw.get(f"/v1/escalation/audio/{'f' * 32}.mp3")
    assert r.status_code == 200 and r.content == b"ID3x"
    assert r.headers["content-type"] == "audio/mpeg"
    assert gw.get(f"/v1/escalation/audio/{'0' * 32}.mp3").status_code == 404


def test_the_twiml_webhook_is_signed_over_its_token_query(gw):
    """Twilio fetches the TwiML by GET with ?token=…; the signature covers
    that full URL. Unsigned → 403 (the caller hears a hangup — but a forged
    fetch could not learn the stream URL either)."""
    asyncio.run(routes.store().create(CallSessionRecord(
        tenant="acme", call_id="c-tw", session_token="tw-tok", mode="twilio")))
    assert gw.get("/v1/twilio/twiml", params={"token": "tw-tok"}).status_code == 403
    url = f"{PUBLIC}/v1/twilio/twiml?token=tw-tok"
    r = gw.get("/v1/twilio/twiml", params={"token": "tw-tok"},
               headers={"X-Twilio-Signature": _sign(url, {})})
    assert r.status_code == 200 and "<Stream" in r.text
    # a POST signs its form as well
    form = {"CallSid": "CA1", "CallStatus": "in-progress"}
    r = gw.post("/v1/twilio/twiml?token=tw-tok", data=form,
                headers={"X-Twilio-Signature": _sign(url, form)})
    assert r.status_code == 200 and "<Stream" in r.text
