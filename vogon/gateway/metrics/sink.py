"""
MetricsSink — fire-and-forget metric emission for live calls (the gateway's own sink;
vogon step 2, slice 4 — same line format as the application's, so one dashboard reads both).

Format: OpenTelemetry metrics, OTLP/JSON — one ExportMetricsServiceRequest per line
(app/services/otel_metrics.py: names, units, attributes, and the reader's decoding).
Callers emit (metric, workflow_id, value, **tags) under the legacy names; the encoder maps
them to OpenTelemetry names. Before 2026-10-09 the store was one CSV row per event
(ts_ms,metric,workflow_id,value,tags) — the dashboard still reads those files.

Files partitioned by workflow label and UTC hour, one file per writing process (two
processes never append to the same file):
  {storage_base}/metrics/{workflow_id}/{YYYY-MM-DD-HH}.{service}.{pid}.otlp.jsonl

Designed for asyncio — callers call emit() and return immediately.
A single background coroutine drains the queue so there is never
concurrent file access from multiple coroutines.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vogon.gateway.metrics.otel import encode_line

logger = logging.getLogger(__name__)

# data points per OTLP line: a line stays small enough for any line-oriented reader
MAX_POINTS_PER_LINE = 500


class MetricsSink:
    def __init__(self, storage_base: str = "./storage"):
        self._base = Path(storage_base) / "metrics"
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def start(self, storage_base: str | None = None, service: str = "") -> None:
        """Start the background writer under ``{storage_base}/metrics``. Must be called from an
        asyncio context. Every process that emits must start it: emit() on a sink that was never
        started drops the row (the standalone gateway never did, and every gateway metric was
        lost in production). Starting twice in one process (the harnesses run the turn service
        and the gateway together) keeps the first writer."""
        if self._task is not None and not self._task.done():
            return
        if storage_base is not None:
            self._base = Path(storage_base) / "metrics"
        self.service = service or "voice-gateway"
        self._queue = asyncio.Queue(maxsize=8000)
        self._task = asyncio.create_task(self._writer(), name="metrics-writer")
        logger.info("MetricsSink started (base=%s service=%s)", self._base, self.service)

    async def stop(self) -> None:
        """Drain remaining rows and stop the writer. Call during lifespan shutdown."""
        if self._queue is not None:
            await self._queue.put(None)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()

    # ── Public emit interface ─────────────────────────────────────────────────

    def emit(self, metric: str, workflow_id: str, value: float, **tags: str) -> None:
        """Put a single metric row on the queue. Non-blocking; safe to call from any coroutine."""
        if self._queue is None:
            return
        row = (int(time.time() * 1000), metric, workflow_id, value, tags)
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("MetricsSink queue full — dropping metric %s for %s", metric, workflow_id)

    # ── Typed emit helpers (one helper = one logical event) ───────────────────

    def emit_response_latency(self, workflow_id: str, agent_id: str, instance_id: str, value_ms: float) -> None:
        self.emit("response_latency_ms", workflow_id, value_ms, agent_id=agent_id, instance_id=instance_id)

    def emit_stt_to_planner(self, workflow_id: str, instance_id: str, value_ms: float) -> None:
        self.emit("stt_to_planner_ms", workflow_id, value_ms, instance_id=instance_id)

    def emit_planner_to_agent(self, workflow_id: str, agent_id: str, instance_id: str, value_ms: float) -> None:
        self.emit("planner_to_agent_ms", workflow_id, value_ms, agent_id=agent_id, instance_id=instance_id)

    def emit_agent_handle(self, workflow_id: str, agent_id: str, instance_id: str, value_ms: float, chain_depth: int = 0) -> None:
        self.emit("agent_handle_ms", workflow_id, value_ms, agent_id=agent_id, instance_id=instance_id, chain_depth=str(chain_depth))

    def emit_llm_call(
        self,
        workflow_id: str,
        agent_id: str,
        call_type: str,
        instance_id: str,
        latency_ms: float,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
        pricing: str = "rate",
        extra_tags: dict | None = None,
    ) -> None:
        # pricing: "rate" — the tokens are Cerebras tokens and the dashboard
        # re-prices them at the published rates; "api" — the provider
        # reported the cost itself (a Jev decisions call) and the stored
        # figure is the truth: its tokens are not Cerebras tokens.
        # extra_tags: what the call was doing (task, task_kind, decider, model, status —
        # app/services/decision_metrics.py)
        tags = dict(agent_id=agent_id, call_type=call_type, instance_id=instance_id,
                    pricing=pricing, **(extra_tags or {}))
        self.emit("llm_latency_ms",    workflow_id, latency_ms,            **tags)
        self.emit("llm_input_tokens",  workflow_id, float(input_tokens),   **tags)
        self.emit("llm_output_tokens", workflow_id, float(output_tokens),  **tags)
        self.emit("llm_cached_tokens", workflow_id, float(cached_tokens),  **tags)
        self.emit("llm_cost_usd",      workflow_id, cost_usd,              **tags)

    def emit_llm_failure(self, workflow_id: str, latency_ms: float, **tags) -> None:
        """A model call that failed for good: timed (the turn waited), not metered — it has no
        tokens to price. Tagged like a successful call, with status=error."""
        self.emit("llm_latency_ms", workflow_id, latency_ms, **tags)

    def emit_decision(self, workflow_id: str, latency_ms: float, **tags) -> None:
        """One decision: how long the turn waited for it (decision, decider, status, result)."""
        self.emit("decision_latency_ms", workflow_id, latency_ms, **tags)

    def emit_decision_outcome(self, workflow_id: str, **tags) -> None:
        """One answered question of a decision (decision, decider, question, answer)."""
        self.emit("decision_outcome", workflow_id, 1.0, **tags)

    def emit_tts_stream(self, workflow_id: str, instance_id: str, first_chunk_ms: float, duration_ms: float) -> None:
        self.emit("tts_first_chunk_ms",    workflow_id, first_chunk_ms, instance_id=instance_id)
        self.emit("tts_stream_duration_ms", workflow_id, duration_ms,   instance_id=instance_id)

    def emit_call_completed(
        self,
        workflow_id: str,
        instance_id: str,
        end_reason: str,
        duration_sec: float,
        turn_count: int,
    ) -> None:
        base = dict(end_reason=end_reason, instance_id=instance_id)
        self.emit("call_completed",    workflow_id, 1.0,               **base)
        self.emit("call_duration_sec", workflow_id, duration_sec,      **base)
        self.emit("call_turn_count",   workflow_id, float(turn_count), instance_id=instance_id)

    def emit_agent_llm_rollup(
        self,
        workflow_id: str,
        agent_id: str,
        instance_id: str,
        calls_total: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        base = dict(agent_id=agent_id, instance_id=instance_id)
        self.emit("llm_calls_total",         workflow_id, float(calls_total),   **base)
        self.emit("llm_tokens_total_input",  workflow_id, float(input_tokens),  **base)
        self.emit("llm_tokens_total_output", workflow_id, float(output_tokens), **base)
        self.emit("llm_cost_total_usd",      workflow_id, cost_usd,             **base)

    def emit_error(self, workflow_id: str, instance_id: str, error_type: str, count: int = 1) -> None:
        self.emit("error_count", workflow_id, float(count), error_type=error_type, instance_id=instance_id)

    def emit_outcome(self, workflow_id: str, instance_id: str, outcome: str, metric: str = "call_outcome") -> None:
        self.emit(metric, workflow_id, 1.0, outcome=outcome, instance_id=instance_id)

    def emit_qualification_outcome(self, workflow_id: str, instance_id: str, outcome: str) -> None:
        self.emit("qualification_outcome", workflow_id, 1.0, outcome=outcome, instance_id=instance_id)

    def emit_payment_outcome(self, workflow_id: str, instance_id: str, outcome: str) -> None:
        self.emit("payment_outcome", workflow_id, 1.0, outcome=outcome, instance_id=instance_id)

    def emit_field_reask(self, workflow_id: str, instance_id: str, field_name: str, count: int) -> None:
        self.emit("field_reask_count", workflow_id, float(count), field_name=field_name, instance_id=instance_id)

    # ── Background writer ─────────────────────────────────────────────────────

    async def _writer(self) -> None:
        batch: list = []
        while True:
            try:
                row = await asyncio.wait_for(self._queue.get(), timeout=2.0)
                if row is None:
                    break
                batch.append(row)
                # Drain additional queued rows — up to 200 per flush
                while not self._queue.empty() and len(batch) < 200:
                    extra = self._queue.get_nowait()
                    if extra is None:
                        batch.append(None)
                        break
                    batch.append(extra)
                real_rows = [r for r in batch if r is not None]
                stopping = len(real_rows) != len(batch)
                if real_rows:
                    await asyncio.to_thread(self._flush_batch, real_rows)
                batch.clear()
                if stopping:
                    break
            except asyncio.TimeoutError:
                if batch:
                    await asyncio.to_thread(self._flush_batch, batch)
                    batch.clear()
            except Exception as exc:
                logger.error("MetricsSink writer error: %s", exc, exc_info=True)
                batch.clear()

    def _flush_batch(self, rows: list) -> None:
        """Append a batch to the hour's OTLP/JSON file for each workflow label. Runs in a thread."""
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for ts_ms, metric, workflow_id, value, tags in rows:
            hour_key = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d-%H")
            groups[(workflow_id, hour_key)].append((ts_ms, metric, workflow_id, value, tags))
        service = getattr(self, "service", "") or "voice-gateway"
        for (workflow_id, hour_key), group_rows in groups.items():
            path = self._base / workflow_id / f"{hour_key}.{service}.{os.getpid()}.otlp.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for start in range(0, len(group_rows), MAX_POINTS_PER_LINE):
                    handle.write(encode_line(group_rows[start:start + MAX_POINTS_PER_LINE], service=service) + "\n")


# Global singleton — imported by handler, transport, and agent instrumentation.
metrics_sink = MetricsSink()
