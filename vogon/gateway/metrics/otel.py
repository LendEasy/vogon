"""Metrics in the OpenTelemetry format: OTLP/JSON, one ExportMetricsServiceRequest per line.

The store is files (``{base}/metrics/{label}/{YYYY-MM-DD-HH}.{service}.{pid}.otlp.jsonl``) so
any standard reader can use them: the OpenTelemetry Collector's ``otlpjsonfile`` receiver reads
them as written (and forwards to Prometheus, Grafana, Datadog, CloudWatch, …), they can be copied
to S3 as they are (the Collector's S3 exporter writes the same format), and each line is a
self-describing JSON object for jq, DuckDB or Athena.

Encoding (OTLP/JSON = proto3 JSON mapping): lowerCamelCase keys; 64-bit integers
(``timeUnixNano``, ``count``, ``bucketCounts``) as strings; enums as integers. Every data point
is one observation with DELTA temporality (``aggregationTemporality: 1``) — a Collector
forwarding to Prometheus needs a delta-to-cumulative step.

Names follow the OpenTelemetry conventions: durations are histograms in SECONDS (model calls
under the GenAI semantic-convention names ``gen_ai.client.operation.duration`` /
``gen_ai.client.token.usage``), counts are monotonic sums, everything else of ours sits under
``<metrics_namespace>.`` (``vogon.`` by default). Callers keep the legacy emit names (``llm_latency_ms`` …); this module maps them.
``decode_line`` maps back, so the dashboard reads the new files and the legacy CSV as the same
rows.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from vogon.gateway.settings import settings

_PREFIX = f"{settings.metrics_namespace}."
SCOPE = {"name": _PREFIX + "metrics", "version": "1"}
# OpenTelemetry's default explicit bounds for a duration histogram in seconds
DURATION_BOUNDS_SECONDS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]
HISTOGRAM, SUM, GAUGE = "histogram", "sum", "gauge"
DELTA = 1                                      # AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA


@dataclass(frozen=True)
class MetricSpec:
    """How one legacy emit name is written: the OpenTelemetry name, unit and instrument; a
    multiplier from the emitted value to that unit; attributes that distinguish it from other
    legacy names written under the same OpenTelemetry name."""
    name: str
    unit: str
    kind: str
    scale: float = 1.0
    attributes: dict = field(default_factory=dict)


_MS = 0.001
METRICS: dict[str, MetricSpec] = {
    # latency (milliseconds emitted, seconds stored)
    "response_latency_ms": MetricSpec(_PREFIX + "response.duration", "s", HISTOGRAM, _MS),
    "tts_first_chunk_ms": MetricSpec(_PREFIX + "tts.first_chunk.duration", "s", HISTOGRAM, _MS),
    "tts_stream_duration_ms": MetricSpec(_PREFIX + "tts.stream.duration", "s", HISTOGRAM, _MS),
    "front_first_chunk_ms": MetricSpec(_PREFIX + "front.first_chunk.duration", "s", HISTOGRAM, _MS),
    "stt_to_planner_ms": MetricSpec(_PREFIX + "pipeline.stt_to_planner.duration", "s", HISTOGRAM, _MS),
    "planner_to_agent_ms": MetricSpec(_PREFIX + "pipeline.planner_to_agent.duration", "s", HISTOGRAM, _MS),
    "agent_handle_ms": MetricSpec(_PREFIX + "agent.handle.duration", "s", HISTOGRAM, _MS),
    "decision_latency_ms": MetricSpec(_PREFIX + "decision.duration", "s", HISTOGRAM, _MS),
    # model calls (GenAI semantic conventions where one exists)
    "llm_latency_ms": MetricSpec("gen_ai.client.operation.duration", "s", HISTOGRAM, _MS),
    "llm_input_tokens": MetricSpec("gen_ai.client.token.usage", "{token}", HISTOGRAM,
                                   attributes={"gen_ai.token.type": "input"}),
    "llm_output_tokens": MetricSpec("gen_ai.client.token.usage", "{token}", HISTOGRAM,
                                    attributes={"gen_ai.token.type": "output"}),
    "llm_cached_tokens": MetricSpec(_PREFIX + "gen_ai.client.token.cached", "{token}", HISTOGRAM),
    "llm_cost_usd": MetricSpec(_PREFIX + "gen_ai.client.cost", "{USD}", SUM),
    "llm_calls_total": MetricSpec(_PREFIX + "gen_ai.client.calls", "{call}", SUM),
    "llm_tokens_total_input": MetricSpec(_PREFIX + "gen_ai.client.tokens", "{token}", SUM,
                                         attributes={"gen_ai.token.type": "input"}),
    "llm_tokens_total_output": MetricSpec(_PREFIX + "gen_ai.client.tokens", "{token}", SUM,
                                          attributes={"gen_ai.token.type": "output"}),
    "llm_cost_total_usd": MetricSpec(_PREFIX + "gen_ai.client.cost.total", "{USD}", SUM),
    # decisions
    "decision_outcome": MetricSpec(_PREFIX + "decision.outcomes", "{answer}", SUM),
    # calls
    "call_completed": MetricSpec(_PREFIX + "call.completed", "{call}", SUM),
    "call_duration_sec": MetricSpec(_PREFIX + "call.duration", "s", HISTOGRAM),
    "call_turn_count": MetricSpec(_PREFIX + "call.turns", "{turn}", HISTOGRAM),
    "error_count": MetricSpec(_PREFIX + "errors", "{error}", SUM),
    "call_outcome": MetricSpec(_PREFIX + "call.outcome", "{call}", SUM),
    "qualification_outcome": MetricSpec(_PREFIX + "qualification.outcome", "{call}", SUM),
    "payment_outcome": MetricSpec(_PREFIX + "payment.outcome", "{call}", SUM),
    "field_reask_count": MetricSpec(_PREFIX + "field.reasks", "{reask}", SUM),
    "barge_in_event": MetricSpec(_PREFIX + "barge_in.rms", "1", HISTOGRAM),
}

# legacy tag key -> OpenTelemetry attribute key (semantic conventions where one exists)
ATTRIBUTES: dict[str, str] = {
    "instance_id": _PREFIX + "call.id",
    "agent_id": _PREFIX + "agent.id",
    "call_type": _PREFIX + "llm.tag",
    "pricing": _PREFIX + "gen_ai.pricing",
    "end_reason": _PREFIX + "call.end_reason",
    "outcome": _PREFIX + "outcome",
    "error_type": "error.type",
    "field_name": _PREFIX + "field.name",
    "chain_depth": _PREFIX + "chain.depth",
    "model": "gen_ai.request.model",
    "system": "gen_ai.system",
    "decision": _PREFIX + "decision",
    "decider": _PREFIX + "decider",
    "status": _PREFIX + "status",
    "result": _PREFIX + "decision.result",
    "question": _PREFIX + "decision.question",
    "answer": _PREFIX + "decision.answer",
    "task": _PREFIX + "task",
    "task_kind": _PREFIX + "task.kind",
}
WORKFLOW_ATTRIBUTE = _PREFIX + "workflow.id"
_REVERSE_ATTRIBUTES = {otel: legacy for legacy, otel in ATTRIBUTES.items()}


def spec_for(metric: str) -> MetricSpec:
    """The spec for a legacy emit name; an unknown name is a gauge under the namespace."""
    return METRICS.get(metric) or MetricSpec(_PREFIX + metric, "1", GAUGE)


def _legacy_metric(name: str, attributes: dict) -> str:
    candidates = [legacy for legacy, spec in METRICS.items() if spec.name == name]
    for legacy in candidates:
        if all(attributes.get(key) == value for key, value in METRICS[legacy].attributes.items()):
            return legacy
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else name


def _attribute(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attribute_value(value: dict):
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    return None


def _data_point(spec: MetricSpec, ts_ms: int, value: float, attributes: list[dict]) -> dict:
    nanos = str(int(ts_ms) * 1_000_000)
    point: dict = {"attributes": attributes, "startTimeUnixNano": nanos, "timeUnixNano": nanos}
    scaled = float(value) * spec.scale
    if spec.kind == HISTOGRAM:
        bounds = DURATION_BOUNDS_SECONDS if spec.unit == "s" else []
        buckets = ["0"] * (len(bounds) + 1)
        buckets[next((index for index, bound in enumerate(bounds) if scaled <= bound), len(bounds))] = "1"
        point.update(count="1", sum=scaled, min=scaled, max=scaled, explicitBounds=bounds, bucketCounts=buckets)
    else:
        point["asDouble"] = scaled
    return point


def encode_request(rows: list[tuple], *, service: str) -> dict:
    """One ExportMetricsServiceRequest for ``rows`` — (ts_ms, metric, workflow_id, value, tags)
    as MetricsSink queues them — grouped by OpenTelemetry metric name."""
    metrics: dict[tuple, dict] = {}
    for ts_ms, metric, workflow_id, value, tags in rows:
        spec = spec_for(metric)
        attributes = [_attribute(WORKFLOW_ATTRIBUTE, workflow_id)]
        attributes += [_attribute(key, value_) for key, value_ in spec.attributes.items()]
        attributes += [_attribute(ATTRIBUTES.get(key, _PREFIX + key), value_) for key, value_ in (tags or {}).items()
                       if value_ is not None]
        entry = metrics.setdefault((spec.name, spec.unit, spec.kind), {"name": spec.name, "unit": spec.unit})
        if spec.kind == HISTOGRAM:
            body = entry.setdefault("histogram", {"dataPoints": [], "aggregationTemporality": DELTA})
        elif spec.kind == SUM:
            body = entry.setdefault("sum", {"dataPoints": [], "aggregationTemporality": DELTA, "isMonotonic": True})
        else:
            body = entry.setdefault("gauge", {"dataPoints": []})
        body["dataPoints"].append(_data_point(spec, ts_ms, value, attributes))
    resource = {"attributes": [_attribute("service.name", service), _attribute("service.namespace", settings.metrics_namespace),
                               _attribute("process.pid", os.getpid())]}
    return {"resourceMetrics": [{"resource": resource,
                                 "scopeMetrics": [{"scope": dict(SCOPE), "metrics": list(metrics.values())}]}]}


def encode_line(rows: list[tuple], *, service: str) -> str:
    return json.dumps(encode_request(rows, service=service), separators=(",", ":"))


def decode_line(line: str) -> list[dict]:
    """The rows of one OTLP/JSON line, in the dashboard's row shape: ts_ms, metric (the legacy
    name), workflow_id, value (in the legacy unit) and the legacy tag keys. Any OTLP metrics
    line is read — names and attributes this module does not know keep their own names."""
    request = json.loads(line)
    rows: list[dict] = []
    for resource_metrics in request.get("resourceMetrics", []):
        for scope_metrics in resource_metrics.get("scopeMetrics", []):
            for metric in scope_metrics.get("metrics", []):
                kind = next((candidate for candidate in (HISTOGRAM, SUM, GAUGE) if candidate in metric), None)
                if kind is None:
                    continue
                for point in metric[kind].get("dataPoints", []):
                    attributes = {item["key"]: _attribute_value(item.get("value", {})) for item in point.get("attributes", [])}
                    legacy = _legacy_metric(metric["name"], attributes)
                    spec = METRICS.get(legacy)
                    if kind == HISTOGRAM:
                        count = int(point.get("count", "0") or 0)
                        value = float(point.get("sum", 0.0)) / count if count else 0.0
                    else:
                        value = float(point.get("asDouble", point.get("asInt", 0)))
                    if spec is not None and spec.scale:
                        value = value / spec.scale
                    for key in (spec.attributes if spec is not None else {}):
                        attributes.pop(key, None)
                    workflow_id = str(attributes.pop(WORKFLOW_ATTRIBUTE, "") or "")
                    tags = {_REVERSE_ATTRIBUTES.get(key, key[len(_PREFIX):] if key.startswith(_PREFIX) else key): value_
                            for key, value_ in attributes.items()}
                    rows.append({"ts_ms": int(point.get("timeUnixNano", "0")) // 1_000_000, "metric": legacy,
                                 "workflow_id": workflow_id, "value": value, **tags})
    return rows
