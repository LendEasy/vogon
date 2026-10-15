"""CallClocks acceptance tests (voice-gateway-phase0 §4.1, step 3).

The gateway clock engine replaces the handler's 1 Hz silence poll, the
max-duration watchdog, and the inline filler loop. These tests pin the
ratified silence semantics — CALLER ACTIVITY ONLY re-arms the window; agent
playback (including the probe) pauses the idle clock but never resets it —
which deliberately kills the old mark-echo race (the probe-loop-vs-exit
divergence characterized before this step; see git history of
tests/test_silence_ladder.py).
"""
from __future__ import annotations

import asyncio
import time as _time

import pytest

from vogon.gateway.transport.clocks import (
    PLAYOUT_STUCK_CAP_SEC,
    SILENCE_PAUSE_CAP_SEC,
    CallClocks,
    HoldReport,
)
from vogon.gateway.transport.delivery import DeliveryReport
from vogon_contract import (
    CallConfig,
    DurationThreshold,
    FillerPolicy,
    Threshold,
)

T0 = 1000.0

SILENCE = [
    Threshold(id="warn", after_sec=8.0),
    Threshold(id="alert", after_sec=10.0),
    Threshold(id="exit", after_sec=37.0),
]


def make_clocks(silence=(), duration=(), filler=None, speak=None):
    events: list = []
    cfg = CallConfig(
        persona_voice="v",
        silence_thresholds=list(silence),
        duration_thresholds=list(duration),
        filler=filler,
    )
    async def _no_speak(text, audio_id):
        raise AssertionError("speak not expected")
    clocks = CallClocks(cfg, events.append, speak or _no_speak)
    # Prime the time base without spawning the ticker task (tests drive
    # tick() directly).
    clocks._start_t = T0
    clocks._last_tick_t = T0
    return clocks, events


def run_ticks(clocks, start, end):
    """Drive 1 Hz ticks for offsets [start, end)."""
    for off in range(start, end):
        clocks.tick(T0 + off)


