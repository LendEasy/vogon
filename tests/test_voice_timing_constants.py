"""voice-gateway/docs/voice-timing-constants.md is checked against the code, both ways.

A reference table that nobody verifies is a snapshot that starts rotting the day
it is written, and the values in this one were each bought with a production
incident. So:

  * every value the doc states must equal the value in the code;
  * every constant this test knows about must appear in the doc (add a row);
  * every row in the doc must be one this test knows how to resolve (add it
    here, or the row is decoration).

The `websockets` rows are the point of the exercise. `max_queue=32` and
ping_interval/ping_timeout=20 are THIRD-PARTY defaults we now depend on: a
consumer slower than the producer fills that queue, the reader parks, PONGs stop
being read from inside that same parked loop, and the client kills its own
healthy socket at interval + timeout. That is call 18b3f7a3 exactly. An upgrade
that changes either number silently re-arms it, so they are asserted from the
installed package rather than described in prose.
"""
from __future__ import annotations

import functools
import inspect
import re
from pathlib import Path

import pytest
from deepgram.speak.v1 import client as speak_client
from websockets.legacy.client import connect

from vogon.gateway.pipeline import tts
from vogon.gateway.settings import GatewaySettings
from vogon.gateway.transport import clocks, delivery
from vogon.gateway.transport import twilio_transport as tt
from vogon.gateway_service.escalation import callbacks as esc_callbacks
from vogon.gateway_service.escalation import report as esc_report
from vogon.gateway_service.escalation import state as esc_state
from vogon.gateway_service.escalation import transfer as esc_transfer
from vogon.gateway_service.escalation import voice as esc_voice
from vogon.websocket import handler as driver

_DOC = Path(__file__).resolve().parents[1] / "docs" / "voice-timing-constants.md"

