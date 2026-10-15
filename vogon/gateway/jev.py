"""The gateway's OpenRouter Decisions ("Jev") client — its own copy, domain-free.

Jev (``typesafe/jev-1.13``) is not a text generator. It takes STATE (a plain
string, or a JSON object of related context) plus typed QUESTIONS and returns
one typed answer per question with a probability attached — no prompt asking
for JSON, no parsing, nothing to validate. Three question shapes:

    noul    yes/no            -> a probability that the answer is yes
    choice  one of N criteria -> the chosen key, a probability per key, and a
                                 confidence
    score   a point on a scale you define (ordered criteria)

It is reachable ONLY through ``POST https://openrouter.ai/api/alpha/decisions``
(``/api/v1/chat/completions`` rejects the model). Measured 2026-09-18: 1
question 341 ms; 35 questions over a JSON-object state 276 ms, 2,126 input
tokens, $0.00009. Output tokens are free; input is $0.042/M.

The gateway makes two decisions calls of its own — the front model's move
(transport/front_moves.py) and the line detector's recording question
(vogon/websocket/line_instruments.py) — and this module owns their transport,
answer shapes and TRACING: every call, successful or failed, goes to the
per-call tracer the driver installs (``set_tracer``), which feeds the decision
metrics (metrics/decisions.py). It knows nothing about moves, lines or
borrowers: every question is built by the caller and every criteria key is an
opaque string here.

Copied from the application's client (data-plane/app/llm/jev.py) 2026-10-10
(vogon step 2, slice 6) so the gateway tree reaches nothing outside itself.
Settings: ``OPEN_ROUTER_API_KEY`` (``OPENROUTER_API_KEY``, the SDK's name, is
honoured too), ``JEV_MODEL``, ``JEV_TIMEOUT_MS`` on GatewaySettings.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)

JEV_MODEL: str = settings.jev_model
# One decision call sits on the turn's critical path exactly like a structured
# LLM call; the same hard ceiling applies (docs/voice-timing-constants.md).
# Measured p50 is ~300 ms.
JEV_TIMEOUT_MS: int = int(settings.jev_timeout_ms)

_ENV_KEYS = ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY")


class JevError(RuntimeError):
    """A decision call failed: transport, auth, quota or a malformed answer."""


# RETRY: a rate limit (429) or a server / transport failure is retried with
# exponential backoff — 0.125 s doubling to a 1 s ceiling (0.125, 0.25, 0.5, 1.0:
# four retries, < 2 s added). A request the API refused for what it IS (any
# other 4xx) is not retried: the same request fails the same way. Measured
# 2026-10-08: a full ladders run lost 15 of 94 calls to "Rate limit exceeded.
# Please retry shortly." — each a whole call thrown away for one transient refusal.
RETRY_BACKOFF_S: tuple[float, ...] = (0.125, 0.25, 0.5, 1.0)
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _retryable(exc: BaseException) -> bool:
    """A transient refusal: rate limited, the server failed, or the request never landed."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "raw_response", None), "status_code", None)
    if status is not None:
        return int(status) in _RETRY_STATUS
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    text = str(exc)
    return "HTTP 429" in text or "Rate limit" in text or any(f"HTTP {c}" in text for c in (500, 502, 503, 504))


# ── question builders ───────────────────────────────────────────────────────
# Plain dicts, exactly the wire shape. Callers can build them by hand too; the
# helpers just keep the three shapes spelled the same everywhere.

def choice(instructions: str, criteria: Mapping[str, str]) -> dict:
    """One key from ``criteria``. Every key is an opaque label; its value is
    the description Jev matches the state against. Include an explicit
    "unclear"/"none" key whenever the state may fit no option — a forced
    choice puts every utterance somewhere."""
    if len(criteria) < 2:
        raise ValueError("a choice question needs at least two criteria")
    return {"type": "choice", "instructions": instructions,
            "criteria": dict(criteria)}


def noul(instructions: str, yes: str, no: str) -> dict:
    """A yes/no question. ``yes``/``no`` describe what makes each answer
    true — put the contrast pair here, not in the instructions."""
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": yes, "false": no}}


def score(instructions: str, criteria: list[str]) -> dict:
    """A point on an ordered scale; ``criteria`` runs from lowest to highest."""
    if len(criteria) < 2:
        raise ValueError("a score question needs at least two rungs")
    return {"type": "score", "instructions": instructions,
            "criteria": list(criteria)}


# ── answers ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    def ranked(self) -> list[tuple[str, float]]:
        """Every criteria key by probability, highest first — the free top-k."""
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])


@dataclass(frozen=True)
class NoulAnswer:
    probability: float

    def value(self, threshold: float = 0.5) -> bool:
        return self.probability >= threshold


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    confidence: float = 0.0


