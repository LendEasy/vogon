"""The standalone gateway writes its metrics (2026-10-09).

The gateway emits response latency, TTS first chunk, filler first chunk and barge-in events
through the shared MetricsSink, but its own process (vogon.gateway_service.main, how production
runs it) never started the sink — emit() on an unstarted sink drops the row, so every gateway
metric was silently lost. The harnesses never showed it: they run the gateway inside the data
plane's process, whose startup started the shared sink.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from vogon.gateway.metrics.sink import metrics_sink
from vogon.gateway.settings import settings
from vogon.gateway_service.main import create_app


def test_the_standalone_gateway_starts_the_sink_and_its_metrics_reach_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_base_path", str(tmp_path))
    monkeypatch.setenv("GATEWAY_SESSION_STORE", "memory")
    monkeypatch.setenv("GATEWAY_CAPABILITY_POLICY", "warn")
    with TestClient(create_app()):
        metrics_sink.emit("tts_first_chunk_ms", "collections_ladders", 101.0, instance_id="c1")
    written = list((tmp_path / "metrics").rglob("*"))
    assert any(path.is_file() for path in written), written
    assert "vogon.tts.first_chunk.duration" in "".join(path.read_text() for path in written if path.is_file())
