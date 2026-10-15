"""The front line's structural guards — docs/voice-gateway-front-model.md §8.

One constructed violation per guard, plus the pool lint. The guards are the
part of the front model that must never depend on a prompt behaving.
"""
from __future__ import annotations

import asyncio

import pytest

from vogon.gateway import settings as gw_settings
from vogon.gateway.transport.front_guard import guard, lint_pool, sentences
from vogon.gateway.transport.front_source import FrontConfig, FrontModelSource


def _g(text, **kw):
    kw.setdefault("max_words", 12)
    return guard(text, **kw)


class TestShape:
    def test_a_clean_one_sentence_line_passes(self):
        assert _g("Give me a moment to look at that for you.") == []

    def test_a_second_sentence_silences_the_whole_line(self):
        assert "shape:sentences" in _g("Thanks for laying that out. Give me a moment.")

    def test_over_max_words(self):
        assert "length" in _g("Let me take a proper look at that for you, bear with me a second here.")

    def test_a_line_break(self):
        assert "shape:linebreak" in _g("One moment.\nStill here.")

    def test_a_question(self):
        assert "question" in _g("Can you hold on a second?")


class TestContentClass:
    @pytest.mark.parametrize("line", ["Give me 2 minutes.", "That's $75.", "About 10% of it."])
    def test_digits_and_symbols(self, line):
        assert "content:number" in _g(line)

    def test_spelled_numbers(self):
        assert "content:spelled_number" in _g("Give me twenty seconds on that.")

    def test_one_moment_is_not_a_number(self):
        assert "content:spelled_number" not in _g("One moment.")
        assert "content:spelled_number" not in _g("Give me one second.")

    def test_calendar_words(self):
        assert "content:date_word" in _g("Let me look at tomorrow for you.")

    def test_domain_tokens(self):
        assert "content:payment" in _g("Let me check that payment.", must_not_say=["payment"])

    def test_the_borrower_name_is_never_sayable_in_v1(self):
        assert "content:name" in _g("I hear you, Alex.", forbidden_names=["Alex"])


class TestCommitment:
    """The reason the model exists: no word of assent, refusal or commitment
    anywhere in the line — not only at the start."""

    @pytest.mark.parametrize("line", [
        "Sure, one moment.", "Let me look at that, no problem.",
        "That's fine, one moment.", "Got it.", "Understood.",
        "I'll get that approved for you.", "Consider it done.",
        "Go ahead.", "Please proceed.",
    ])
    def test_commitment_words_anywhere(self, line):
        assert "commitment" in _g(line)

    @pytest.mark.parametrize("line", [
        "Noting that authorization.", "Processing that now.", "Taking care of that now.",
        "I've noted that.", "Let me put that through.", "Scheduling that for you.",
    ])
    def test_claimed_actions_anywhere(self, line):
        """The other half of no-authority: attending is allowed, acting is not.
        "Noting that authorization." slipped past the prompt in the 2026-09-20
        ladder run."""
        assert "action" in _g(line)

    def test_attention_verbs_pass(self):
        for line in ("Checking it now.", "Taking a look.", "I hear you, and I'm not rushing you."):
            assert "commitment" not in _g(line), line


class TestRepetition:
    def test_a_repeated_line_is_silenced(self):
        earlier = ["Let me pull that up for you."]
        assert "repeat" in _g("Let me pull that up for you.", earlier=earlier)

    def test_a_four_word_beat_may_recur(self):
        assert _g("One moment.", earlier=["One moment."]) == []
        assert _g("Checking it now.", earlier=["Checking it now.", "One moment."]) == []

    def test_the_opener_rule_is_three_words(self):
        earlier = ["Let me look at that for you."]
        assert "opener" in _g("Let me look into that now.", earlier=earlier)
        assert "opener" not in _g("Let me pull that up for you.", earlier=earlier)

    def test_the_opener_rule_reads_only_the_previous_line(self):
        earlier = ["Let me look at that for you.", "One moment."]
        assert "opener" not in _g("Let me look into that now.", earlier=earlier)


class TestPoolLint:
    def test_a_safe_pool_loads(self):
        assert lint_pool(["One moment.", "Checking on that.", "Bear with me."]) == []

    def test_a_pool_with_a_commitment_word_refuses(self):
        bad = lint_pool(["One moment.", "Okay, one moment.", "Got it."])
        assert [b[0] for b in bad] == ["Okay, one moment.", "Got it."]

    def test_a_pool_beat_longer_than_four_words_refuses(self):
        assert lint_pool(["Bear with me a moment."]) and lint_pool(["Bear with me."]) == []


def test_sentence_split():
    assert sentences("One moment. Still here.") == ["One moment.", "Still here."]
    assert sentences("I hear you, and I'm not rushing you.") == ["I hear you, and I'm not rushing you."]


# ── The source's beat is withheld once the borrower speaks again ─────────────

def test_a_pending_beat_is_withheld_when_the_borrower_speaks_again(monkeypatch):
    """2026-09-20 smoke: Flux finalised "Yes.", the model said nothing, the
    desk was slow, and the beat fired under "go ahead and take the seventy
    five". A beat waiting for its deadline gives up the moment a newer
    utterance primes the source."""


    monkeypatch.setattr(gw_settings.settings, "front_min_gap_sec", 0.0)
    # These two pin the LLM engine on purpose: they patch _generate and are
    # about the WITHHOLDING mechanism, which is engine-independent. The
    # default engine is "jev", whose _can_compose needs move criteria this
    # bare config does not carry — a test must not ride on that default.
    monkeypatch.setattr(gw_settings.settings, "front_engine", "llm")
    cfg = FrontConfig("prompt", {"hold": ["One moment."]}, [])
    src = FrontModelSource(cfg, agent_name="Maya", lender_name="")

    async def empty(_):
        return ""

    monkeypatch.setattr(FrontModelSource, "_generate", lambda self, u: empty(u))

    async def scenario():
        src.prime("Yes.")
        task = asyncio.create_task(src.hold_line(0.05, 0.6))
        await asyncio.sleep(0.2)
        src.prime("go ahead and take the seventy five")     # the borrower resumed
        return await task

    assert asyncio.run(scenario()) == ("", "")
    assert src.metrics.get("front_beat_withheld") == 1


def test_a_deliberate_silence_still_gets_a_beat_when_the_desk_is_slow(monkeypatch):
    monkeypatch.setattr(gw_settings.settings, "front_min_gap_sec", 0.0)
    # These two pin the LLM engine on purpose: they patch _generate and are
    # about the WITHHOLDING mechanism, which is engine-independent. The
    # default engine is "jev", whose _can_compose needs move criteria this
    # bare config does not carry — a test must not ride on that default.
    monkeypatch.setattr(gw_settings.settings, "front_engine", "llm")
    cfg = FrontConfig("prompt", {"hold": ["One moment."]}, [])
    src = FrontModelSource(cfg, agent_name="Maya", lender_name="")

    async def empty(_):
        return ""

    monkeypatch.setattr(FrontModelSource, "_generate", lambda self, u: empty(u))

    async def scenario():
        src.prime("Yes.")
        return await src.hold_line(0.05, 0.3)

    text, audio_id = asyncio.run(scenario())
    assert (text, audio_id) == ("One moment.", "front-1")