Answer = ChoiceAnswer | NoulAnswer | ScoreAnswer


@dataclass(frozen=True)
class JevResult:
    answers: dict[str, Answer]
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    request_id: str = ""

    def choice(self, name: str) -> ChoiceAnswer:
        a = self.answers[name]
        if not isinstance(a, ChoiceAnswer):
            raise JevError(f"{name!r} is not a choice answer: {a!r}")
        return a

    def noul(self, name: str) -> NoulAnswer:
        a = self.answers[name]
        if not isinstance(a, NoulAnswer):
            raise JevError(f"{name!r} is not a noul answer: {a!r}")
        return a

    def score(self, name: str) -> ScoreAnswer:
        a = self.answers[name]
        if not isinstance(a, ScoreAnswer):
            raise JevError(f"{name!r} is not a score answer: {a!r}")
        return a


# ── tracing ─────────────────────────────────────────────────────────────────
# One record per decisions call, successful or failed, to the tracer of the
# current context. The driver installs a per-call tracer before the transport
# and its tasks start (so they inherit it) and clears it at teardown; the
# tracer turns the record into decision metrics (metrics/decisions.py, which
# reads tag, response, model, latency_ms, the token counts, cost_usd, error
# and backend). The state and the questions ride along so a sidecar writer
# can record the perception surface too.

@dataclass
class DecisionTrace:
    tag: str
    system_prompt: str      # the questions, as JSON
    messages: list[dict]    # one user message: the state
    response: str           # every answer WITH its probabilities, as JSON; "" on failure
    model: str
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None   # the cost the API itself reported
    error: str = ""
    backend: str = "jev"
    ts: float = field(default_factory=time.time)


Tracer = Callable[[DecisionTrace], None]

_tracer: contextvars.ContextVar[Tracer | None] = contextvars.ContextVar(
    "_gateway_decision_tracer", default=None)


def set_tracer(fn: Tracer | None) -> None:
    """Install the per-call tracer in the current context (``None`` clears it)."""
    _tracer.set(fn)


def current_tracer() -> Tracer | None:
    return _tracer.get(None)


def _emit(record: DecisionTrace) -> None:
    fn = _tracer.get(None)
    if fn is None:
        return
    try:
        fn(record)
    except Exception:                                        # noqa: BLE001
        logger.debug("decision tracer raised", exc_info=True)   # never let tracing disrupt a live call


# ── transport ───────────────────────────────────────────────────────────────

def _api_key() -> str:
    configured = settings.open_router_api_key
    if configured:
        return str(configured)
    for k in _ENV_KEYS:
        v = os.environ.get(k)
        if v:
            return v
    raise JevError("OPEN_ROUTER_API_KEY is not set (the gateway's env or .env)")


_client = None


def _sdk():
    global _client
    if _client is None:
        from openrouter import OpenRouter  # noqa: PLC0415 — the SDK is only needed for a real call (tests fake decide)
        _client = OpenRouter(api_key=_api_key())
    return _client


def _parse(raw: Any, latency_ms: float) -> JevResult:
    answers: dict[str, Answer] = {}
    for name, a in (raw.answers or {}).items():
        kind = getattr(a, "type", None)
        if kind == "choice":
            answers[name] = ChoiceAnswer(
                choice=a.choice, probabilities=dict(a.probabilities or {}),
                confidence=float(a.confidence or 0.0))
        elif kind == "noul":
            answers[name] = NoulAnswer(probability=float(a.noul))
        elif kind == "score":
            answers[name] = ScoreAnswer(score=float(a.score),
                                        confidence=float(a.confidence or 0.0))
        else:
            raise JevError(f"unknown answer shape for {name!r}: {a!r}")
    usage = getattr(raw, "usage", None)
    return JevResult(
        answers=answers, latency_ms=latency_ms,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cost_usd=float(getattr(usage, "cost", 0.0) or 0.0),
        model=str(getattr(raw, "model", "") or ""),
        request_id=str(getattr(raw, "id", "") or ""))


def _check(questions: Mapping[str, dict]) -> None:
    if not questions:
        raise ValueError("at least one question is required")
    for name, q in questions.items():
        if q.get("type") not in ("choice", "noul", "score"):
            raise ValueError(f"question {name!r}: unknown type {q.get('type')!r}")


def _answer_json(result: JevResult) -> str:
    out: dict[str, Any] = {}
    for name, a in result.answers.items():
        if isinstance(a, ChoiceAnswer):
            out[name] = {"choice": a.choice, "confidence": round(a.confidence, 3),
                         "probabilities": {k: round(v, 3) for k, v in a.probabilities.items()}}
        elif isinstance(a, NoulAnswer):
            out[name] = {"noul": round(a.probability, 3)}
        else:
            out[name] = {"score": a.score, "confidence": round(a.confidence, 3)}
    return json.dumps(out, sort_keys=True)


