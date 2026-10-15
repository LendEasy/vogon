"""The gateway-owned barge judge — one small, self-contained LLM client.

Decision D1 (voice-gateway-phase1-standalone.md §8): the semantic barge
verdict stays ON the gateway's latency path, as the gateway's own client —
this module talks to Cerebras directly and imports nothing from the
reasoning side (app.llm / app.pipeline are gone from the gateway tree).

Synchronous BY DESIGN: the transport runs it in an executor under the
explicit ``semantic_barge_llm_timeout_ms`` cap — this call gates an audio
cut and is never allowed to block unboundedly. Every failure returns to the
caller as an exception; the caller's fallback is ABSORB (when unsure, keep
speaking).
"""
from __future__ import annotations

import json
import logging

from cerebras.cloud.sdk import Cerebras

from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)

_client = None


def _cerebras():
    global _client
    if _client is None:
        _client = Cerebras(api_key=settings.cerebras_api_key,
                           timeout=max(.05, settings.semantic_barge_llm_timeout_ms / 1000),
                           max_retries=0)
    return _client


def judge_json(system: str, user: str, *, max_tokens: int = 256) -> dict:
    """One JSON-mode completion; returns the parsed object. Raises on any
    failure — the caller owns the fallback."""
    resp = _cerebras().chat.completions.create(
        model=settings.cerebras_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)
