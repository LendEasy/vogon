"""Session manager — public entry point for WebSocket call handling.

Phase 7 structural split:
  session_manager  — public API, session lifecycle entry point (this file)
  handler          — utterance processing, agent dispatch, call orchestration
  call_persistence — checkpoint store, JSONL log writer, escalation helper
  orchestrator     — TurnOrchestrator, StateDeltaProposal (framework layer)

External callers (main.py, tests) import handle_call from here.  The
implementation lives in handler.py and is re-exported unchanged.
"""
from vogon.websocket.handler import handle_call  # re-export

__all__ = ["handle_call"]
