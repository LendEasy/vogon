"""
Escalation simulation: TransferOrchestrator no-answer fallback.

Tests the transfer orchestrator fallback chain without real Twilio calls:
  1. test_no_answer_advances_to_next_target — handle_no_answer() advances idx and dials next.
  2. test_no_answer_fallback_when_all_exhausted — all targets tried → voicemail + callback.
  3. test_state_operations — CRUD on the in-memory state store (set/get/update/remove).

Twilio client is mocked via patch("vogon.gateway_service.escalation.transfer.TwilioClient").
State store _store is isolated per test via unique call_sid values.

Run:
    PYTHONPATH=. python3 -m pytest tests/simulate_escalation_no_answer.py -v -s
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock, patch

from vogon.gateway_service.escalation.state import (
    TransferState,
    get_transfer,
    remove_transfer,
    set_transfer,
    update_transfer,
)
from vogon.gateway_service.escalation.transfer import TransferOrchestrator

# ── Helpers ────────────────────────────────────────────────────────────────────

def _unique_sid(prefix: str = "CA") -> str:
    """Unique call SID per test to avoid cross-test state pollution."""
    return f"{prefix}test_{uuid.uuid4().hex[:12]}"


def _make_targets() -> list[dict]:
    return [
        {"label": "Primary Agent", "phone_number": "+15551234567", "priority": 1, "enabled": True},
        {"label": "Secondary Agent", "phone_number": "+15557654321", "priority": 2, "enabled": True},
    ]


def _make_state(call_sid: str, current_target_idx: int = 0) -> TransferState:
    """Build a minimal TransferState with 2 targets."""
    return TransferState(
        call_sid=call_sid,
        owner_id="owner_test",
        workflow_id="appointment_booking",
        conference_name=f"esc_{call_sid[:20]}",
        summary_text="Caller needed specialist assistance.",
        config={
            "proactive_offer_after_reasks": 3,
            "agent_connect_timeout_sec": 30,
            "caller_hold_message": "Please hold.",
            "caller_callback_message": (
                "Our specialist is unavailable right now. Someone will call you back shortly."
            ),
            "voicemail_script": (
                "Hi, this is an automated message. A caller requested a specialist."
            ),
            "targets": _make_targets(),
        },
        targets=_make_targets(),
        current_target_idx=current_target_idx,
    )


def _make_orchestrator(mock_twilio_cls) -> TransferOrchestrator:
    """Build TransferOrchestrator using the mocked TwilioClient class."""
    return TransferOrchestrator(
        account_sid="ACtest123",
        auth_token="authtoken",
        from_number="+15550000000",
        data_plane_base_url="https://data.example.com",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_answer_advances_to_next_target():
    """
    State has 2 targets, current_target_idx=0.
    handle_no_answer() should advance idx to 1 and dial the second target.
    """
    print("\n[no_answer] test_no_answer_advances_to_next_target")

    call_sid = _unique_sid()
    state = _make_state(call_sid, current_target_idx=0)

    asyncio.run(set_transfer(call_sid, state))

    # Mock fake agent call sid returned by Twilio
    mock_agent_call = MagicMock()
    mock_agent_call.sid = "CAagent_second_001"

    with patch("vogon.gateway_service.escalation.transfer.TwilioClient") as MockTwilio:
        mock_client = MockTwilio.return_value
        mock_client.calls.create.return_value = mock_agent_call
        mock_client.calls.return_value.update.return_value = None

        orchestrator = _make_orchestrator(MockTwilio)
        asyncio.run(orchestrator.handle_no_answer(call_sid))

    # State must have advanced to next target
    updated_state = asyncio.run(get_transfer(call_sid))
    assert updated_state is not None, "State should still exist after advancing to next target"
    assert updated_state.current_target_idx == 1, (
        f"Expected current_target_idx=1, got {updated_state.current_target_idx}"
    )
    print(f"  current_target_idx after no-answer: {updated_state.current_target_idx}")

    # Twilio calls.create must have been called once (for second target)
    assert mock_client.calls.create.call_count == 1, (
        f"Expected calls.create called once for second target dial, "
        f"got {mock_client.calls.create.call_count}"
    )
    dial_kwargs = mock_client.calls.create.call_args
    assert dial_kwargs is not None, "calls.create was not called"
    # The 'to' arg should be the second target's phone number
    to_number = dial_kwargs.kwargs.get("to") or (
        dial_kwargs.args[0] if dial_kwargs.args else None
    )
    assert to_number == "+15557654321", (
        f"Expected second target +15557654321, got {to_number!r}"
    )

    print(f"  calls.create called with to={to_number!r}")
    print("  PASS: advanced to second target and dialled")

    # Cleanup
    asyncio.run(remove_transfer(call_sid))


def test_no_answer_fallback_when_all_exhausted():
    """
    State has 2 targets, current_target_idx=1 (last target already tried).
    handle_no_answer() should:
      - Leave voicemail (calls.create for last target)
      - Tell caller someone will call back (calls(call_sid).update)
      - Remove state from store.
    """
    print("\n[no_answer] test_no_answer_fallback_when_all_exhausted")

    call_sid = _unique_sid()
    state = _make_state(call_sid, current_target_idx=1)  # last target already tried

    asyncio.run(set_transfer(call_sid, state))

    mock_voicemail_call = MagicMock()
    mock_voicemail_call.sid = "CAvoicemail001"

    with patch("vogon.gateway_service.escalation.transfer.TwilioClient") as MockTwilio:
        mock_client = MockTwilio.return_value
        mock_client.calls.create.return_value = mock_voicemail_call
        mock_client.calls.return_value.update.return_value = None

        orchestrator = _make_orchestrator(MockTwilio)
        asyncio.run(orchestrator.handle_no_answer(call_sid))

    # State must be removed after fallback
    removed_state = asyncio.run(get_transfer(call_sid))
    assert removed_state is None, (
        f"Expected state removed after fallback, but found: {removed_state}"
    )
    print("  state removed after fallback: OK")

    # calls.create must have been called once (voicemail dial)
    assert mock_client.calls.create.call_count == 1, (
        f"Expected calls.create called once for voicemail dial, "
        f"got {mock_client.calls.create.call_count}"
    )
    print(f"  calls.create (voicemail) call_count={mock_client.calls.create.call_count}")

    # calls(call_sid).update must have been called once (caller callback TwiML)
    assert mock_client.calls.return_value.update.call_count == 1, (
        f"Expected calls(call_sid).update called once for caller callback TwiML, "
        f"got {mock_client.calls.return_value.update.call_count}"
    )
    print(
        f"  calls(call_sid).update (caller callback) "
        f"call_count={mock_client.calls.return_value.update.call_count}"
    )
    print("  PASS: fallback voicemail + caller callback + state removed")


def test_state_operations():
    """
    Direct CRUD test of the in-memory state store:
      set_transfer → stores state
      get_transfer → retrieves it
      update_transfer → mutates in place
      remove_transfer → removes it
      get_transfer after remove → None
    """
    print("\n[no_answer] test_state_operations")

    call_sid = _unique_sid()
    state = _make_state(call_sid, current_target_idx=0)

    # 1. set_transfer stores state
    asyncio.run(set_transfer(call_sid, state))
    print(f"  set_transfer({call_sid!r}) done")

    # 2. get_transfer retrieves it
    retrieved = asyncio.run(get_transfer(call_sid))
    assert retrieved is not None, "get_transfer must return state after set_transfer"
    assert retrieved.call_sid == call_sid, (
        f"Retrieved state.call_sid={retrieved.call_sid!r} != {call_sid!r}"
    )
    assert retrieved.current_target_idx == 0, (
        f"Expected current_target_idx=0, got {retrieved.current_target_idx}"
    )
    print(f"  get_transfer returned: call_sid={retrieved.call_sid!r}  idx={retrieved.current_target_idx}")

    # 3. update_transfer mutates in place
    asyncio.run(update_transfer(call_sid, current_target_idx=1, agent_call_sid="CAagent999"))
    mutated = asyncio.run(get_transfer(call_sid))
    assert mutated is not None, "State should still exist after update"
    assert mutated.current_target_idx == 1, (
        f"Expected current_target_idx=1 after update, got {mutated.current_target_idx}"
    )
    assert mutated.agent_call_sid == "CAagent999", (
        f"Expected agent_call_sid='CAagent999', got {mutated.agent_call_sid!r}"
    )
    print(
        f"  update_transfer: idx={mutated.current_target_idx}  "
        f"agent_call_sid={mutated.agent_call_sid!r}"
    )

    # 4. remove_transfer removes it
    asyncio.run(remove_transfer(call_sid))
    print(f"  remove_transfer({call_sid!r}) done")

    # 5. get_transfer after remove returns None
    gone = asyncio.run(get_transfer(call_sid))
    assert gone is None, (
        f"Expected None after remove_transfer, got {gone}"
    )
    print("  get_transfer after remove: None (correct)")
    print("  PASS: all state store operations verified")