class TestSilenceWindow:

    def test_not_armed_before_first_playout_end(self):
        clocks, events = make_clocks(silence=SILENCE)
        run_ticks(clocks, 1, 20)
        assert events == []  # dead air before the agent ever spoke is not silence

    def test_thresholds_fire_once_in_order(self):
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()          # greeting finished at T0
        run_ticks(clocks, 1, 12)
        assert [(e.threshold_id, e.silent_for_sec) for e in events] == [
            ("warn", 8.0), ("alert", 10.0),
        ]

    def test_playback_pauses_idle_clock_without_reset(self):
        """THE ratified rule: the probe's playback pauses accumulation; the
        window and its fired set survive — no re-warn, no re-probe, and exit
        fires deterministically at 37 s of accumulated idle."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        run_ticks(clocks, 1, 11)                     # warn@8, alert@10
        clocks.note_playout_start()                  # probe starts playing at t=10
        run_ticks(clocks, 11, 14)                    # 3 s of probe audio — no idle accrues
        clocks.note_playout_end()                    # probe finished at t=13
        run_ticks(clocks, 14, 45)
        fired = [e.threshold_id for e in events]
        assert fired == ["warn", "alert", "exit"]    # each exactly once — no probe loop
        assert events[-1].silent_for_sec == pytest.approx(37.0)
        # Wall-clock of exit = 40 (37 idle + 3 playing): pause shifts, never resets.

    def test_caller_activity_rearms_the_window(self):
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        run_ticks(clocks, 1, 9)                      # warn fired
        clocks.note_caller_activity()                # caller spoke
        run_ticks(clocks, 9, 18)
        assert [e.threshold_id for e in events] == ["warn", "warn"]  # fresh window re-fires

    def test_continuous_activity_holds_the_window_open(self):
        """A caller who talks WITHOUT PAUSING must never trip the ladder.

        Deepgram Flux sends a final only at EndOfTurn, so a long answer is
        carried entirely by per-Update interims. Before the transport re-armed
        on interims, the window accumulated straight through live speech and
        the probe was spoken over the caller at 10 s (measured). This pins the
        clock half of that contract: repeated activity, no thresholds, ever.
        """
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        for off in range(1, 40):                     # 39 s of continuous talking
            clocks.tick(T0 + off)
            clocks.note_caller_activity()            # one interim per second
        assert events == []                          # not even warn(8)

    def test_watchdog_on_a_real_turn_resets_the_window(self):
        """A lost mark must not cost the caller their grace window.

        The watchdog stands in for the mark echo that never came; before it
        also stood in for the RESET, idle banked before the agent spoke
        survived, so warn fired ~1 s after the watchdog released — over a
        caller who had just been asked a question.
        """
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()                    # arm (an earlier turn)
        run_ticks(clocks, 1, 8)                      # 7 s banked before we speak
        clocks.note_playout_start(is_turn_audio=True)
        run_ticks(clocks, 8, 9)
        clocks.note_playout_sent(12_000.0)           # 12 s of audio; limit = 22 s
        run_ticks(clocks, 9, 40)                     # mark never echoes
        assert [e.threshold_id for e in events] == ["warn", "alert"]
        # The TIME is the discriminator, not silent_for_sec: that reads 8.0
        # either way (the threshold is 8), so asserting it proves nothing —
        # the first draft of this test passed with the fix disabled. The
        # watchdog releases at T0+30 (playout observed at +8, limit 22). With
        # the reset the caller gets a fresh 8 s, so warn lands at T0+37; the
        # unfixed clock fired it at T0+30, the instant the watchdog released.
        assert events[0].at == pytest.approx(T0 + 37.0)

    def test_watchdog_on_a_probe_does_not_reset_the_window(self):
        """The other half: a probe's own stuck playout must NOT reset, or the
        exit threshold becomes unreachable and a dead call is never hung up."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        run_ticks(clocks, 1, 11)                     # warn@8, alert@10
        clocks.note_playout_start(is_turn_audio=False)   # the probe plays
        run_ticks(clocks, 11, 12)
        clocks.note_playout_sent(3_000.0)            # limit = 13 s
        run_ticks(clocks, 12, 60)                    # mark lost
        # Idle survived the probe, so exit still arrives.
        assert [e.threshold_id for e in events] == ["warn", "alert", "exit"]

    def test_a_known_length_replaces_the_blanket_cap_instead_of_being_clamped(self):
        """The cap covers the window where no length is known — not a playout
        whose length the transport has reported.

        A turn that runs past PLAYOUT_STUCK_CAP_SEC is not hypothetical: call
        18b3f7a3 carried 84.6s of audio across two attempts of one playout, and
        the retry path can push a single playout well beyond it. Clamping there
        ends the playout while the telco is still playing, and the ladder probes
        over the agent's own voice — the exact failure the watchdog's own
        audio-length bound exists to prevent.
        """
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        clocks.note_playout_start(is_turn_audio=True)
        run_ticks(clocks, 1, 2)                      # playout observed at T0+1
        clocks.note_playout_sent(120_000.0)          # 120s > the 90s cap
        run_ticks(clocks, 2, 115)                    # past cap+warn, before 130
        assert events == [], (
            "the ladder fired while 120s of audio was still playing: "
            f"{[(e.threshold_id, round(e.at - T0, 1)) for e in events]}")

        # ...and it still ends: the bound is length + margin, not "never".
        run_ticks(clocks, 115, 145)
        assert [e.threshold_id for e in events] == ["warn", "alert"], (
            "a lost mark on a long playout froze the ladder")

    def test_the_reasoning_gap_is_not_the_callers_silence(self):
        """A turn slower than the alert threshold used to earn a probe.

        The caller answered and we have not replied yet — that gap is neither
        of the two things the window measures. Nothing paused the clock across
        it: the filler hold is the only pause and lifecycle configures a
        FillerPolicy only for mode "voice" while the thresholds only exist for
        mode "twilio", so on a phone call the ladder runs unpaused through
        every LLM turn.

        Idle BANKED before the pause is the discriminator. Without the pause
        warn lands at T0+8, mid-request. With it, the 5s banked survives and
        warn needs only 3 more seconds once the answer is back — a reset would
        push it to T0+34 and a dead call would never reach exit.
        """
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        run_ticks(clocks, 1, 6)                      # 5s of real caller silence
        clocks.pause_silence()                       # the turn is computing
        run_ticks(clocks, 6, 26)                     # a 20s turn
        assert events == [], (
            f"a slow turn was counted as caller silence: "
            f"{[(e.threshold_id, round(e.at - T0, 1)) for e in events]}")

        clocks.resume_silence()
        run_ticks(clocks, 26, 40)
        assert [e.threshold_id for e in events] == ["warn", "alert"], (
            "the ladder did not resume after the turn landed")
        # 5s banked before the pause + 3 ticks after the resume = warn at 8.
        # Unpaused it would be T0+8, mid-request; reset it would be T0+34.
        assert events[0].at == pytest.approx(T0 + 28.0), (
            f"warn at T0+{events[0].at - T0:.0f}: the window RESET across the "
            f"pause instead of resuming — banked idle must survive or a dead "
            f"call never reaches exit")

    def test_the_pause_lifts_itself_if_the_turn_side_never_answers(self):
        """The ladder is what hangs up a call nobody is on, so a turn service
        that times out and retries (6 attempts x 40s + backoff, over four
        minutes) must not be able to hold it. Accumulated idle survives the
        lift, so exit stays reachable."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        clocks.pause_silence()
        run_ticks(clocks, 1, 100)                    # the request never returns
        fired = [e.threshold_id for e in events]
        assert "exit" in fired, (
            f"the pause froze the ladder on a call nobody is on: {fired}")
        # ...and not before the cap: the whole point is to allow a slow turn.
        assert events[0].at >= T0 + SILENCE_PAUSE_CAP_SEC, (
            f"warn fired at T0+{events[0].at - T0:.0f}, inside the pause cap")

    def test_an_unbalanced_resume_cannot_disable_the_ladder(self):
        """Clock-driven requests deliberately do not pause — a probe request
        pausing the ladder that fired it would suppress the next one. Pinned as
        the clock-side invariant: a stray resume must not drive the depth
        negative and leave the ladder permanently off."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        clocks.resume_silence()                      # stray, with no pause open
        run_ticks(clocks, 1, 12)
        assert [e.threshold_id for e in events] == ["warn", "alert"]

    def test_absorbed_activity_is_activity(self):
        # §C — absorbed speech is still speech; the transport routes it to the
        # same note_caller_activity signal. Semantically identical to above,
        # pinned separately because a real call (5c171d0b) regressed on it.
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        run_ticks(clocks, 1, 10)                     # warn@8; alert would fire at 10
        clocks.note_caller_activity()                # absorbed backchannel at t=9
        run_ticks(clocks, 10, 17)                    # fresh window: idle reaches 7 < warn
        assert [e.threshold_id for e in events] == ["warn"]  # no false probe at t=10


