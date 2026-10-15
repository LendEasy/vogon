"""Per-call session object holding all call state."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from vogon.websocket.state_machine import CallState, StateMachine


@dataclass
class CallSession:
    call_id: str
    session_token: str
    config: dict
    state_machine: StateMachine = field(default_factory=StateMachine)
    notes_buffer: str = ""
    conversation_history: list[dict] = field(default_factory=list)
    transcript_lines: list[str] = field(default_factory=list)
    analytics_events: list[dict] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)

    def add_user_turn(self, text: str) -> None:
        self.conversation_history.append({"role": "user", "content": text})
        elapsed = time.monotonic() - self.start_time
        self.transcript_lines.append(f"[{elapsed:06.2f}s] CALLER: {text}")

    def add_assistant_turn(self, text: str) -> None:
        self.conversation_history.append({"role": "assistant", "content": text})
        elapsed = time.monotonic() - self.start_time
        persona = self.config.get("assistant", {}).get("persona_name", "ASSISTANT")
        self.transcript_lines.append(f"[{elapsed:06.2f}s] {persona.upper()}: {text}")

    def record_analytics(
        self,
        event_name: str,
        stage: str,
        latency_ms: float,
        token_count: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.analytics_events.append({
            "event_name": event_name,
            "stage": stage,
            "latency_ms": latency_ms,
            "token_count": token_count,
            "event_metadata": json.dumps(metadata) if metadata is not None else None,
        })

    def duration_sec(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def state(self) -> CallState:
        return self.state_machine.state
