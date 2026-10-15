"""The gateway must be visible in its own journal.

Two halves, both of which were missing when the service was split out:

  the BOOTSTRAP  basicConfig is a no-op the moment anything has already put
                 a handler on root — a different runner, an embedding app, a
                 stray basicConfig in an import. Root then keeps its WARNING
                 default and every vogon.gateway INFO line disappears with
                 nothing to show it tried. The data-plane sets the level on
                 `app` explicitly for exactly this reason; the gateway
                 shipped without that guard.

  the STATEMENTS routes.py had three log calls, all warnings, all on failure
                 paths. A call that worked start to finish produced NOTHING
                 in `journalctl -u voice-gateway` beyond the startup banner —
                 which is what "I should be able to see logs in the voice
                 gateway" was about.

Deterministic: no sockets, no Twilio, no network.
"""
from __future__ import annotations

import inspect
import logging

import pytest
from fastapi.testclient import TestClient

from vogon.gateway.logging_context import call_id_var
from vogon.gateway_service import routes
from vogon.gateway_service.main import _configure_logging, create_app


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    before = (list(root.handlers), root.level,
              logging.getLogger("app").level)
    yield
    root.handlers, root.level = before[0], before[1]
    logging.getLogger("app").setLevel(before[2])
    call_id_var.set("-")


def _capture() -> tuple[logging.Handler, list[str]]:
    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    h = _Sink()
    h.setFormatter(logging.Formatter(
        "%(levelname)s [%(name)s] [%(call_id)s] %(message)s"))
    return h, lines


def test_info_survives_a_root_handler_installed_before_us():
    """The failure that would hide every gateway line. With a handler already
    on root, basicConfig does nothing — only the explicit level on `app`
    keeps INFO alive."""
    logging.getLogger().addHandler(logging.NullHandler())
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(logging.WARNING)

    _configure_logging("INFO")

    assert logging.getLogger("vogon.gateway.pipeline.tts").isEnabledFor(
        logging.INFO), "vogon.gateway INFO is disabled — the journal stays empty"


def test_the_level_is_configurable():
    _configure_logging("WARNING")
    assert not logging.getLogger("vogon.gateway").isEnabledFor(logging.DEBUG)
    _configure_logging("DEBUG")
    assert logging.getLogger("vogon.gateway").isEnabledFor(logging.DEBUG)


def test_a_bad_level_name_falls_back_to_info_rather_than_crashing():
    _configure_logging("chatty")
    assert logging.getLogger("vogon.gateway").isEnabledFor(logging.INFO)


def test_every_line_carries_a_call_id_slot():
    """The format names %(call_id)s, so a record without the filter raises a
    formatting error and the line is lost. The filter must reach the
    handlers, including uvicorn's."""
    _configure_logging("INFO")
    handler, lines = _capture()
    handler.addFilter(next(iter(
        f for h in logging.root.handlers for f in h.filters)))
    log = logging.getLogger("vogon.gateway.transport.twilio_transport")
    log.addHandler(handler)
    try:
        call_id_var.set("gwc-abc123")
        log.info("TTS sentence stream done | frames=610")
    finally:
        log.removeHandler(handler)
    assert lines and "[gwc-abc123]" in lines[0], lines
    assert "vogon.gateway.transport.twilio_transport" in lines[0], lines


def test_outside_a_call_the_slot_is_filled_not_broken():
    """Startup lines have no call — they must still format."""
    _configure_logging("INFO")
    handler, lines = _capture()
    handler.addFilter(next(iter(
        f for h in logging.root.handlers for f in h.filters)))
    log = logging.getLogger("vogon.gateway_service.main")
    log.addHandler(handler)
    try:
        log.info("voice gateway up")
    finally:
        log.removeHandler(handler)
    assert lines and "[-]" in lines[0], lines


def test_the_call_lifecycle_is_instrumented():
    """A call that WORKS must leave a trail: created, TwiML served, provider
    status, media attached and detached. All five were silent."""


    src = inspect.getsource(routes)
    for marker in ("CALL CREATED", "TwiML served", "Twilio status",
                   "MEDIA ATTACHED", "MEDIA DETACHED"):
        assert marker in src, f"the {marker!r} lifecycle line is gone"


def test_the_lifecycle_routes_bind_the_call_id():
    """Without this the HTTP routes log under '-' and cannot be grepped by
    call — handle_call binds it only for the socket's own duration."""


    for fn in (routes.create_call, routes.twiml, routes.twilio_status,
               routes._drive):
        assert "call_id_var.set" in inspect.getsource(fn), (
            f"{fn.__name__} logs without binding the call id")


# ── the provider's webhook verbs (production, 2026-08-29) ────────────────────
# Twilio defaults `url` to POST. The monolith always pinned method="GET" on
# the dial; the extraction into this service dropped it while keeping the
# route GET-only, so every ANSWERED call got 405 Method Not Allowed and the
# caller heard "an application error has occurred". Nothing in the gateway
# log said so — uvicorn's access line was the only trace.

def test_the_twiml_webhook_accepts_both_verbs():
    """Never depend on a provider's default again: whichever verb Twilio
    sends, the call must get its TwiML."""


    with TestClient(create_app()) as client:
        for verb in ("get", "post"):
            resp = getattr(client, verb)("/v1/twilio/twiml?token=bogus")
            assert resp.status_code == 200, (
                f"{verb.upper()} /v1/twilio/twiml -> {resp.status_code}; "
                "Twilio hears 405 and the caller hears an apology")
            assert "<Response>" in resp.text, resp.text


def test_the_dial_states_the_verb_it_wants():
    """The route accepting both is the belt; this is the braces — the dial
    must not inherit the provider's default."""


    src = inspect.getsource(routes._place_twilio_call)
    assert 'method="GET"' in src, (
        "the dial no longer pins the TwiML verb — Twilio will POST")
    assert 'status_callback_method="POST"' in src, (
        "the status callback verb is unpinned")


def test_a_refused_twiml_is_not_silent():
    """The hangup path must say so: this is the branch that ends a live
    call, and it returned without a word until now."""



    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink()
    log = logging.getLogger("vogon.gateway_service.routes")
    log.addHandler(sink)
    try:
        with TestClient(create_app()) as client:
            client.get("/v1/twilio/twiml?token=nope")
    finally:
        log.removeHandler(sink)
    assert any("REFUSED" in r.getMessage() for r in records), (
        "a call was hung up on with nothing in the journal")