class TestDurationLadder:

    def test_fires_once_at_elapsed(self):
        clocks, events = make_clocks(duration=[Threshold(id="max_duration", after_sec=5.0)])
        run_ticks(clocks, 1, 10)
        assert len(events) == 1
        assert isinstance(events[0], DurationThreshold)
        assert events[0].threshold_id == "max_duration"
        assert events[0].elapsed_sec == pytest.approx(5.0)

    def test_counts_through_playback(self):
        clocks, events = make_clocks(duration=[Threshold(id="max_duration", after_sec=3.0)])
        clocks.note_playout_start()                  # duration ignores playing state
        run_ticks(clocks, 1, 5)
        assert [e.threshold_id for e in events] == ["max_duration"]


class TestFillerHold:

    def _policy(self, **kw):
        defaults = dict(first_after_ms=30, interval_ms=20, max=3,
                        phrases=["One moment."], continuations=["Almost.", "Still here."])
        defaults.update(kw)
        return FillerPolicy(**defaults)

    def _speak_recorder(self, statuses=None):
        sent: list[str] = []
        async def speak(text, audio_id):
            sent.append(text)
            status = (statuses or ["complete"])[min(len(sent) - 1, len(statuses or ["x"]) - 1)]
            return DeliveryReport(audio_id=audio_id, status=status, chunks_sent=1)
        return sent, speak

    def test_no_policy_no_hold(self):
        clocks, _ = make_clocks()
        assert clocks.begin_hold() is False

        async def run():
            return await clocks.end_hold()
        assert asyncio.run(run()) == HoldReport()

    def test_fast_compute_plays_no_filler(self):
        sent, speak = self._speak_recorder()

        async def run():
            clocks, _ = make_clocks(filler=self._policy(), speak=speak)
            assert clocks.begin_hold() is True
            await asyncio.sleep(0.005)               # compute finished before first_after
            return await clocks.end_hold()

        report = asyncio.run(run())
        assert report == HoldReport(fillers_played=0, interrupted=False)
        assert sent == []

    def test_slow_compute_plays_fillers_up_to_max(self):
        sent, speak = self._speak_recorder()

        async def run():
            clocks, _ = make_clocks(filler=self._policy(), speak=speak)
            clocks.begin_hold()
            await asyncio.sleep(0.3)                 # long compute — cap applies
            return await clocks.end_hold()

        report = asyncio.run(run())
        assert report.fillers_played == 3            # MAX cap
        assert report.interrupted is False
        assert sent[0] == "One moment."              # primary first
        assert set(sent[1:]) <= {"Almost.", "Still here."}

    def test_truncated_filler_reports_interrupted_and_stops(self):
        sent, speak = self._speak_recorder(statuses=["truncated"])

        async def run():
            clocks, _ = make_clocks(filler=self._policy(), speak=speak)
            clocks.begin_hold()
            await asyncio.sleep(0.2)
            return await clocks.end_hold()

        report = asyncio.run(run())
        assert report.interrupted is True
        assert report.fillers_played == 1            # stopped at the barge
        assert len(sent) == 1

    def test_end_hold_between_fillers_stops_cleanly(self):
        sent, speak = self._speak_recorder()

        async def run():
            clocks, _ = make_clocks(filler=self._policy(interval_ms=500), speak=speak)
            clocks.begin_hold()
            await asyncio.sleep(0.06)                # one filler played, now in interval
            return await clocks.end_hold()

        report = asyncio.run(run())
        assert report.fillers_played == 1
        assert report.interrupted is False

    # ── Response-arrival timing (the three cases, pinned) ────────────────
    # What happens when the turn's response arrives relative to the filler
    # schedule. The driver awaits end_hold() before executing the response,
    # so end_hold's return IS the moment the answer may start playing.

    def test_response_mid_filler_lets_the_phrase_finish(self):
        """Response arrives while a phrase is PLAYING: end_hold waits for
        that playout to complete (never cut mid-word), then returns — and
        no further filler starts."""

        timeline: list[str] = []

        async def slow_speak(text, audio_id):
            timeline.append(f"start:{audio_id}")
            await asyncio.sleep(0.15)          # the phrase is ~150ms of audio
            timeline.append(f"end:{audio_id}")
            return DeliveryReport(audio_id=audio_id, status="complete",
                                  chunks_sent=1)

        async def run():
            clocks, _ = make_clocks(filler=self._policy(first_after_ms=20),
                                    speak=slow_speak)
            clocks.begin_hold()
            await asyncio.sleep(0.06)          # filler-1-1 is now mid-playout
            t0 = _time.monotonic()
            report = await clocks.end_hold()   # ← the response arrived here
            return report, _time.monotonic() - t0

        report, waited = asyncio.run(run())
        assert report == HoldReport(fillers_played=1, interrupted=False)
        assert timeline == ["start:filler-1-1", "end:filler-1-1"], timeline
        assert waited > 0.05, (
            f"end_hold returned in {waited:.3f}s — it must wait out the "
            "in-flight phrase, not abandon it mid-word")

    def test_response_in_the_gap_cuts_the_gap_short(self):
        """Response arrives in the between-filler gap: end_hold returns in
        milliseconds (the gap is not waited out) and no next filler plays."""

        sent, speak = self._speak_recorder()

        async def run():
            clocks, _ = make_clocks(
                filler=self._policy(first_after_ms=10, interval_ms=5000),
                speak=speak)
            clocks.begin_hold()
            await asyncio.sleep(0.06)          # filler played; deep in the gap
            t0 = _time.monotonic()
            report = await clocks.end_hold()
            return report, _time.monotonic() - t0

        report, waited = asyncio.run(run())
        assert report == HoldReport(fillers_played=1, interrupted=False)
        assert len(sent) == 1
        assert waited < 0.1, (
            f"end_hold waited {waited:.3f}s — the 5s inter-filler gap must "
            "be cut short the moment the response arrives")

    def test_response_before_first_filler_adds_no_latency(self):
        """Response beats first_after_ms: zero fillers AND zero added
        latency — end_hold must not wait out the initial window."""

        sent, speak = self._speak_recorder()

        async def run():
            clocks, _ = make_clocks(filler=self._policy(first_after_ms=5000),
                                    speak=speak)
            clocks.begin_hold()
            await asyncio.sleep(0.01)          # fast compute
            t0 = _time.monotonic()
            report = await clocks.end_hold()
            return report, _time.monotonic() - t0

        report, waited = asyncio.run(run())
        assert report == HoldReport(fillers_played=0, interrupted=False)
        assert sent == []
        assert waited < 0.1, (
            f"end_hold waited {waited:.3f}s — a fast response must not pay "
            "for the filler window it never needed")


