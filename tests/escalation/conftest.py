"""Warm-transfer escalation tests: the gateway's Twilio mechanics, driven by
a recording fake of the Twilio REST client and by the callback bodies
Twilio would hit, in the order Twilio would hit them."""
# ruff: noqa: E402 — its imports follow the sys.path setup
from types import SimpleNamespace

import pytest

from vogon.gateway_service.escalation import report
from vogon.gateway_service.escalation import state as esc_state


class FakeCallContext:
    """`client.calls(sid)` — records every update against that call."""

    def __init__(self, twilio, sid):
        self._twilio, self._sid = twilio, sid

    def update(self, **kwargs):
        self._twilio.updates.append((self._sid, kwargs))
        err = self._twilio.update_errors.get(self._sid)
        if err is not None:
            raise err
        return SimpleNamespace(sid=self._sid)


class FakeCalls:
    def __init__(self, twilio):
        self._twilio = twilio

    def __call__(self, sid):
        return FakeCallContext(self._twilio, sid)

    def create(self, **kwargs):
        self._twilio.creates.append(kwargs)
        err = self._twilio.create_errors.get(kwargs.get("to"))
        if err is not None:
            raise err
        self._twilio.leg_counter += 1
        return SimpleNamespace(sid=f"CAleg{self._twilio.leg_counter}")


class FakeTwilio:
    """Stands in for twilio.rest.Client. `create_errors` maps a dialled
    number to the exception the REST call raises; `update_errors` maps a
    call sid to the exception its update raises."""

    def __init__(self):
        self.creates: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.create_errors: dict = {}
        self.update_errors: dict = {}
        self.leg_counter = 0
        self.calls = FakeCalls(self)


@pytest.fixture()
def twilio(monkeypatch):
    fake = FakeTwilio()
    monkeypatch.setattr(
        "vogon.gateway_service.escalation.transfer.TwilioClient",
        lambda *a, **kw: fake)
    return fake


class FakeRender:
    """Stands in for the Deepgram REST render — no network in tests.
    `fail` names lines whose render raises."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    async def __call__(self, text, voice):
        self.calls.append((text, voice))
        if any(text.startswith(f) for f in self.fail):
            raise RuntimeError("render down")
        return b"ID3" + text.encode()[:16]


@pytest.fixture(autouse=True)
def fake_render(monkeypatch):
    fake = FakeRender()
    monkeypatch.setattr("vogon.gateway_service.escalation.voice._render_one", fake)
    return fake


class RecordingReporter:
    """Captures post-call facts instead of POSTing them."""

    def __init__(self):
        self.facts: list = []
        self.targets: list = []

    def send(self, state, fact):
        self.facts.append(fact)
        self.targets.append((state.report_url, state.report_token, state.call_id))

    def kinds(self):
        return [getattr(f, "event", None) or f"resolved:{f.result}"
                for f in self.facts]


@pytest.fixture(autouse=True)
def reported():
    before = report.reporter()
    rec = RecordingReporter()
    report.use_reporter(rec)
    yield rec
    report.use_reporter(before)


@pytest.fixture(autouse=True)
def fresh_transfer_store():
    """Every test starts with an empty in-process transfer store — and leaves
    one behind: the store is a module global, and a Redis store installed by
    the last test in this directory leaked into the data plane's escalation
    seam test (2026-10-10)."""
    esc_state.use_transfer_store(esc_state.MemoryTransferStore())
    yield
    esc_state.use_transfer_store(esc_state.MemoryTransferStore())
