"""The complementing front voice — the pick ladder and the repeat short-circuit.

docs/FRONT_COMPLEMENT_DESIGN.md. Every rung of the ladder comes from a column
of the survey's safety table, so each has a test naming the survey fact that
put it there.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from vogon.gateway.settings import settings
from vogon.gateway.transport import front_moves
from vogon.gateway.transport.front_guard import BEAT_MAX_WORDS, guard
from vogon.gateway.transport.front_moves import MOVES, NO_LOOKUP, FrontRead
from vogon.gateway.transport.front_pick import BEAT, COURTESY, LOOKUP, REPEAT, SILENCE, Pick, pick
from vogon.gateway.transport.front_source import FrontConfig, FrontModelSource

# The collections front artifact — the domain's file, not the gateway's. The
# gateway ships no words of its own (examples/front.yaml is a neutral template
# with beats only), so the tests that exercise a FULL artifact read the
# application's: FRONT_CONFIG_PATH when set, else the monorepo sibling, else skip.
_ARTIFACT = settings.resolved_front_config_path() or str(
    pathlib.Path(__file__).resolve().parents[2] / "data-plane" / "app" / "agents" / "collections" / "front.yaml")


def _artifact() -> FrontConfig:
    if not pathlib.Path(_ARTIFACT).exists():
        pytest.skip(f"no full front artifact in this checkout ({_ARTIFACT})")
    cfg = FrontConfig.load(_ARTIFACT)
    assert cfg is not None
    return cfg

TIERS = {"beat": ["One moment."],
         "lookup": ["Let me pull that up for you, one moment."],
         "courtesy": ["Thanks for letting me know."],
         "repeat": ["Sorry about that."]}
FIRST = lambda key, pool: pool[0]          # noqa: E731 — deterministic rotation


def _pick(read, has_spoken=True):
    return pick(read, TIERS, FIRST, has_spoken=has_spoken)


class TestContradiction:
    """A look-up line must never precede a move that is about to deny,
    correct, or read back — 30 of the 204 desk lines open with a REFUSAL,
    the single largest class."""

    @pytest.mark.parametrize("move", sorted(NO_LOOKUP))
    def test_only_a_beat_precedes_a_denial(self, move):
        got = _pick(FrontRead(move, 0.9))
        assert got.tier == BEAT, f"{move} got {got.tier}"

    @pytest.mark.parametrize("move", ["FIGURE", "EXPLANATION", "OFFER"])
    def test_only_a_genuine_go_and_look_earns_a_lookup(self, move):
        assert _pick(FrontRead(move, 0.9)).tier == LOOKUP

    @pytest.mark.parametrize("move", ["ASSENT", "DONE", "THANKS", "SYMPATHY",
                                      "PRESENCE", "PROCEDURE"])
    def test_every_other_speaking_move_gets_a_beat(self, move):
        """Measured on 213 recorded turns: allowing a look-up outside
        NO_LOOKUP produced 9 contradictions, 8 of them predicted ASSENT —
        which preceded ASSENT 14 times, READBACK 13, FIGURE 12 and REFUSAL 7,
        so it says nothing about the desk. Allow-listing took contradictions
        to zero at the same speak rate."""
        assert _pick(FrontRead(move, 0.9)).tier == BEAT

    def test_every_move_is_handled(self):
        for m in MOVES:
            assert _pick(FrontRead(m, 0.9)).tier in {SILENCE, BEAT, LOOKUP, COURTESY}


class TestSilence:
    def test_an_unsure_read_says_nothing(self):
        assert _pick(FrontRead("UNSURE", 0.99)).tier == SILENCE

    def test_a_low_confidence_read_says_nothing(self):
        """Mispredicting the move is how a contradiction happens, so the
        floor is a floor, not a tiebreak."""
        assert _pick(FrontRead("FIGURE", 0.1)).tier == SILENCE

    @pytest.mark.parametrize("rider", ["identity_data", "unintelligible"])
    def test_the_silent_classes(self, rider):
        assert _pick(FrontRead("FIGURE", 0.9, {rider: 0.9})).tier == SILENCE

    def test_distress_defers_to_the_desk(self):
        """The survey's sharpest finding: the desk opens with SYMPATHY or
        THANKS on 12 of the 20 hardship lines, so anything warm here
        duplicates more often than it lands."""
        assert _pick(FrontRead("OFFER", 0.9, {"distress": 0.9})).tier == SILENCE

    def test_a_wrong_distress_answer_costs_silence_not_a_double(self):
        assert not _pick(FrontRead("FIGURE", 0.9, {"distress": 0.8})).text


class TestCourtesy:
    def test_a_stranger_gets_a_courtesy(self):
        """Safe because the wrong-party lines open PROCEDURE, ASSENT or
        REFUSAL — never with a thank-you."""
        got = _pick(FrontRead("PROCEDURE", 0.9, {"not_the_borrower": 0.9}))
        assert got.tier == COURTESY

    def test_identity_data_outranks_courtesy(self):
        got = _pick(FrontRead("PROCEDURE", 0.9,
                              {"not_the_borrower": 0.9, "identity_data": 0.9}))
        assert got.tier == SILENCE


class TestRepeat:
    """The caller missed what we said: an acknowledgement ONLY. The desk
    re-delivers the content. Two live phone-path calls (2026-09-24) showed why
    the gateway must not replay: the desk already re-delivers, so the caller
    heard the figures twice, and on a cut-off line the replay kept the END of
    the missed text and skipped the required notice where the caller had lost
    us — then that replay was cut short, and the desk's correct version was
    suppressed behind it, so the notice was never heard."""

    def test_a_repeat_request_gets_an_acknowledgement(self):
        got = _pick(FrontRead("FIGURE", 0.9, {"repeat_request": 0.9}))
        assert got.tier == REPEAT and got.text == "Sorry about that."

    def test_the_acknowledgement_carries_no_content(self):
        """Nothing of what was said comes back from the gateway — no figure,
        no notice, no sentence of the desk's line."""
        got = _pick(FrontRead("FIGURE", 0.9, {"repeat_request": 0.9}))
        assert got.text in TIERS["repeat"]
        assert "$" not in got.text and len(got.text.split()) <= 4

    def test_nothing_said_yet_means_nothing_to_acknowledge(self):
        got = _pick(FrontRead("FIGURE", 0.9, {"repeat_request": 0.9}), has_spoken=False)
        assert got.tier == SILENCE and got.reason.startswith("rider:")

    def test_it_outranks_the_rest_of_the_ladder(self):
        got = _pick(FrontRead("REFUSAL", 0.9,
                              {"repeat_request": 0.9, "plain_answer": 0.9}))
        assert got.tier == REPEAT


