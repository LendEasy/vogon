"""From a read of the coming turn to the words — pure, deterministic, testable.

docs/FRONT_COMPLEMENT_DESIGN.md §4-5. The model perceives (front_moves); this
decides (CLAUDE.md: "perceive with the LLM, decide with deterministic code").
No network, no clock, no I/O — everything here is a function of the read and
the pools.

Four outcomes, and silence is the default:

  SILENCE   safe against all twelve desk moves
  BEAT      <=4 words, also safe against all twelve
  LOOKUP    one sentence, NEVER before REFUSAL / CORRECTION / READBACK
  COURTESY  only when the speaker is not the caller
  REPEAT    the caller missed what we said: a short acknowledgement only.
            The DESK re-delivers the content — it already tracks what was
            heard and which lines are required (framework/conversational_repeat
            and the delivery gate's re-speak).

SYMPATHY and ROOM are deliberately absent — the survey found the desk opens
with SYMPATHY or THANKS on 12 of 20 hardship lines, and says "take your time"
itself, so both tiers duplicated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from vogon.gateway.transport.front_moves import LOOKUP_EARNED

logger = logging.getLogger(__name__)

SILENCE, BEAT, LOOKUP, COURTESY, REPEAT = "silence", "beat", "lookup", "courtesy", "repeat"

# Below this confidence on the desk move, say nothing: mispredicting the move
# is exactly how a contradiction happens, so an unsure read must not speak.
DEFAULT_MOVE_FLOOR = 0.45

@dataclass(frozen=True)
class Pick:
    tier: str = SILENCE
    text: str = ""
    reason: str = ""




def pick(read, tiers: dict, rotate, *, has_spoken: bool = True,
         move_floor: float = DEFAULT_MOVE_FLOOR) -> Pick:
    """The ladder. Every rung is a column of the §3 safety table.

    ``rotate(key, pool)`` is supplied by the caller so spacing and
    non-repetition stay in one place (the source's own ledger).
    """
    # 1. The caller missed what we said. Acknowledge it and nothing more.
    #
    #    This used to REPLAY the missed text, and two live phone-path calls
    #    (2026-09-24) showed why it must not. The desk already re-delivers
    #    missed content, so the caller heard it twice back to back. Worse, on a
    #    cut-off line the replay kept the END of the missed text to fit a
    #    length cap and skipped the required notice where the caller had lost
    #    us — and when that replay was then cut short, the rule that suppresses
    #    the desk after a cut filler threw away the desk's correct version, so
    #    the notice was never heard at all. The desk knows what was heard and
    #    which lines are required; the gateway only fills the gap.
    if read.rider("repeat_request"):
        pool = tiers.get("repeat") or []
        if has_spoken and pool:
            return Pick(REPEAT, rotate("repeat", pool), "rider:repeat_request")
        return Pick(SILENCE, "", "rider:repeat_nothing_said")

    # 2. Never speak over identity data, into audio we could not resolve, or
    #    across a goodbye. The desk's own closing lines read as THANKS, so the
    #    move alone cannot see a closing — the 2026-09-24 smoke answered
    #    "That's everything, thanks, goodbye." with "Give me a moment to look
    #    at that for you." until this rider existed.
    if (read.rider("identity_data") or read.rider("unintelligible")
            or read.rider("closing")):
        return Pick(SILENCE, "", "rider:silent_class")

    # 2b. A PLAIN ANSWER owes nothing. The caller answered what was asked and
    #     the desk simply carries on — there is no gap to fill and nothing to
    #     look up. Predicting the desk's move cannot see this: after a plain
    #     answer the desk does PROCEDURE, which used to earn a look-up. On a
    #     real call that turned "Yeah. This is John speaking." into "Let me
    #     check that for you now.", which then collided with the desk's own
    #     line and drew the silence ladder on top of it.
    #     Authorisations are deliberately NOT plain answers: "Yes, I authorize
    #     it." sets the desk working and is the moment a beat earns its place.
    if read.rider("plain_answer"):
        return Pick(SILENCE, "", "rider:plain_answer")

    # 3. An unsure read must not speak: a wrong move is how we contradict.
    if read.move == "UNSURE" or read.confidence < move_floor:
        return Pick(SILENCE, "", f"unsure:{read.move}:{read.confidence:.2f}")

    # 4. Distress is a reason to stay QUIET, not to speak. Measured: the desk
    #    opens with SYMPATHY or THANKS on 12 of 20 hardship lines, so anything
    #    warm here duplicates more often than it lands.
    if read.rider("distress"):
        return Pick(SILENCE, "", "rider:distress_defers_to_desk")

    # 5. A stranger gets a courtesy — the wrong-party lines open PROCEDURE,
    #    ASSENT or REFUSAL, never with a thank-you, so this cannot duplicate.
    if read.rider("not_the_borrower"):
        pool = tiers.get("courtesy") or []
        if pool:
            return Pick(COURTESY, rotate("courtesy", pool), "rider:not_the_borrower")

    # 6. A look-up line is ALLOW-listed, not deny-listed. Only the moves where
    #    the desk is genuinely about to go and find something earn one; every
    #    other speaking turn gets a beat, which is safe before all twelve.
    #    Measured: deny-listing let 9 contradictions through on 213 recorded
    #    turns, 8 of them predicted ASSENT; allow-listing took that to zero at
    #    the same speak rate.
    if read.move in LOOKUP_EARNED:
        pool = tiers.get("lookup") or []
        if pool:
            return Pick(LOOKUP, rotate("lookup", pool), f"move:{read.move}")
    pool = tiers.get("beat") or []
    return (Pick(BEAT, rotate("beat", pool), f"move:{read.move}")
            if pool else Pick(SILENCE, "", "no_pool"))
