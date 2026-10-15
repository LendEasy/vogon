"""What is the DESK about to say? — the gateway's read on the coming turn.

docs/FRONT_COMPLEMENT_DESIGN.md.

The gateway speaks BEFORE the desk has answered, so what it may safely say is
decided by what the desk is ABOUT to say, not by what the caller "is owed".
This module asks that question and nothing else; ``front_pick`` turns the
answer into words.

The twelve moves are not invented. Every one of the 204 recited lines in the
domain's line registry was classified by the move its opening sentence makes,
and these are the classes that came out, with REFUSAL the largest at 14.7%.

Shape: ONE Jev decisions call — a ``choice`` over the moves plus riders as
``noul``. Jev answers every question in one request at one request's latency
(measured: 35 questions, 276 ms), so granularity is close to free.

Gateway-owned and domain-free: the criteria text is loaded from the domain
artifact, and every key here is an opaque string. Failure of any kind reads
as UNSURE, which ``front_pick`` turns into silence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vogon.gateway import jev
from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)

# The desk's opening move. UNSURE is the bail-out every Jev choice needs
# (CLAUDE.md: "every classifier needs an unclear bucket").
MOVES = ("ASSENT", "REFUSAL", "SYMPATHY", "THANKS", "FIGURE", "READBACK",
         "DONE", "OFFER", "CORRECTION", "EXPLANATION", "PROCEDURE",
         "PRESENCE", "UNSURE")

# Moves a one-sentence look-up line must NEVER precede: it reads as a false
# hope in front of a refusal, as agreement in front of a correction, and as a
# confirmation in front of a readback.
NO_LOOKUP = frozenset({"REFUSAL", "CORRECTION", "READBACK"})

# Moves that EARN a look-up line: the desk is genuinely about to go and find
# something, so "let me pull that up" is what it is doing.
#
# Measured on 213 recorded ladder turns (2026-09-24): allowing a look-up
# everywhere outside NO_LOOKUP produced 9 contradictions, and EIGHT of the
# nine were predicted ASSENT. ASSENT turns out to be uninformative about the
# desk — the same prediction preceded ASSENT 14 times, READBACK 13, FIGURE 12,
# DONE 9 and REFUSAL 7 — because "the caller said something agreeable" says
# nothing about what the desk will do with it. Allow-listing instead of
# deny-listing took contradictions to ZERO at the SAME speak rate (46%), since
# everything displaced goes to a beat, which is safe before all twelve moves.
# PROCEDURE dropped 2026-09-24: a verification step ("Before I can share any
# account details I need to verify your identity") needs no look-up in front
# of it, and PROCEDURE is what the desk does after a plain answer — which is
# how "Yeah. This is John speaking." earned "Let me check that for you now."
# on a real call. n=1 in the 213-turn measurement, so nothing measured is lost.
LOOKUP_EARNED = frozenset({"FIGURE", "EXPLANATION", "OFFER"})

# Riders — facts that can be true ALONGSIDE any move, so they are their own
# yes/no questions rather than extra keys in the choice (JEV_CLASSIFICATION
# _DESIGN.md: "a fact that must survive beside an exclusive family's winner is
# its own yes/no rider").
RIDERS = ("distress", "not_the_borrower", "identity_data", "unintelligible",
          "repeat_request", "closing", "plain_answer")


@dataclass(frozen=True)
class FrontRead:
    """One read of the coming turn. Never raises; an unusable answer is
    ``UNSURE`` with every rider false, which picks silence."""
    move: str = "UNSURE"
    confidence: float = 0.0
    riders: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    error: str = ""

    def rider(self, name: str, threshold: float = 0.5) -> bool:
        return float(self.riders.get(name, 0.0)) >= threshold


# The move question when the artifact does not word its own: it names no domain.
DEFAULT_MOVE_QUESTION = (
    "This is what a caller has just said on a phone call with a service agent. The "
    "agent will answer in a moment. What move will the agent's answer OPEN with?")


def build_questions(criteria: dict, rider_text: dict, question: str = "") -> dict:
    """The one request's questions. ``criteria``, ``rider_text`` and the move
    ``question`` come from the domain artifact — this module ships no domain words."""
    q = {"move": jev.choice(question or DEFAULT_MOVE_QUESTION, criteria)}
    for name in RIDERS:
        spec = rider_text.get(name)
        if spec:
            q[name] = jev.noul(spec["ask"], spec["yes_when"], spec["no_when"])
    return q


def _state(agent_last: str, utterance: str, history: list[tuple[str, str]]) -> dict:
    return {
        "agent_just_said": agent_last or "(the call has just opened)",
        "caller_just_said": utterance,
        "recent_turns": [f"{who}: {what}" for who, what in (history or [])[-6:]],
    }


async def read_async(criteria: dict, rider_text: dict, *, agent_last: str,
                     utterance: str, history=None, timeout_ms: int = 0,
                     question: str = "") -> FrontRead:
    """One decisions call. Any failure — transport, quota, a malformed answer
    — returns UNSURE rather than raising: the hold must never break because
    the read did."""
    try:
        questions = build_questions(criteria, rider_text, question)
        res = await jev.decide_async(
            _state(agent_last, utterance, history), questions,
            timeout_ms=timeout_ms or settings.front_beat_after_ms,
            tag="front_move")
    except Exception as exc:                                  # noqa: BLE001
        # The client timeout IS front_beat_after_ms, deliberately: a read that
        # would miss the beat deadline is worth nothing, so it is abandoned
        # and the hold's beat plays instead. Measured 2026-09-24: p50 112 ms,
        # but 2 of 20 reads ran past 900 ms. Timeouts carry no message, which
        # is why this says so explicitly.
        detail = str(exc) or (f"{type(exc).__name__} (likely timeout at "
                                 f"{timeout_ms or settings.front_beat_after_ms} ms)")
        logger.warning("FRONT | move read failed: %s", detail)
        return FrontRead(error=detail)

    try:
        ch = res.choice("move")
        riders = {}
        for name in RIDERS:
            if name in res.answers:
                riders[name] = res.noul(name).probability
        move = ch.choice if ch.choice in MOVES else "UNSURE"
        return FrontRead(move=move, confidence=float(ch.confidence),
                         riders=riders, latency_ms=res.latency_ms,
                         cost_usd=res.cost_usd)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("FRONT | move answer unusable: %s", exc)
        return FrontRead(error=str(exc))
