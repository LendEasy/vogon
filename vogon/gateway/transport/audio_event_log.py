"""AudioEventLog — sparse, timestamped transport-level audio events per call.

The third per-call audio artifact (docs/audio-timeline.md), alongside the
stereo recording WAV and the Phase 0 frames CSV:

    <recordings_path>/<call_id>.audio_events.json

Events are appended in-memory with ABSOLUTE monotonic timestamps and rebased
to a shared t0 at flush — pass the AudioRecorder's first-chunk t0 so events,
frames.csv and the WAV all align sample-accurately on one timeline.

Cheap enough to be always-on: dozens of events per call, one list append per
emit, no I/O until call end. Flush failures must never block teardown (the
transport wraps the call).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioEventLog:
    """Collects (t_abs, event, detail) rows; writes rebased JSON at call end."""

    def __init__(self, sink=None) -> None:
        self._events: list[tuple[float, str, dict]] = []
        self._sink = sink

    def emit(self, now: float, event: str, **detail) -> None:
        """Record one event at absolute monotonic time ``now``."""
        self._events.append((now, event, detail))
        if self._sink is not None:
            try:
                self._sink(now, event, detail)
            except Exception:
                logger.exception('Audio event publication failed: %s', event)

    def __len__(self) -> int:
        return len(self._events)

    def suppressed_speech_summary(self) -> dict:
        """Snapshot final suppressions and provisional ones still pending at end.

        Revisions replace text within an episode. A later yield/allowed turn
        removes provisional suppression; finalized suppressed turns remain.
        """
        pending, finalized = {}, []
        for ts, event, detail in self._events:
            episode = detail.get('episode_id')
            if event == 'speech_revision':
                pending.setdefault(episode, {}).update(text=detail.get('text', ''))
            elif event == 'floor_decision':
                if detail.get('action') == 'KEEP_PLAYING':
                    pending.setdefault(episode, {}).update(
                        reason=detail.get('reason'), audio_id=detail.get('audio_id'),
                        episode_id=episode, at=ts)
                elif detail.get('action') in ('CUT', 'TAKE_FLOOR', 'DEFER', 'GATHER_MORE'):
                    pending.get(episode, {}).pop('reason', None)
            elif event == 'caller_transcript':
                pending.pop(episode, None)
                if detail.get('action') == 'SUPPRESS_TURN':
                    finalized.append(dict(text=detail.get('text', ''), reason=detail.get('reason'),
                                          episode_id=episode, audio_id=detail.get('audio_id'),
                                          at=ts, final=True))
        partials = [dict(row, final=False) for row in pending.values()
                    if row.get('text') and row.get('reason')]
        return dict(final_count=len(finalized), pending_count=len(partials),
                    utterances=finalized + partials)

    def flush(self, path: Path, t0: float | None, meta: dict) -> None:
        """Write ``{meta, events:[{t, event, ...detail}]}`` rebased to ``t0``.

        ``t0`` is the recorder's first-chunk timestamp (the WAV's time zero);
        falls back to the first event's own timestamp when no audio was
        recorded. No-op when no events were emitted.
        """
        if not self._events:
            return
        base = t0 if t0 is not None else self._events[0][0]
        rows = [
            {"t": round(ts - base, 3), "event": event, **detail}
            for ts, event, detail in self._events
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"meta": meta, "events": rows}, fh)
        logger.info("audio event log written | events=%d path=%s", len(rows), path)