class TestArtifactPools:
    def test_the_shipped_pools_load_and_lint(self):
        cfg = _artifact()
        for kind in ("beat", "lookup", "courtesy"):
            assert cfg.tiers.get(kind), f"{kind} pool refused by the guard at load"
        assert cfg.tiers.get("repeat"), "the acknowledgement pool refused by the guard"
        assert cfg.can_read_moves, "the jev engine needs criteria and pools"

    def test_every_shipped_line_passes_the_guard(self):
        """A pool line may be chosen WITHOUT re-reading the borrower, so each
        has to be safe at any moment of the call."""
        cfg = _artifact()
        for kind, pool in cfg.tiers.items():
            cap = BEAT_MAX_WORDS if kind == "beat" else settings.front_max_words
            for line in pool:
                assert not guard(line, max_words=cap,
                                 must_not_say=cfg.must_not_say), f"{kind}: {line!r}"


class TestEngineGate:
    def test_off_composes_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "front_engine", "off")
        cfg = _artifact()
        assert not FrontModelSource(cfg, agent_name="Maya", lender_name="")._can_compose()

    def test_jev_is_the_default_and_can_compose(self, monkeypatch):
        monkeypatch.setattr(settings, "front_engine", "jev")
        cfg = _artifact()
        assert FrontModelSource(cfg, agent_name="Maya", lender_name="")._can_compose()

    def test_nothing_is_exempt_from_the_guard_any_more(self):
        """The replay needed a content-guard exemption because it carried the
        desk's figures. The acknowledgement carries none, so a line with a
        figure is refused like any other gateway line."""
        cfg = _artifact()
        src = FrontModelSource(cfg, agent_name="Maya", lender_name="")
        assert src._admit("Here it is again. Your past due is $202.36.") == ("", "")
        assert src._admit("Sorry about that.")[0] == "Sorry about that."

    def test_fragments_cannot_stack_acknowledgements(self, monkeypatch):
        """Flux cut "Hello? Are you still there? I couldn't hear you." into
        three turns on a live call. The ordinary spacing rule now holds the
        acknowledgement to one."""
        monkeypatch.setattr(settings, "front_min_gap_sec", 6.0)
        cfg = _artifact()
        src = FrontModelSource(cfg, agent_name="Maya", lender_name="")
        assert src._admit("Sorry about that.")[0]
        assert src._admit("My apologies.") == ("", "")


class TestDistressLatch:
    """Flux cut "I can't do the full amount, my hours got cut last month and
    money is really tight." into two turns. The front read the refusal half
    first (distress 0.03) and spoke; only the second half carried the hardship
    (0.92, verified by probe). The first line cannot be prevented — the
    hardship had not been said yet — but every LATER fragment of that turn
    must stay quiet."""

    def _src(self):
        cfg = _artifact()
        return FrontModelSource(cfg, agent_name="Maya", lender_name="")

    def test_distress_sticks_until_the_desk_answers(self):
        src = self._src()
        assert not src._distress_latch
        src._distress_latch = True
        src.note_agent("Let me take a look.", front=True)     # our own filler
        assert src._distress_latch, "a front line does not end the turn"
        src.note_agent("I'm sorry — that's a lot to deal with.")
        assert not src._distress_latch, "the desk's answer starts a clean turn"

    def test_a_latched_read_picks_silence_whatever_the_move(self):
        latched = FrontRead("FIGURE", 0.99, {"distress": 1.0})
        got = pick(latched, TIERS, FIRST)
        assert got.tier == SILENCE and got.reason == "rider:distress_defers_to_desk"