def test_explicit_hold_probes_at_sixty_and_exits_fifteen_seconds_later():
    clocks, events = make_clocks(silence=SILENCE)
    clocks.note_playout_end()
    clocks.grant_silence_grace(60)
    run_ticks(clocks, 1, 60)
    assert events == []
    clocks.tick(T0 + 60)
    assert [e.threshold_id for e in events] == ["warn", "alert"]
    run_ticks(clocks, 61, 75)
    assert not any(e.threshold_id == "exit" for e in events)
    clocks.tick(T0 + 75)
    assert events[-1].threshold_id == "exit"
    clocks.note_caller_activity()
    assert clocks._sil_grace_sec == 0


# ── The hold with a line source (docs/voice-gateway-front-model.md §3) ─────────

class _ScriptedSource:
    """Stands in for FrontModelSource: hold_line resolves after `ready_after`
    seconds with `line`, or "" then the hold beat; wait_line is the wait beat."""

    def __init__(self, line="Give me a moment to look at that.", ready_after=0.0,
                 hold_beat="One moment.", wait_beat="Still with you."):
        self.line, self.ready_after = line, ready_after
        self.hold_beat, self.wait_beat = hold_beat, wait_beat
        self.played: list[str] = []
        self.n = 0

    def _id(self):
        self.n += 1
        return f"front-{self.n}"

    async def hold_line(self, deadline_s, deadline_empty_s=None):
        if self.line and self.ready_after <= deadline_s:
            await asyncio.sleep(self.ready_after)
            return self.line, self._id()
        await asyncio.sleep(deadline_s)
        return (self.hold_beat, self._id()) if self.hold_beat else ("", "")

    def wait_line(self):
        return (self.wait_beat, self._id()) if self.wait_beat else ("", "")

    def note_played(self, text):
        self.played.append(text)


