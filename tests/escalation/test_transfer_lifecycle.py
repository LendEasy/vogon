"""The warm transfer, leg by leg, against a fake Twilio REST client.

What these pin (each was a gap found reading the code, 2026-10-01):

  1. the agent dial registers only status-callback events Twilio accepts
     (initiated|ringing|answered|completed — anything else is error 21626,
     which rejected EVERY agent dial) and an integer timeout
  2. "initiated" means the caller is on hold AND the first agent leg was
     placed — not "a dial was scheduled"
  3. a dial that fails (first or later) never strands the caller on hold:
     the next target is tried, then the callback fallback runs
  4. an agent who answers but never presses a key hands off to the next
     target, and that target's whisper still finds the transfer
  5. a status for a leg that is no longer current changes nothing
  6. a bridged transfer ends cleanly when the agent hangs up
  7. a caller who hangs up on hold cancels the agent leg
  8. the voicemail fallback waits for the target's greeting to end

    cd voice-gateway
    python3 -m pytest tests/escalation -q
"""
from __future__ import annotations

import asyncio

from vogon.gateway_service.escalation import callbacks
from vogon.gateway_service.escalation.state import (
    TransferState,
    get_transfer,
    set_transfer,
)
from vogon.gateway_service.escalation.transfer import TransferOrchestrator
from vogon_contract import EscalationOrder, EscalationTarget

CALLER = "CAcaller0001"
T1, T2 = "+15550001111", "+15550002222"
TWILIO_STATUS_EVENTS = {"initiated", "ringing", "answered", "completed"}


def _orch():
    return TransferOrchestrator("AC", "tok", "+15559990000",
                                "https://gw.example.com/gw",
                                callback_prefix="/v1/escalation")


def _order(targets=(T1, T2), **kw):
    base = dict(
        targets=[EscalationTarget(number=n, label=f"L{i}")
                 for i, n in enumerate(targets)],
        whisper_text="Caller wants a human.", agent_timeout_sec=25.0,
        hold_message="Hold please.", voicemail_script="Please call back.",
        callback_message="We will call you back.")
    base.update(kw)
    return EscalationOrder(**base)


def _seed(idx=0, agent_call_sid="CAleg1", connected=False, targets=(T1, T2)):
    asyncio.run(set_transfer(CALLER, TransferState(
        call_sid=CALLER, owner_id="", workflow_id="",
        conference_name=f"esc_{CALLER}", summary_text="The whisper.",
        config={"agent_connect_timeout_sec": 25,
                "voicemail_script": "Please call back.",
                "caller_callback_message": "We will call you back."},
        targets=[{"phone_number": n, "label": f"L{i}"}
                 for i, n in enumerate(targets)],
        current_target_idx=idx, agent_call_sid=agent_call_sid,
        connected=connected)))


def _status(call_status, leg_sid):
    return asyncio.run(callbacks.status_body(
        CALLER, call_status, _orch(), agent_call_sid=leg_sid))


def _caller_updates(twilio):
    return [kw for sid, kw in twilio.updates if sid == CALLER]


# ── 1. the dial Twilio will accept ──────────────────────────────────────────

def test_agent_dial_registers_only_events_twilio_accepts(twilio):
    assert asyncio.run(_orch().execute(CALLER, _order())) == "initiated"
    (dial,) = twilio.creates
    events = set(dial["status_callback_event"])
    assert events <= TWILIO_STATUS_EVENTS, events - TWILIO_STATUS_EVENTS
    assert "completed" in events      # terminal statuses ride this event
    assert isinstance(dial["timeout"], int) and dial["timeout"] == 25


# ── 2. "initiated" is a fact, not a promise ─────────────────────────────────

def test_initiated_means_the_agent_leg_was_placed(twilio):
    async def scenario():
        status = await _orch().execute(CALLER, _order())
        # no sleep: the dial must have happened before the status came back
        return status, list(twilio.creates), await get_transfer(CALLER)

    status, creates, state = asyncio.run(scenario())
    assert status == "initiated"
    assert [c["to"] for c in creates] == [T1]
    assert state is not None and state.agent_call_sid == "CAleg1"


# ── 3. a failed dial never strands the caller ───────────────────────────────

def test_a_failed_first_dial_tries_the_next_target(twilio):
    twilio.create_errors[T1] = RuntimeError("21215 geo permission")
    assert asyncio.run(_orch().execute(CALLER, _order())) == "initiated"
    assert [c["to"] for c in twilio.creates] == [T1, T2]
    assert asyncio.run(get_transfer(CALLER)).current_target_idx == 1


def test_every_dial_failing_is_failed_and_the_caller_hears_the_callback(twilio):
    twilio.create_errors[T1] = RuntimeError("down")
    twilio.create_errors[T2] = RuntimeError("down")
    assert asyncio.run(_orch().execute(CALLER, _order())) == "failed"
    last = _caller_updates(twilio)[-1]
    assert "We will call you back." in last.get("twiml", "")
    assert asyncio.run(get_transfer(CALLER)) is None