def _state_message(state: Any) -> list[dict]:
    return [{"role": "user", "content": state if isinstance(state, str)
             else json.dumps(state, default=str)}]


def _trace(tag: str, state: Any, questions: Mapping[str, dict], result: JevResult) -> None:
    _emit(DecisionTrace(
        tag=tag, system_prompt=json.dumps(dict(questions), sort_keys=True),
        messages=_state_message(state), response=_answer_json(result),
        model=result.model or JEV_MODEL, latency_ms=result.latency_ms,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_usd=result.cost_usd))


def _trace_failure(tag: str, state: Any, questions: Mapping[str, dict],
                   latency_ms: float, error: Exception) -> None:
    """A decisions call that failed for good is traced too — timed (the turn waited for it),
    not metered: a failed decision is a decision with status=error, not a missing one."""
    _emit(DecisionTrace(
        tag=tag, system_prompt=json.dumps(dict(questions), sort_keys=True),
        messages=_state_message(state), response="", model=JEV_MODEL,
        latency_ms=latency_ms, error=f"{type(error).__name__}: {error}"))


def decide(state: str | Mapping[str, Any] | list, questions: Mapping[str, dict],
           *, model: str = JEV_MODEL, timeout_ms: int = JEV_TIMEOUT_MS,
           tag: str = "jev") -> JevResult:
    """One decisions call. ``state`` is a string or a JSON-able object; every
    question is answered in the same response. Raises ``JevError`` on any
    failure — the caller owns the fallback (never a guessed answer here)."""
    _check(questions)
    t0 = time.monotonic()
    for attempt, wait in enumerate((*RETRY_BACKOFF_S, None)):
        try:
            raw = _sdk().alpha.decisions.create(
                model=model, state=state, questions=dict(questions),
                timeout_ms=timeout_ms)
            break
        except Exception as exc:  # noqa: BLE001 — every SDK error is one class here
            latency = (time.monotonic() - t0) * 1000
            if wait is not None and _retryable(exc):
                logger.warning("jev call retry %d in %.3fs | tag=%s latency=%.0fms err=%s",
                               attempt + 1, wait, tag, latency, exc)
                time.sleep(wait)          # a synchronous call (worker threads); decide_async awaits
                continue
            logger.warning("jev call failed | tag=%s latency=%.0fms attempts=%d err=%s",
                           tag, latency, attempt + 1, exc)
            _trace_failure(tag, state, questions, latency, exc)
            raise JevError(str(exc)) from exc
    result = _parse(raw, (time.monotonic() - t0) * 1000)
    logger.info("jev | tag=%s questions=%d latency=%.0fms in=%d out=%d cost=$%.6f",
                tag, len(questions), result.latency_ms, result.input_tokens,
                result.output_tokens, result.cost_usd)
    _trace(tag, state, questions, result)
    return result


async def decide_async(state: str | Mapping[str, Any] | list,
                       questions: Mapping[str, dict], *, model: str = JEV_MODEL,
                       timeout_ms: int = JEV_TIMEOUT_MS,
                       tag: str = "jev") -> JevResult:
    """The same call from a coroutine. Uses the SDK's async client with an
    explicit ``asyncio.wait_for`` so a hung socket can never stall a turn."""
    _check(questions)
    t0 = time.monotonic()
    for attempt, wait in enumerate((*RETRY_BACKOFF_S, None)):
        try:
            raw = await asyncio.wait_for(
                _sdk().alpha.decisions.create_async(
                    model=model, state=state, questions=dict(questions),
                    timeout_ms=timeout_ms),
                timeout=timeout_ms / 1000 + 0.5)
            break
        except Exception as exc:  # noqa: BLE001
            latency = (time.monotonic() - t0) * 1000
            if wait is not None and _retryable(exc):
                logger.warning("jev call retry %d in %.3fs | tag=%s latency=%.0fms err=%s",
                               attempt + 1, wait, tag, latency, exc)
                await asyncio.sleep(wait)
                continue
            logger.warning("jev call failed | tag=%s latency=%.0fms attempts=%d err=%s",
                           tag, latency, attempt + 1, exc)
            _trace_failure(tag, state, questions, latency, exc)
            raise JevError(str(exc)) from exc
    result = _parse(raw, (time.monotonic() - t0) * 1000)
    logger.info("jev | tag=%s questions=%d latency=%.0fms in=%d out=%d cost=$%.6f",
                tag, len(questions), result.latency_ms, result.input_tokens,
                result.output_tokens, result.cost_usd)
    _trace(tag, state, questions, result)
    return result