def _source_clocks(source, spoken, *, beat_after_ms=100, wait_again_after_ms=150,
                   truncate=()):
    async def speak(text, audio_id):
        spoken.append((audio_id, text))
        return DeliveryReport(audio_id=audio_id,
                              status="truncated" if text in truncate else "complete",
                              chunks_sent=1)
    clocks, _ = make_clocks(speak=speak)
    clocks.install_line_source(source, beat_after_ms=beat_after_ms,
                               wait_again_after_ms=wait_again_after_ms)
    return clocks


class TestSourceHold:
    def test_a_source_opens_the_hold_without_a_filler_policy(self):
        clocks = _source_clocks(_ScriptedSource(), [])

        async def scenario():
            opened = clocks.begin_hold()
            await clocks.end_hold()
            return opened

        assert asyncio.run(scenario()) is True

    def test_the_model_line_plays_then_a_wait_beat_then_stops_at_the_cap(self):
        spoken: list = []
        src = _ScriptedSource(ready_after=0.01)
        clocks = _source_clocks(src, spoken)

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.5)          # a slow turn: line, then the wait beat
            return await clocks.end_hold()

        report = asyncio.run(scenario())
        assert [t for _, t in spoken] == ["Give me a moment to look at that.", "Still with you."]
        assert [a for a, _ in spoken] == ["front-1", "front-2"]
        assert report == HoldReport(fillers_played=2, interrupted=False)
        assert src.played == [t for _, t in spoken]

    def test_the_answer_arriving_first_drops_the_line(self):
        spoken: list = []
        clocks = _source_clocks(_ScriptedSource(ready_after=0.2), spoken, beat_after_ms=500)

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.02)         # the desk was fast
            return await clocks.end_hold()

        report = asyncio.run(scenario())
        assert spoken == [] and report.fillers_played == 0

    def test_nothing_from_the_model_falls_back_to_the_hold_beat(self):
        spoken: list = []
        clocks = _source_clocks(_ScriptedSource(line="", ready_after=0.0), spoken,
                                beat_after_ms=50)

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.12)
            return await clocks.end_hold()

        asyncio.run(scenario())
        assert [t for _, t in spoken] == ["One moment."]

    def test_a_late_model_falls_back_to_the_hold_beat(self):
        spoken: list = []
        clocks = _source_clocks(_ScriptedSource(ready_after=5.0), spoken, beat_after_ms=50)

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.12)
            return await clocks.end_hold()

        asyncio.run(scenario())
        assert [t for _, t in spoken] == ["One moment."]

    def test_a_barged_line_ends_the_hold_and_reports_it(self):
        spoken: list = []
        clocks = _source_clocks(_ScriptedSource(ready_after=0.01), spoken,
                                truncate=("Give me a moment to look at that.",))

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.4)
            return await clocks.end_hold()

        report = asyncio.run(scenario())
        assert [t for _, t in spoken] == ["Give me a moment to look at that."]
        assert report == HoldReport(fillers_played=1, interrupted=True)

    def test_the_hold_speak_can_be_rerouted(self):
        """The driver installs its own send path so the playout produces a
        DeliveryOutcome fact — the route to the call log."""
        spoken: list = []
        clocks = _source_clocks(_ScriptedSource(ready_after=0.01), [])
        rerouted: list = []

        async def driver_send(text, audio_id):
            rerouted.append((audio_id, text))
            return DeliveryReport(audio_id=audio_id, status="complete", chunks_sent=1)

        clocks.install_hold_speak(driver_send)

        async def scenario():
            clocks.begin_hold()
            await asyncio.sleep(0.05)
            return await clocks.end_hold()

        asyncio.run(scenario())
        assert rerouted == [("front-1", "Give me a moment to look at that.")] and spoken == []


