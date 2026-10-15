"""Voice Gateway — the stateful audio half of the data plane.

Phase 0 (docs/voice-gateway-phase0.md): this package holds the Turn API
contract types and, as migration proceeds, the transports, pipelines,
clocks, and event spool. Import rule (enforced by
tests/test_voice_pillar_imports.py once step 7 lands): nothing under
vogon.gateway imports app.agents, app.framework, or app.turns.
"""
