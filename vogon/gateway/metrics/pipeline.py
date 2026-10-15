"""Shared helpers for per-turn pipeline latency metrics.

Metrics inversion (docs/voice-gateway-phase0.md §7): every helper takes plain
monotonic timestamps. The handler owns the reasoning-side anchors (utterance
anchor, agent-ready time) and reads playout timings from the DeliveryReport —
these functions never touch the transport.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vogon.gateway.metrics.sink import MetricsSink


def emit_stt_to_planner(
    sink: MetricsSink,
    workflow_id: str,
    instance_id: str,
    anchor_t: float,
    planner_done_t: float,
) -> None:
    if anchor_t > 0:
        sink.emit_stt_to_planner(
            workflow_id, instance_id, (planner_done_t - anchor_t) * 1000,
        )


def emit_planner_to_agent(
    sink: MetricsSink,
    workflow_id: str,
    agent_id: str,
    instance_id: str,
    planner_done_t: float,
) -> None:
    sink.emit_planner_to_agent(
        workflow_id, agent_id, instance_id, (time.monotonic() - planner_done_t) * 1000,
    )


def emit_agent_handle(
    sink: MetricsSink,
    workflow_id: str,
    agent_id: str,
    instance_id: str,
    elapsed_ms: float,
    chain_depth: int,
) -> None:
    sink.emit_agent_handle(workflow_id, agent_id, instance_id, elapsed_ms, chain_depth)


def emit_response_latency(
    sink: MetricsSink,
    workflow_id: str,
    agent_id: str,
    instance_id: str,
    anchor_t: float,
    first_chunk_t: float,
) -> None:
    if anchor_t <= 0 or first_chunk_t <= 0:
        return
    sink.emit_response_latency(
        workflow_id, agent_id or "unknown", instance_id,
        (first_chunk_t - anchor_t) * 1000,
    )
