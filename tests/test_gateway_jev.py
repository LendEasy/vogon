"""Offline tests for the gateway's own Jev client (vogon/gateway/jev.py, vogon step 2 slice 6).

No network: the SDK call is replaced with a fake that returns the wire shapes the endpoint
produced on 2026-09-18. Pinned here is the contract on OUR side — question builders, answer
parsing, the retry policy, and that every call reaches the gateway's own decision tracer —
not Jev's judgement. The data-plane conftest's offline guard resets this module's client too.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from vogon.gateway import jev


def _fake_response(answers: dict) -> SimpleNamespace:
    objs = {}
    for name, a in answers.items():
        if a["type"] == "choice":
            objs[name] = SimpleNamespace(type="choice", choice=a["choice"],
                                         probabilities=a.get("probabilities"),
                                         confidence=a.get("confidence"))
        elif a["type"] == "noul":
            objs[name] = SimpleNamespace(type="noul", noul=a["noul"])
        else:
            objs[name] = SimpleNamespace(type="score", score=a["score"],
                                         confidence=a.get("confidence"))
    return SimpleNamespace(answers=objs, model="typesafe/jev-1.13-20260917",
                           usage=SimpleNamespace(input_tokens=398, output_tokens=55,
                                                 cost=0.000016716),
                           id="gen-dec-test")


class _FakeDecisions:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return _fake_response(self.answers)

    async def create_async(self, **kw):
        self.calls.append(kw)
        return _fake_response(self.answers)


@pytest.fixture
def fake_sdk(monkeypatch):
    def install(answers):
        dec = _FakeDecisions(answers)
        monkeypatch.setattr(jev, "_client", SimpleNamespace(alpha=SimpleNamespace(decisions=dec)))
        return dec
    return install


@pytest.fixture
def traces():
    seen = []
    jev.set_tracer(seen.append)
    try:
        yield seen
    finally:
        jev.set_tracer(None)


def test_question_builders_produce_the_wire_shape():
    assert jev.choice("q", {"a": "A", "b": "B"}) == {
        "type": "choice", "instructions": "q", "criteria": {"a": "A", "b": "B"}}
    assert jev.noul("q", yes="Y", no="N") == {
        "type": "noul", "instructions": "q", "criteria": {"true": "Y", "false": "N"}}
    assert jev.score("q", ["low", "high"]) == {
        "type": "score", "instructions": "q", "criteria": ["low", "high"]}
    with pytest.raises(ValueError):
        jev.choice("q", {"only": "one"})


def test_decide_parses_the_sample_payload(fake_sdk):
    dec = fake_sdk({
        "team": {"type": "choice", "choice": "billing",
                 "probabilities": {"sales": 0, "billing": 1, "technical": 0}, "confidence": 0.99},
        "urgent": {"type": "noul", "noul": 0.12},
        "mood": {"type": "score", "score": 2, "confidence": 0.6}})
    r = jev.decide("state", {"team": jev.choice("?", {"sales": "s", "billing": "b", "technical": "t"}),
                             "urgent": jev.noul("?", yes="y", no="n"),
                             "mood": jev.score("?", ["low", "mid", "high"])}, tag="t")
    assert r.choice("team").choice == "billing" and r.choice("team").ranked()[0] == ("billing", 1)
    assert r.noul("urgent").value() is False and r.score("mood").score == 2
    assert (r.input_tokens, r.output_tokens, r.cost_usd) == (398, 55, 0.000016716)
    assert dec.calls[0]["timeout_ms"] == jev.JEV_TIMEOUT_MS and dec.calls[0]["model"] == jev.JEV_MODEL
    with pytest.raises(jev.JevError):
        r.noul("team")


def test_decide_rejects_an_unknown_question_type(fake_sdk):
    fake_sdk({})
    with pytest.raises(ValueError):
        jev.decide("x", {"q": {"type": "essay", "instructions": "write"}})
    with pytest.raises(ValueError):
        jev.decide("x", {})


class _RateLimited(Exception):
    status_code = 429


class _BadRequest(Exception):
    status_code = 400


def _flaky(monkeypatch, failures, answers=None):
    """A decisions client that raises `failures` in order, then answers."""
    seen, waits = [], []

    class Flaky:
        def create(self, **kw):
            seen.append(kw)
            if failures:
                raise failures.pop(0)
            return _fake_response(answers or {"q": {"type": "noul", "noul": True}})
    monkeypatch.setattr(jev, "_client", SimpleNamespace(alpha=SimpleNamespace(decisions=Flaky())))
    monkeypatch.setattr(jev.time, "sleep", waits.append)
    return seen, waits


def test_a_rate_limited_call_is_retried_with_exponential_backoff(monkeypatch):
    seen, waits = _flaky(monkeypatch, [_RateLimited("HTTP 429"), _RateLimited("HTTP 429")])
    jev.decide("x", {"q": jev.noul("?", yes="y", no="n")})
    assert len(seen) == 3 and waits == [0.125, 0.25]


def test_the_backoff_stops_at_one_second_then_the_error_stands(monkeypatch, traces):
    seen, waits = _flaky(monkeypatch, [_RateLimited("HTTP 429") for _ in range(9)])
    with pytest.raises(jev.JevError):
        jev.decide("x", {"q": jev.noul("?", yes="y", no="n")}, tag="front_move")
    assert waits == [0.125, 0.25, 0.5, 1.0] and len(seen) == 5
    (rec,) = traces                                   # a failure is traced: timed, not metered
    assert rec.tag == "front_move" and rec.error.startswith("_RateLimited") and rec.response == ""
    assert rec.cost_usd is None and rec.input_tokens == 0


def test_a_request_refused_for_what_it_is_is_not_retried(monkeypatch):
    seen, waits = _flaky(monkeypatch, [_BadRequest("HTTP 400: bad question")])
    with pytest.raises(jev.JevError):
        jev.decide("x", {"q": jev.noul("?", yes="y", no="n")})
    assert len(seen) == 1 and waits == []


def test_decide_async_uses_the_async_client_and_traces(fake_sdk, traces):
    """Run with asyncio.run, like the rest of the suite: the tree must not depend on
    an asyncio pytest plugin being installed (without one a marked coroutine test is
    silently skipped with a warning — measured 2026-10-10)."""
    dec = fake_sdk({"recording": {"type": "noul", "noul": 0.92}})
    r = asyncio.run(jev.decide_async({"k": "v"}, {"recording": jev.noul("?", yes="y", no="n")},
                                     tag="line_detector"))
    assert r.noul("recording").value() is True
    assert dec.calls[0]["state"] == {"k": "v"}
    (rec,) = traces
    assert rec.tag == "line_detector" and rec.backend == "jev"
    assert rec.model == "typesafe/jev-1.13-20260917" and rec.cost_usd == 0.000016716
    assert json.loads(rec.response) == {"recording": {"noul": 0.92}}
    assert json.loads(rec.messages[0]["content"]) == {"k": "v"} and "?" in rec.system_prompt


def test_without_a_tracer_a_call_is_simply_not_traced(fake_sdk):
    fake_sdk({"q": {"type": "noul", "noul": 0.5}})
    assert jev.current_tracer() is None
    assert jev.decide("x", {"q": jev.noul("?", yes="y", no="n")}).noul("q").probability == 0.5


def test_a_raising_tracer_never_breaks_the_call(fake_sdk):
    fake_sdk({"q": {"type": "noul", "noul": 0.5}})

    def bad(record):
        raise RuntimeError("sink down")
    jev.set_tracer(bad)
    try:
        assert jev.decide("x", {"q": jev.noul("?", yes="y", no="n")}).noul("q").probability == 0.5
    finally:
        jev.set_tracer(None)


def test_the_api_key_comes_from_settings_then_either_env_spelling(monkeypatch):
    monkeypatch.setattr(jev.settings, "open_router_api_key", "")
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(jev.JevError):
        jev._api_key()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sdk-name")
    assert jev._api_key() == "sdk-name"
    monkeypatch.setattr(jev.settings, "open_router_api_key", "configured")
    assert jev._api_key() == "configured"
