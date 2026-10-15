"""The standalone voice-gateway service (Phase 1, M1 items 5+6).

The gateway as its own process: call creation (`POST /v1/tenants/{tenant}/calls`
with the opaque app payload), the Twilio webhooks, the media/widget WS
endpoints, the session store, and the three-layer configuration model.
Design: docs/voice-gateway-phase1-standalone.md §2.1, §2.4–§2.6.

Run standalone:  uvicorn vogon.gateway_service.main:app --port 8002

This package may import the driver half (vogon.websocket, vogon.gateway,
app.ledger) and NEVER the reasoning side (app.turns, app.agents,
app.framework) — enforced by tests/test_voice_pillar_imports.py.
"""
