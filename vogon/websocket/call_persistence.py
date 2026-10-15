"""Call persistence helpers — checkpoint store, JSONL log writer, escalation.

Extracted from handler.py (Phase 7 structural split). The JSONL/stream log
writer moved to app/ledger/ (the CallLedger substrate); the escalation
helper moved to the gateway (vogon/gateway_service/escalation/, 2026-08-25).
The in-process checkpoint dict remains.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# In-process checkpoint store (Phase 0 — Redis/S3 deferred to future phase).
# Keyed by call_id; value is the serialized checkpoint dict.
call_checkpoints: dict[str, dict] = {}

# The ledger operations (ordered append + JSONL/stream writer) moved to
# app/ledger/substrate.py (2026-08-25, docs/call-ledger-design.md §6).
# This module keeps the two non-ledger helpers below.