# A row is `| `NAME` | `VALUE` | `FILE` | ... |`. All three are code-quoted in
# the doc, which is what makes the table parseable without a markdown library.
# The FILE column says which side owns the constant: rows under `data-plane/`
# are the application's and are checked by its own copy of this test
# (data-plane/tests/test_voice_timing_constants.py); every other row is ours.
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`")
_APPLICATION = "data-plane/"


def _rows() -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line.strip())
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def _documented() -> dict[str, str]:
    return {name: value for name, (value, _file) in _rows().items()}


def _ours() -> dict[str, str]:
    return {name: value for name, (value, file) in _rows().items()
            if not file.startswith(_APPLICATION)}


def _settings_default(field: str):
    return GatewaySettings.model_fields[field].default


def _ws_connect_default(param: str):
    return inspect.signature(connect.__init__).parameters[param].default


def _actual() -> dict[str, object]:
    """name -> the live value. Adding a constant to the doc means adding it
    here; a row this cannot resolve fails the test rather than passing
    silently."""

    return {
        # 1. how long a playout can be believed to last
        "PLAYOUT_STUCK_CAP_SEC": clocks.PLAYOUT_STUCK_CAP_SEC,
        "PLAYOUT_MARK_MARGIN_SEC": clocks.PLAYOUT_MARK_MARGIN_SEC,
        # 2. what counts as the caller's silence (the ladder's rungs are the application's)
        "SILENCE_PAUSE_CAP_SEC": clocks.SILENCE_PAUSE_CAP_SEC,
        # 3. what counts as heard
        "MS_PER_WORD_ESTIMATE": delivery.MS_PER_WORD_ESTIMATE,
        "tts_buffer_lead_ms": _settings_default("tts_buffer_lead_ms"),
        # 4. how many times we try again — the application's rows
        # 5. the TTS socket
        "_CANCEL_DRAIN_TIMEOUT": tts._CANCEL_DRAIN_TIMEOUT,
        "_DRAIN_WINDOW_MAX_SEC": tt._DRAIN_WINDOW_MAX_SEC,
        "_TTS_FRAME_GAP_WARN_MS": tt._TTS_FRAME_GAP_WARN_MS,
        "_SEND_QUEUE_DEPTH_WARN": tt._SEND_QUEUE_DEPTH_WARN,
        # ...and the borrowed defaults it depends on
        "websockets.max_queue": _ws_connect_default("max_queue"),
        "websockets.ping_interval": _ws_connect_default("ping_interval"),
        "websockets.ping_timeout": _ws_connect_default("ping_timeout"),
        # 6. where the pause cap's worst case comes from
        "turn_service_timeout_sec": _settings_default("turn_service_timeout_sec"),
        "turn_retry_delays": _settings_default("turn_retry_delays"),
        # 7. the session policy
        "max_call_duration_sec": _settings_default("max_call_duration_sec"),
        # 8. the front model
        "front_beat_after_ms": _settings_default("front_beat_after_ms"),
        "front_beat_after_empty_ms": _settings_default("front_beat_after_empty_ms"),
        "front_wait_again_after_ms": _settings_default("front_wait_again_after_ms"),
        "front_min_gap_sec": _settings_default("front_min_gap_sec"),
        # 9. the warm transfer
        "_HANDOFF_HEARD_CAP_SEC": driver._HANDOFF_HEARD_CAP_SEC,
        "_ESCALATION_TEARDOWN_CAP_SEC": driver._ESCALATION_TEARDOWN_CAP_SEC,
        "_FINALIZE_TEARDOWN_CAP_SEC": driver._FINALIZE_TEARDOWN_CAP_SEC,
        "_TWILIO_REST_TIMEOUT_SEC": esc_transfer._TWILIO_REST_TIMEOUT_SEC,
        "TRANSFER_TTL_SEC": esc_state.TRANSFER_TTL_SEC,
        "KEPT_AUDIO_TTL_SEC": esc_state.KEPT_AUDIO_TTL_SEC,
        "HOLD_RENDER_WAIT_SEC": esc_voice.HOLD_RENDER_WAIT_SEC,
        "_LINE_RENDER_TIMEOUT_SEC": esc_voice._LINE_RENDER_TIMEOUT_SEC,
        "_WHISPER_AUDIO_WAIT_SEC": esc_callbacks._WHISPER_AUDIO_WAIT_SEC,
        "_POST_CALL_RETRY_DELAYS_SEC": esc_report._POST_CALL_RETRY_DELAYS_SEC,
        "_POST_CALL_HTTP_TIMEOUT_SEC": esc_report._POST_CALL_HTTP_TIMEOUT_SEC,
    }


def test_the_doc_exists_where_the_code_comments_say_it_does():
    assert _DOC.exists(), f"missing {_DOC}"


@pytest.mark.parametrize("name", sorted(_actual()))
def test_each_constant_matches_the_doc(name):
    documented = _documented()
    assert name in documented, (
        f"{name} is not in voice-timing-constants.md — add a row, or the next "
        f"person changes it with nothing to check against")
    assert str(_actual()[name]) == documented[name], (
        f"{name} is {_actual()[name]!r} in the code and {documented[name]!r} in "
        f"voice-timing-constants.md. These values are bought with production "
        f"incidents: change the doc in the same commit as the constant, and say "
        f"what moved the judgment")


def test_the_doc_documents_nothing_it_cannot_check():
    """Every row that is not the application's (its FILE column does not
    start with data-plane/) must be one this test resolves."""
    unknown = sorted(set(_ours()) - set(_actual()))
    assert not unknown, (
        f"voice-timing-constants.md lists {unknown} but this test cannot "
        f"resolve them, so those rows are unchecked decoration. Add them to "
        f"_actual(), or mark the FILE column data-plane/ if they are the "
        f"application's.")


def test_every_gateway_constant_is_filed_under_the_gateway():
    """The split rests on the FILE column: a row this test resolves must not
    claim to be the application's."""
    mislabelled = sorted(set(_actual()) - set(_ours()))
    assert not mislabelled, f"rows resolved here but filed under data-plane/: {mislabelled}"


def test_the_speak_socket_is_still_unbounded_against_that_default():
    """The row that ties §5 together: the library default is what the patch
    exists to override. If websockets ever ships max_queue=None by default this
    test says so, and the patch can go."""



    assert _ws_connect_default("max_queue") is not None, (
        "websockets now defaults max_queue to None — the speak-socket patch in "
        "tts.py is redundant and the doc's §5 needs rewriting")
    fn = speak_client.websockets_client_connect
    assert isinstance(fn, functools.partial) and fn.keywords.get("max_queue") is None, (
        "the speak socket is not opening with an unbounded receive queue")