class TestOverlappingPlayouts:
    """A single _playing boolean cannot say "the front line finished but the
    desk's answer is still playing", and on a real call it said the wrong one:

        SIYA: Thanks. This is an attempt to collect a debt... (8.9s of audio)
        SIYA: Let me check that for you now.          <- front line, ends first
        SIYA: Are you still there?                    <- 8s later, mid-answer

    The caller was listening, not silent."""

    def test_the_clock_stays_paused_while_a_second_playout_is_live(self):
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)          # arm
        clocks.note_playout_start(is_turn_audio=True)       # the front line
        clocks.tick(T0 + 1)
        clocks.note_playout_start(is_turn_audio=True)       # the desk's answer
        clocks.note_playout_sent(9000.0)                    # 9 s at the telco
        clocks.note_playout_end(reset_window=True)          # the FRONT line's mark
        assert clocks._playing, "the desk is still talking"
        run_ticks(clocks, 2, 14)
        assert events == [], "no probe may fire over the agent's own audio"

    def test_it_resumes_once_every_playout_has_ended(self):
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_end(reset_window=True)
        clocks.note_playout_end(reset_window=True)
        assert not clocks._playing
        run_ticks(clocks, 1, 12)
        assert [e.threshold_id for e in events] == ["warn", "alert"]

    def test_an_unmatched_end_cannot_drive_the_depth_negative(self):
        """A drain-clear fallback may end a playout that was never started."""
        clocks, _ = make_clocks(silence=SILENCE)
        for _ in range(3):
            clocks.note_playout_end()
        assert clocks._playing_depth == 0
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_end()
        assert not clocks._playing, "one start, one end — not stuck playing"

    def test_a_lost_mark_still_cannot_freeze_the_ladder(self):
        """The dangerous direction (call audit P3: a swallowed mark froze the
        ladder for 9.5 minutes). The watchdog clears the depth, not just the
        flag. Realistic ordering: streaming is sequential, so each playout's
        length is reported before the next one starts."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_sent(1000.0)                     # 1 s
        clocks.note_playout_start(is_turn_audio=True)        # neither mark ever echoes
        clocks.note_playout_sent(1000.0)                     # 1 s
        run_ticks(clocks, 1, 25)
        assert clocks._playing_depth == 0, "the watchdog must clear the depth"
        assert [e.threshold_id for e in events][:1] == ["warn"]

    def test_an_earlier_playout_of_unknown_length_is_bounded_by_the_cap(self):
        """If a second playout starts before the first's length was reported,
        the first is timed by the blanket cap — the documented rule for a
        playout of unknown length. Release is LATER than it used to be (the
        old code let the second start overwrite the first's watch, which is
        the defect below), but it is still bounded: the ladder cannot freeze."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        clocks.note_playout_start(is_turn_audio=True)        # length never reported
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_sent(1000.0)
        run_ticks(clocks, 1, int(PLAYOUT_STUCK_CAP_SEC) - 5)
        assert clocks._playing, "an unknown-length playout gets the full cap"
        run_ticks(clocks, int(PLAYOUT_STUCK_CAP_SEC) - 5, int(PLAYOUT_STUCK_CAP_SEC) + 12)
        assert clocks._playing_depth == 0, "but it is released, not frozen"
        assert [e.threshold_id for e in events][:1] == ["warn"]