class TestPlainAnswer:
    """A plain answer owes nothing: the caller answered what was asked and the
    desk simply carries on. Predicting the desk's move cannot see this —
    after a plain answer the desk does PROCEDURE, which used to earn a
    look-up. On a real call that turned "Yeah. This is John speaking." into
    "Let me check that for you now.", which collided with the desk's own line
    and drew the silence ladder on top of it."""

    def test_a_plain_answer_says_nothing(self):
        got = _pick(FrontRead("PROCEDURE", 0.95, {"plain_answer": 0.94}))
        assert got.tier == SILENCE and got.reason == "rider:plain_answer"

    def test_it_outranks_every_speaking_tier(self):
        for move in ("FIGURE", "OFFER", "EXPLANATION", "ASSENT", "DONE"):
            assert _pick(FrontRead(move, 0.95, {"plain_answer": 0.9})).tier == SILENCE

    def test_a_repeat_request_still_wins(self):
        """They are telling us the delivery failed, not answering."""
        got = _pick(FrontRead("FIGURE", 0.9,
                              {"plain_answer": 0.9, "repeat_request": 0.9}))
        assert got.tier == REPEAT

    def test_procedure_no_longer_earns_a_lookup(self):
        """A verification step needs no look-up in front of it."""
        assert _pick(FrontRead("PROCEDURE", 0.95)).tier == BEAT


class TestReasonedSilenceIsAbsolute:
    """Two kinds of quiet. A rider fired means we KNOW to be quiet and no beat
    may play over it; an unsure read means we do not know, and there a beat
    still beats dead air.

    These drive the REAL path — prime() -> _read_and_pick() -> hold_line() —
    with only the Jev read stubbed. The first version of this test set
    _deliberate_silence by hand, so it passed while a __bool__ override on
    Pick made every silent pick lose its reason and the flag was never set.
    A live phone-path call caught it: "Yes, that's fine." -> silent ->
    "Taking a look." played anyway."""

    def _run(self, monkeypatch, read):
        monkeypatch.setattr(settings, "front_engine", "jev")
        monkeypatch.setattr(settings, "front_min_gap_sec", 0.0)

        async def fake_read(*a, **k):
            return read
        monkeypatch.setattr(front_moves, "read_async", fake_read)
        cfg = _artifact()
        src = FrontModelSource(cfg, agent_name="Maya", lender_name="")

        async def go():
            src.prime("Yes, that's fine.")
            return await src.hold_line(0.2, 0.3)
        return src, asyncio.run(go())

    def test_a_plain_answer_plays_no_beat_through_the_real_path(self, monkeypatch):
        src, got = self._run(monkeypatch, FrontRead("PROCEDURE", 0.92, {"plain_answer": 0.95}))
        assert got == ("", ""), "a reasoned silence must not get the hold's beat"
        assert src._deliberate_silence is True
        assert src.metrics.get("front_silent_on_purpose") == 1

    def test_distress_plays_no_beat_through_the_real_path(self, monkeypatch):
        src, got = self._run(monkeypatch, FrontRead("SYMPATHY", 0.9, {"distress": 0.9}))
        assert got == ("", "")

    def test_an_unsure_read_still_gets_a_beat(self, monkeypatch):
        src, got = self._run(monkeypatch, FrontRead("UNSURE", 0.3))
        text, aid = got
        assert text and aid.startswith("front-"), "dead air is worse when we do not know"
        assert src._deliberate_silence is False

    def test_disabling_repeats_does_not_silence_the_front(self, monkeypatch):
        """FRONT_REPEAT_ENABLED=0 used to raise the confidence floor to 1.1,
        which silenced every tier, not just the repeat."""
        monkeypatch.setattr(settings, "front_repeat_enabled", False)
        src, got = self._run(monkeypatch, FrontRead("FIGURE", 0.9))
        assert got[0] in src._cfg.tiers["lookup"], "a FIGURE read still earns a look-up"

    def test_a_silent_pick_keeps_its_reason(self):
        """The root cause, pinned directly: a Pick with no text must still be
        a Pick — never falsy — so its reason survives."""
        p = Pick(SILENCE, "", "rider:plain_answer")
        assert (p.reason if p is not None else "lost") == "rider:plain_answer"
        assert "__bool__" not in vars(Pick), "a silent Pick must never be falsy"
        assert bool(p) is True
