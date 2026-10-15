"""The gateway's own model calls reach the metrics as decisions (2026-10-09).

The front model's move and the line detector's recording question are Jev calls the gateway
makes itself (vogon.gateway.jev). In the standalone gateway no tracer was set, so they were
measured nowhere; the handler now installs a per-call tracer that emits their decision metrics
under the call's metrics label. A record of a tag the handler does not declare is left alone —
the turn service traces its own calls through its own tracer.
"""
from __future__ import annotations

import asyncio
import json

from vogon.gateway.jev import DecisionTrace
from vogon.gateway.metrics.sink import MetricsSink
from vogon.websocket import handler


def _record(tag, response):
    return DecisionTrace(tag=tag, system_prompt="", messages=[], response=response, model="typesafe/jev",
                         latency_ms=180.0, input_tokens=10, output_tokens=5, cost_usd=0.00009)


def test_the_gateways_decisions_are_emitted_and_other_tags_are_ignored(monkeypatch):
    emitted = []

    class RecordingSink(MetricsSink):
        def emit(self, metric, workflow_id, value, **tags):
            emitted.append((metric, workflow_id, tags))
    monkeypatch.setattr(handler, "metrics_sink", RecordingSink())

    async def scenario():
        trace = handler._gateway_decision_tracer("call-1", "collections-demo/collections_ladders")
        trace(_record("front_move", json.dumps({"move": {"choice": "ASSENT"}})))
        trace(_record("line_detector", json.dumps({"recording": {"noul": 0.92}})))
        trace(_record("jev_planner", "{}"))                   # not the gateway's: the turn side's
        await asyncio.sleep(0)                                # the emits are scheduled on the loop
    asyncio.run(scenario())
    decisions = {tags["decision"]: tags for metric, _label, tags in emitted if metric == "decision_latency_ms"}
    assert decisions["front.move"]["result"] == "ASSENT" and decisions["line.recording"]["result"] == "yes"
    assert set(decisions) == {"front.move", "line.recording"}
    assert {label for _metric, label, _tags in emitted} == {"collections-demo/collections_ladders"}