def test_a_failed_later_dial_falls_back_instead_of_stranding_the_caller(twilio):
    _seed(idx=0, agent_call_sid="CAleg1")
    twilio.create_errors[T2] = RuntimeError("down")
    _status("no-answer", "CAleg1")
    assert any("We will call you back." in kw.get("twiml", "")
               for kw in _caller_updates(twilio))
    assert asyncio.run(get_transfer(CALLER)) is None


# ── 4. answered but no keypress ─────────────────────────────────────────────

def test_an_unanswered_whisper_hands_off_and_the_next_whisper_finds_state(twilio):
    _seed(idx=0, agent_call_sid="CAleg1")
    _status("completed", "CAleg1")              # answered, Gather timed out
    assert [c["to"] for c in twilio.creates] == [T2]
    state = asyncio.run(get_transfer(CALLER))
    assert state is not None and state.current_target_idx == 1
    twiml = asyncio.run(callbacks.whisper_body(
        CALLER, "https://gw.example.com/gw", "/v1/escalation"))
    assert "The whisper." in twiml and "not found" not in twiml


# ── 5. stale legs ───────────────────────────────────────────────────────────

def test_a_status_for_a_leg_that_is_no_longer_current_is_ignored(twilio):
    _seed(idx=1, agent_call_sid="CAleg2")
    _status("no-answer", "CAleg1")              # a late duplicate for leg 1
    assert twilio.creates == [] and _caller_updates(twilio) == []
    assert asyncio.run(get_transfer(CALLER)).current_target_idx == 1


# ── 6. the bridged transfer ends ────────────────────────────────────────────

def test_keypress_then_agent_hangup_ends_the_transfer(twilio):
    _seed(idx=0, agent_call_sid="CAleg1")
    bridge = asyncio.run(callbacks.keypress_body(CALLER))
    assert f"esc_{CALLER}" in bridge
    _status("completed", "CAleg1")
    assert twilio.creates == []
    assert asyncio.run(get_transfer(CALLER)) is None


# ── 7. the caller leaves while on hold ──────────────────────────────────────

def test_a_caller_who_hangs_up_on_hold_cancels_a_ringing_agent_leg(twilio):
    _seed(idx=0, agent_call_sid="CAleg1")
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    assert ("CAleg1", {"status": "canceled"}) in twilio.updates
    assert asyncio.run(get_transfer(CALLER)) is None


def test_a_caller_who_hangs_up_during_the_whisper_ends_the_answered_leg(twilio):
    _seed(idx=0, agent_call_sid="CAleg1")
    asyncio.run(callbacks.whisper_body(CALLER, "https://gw.example.com/gw",
                                       "/v1/escalation"))
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    assert ("CAleg1", {"status": "completed"}) in twilio.updates
    assert asyncio.run(get_transfer(CALLER)) is None


def test_a_bridged_caller_hanging_up_only_clears_the_transfer(twilio):
    _seed(idx=0, agent_call_sid="CAleg1", connected=True)
    asyncio.run(callbacks.caller_status_body(CALLER, "completed", _orch))
    assert twilio.updates == []            # the conference already ended both
    assert asyncio.run(get_transfer(CALLER)) is None


# ── 8. the voicemail fallback ───────────────────────────────────────────────

def test_the_voicemail_fallback_waits_for_the_greeting_to_end(twilio):
    _seed(idx=1, agent_call_sid="CAleg2")
    _status("no-answer", "CAleg2")
    (vm,) = twilio.creates
    assert vm["to"] == T2
    assert vm.get("machine_detection") == "DetectMessageEnd"
    assert "Please call back." in vm["twiml"]


# ── 9. the caller leaves while a dial is in flight ──────────────────────────

def test_a_leg_placed_after_the_caller_hung_up_is_cancelled(twilio):
    """The caller's hang-up lands while calls.create is still in flight:
    handle_caller_gone sees DIALING (no leg to cancel yet) and closes the
    transfer. The leg that create then returns would ring an agent into an
    empty conference — it is cancelled instead."""
    orch = _orch()
    real_create = twilio.calls.create
    running = {}

    def create_while_caller_hangs_up(**kwargs):
        # create runs on an executor thread; the caller's status webhook is
        # delivered on the event loop before the dial returns its sid
        leg = real_create(**kwargs)
        asyncio.run_coroutine_threadsafe(
            callbacks.caller_status_body(CALLER, "completed", lambda: orch),
            running["loop"]).result(timeout=5)
        return leg
    twilio.calls.create = create_while_caller_hangs_up

    async def scenario():
        running["loop"] = asyncio.get_running_loop()
        return await orch.execute(CALLER, _order(targets=(T1,)))

    assert asyncio.run(scenario()) == "failed"
    assert ("CAleg1", {"status": "canceled"}) in twilio.updates
    assert asyncio.run(get_transfer(CALLER)) is None
