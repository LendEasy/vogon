"""Who answered — a range of voicemail greetings, and our own opening leaking into the far end's
transcript (docs/VOICEMAIL_DETECTION_DESIGN.md §5, §13). The offline transport, a fake
instrument that records what it was asked, a fake clock; the instrument's judgement itself is
the live harness's to measure (data-plane/tests/gateway_e2e/voicemail_duplex_call.py). What these pin:

  1. every greeting reaches the instrument as the GREETING's words — with or without our
     opening leaked in front of it (strip_echo) — and a machine answer is a machine verdict
  2. a greeting too short to judge is never asked about (RECORDED: unknown, source none)
  3. our opening leaking alone, as live calls transcribed it (2026-10-09), never asks;
     leaked in front of the borrower's answer, the instrument hears the answer
  4. RECORDED DEFECT (strict xfail): a partial transcript cut inside a misheard brand name
     leaves four of OUR words, and the instrument is asked about them
"""
import asyncio

import pytest
from test_line_detector import _detector, _queued, _settle  # a sibling; pytest puts this directory on sys.path

from vogon.gateway.transport.line_detector import strip_echo

# the ladder engine's opening, as spoken (collections_ladders, 2026-10-09)
OUR_OPENING = "Hi, this is Maya with LendEasy Finance, calling on a recorded line. May I speak with Alex Morgan?"
# how live STT heard it leaking back at 0.3 echo gain (the e2e duplex runs, 2026-10-09)
LEAK = "Hi. This is Maya with Lundeasy Finance calling on a recorded line. May I speak with Alex Morgan?"

GREETINGS = {
    "personal": "Hi, you've reached Alex Morgan. I can't take your call right now, so leave a message and I'll get back to you.",
    "casual": "Hey it's Alex, I'm not around, you know what to do.",
    "carrier": "The person you are trying to reach is not available. At the tone, please record your message.",
    "short": "Hi, this is Alex. Sorry I missed you. Leave a message.",
    "residence": "Hello, this is the Morgan residence. We're not able to come to the phone right now.",
    "forwarded": "Your call has been forwarded to an automated voice messaging system. Alex Morgan is not available.",
    "mailbox_full": "The mailbox of the person you are calling is full and cannot accept messages at this time. Goodbye.",
    "number_only": "You have reached five five five, zero one zero one. Please leave a message after the tone.",
    "business": "Thank you for calling Morgan Plumbing. Our office is closed. Please leave your name and number.",
}


def _machine_instrument(h):
    """Every ask answered 'a recording' — what is pinned here is WHAT is asked, and the verdict."""
    line = _detector(h, {})
    line.t._last_speak_text = OUR_OPENING
    asked: list[str] = []

    async def ask(state):
        asked.append(state["first_turn"])
        return 0.92
    line.ask = ask
    return line, asked


def _run_final(h, text: str):
    line, asked = _machine_instrument(h)
    t = h.t

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.final(text)
        await _settle()
        verdict = line.verdict
        items = _queued(t)
        await t._floor.close()
        return verdict, items
    verdict, items = asyncio.run(scenario())
    return verdict, items, asked


# ── 1. every greeting, alone and behind our leaked opening ──────────────────

@pytest.mark.parametrize("leaked", [False, True], ids=["alone", "behind_our_leaked_opening"])
@pytest.mark.parametrize("kind", sorted(GREETINGS))
def test_a_greeting_is_judged_on_its_own_words_and_a_recording_is_a_machine_verdict(baseline, kind, leaked):
    greeting = GREETINGS[kind]
    verdict, items, asked = _run_final(baseline, f"{LEAK} {greeting}" if leaked else greeting)
    assert asked, f"{kind}: the instrument was never asked"
    heard = asked[-1]
    assert "recorded line" not in heard.lower() and "maya" not in heard.lower(), heard   # none of OUR words
    assert len(set(heard.lower().split()) & set(greeting.lower().split())) >= 4, heard   # the greeting's
    assert verdict is not None and verdict.answered_by == "machine" and verdict.source == "jev", verdict
    assert [type(item).__name__ for item in items][-2:] == ["LineVerdict", "UtteranceFinal"]


# ── 2. too short to judge ───────────────────────────────────────────────────

def test_a_greeting_under_the_word_floor_is_never_asked_about_as_recorded(baseline):
    """RECORDED (2026-10-09): "Leave a message." is three words, under min_words (4): the
    text instrument is never asked and the call is not detected by its words (the tone
    event and the agent's own party question remain). Lowering the floor is a tuning
    decision — "Hello?"-sized human answers live just below it."""
    line, asked = _machine_instrument(baseline)
    t = baseline.t

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.final("Leave a message.")
        await _settle()
        await t._floor.close()
    asyncio.run(scenario())
    assert asked == [] and line.verdict is None


# ── 3. our own opening leaking back ─────────────────────────────────────────

# what live STT transcribed, every one our opening alone (2026-10-09 duplex runs, echo 0.3)
OBSERVED_LEAKS = [
    "Hi. This is Maya with LendingZ Finance calling on a recorded line. May I speak with Alex Morgan?",
    "Hi. This is Maya with Lundeasy Finance calling on a recorded line. May I speak with Alex Morgan?",
    "Hi. This is Maya with lend easy finance calling on a recorded line. May I speak with Alex?",
    "Hi. This is Maya with lendeasy finance calling on a recorded line. May I speak to Alex?",
    "Hi. This is Maya with LendEasy Finance calling on a recorded line.",
]


@pytest.mark.parametrize("leak", OBSERVED_LEAKS)
def test_our_opening_leaking_back_alone_never_asks(baseline, leak):
    _verdict, _items, asked = _run_final(baseline, leak)
    assert asked == [], f"asked about our own words: {asked}"


def test_our_opening_leaked_in_front_of_the_answer_is_judged_on_the_answer(baseline):
    """The live call that swallowed the borrower's first answer (2026-10-09): STT merged our
    opening and "Yes. This is Alex Morgan." into one turn. The instrument hears the answer."""
    merged = f"{LEAK} Yes. This is Alex Morgan."
    assert strip_echo(merged, OUR_OPENING).endswith("Yes. This is Alex Morgan.")
    _verdict, _items, asked = _run_final(baseline, merged)
    assert asked and "Yes. This is Alex Morgan." in asked[-1] and "recorded line" not in asked[-1].lower(), asked


# ── 4. the recorded defect ──────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason=(
    "RECORDED DEFECT (2026-10-09, live duplex run): an interim cut inside our opening, with the "
    "brand misheard ('lend easy'), leaves 'lend easy finance calling' — four of OUR words, no run "
    "of three matching our line — and the instrument is asked about them (it answered 0.18 live; "
    "a high answer would leave a voicemail on a live person). Not fixed: strip_echo matches runs "
    "of 3+ exact words only."))
def test_a_partial_transcript_inside_our_misheard_opening_never_asks(baseline):
    line, asked = _machine_instrument(baseline)
    t = baseline.t

    async def scenario():
        await t.start()
        line.note_playout_start()
        await t._stt.interim("Hi. This is Maya with lend easy finance calling")
        await _settle()
        await t._floor.close()
    asyncio.run(scenario())
    assert asked == [], f"asked about our own words: {asked}"