class TestWatchdogCoversEveryLivePlayout:
    """The watchdog timed only the NEWEST playout: every start reset its anchor
    and length. A short line starting while a long one was still playing made
    it time the short one, give up early, zero the count and restart the
    silence clock under the long answer — the "Are you still there?" bug
    again, in a narrower form."""

    def test_a_short_line_does_not_cut_the_long_ones_watch_short(self):
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)           # arm
        clocks.note_playout_start(is_turn_audio=True)        # the long answer
        clocks.note_playout_sent(30_000.0)                   # 30 s at the telco
        run_ticks(clocks, 1, 2)                              # anchored at t=1
        clocks.note_playout_start(is_turn_audio=True)        # a short line behind it
        clocks.note_playout_sent(1_000.0)                    # 1 s
        run_ticks(clocks, 2, 21)                             # neither mark echoes
        assert clocks._playing, "the 30 s answer cannot be over at t=20"
        assert events == [], "no probe may fire under the long answer"

    def test_it_still_releases_once_the_longest_could_have_ended(self):
        """The P3 guarantee holds: a lost mark is bounded by the LONGEST live
        playout's length plus margin, not frozen."""
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end(reset_window=True)
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_sent(30_000.0)
        run_ticks(clocks, 1, 2)
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_sent(1_000.0)
        run_ticks(clocks, 2, 43)                             # past 1 + 30 + 10
        assert not clocks._playing and clocks._playing_depth == 0
